"""数据库查询连接器：sql_query（SQLAlchemy，任意有驱动的数据库）。

凭据数据两种写法（二选一）::

    {"url": "postgresql://user:pass@host:5432/db"}
    {"host": "...", "port": 5432, "user": "...", "password": "...", "database": "..."}

依赖：pip install plaita-nodes[sql]（sqlalchemy + 常用驱动）。
SELECT 返回 {"rows": [...], "rowcount": n}；写操作返回 {"rowcount": n}。
dry-run（节点字段或 globalContext.dry_run）直接返回 {"rows": [], "rowcount": 0, "dry_run": True}，不解析凭据不连库。

结果上限（#205）：SELECT 结果按 ``max_rows``（默认 1000）与 ``max_bytes``
（默认 4 MiB，按行 JSON 编码后累计）双上限约束，**超限 fail-closed 抛
:class:`SqlResultLimitError`——不返回部分结果、不静默截断**（静默截断会让下游
把"少了几行"当成完整结果算错）。读取走 ``fetchmany`` 分批，不整表物化。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, ClassVar, Dict, Optional

from pydantic import Field

from plaita.credentials import get_credential
from plaita.node.basic import Node

from ._redact import _scrub_url_text

_log = logging.getLogger(__name__)

#: 单次 ``fetchmany`` 的批大小：既不让驱动一次回传整表，也不逐行往返。
_FETCH_BATCH = 256

#: SELECT 结果默认行数上限（#205）。
_DEFAULT_MAX_ROWS = 1000

#: SELECT 结果默认字节上限（#205，按每行 JSON 编码后累计）。
_DEFAULT_MAX_BYTES = 4 * 1024 * 1024

_engine_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


class SqlResultLimitError(RuntimeError):
    """SELECT 结果超过 ``max_rows`` / ``max_bytes``（fail-closed，未返回部分结果）。"""


def _row_size(row: Dict[str, Any]) -> int:
    """一行结果按其 JSON 编码后的字节数计（与节点输出/checkpoint 的体量同口径）。"""
    try:
        return len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        # 不可 JSON 化的值（极端类型的 __str__ 故障等）：按 repr 估算，保证
        # 上限仍然生效；异常文本经 #151 口径清洗后再进日志。
        _log.warning(
            "sql_query 行无法 JSON 序列化，按 repr 估算字节数：%s",
            _scrub_url_text(str(exc)),
        )
        return len(repr(row).encode("utf-8", "replace"))


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
    max_rows: int = Field(default=_DEFAULT_MAX_ROWS, description="SELECT 结果行数上限；超限 fail-closed 抛错")
    max_bytes: int = Field(default=_DEFAULT_MAX_BYTES, description="SELECT 结果字节上限（JSON 编码累计）；超限 fail-closed 抛错")

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
            # stream_results：支持服务端游标的驱动（psycopg2/pymysql）分批回传，
            # 不把整表拉到客户端；不支持的驱动（sqlite）忽略该选项，仍由
            # fetchmany 的批大小约束 Python 侧物化。
            result = conn.execution_options(stream_results=True).execute(text(sql), bind or {})
            if result.returns_rows:
                return self._collect_rows(result)
            return {"rowcount": result.rowcount}

    # -- 结果上限（#205） -----------------------------------------------------

    def _limit(self, name: str, value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"sql_query {name} 必须是正整数：{value!r}") from None
        if limit < 1:
            raise ValueError(f"sql_query {name} 必须是正整数：{value!r}")
        return limit

    def _collect_rows(self, result: Any) -> dict:
        """分批读取 SELECT 结果，行数/字节任一超限即 fail-closed 抛错。

        **不静默截断**：下游把截断后的行数当成完整结果会算错（计数、对账、
        聚合），所以这里宁可让节点失败并给出可读原因，也不返回半份数据。
        """
        max_rows = self._limit("max_rows", self.max_rows)
        max_bytes = self._limit("max_bytes", self.max_bytes)
        rows: list[dict] = []
        used_bytes = 0
        hard_limit = max_rows + 1  # 多读一行用于判定"还有更多行"
        batch = min(_FETCH_BATCH, hard_limit)
        while len(rows) < hard_limit:
            chunk = result.fetchmany(batch)
            if not chunk:
                break
            for row in chunk:
                mapped = dict(row._mapping)
                rows.append(mapped)
                used_bytes += _row_size(mapped)
                if len(rows) > max_rows:
                    raise SqlResultLimitError(
                        f"sql_query 结果超过行数上限 max_rows={max_rows}："
                        f"已读取 {len(rows)} 行，结果集仍有更多行。"
                        "fail-closed：不返回部分结果、不静默截断；"
                        "请用 WHERE/LIMIT/聚合缩小查询，或显式调大 max_rows。"
                    )
                if used_bytes > max_bytes:
                    raise SqlResultLimitError(
                        f"sql_query 结果超过字节上限 max_bytes={max_bytes}："
                        f"读取到第 {len(rows)} 行时累计 {used_bytes} 字节（JSON 编码）。"
                        "fail-closed：不返回部分结果、不静默截断；"
                        "请减少 SELECT 的列/行，或显式调大 max_bytes。"
                    )
        return {"rows": rows, "rowcount": len(rows)}
