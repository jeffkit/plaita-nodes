"""数据库查询连接器：sql_query（SQLAlchemy，任意有驱动的数据库）。

凭据数据两种写法（二选一）::

    {"url": "postgresql://user:pass@host:5432/db"}
    {"host": "...", "port": 5432, "user": "...", "password": "...", "database": "..."}

依赖：pip install plaita-nodes[sql]（sqlalchemy + 常用驱动）。
SELECT 返回 {"rows": [...], "rowcount": n}；写操作返回 {"rowcount": n}。
**结果上限**（plaita#205）：``max_rows`` 默认 1000、``max_bytes`` 默认
4 MiB（按行 JSON 编码累计）；超限抛 :class:`SqlResultLimitError`
（fail-closed，不返回部分结果——静默截断会让下游算错）。两个上限必须为
正整数，显式 0/负值抛 ``ValueError``（不静默夹成 1）。
dry-run（节点字段或 globalContext.dry_run）直接返回 {"rows": [], "rowcount": 0, "dry_run": True}，不解析凭据不连库。

取消（A2R7-07）：引擎在节点预算超时时置位 ``execution.cancel_event`` 并遗弃
节点线程；本节点有看门狗消费该标志，调用驱动的原生取消（psycopg ``cancel()`` /
sqlite3 ``interrupt()``，无原生取消则 ``invalidate()``）打断正在执行的语句，
让 ``engine.begin()`` 尽快回滚并把连接归还池——不会让查询继续占用连接到自然结束。
"""
from __future__ import annotations

import json
import threading
from typing import Any, ClassVar, Dict, Optional

from plaita.credentials import get_credential
from plaita.node.basic import Node

from ._cancel import CANCEL_POLL_SECONDS, NodeCancelledError, cancellation_requested
from ._retry import DeterministicError

_engine_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()

#: plaita#205：SELECT 结果默认上限。行数 + 字节双闸（按行 JSON 编码累计）。
DEFAULT_MAX_ROWS = 1000
DEFAULT_MAX_BYTES = 4 * 1024 * 1024


class SqlResultLimitError(DeterministicError, RuntimeError):
    """SELECT 结果超过节点上限——fail-closed，不返回部分结果。

    plaita#205：``sql_query`` 原先全量物化；大表会 OOM worker 并把整表写进
    checkpoint。超过 ``max_rows`` / ``max_bytes`` 时抛本异常（具名标记），
    日志/调用方能明确看到上限与已读规模；不静默截断（部分结果会让下游算错）。

    确定性错误：同一 SQL + 同一上限重跑必然再次超限，``retryTimes`` 重试只会
    重复执行 SQL（A2R7-04）。
    """

    def __init__(
        self,
        *,
        max_rows: int,
        max_bytes: int,
        observed_rows: int,
        observed_bytes: int,
    ):
        super().__init__(
            f"sql_query 结果超过上限（max_rows={max_rows}, max_bytes={max_bytes}）："
            f"已读取 {observed_rows} 行 / {observed_bytes} 字节；"
            "拒绝返回部分结果（不静默截断，fail-closed）。请在 SQL 中加 WHERE/LIMIT 缩小结果，"
            "或显式调大节点的 max_rows/max_bytes。"
        )
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.observed_rows = observed_rows
        self.observed_bytes = observed_bytes


def _get_engine(url: str, **kwargs: Any):
    """按连接串缓存 SQLAlchemy engine（连接池复用；线程安全）。"""
    try:
        from sqlalchemy import create_engine
    except ImportError as e:
        raise RuntimeError("SQL 查询需要 sqlalchemy：pip install plaita-nodes[sql]") from e
    with _cache_lock:
        engine = _engine_cache.get(url)
        if engine is None:
            engine = create_engine(url, pool_pre_ping=True, **kwargs)
            _engine_cache[url] = engine
        return engine


def _compose_url(cred: Dict[str, Any]) -> str:
    url = cred.get("url")
    if url:
        return url
    parts = ["host", "port", "user", "password", "database"]
    if all(cred.get(p) is not None for p in parts):
        from urllib.parse import quote_plus

        return (
            f"{cred.get('driver', 'postgresql+psycopg2')}://"
            f"{quote_plus(str(cred['user']))}:{quote_plus(str(cred['password']))}"
            f"@{cred['host']}:{cred['port']}/{cred['database']}"
        )
    raise ValueError(
        "凭据数据需提供 url，或 host/port/user/password/database 全集"
    )


def _resolve(execution, value: Any) -> Any:
    """整串表达式则求值，普通字面量原样返回（evaluate 对字面量 dict 会返回 None）。"""
    if isinstance(value, str) and "$" in value:
        return execution.evaluate(value)
    return value


def _positive_limit(name: str, value: Any) -> int:
    """校验节点上限字段为正整数；非正值 fail-closed 拒绝（#205 续）。

    显式 0/负值原先被 ``max(1, ...)`` 静默夹成 1，配置写错却像正常执行；
    这里改为带字段名的 ``ValueError``，让错误在凭据解析/连库之前暴露。
    """
    try:
        limit = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} 必须为正整数（收到 {value!r}）") from e
    if limit <= 0:
        raise ValueError(
            f"{name} 必须为正整数（收到 {value!r}）；非正值 fail-closed 拒绝执行"
        )
    return limit


