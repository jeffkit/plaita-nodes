"""数据库查询连接器：sql_query（SQLAlchemy，任意有驱动的数据库）。

凭据数据两种写法（二选一）::

    {"url": "postgresql://user:pass@host:5432/db"}
    {"host": "...", "port": 5432, "user": "...", "password": "...", "database": "..."}

依赖：pip install plaita-nodes[sql]（sqlalchemy + 常用驱动）。
SELECT 返回 {"rows": [...], "rowcount": n}；写操作返回 {"rowcount": n}。
dry-run（节点字段或 globalContext.dry_run）直接返回 {"rows": [], "rowcount": 0, "dry_run": True}，不解析凭据不连库。
"""
from __future__ import annotations

import threading
from typing import Any, ClassVar, Dict, Optional

from plaita.credentials import get_credential
from plaita.node.basic import Node

_engine_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


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


class SqlQueryNode(Node):
    """SQL 查询：对凭据指向的数据库执行 SQL。

    query 支持 $INPUT/$NODE 表达式（求值后为最终 SQL 文本）；params 为
    绑定参数（同名 :param 替换，防注入，同样先经表达式求值）。
    """

    node_type: ClassVar[str] = "sql_query"
    node_name: ClassVar[str] = "SQL 查询"

    credential: str = ""
    query: Any = None
    params: Optional[Dict[str, Any]] = None
    dry_run: bool = False

    def execute(self, execution):
        # dry-run 最先判：不解析凭据、不连库（params 可能含敏感值，dry 下不碰）
        if self.dry_run or bool(execution.get_global_variable("dry_run", False)):
            return {"rows": [], "rowcount": 0, "dry_run": True}
        if not self.credential:
            raise ValueError("缺少 credential 字段：请填凭据名（编排台「凭据」页创建）")
        url = _compose_url(get_credential(self.credential))
        from sqlalchemy import text

        sql = str(_resolve(execution, self.query) or "").strip()
        if not sql:
            raise ValueError("query 为空")
        # params 逐值解析：整 dict 走 evaluate 在字面量场景会得到 None
        bind = {k: _resolve(execution, v) for k, v in (self.params or {}).items()}

        engine = _get_engine(url)
        with engine.begin() as conn:
            result = conn.execute(text(sql), bind or {})
            if result.returns_rows:
                rows = [dict(r._mapping) for r in result]
                return {"rows": rows, "rowcount": len(rows)}
            return {"rowcount": result.rowcount}
