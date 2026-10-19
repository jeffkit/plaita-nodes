"""A2R7-07 修复后探针：Flow 节点超时 → 慢查询被中断、连接归还池。

基线（r7 probe_sql_surface.py §6）：超时后节点线程仍活、pool checkedout=1，
直到 20M 行递归 CTE 自然结束（本机 ~6.6s）才归零。

修复后：节点 ``timeout=PT0.3S`` 返回后，看门狗消费 cancel_event 调
sqlite3 ``interrupt()``；下表 checkedout 在 0.5s 内为 0。
"""
from __future__ import annotations

import tempfile
import time

import sqlalchemy
from sqlalchemy import text

from plaita.core.executor import FlowExecution
from plaita.core.flow import Flow

import plaita_nodes
import plaita_nodes.database as database

plaita_nodes.register_all()

SLOW_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 20000000) "
    "SELECT count(*) AS n FROM c"
)


def main() -> None:
    path = tempfile.mktemp(suffix=".db", prefix="a2r7-07-")
    url = f"sqlite:///{path}"
    engine = sqlalchemy.create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER)"))
    engine.dispose()
    database.get_credential = lambda name: {"url": url}
    engine = database._get_engine(url)

    flow = Flow.model_validate({
        "nodes": [
            {"type": "start", "id": "start", "next": "q"},
            {"type": "sql_query", "id": "q", "credential": "c", "query": SLOW_SQL,
             "next": "end", "timeout": "PT0.3S"},
            {"type": "end", "id": "end", "resultType": "success", "output": "$NODE.q"},
        ]
    })
    started = time.monotonic()
    try:
        FlowExecution().run_compatible(flow, False)
        print("[sql-timeout] UNEXPECTED success")
    except Exception as exc:  # noqa: BLE001 — 预期 NodeTimeoutError
        print(f"[sql-timeout] flow raised {type(exc).__name__} "
              f"after {time.monotonic() - started:.2f}s")
    for delay in (0.05, 0.2, 0.5, 1.0):
        time.sleep(delay)
        print(f"[sql-timeout] +{delay:.2f}s pool_checkedout={engine.pool.checkedout()}")
    engine.dispose()


if __name__ == "__main__":
    main()