def _interrupt_db_connection(conn: Any) -> bool:
    """尽力中断 ``conn`` 上正在执行的语句（A2R7-07）。

    优先用驱动的原生取消：psycopg/psycopg2 的 ``cancel()`` 发送 cancel
    request，sqlite3 的 ``interrupt()`` 打断 VM。没有原生取消的驱动退化为
    ``Connection.invalidate()``——关闭 DBAPI 连接，阻塞中的调用随即报错，
    连接不会留在池里。返回是否至少发出一次中断信号。
    """
    driver = None
    try:
        fairy = conn.connection
        driver = getattr(fairy, "driver_connection", None)
        if driver is None:  # SQLAlchemy 1.4 旧属性
            driver = getattr(fairy, "connection", None)
    except Exception:  # noqa: BLE001 — 已关闭的连接/桩连接
        driver = None
    if driver is not None:
        for name in ("cancel", "interrupt"):
            action = getattr(driver, name, None)
            if callable(action):
                try:
                    action()
                    return True
                except Exception:  # noqa: BLE001 — 尽力而为，退到下一种方式
                    continue
    try:
        conn.invalidate()
        return True
    except Exception:  # noqa: BLE001
        return False


def _watch_cancel(execution: Any, conn: Any, finished: threading.Event) -> None:
    """看门狗：节点预算超时（cancel_event 置位）时中断仍在跑的语句。

    引擎在节点超时时只遗弃线程（``plaita/core/runner.py::_run_sync_node``）；
    没有这一步，阻塞的 ``conn.execute`` 会继续持有连接直到查询自然结束。
    """
    while not finished.wait(CANCEL_POLL_SECONDS):
        if cancellation_requested(execution):
            _interrupt_db_connection(conn)
            return


class SqlQueryNode(Node):
    """SQL 查询：对凭据指向的数据库执行 SQL。

    query 支持 $INPUT/$NODE 表达式（求值后为最终 SQL 文本）；params 为
    绑定参数（同名 :param 替换，防注入，同样先经表达式求值）。

    结果上限（plaita#205）：``max_rows`` 默认 1000、``max_bytes`` 默认
    4 MiB；超过任一上限抛 :class:`SqlResultLimitError`（fail-closed，不返回
    部分结果）。写操作与 dry-run 不受影响。
    """

    node_type: ClassVar[str] = "sql_query"
    node_name: ClassVar[str] = "SQL 查询"

    credential: str = ""
    query: Any = None
    params: Optional[Dict[str, Any]] = None
    dry_run: bool = False
    max_rows: int = DEFAULT_MAX_ROWS
    max_bytes: int = DEFAULT_MAX_BYTES

    def execute(self, execution):
        # dry-run 最先判：不解析凭据、不连库（params 可能含敏感值，dry 下不碰）
        if self.dry_run or bool(execution.get_global_variable("dry_run", False)):
            return {"rows": [], "rowcount": 0, "dry_run": True}
        # 上限配置错误先于任何凭据解析/连库暴露；不静默夹取（#205 续）
        row_limit = _positive_limit("max_rows", self.max_rows)
        byte_limit = _positive_limit("max_bytes", self.max_bytes)
        if not self.credential:
            raise ValueError("缺少 credential 字段：请填凭据名（编排台「凭据」页创建）")
        url = _compose_url(get_credential(self.credential))
        from sqlalchemy import text

        sql = str(_resolve(execution, self.query) or "").strip()
        if not sql:
            raise ValueError("query 为空")
        # params 逐值解析：整 dict 走 evaluate 在字面量场景会得到 None
        bind = {k: _resolve(execution, v) for k, v in (self.params or {}).items()}

        # 执行级取消（cancel_requested 粘滞）在解密凭据/取连接前就拒绝；
        # 节点级 cancel_event 在节点入口已被引擎复位，这里只会看到本次取消。
        if cancellation_requested(execution):
            raise NodeCancelledError("sql_query cancelled before execution")

        engine = _get_engine(url)
        finished = threading.Event()
        watcher: Optional[threading.Thread] = None
        with engine.begin() as conn:
            # 引擎节点超时 → cancel_event 置位并遗弃本线程；看门狗负责中断
            # 仍在执行的语句，让 engine.begin() 尽快回滚/归还连接（A2R7-07）。
            # 旧执行桩没有取消事件 → 不起线程，行为不变。
            if (getattr(execution, "cancel_event", None) is not None
                    or getattr(execution, "cancel_requested", None) is not None):
                watcher = threading.Thread(
                    target=_watch_cancel, args=(execution, conn, finished),
                    daemon=True, name="plaita-sql-cancel")
                watcher.start()
            try:
                if cancellation_requested(execution):
                    raise NodeCancelledError("sql_query cancelled before statement")
                result = conn.execute(
                    text(sql).execution_options(stream_results=True), bind or {}
                )
                if result.returns_rows:
                    # plaita#205：分批读取并**先判上限再入列**——不先物化整表再
                    # 检查；超限 fail-closed 抛具名异常，绝不返回部分结果。
                    rows: list[Dict[str, Any]] = []
                    used_bytes = 0
                    batch_size = 256
                    while True:
                        if cancellation_requested(execution):
                            raise NodeCancelledError(
                                "sql_query cancelled; statement interrupted")
                        batch = result.fetchmany(batch_size)
                        if not batch:
                            break
                        for row in batch:
                            item = dict(row._mapping)
                            encoded = json.dumps(item, ensure_ascii=False, default=str)
                            size = len(encoded.encode("utf-8"))
                            if len(rows) + 1 > row_limit or used_bytes + size > byte_limit:
                                raise SqlResultLimitError(
                                    max_rows=row_limit,
                                    max_bytes=byte_limit,
                                    observed_rows=len(rows) + 1,
                                    observed_bytes=used_bytes + size,
                                )
                            rows.append(item)
                            used_bytes += size
                    return {"rows": rows, "rowcount": len(rows)}
                return {"rowcount": result.rowcount}
            finally:
                finished.set()
                if watcher is not None:
                    watcher.join(1.0)
