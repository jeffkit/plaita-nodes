"""A2R7-07 回归：SQL 节点超时后中断语句并归还连接。

引擎在节点预算超时时置位 ``execution.cancel_event`` 并遗弃节点线程
（``plaita/core/runner.py::_run_sync_node``）。``sql_query`` 原先不消费该
标志：阻塞的 ``conn.execute`` 继续跑，连接一直 checked out 到查询自然结束
（r7 探针 20M 行递归 CTE ≈ 6.6s，实测「超时后 30s+」的池占用同源）。

契约：
- 真 Flow 节点超时（0.3s）后，慢查询在 2s 内被中断，``pool.checkedout()==0``；
- 直接调用时预置 cancel_event / cancel_requested → 不取连接、抛
  ``NodeCancelledError``；
- 旧执行桩（无取消事件）行为不变，不起看门狗线程。

负控（回退看门狗与循环内取消检查）：flow 返回后 2s 内 ``checkedout()`` 仍为
1（查询继续跑），探针见 ``docs/proofs/data/39-nc-a2r7-07.txt``。
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("sqlalchemy")

import sqlalchemy
from sqlalchemy import text

from plaita.core.executor import FlowExecution
from plaita.core.flow import Flow

import plaita_nodes
import plaita_nodes.database as database
from plaita_nodes._cancel import NodeCancelledError
from plaita_nodes.database import SqlQueryNode

plaita_nodes.register_all()

#: 20M 行递归 CTE ≈ 6.6s（本机）；足以越过 0.3s 节点预算并观察连接占用。
SLOW_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 20000000) "
    "SELECT count(*) AS n FROM c"
)


class _Exec:
    def __init__(self, cancel_event=None, cancel_requested=None):
        self.cancel_event = cancel_event
        self.cancel_requested = cancel_requested

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


def _url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'slow.db'}"
    engine = sqlalchemy.create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER)"))
    engine.dispose()
    return url


def _cleanup(url: str) -> None:
    engine = database._engine_cache.pop(url, None)
    if engine is not None:
        engine.dispose()


def test_flow_timeout_interrupts_query_and_releases_connection(tmp_path, monkeypatch):
    url = _url(tmp_path)
    monkeypatch.setattr(database, "get_credential", lambda name: {"url": url})
    try:
        engine = database._get_engine(url)
        assert engine.pool.checkedout() == 0

        flow = Flow.model_validate({
            "nodes": [
                {"type": "start", "id": "start", "next": "q"},
                {"type": "sql_query", "id": "q", "credential": "c", "query": SLOW_SQL,
                 "next": "end", "timeout": "PT0.3S"},
                {"type": "end", "id": "end", "resultType": "success",
                 "output": "$NODE.q"},
            ]
        })
        started = time.monotonic()
        with pytest.raises(Exception):  # NodeTimeoutError
            FlowExecution().run_compatible(flow, False)
        assert time.monotonic() - started < 3.0

        # 修复前：查询继续跑满 ~6.6s，连接始终 checked out。
        # 修复后：看门狗中断语句，连接在 2s 内归还池（对齐 r8 C1 正控）。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and engine.pool.checkedout():
            time.sleep(0.02)
        assert engine.pool.checkedout() == 0, (
            "flow 超时后连接仍被占用：查询未被中断（A2R7-07）")
    finally:
        _cleanup(url)


def test_cancel_requested_before_execution_skips_connection(tmp_path, monkeypatch):
    url = _url(tmp_path)
    monkeypatch.setattr(database, "get_credential", lambda name: {"url": url})
    try:
        event = threading.Event()
        event.set()
        node = SqlQueryNode(id="q", credential="c", query="SELECT 1")
        with pytest.raises(NodeCancelledError):
            node.execute(_Exec(cancel_requested=event))
        # 取消发生在取连接之前：不创建 engine，也不占用池
        assert url not in database._engine_cache
    finally:
        _cleanup(url)


def test_cancel_event_during_query_returns_connection(tmp_path, monkeypatch):
    """直接调用（非引擎路径）：cancel_event 置位后查询被中断、连接归还。"""
    url = _url(tmp_path)
    monkeypatch.setattr(database, "get_credential", lambda name: {"url": url})
    try:
        engine = database._get_engine(url)
        event = threading.Event()
        node = SqlQueryNode(id="q", credential="c", query=SLOW_SQL)
        result = {}

        def _run():
            try:
                result["out"] = node.execute(_Exec(cancel_event=event))
            except BaseException as exc:  # noqa: BLE001 — 断言中断已发生
                result["exc"] = exc

        thread = threading.Thread(target=_run)
        thread.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and engine.pool.checkedout() == 0:
            time.sleep(0.02)
        assert engine.pool.checkedout() == 1, "慢查询未进入执行（探针前置失败）"

        event.set()
        thread.join(2.0)
        assert not thread.is_alive(), "cancel_event 置位后查询未在 2s 内退出"
        assert "exc" in result, "取消后应抛出中断异常而不是返回结果"
        assert engine.pool.checkedout() == 0
    finally:
        _cleanup(url)


def test_no_cancel_source_keeps_legacy_shape(tmp_path, monkeypatch):
    """旧执行桩没有 cancel_event/cancel_requested：正常查询结果不变。"""
    url = _url(tmp_path)
    monkeypatch.setattr(database, "get_credential", lambda name: {"url": url})
    try:
        node = SqlQueryNode(id="q", credential="c", query="SELECT 1 AS n")
        assert node.execute(_Exec()) == {"rows": [{"n": 1}], "rowcount": 1}
    finally:
        _cleanup(url)
