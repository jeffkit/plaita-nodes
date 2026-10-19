"""A2R8-04：sql_query 大行结果集不得先物化整批再判限。

旧实现固定 ``fetchmany(256)``：300 行 × 128 KiB、``max_bytes=1024`` 时，
第一批判限之前已物化 256 行（tracemalloc 峰值 32.3 MiB = 33,110× 预算）。
修复后首行单读、后续批按剩余字节预算收缩，峰值应回到「单行 + 驱动开销」。
上限语义（fail-closed、不返回部分结果、observed_* 计数）保持不变。
"""
from __future__ import annotations

import tracemalloc

import pytest

pytest.importorskip("sqlalchemy")

import sqlalchemy

from plaita_nodes import database
from plaita_nodes.database import SqlQueryNode, SqlResultLimitError


class _Exec:
    def get_global_variable(self, key, default=None):
        return default

    def evaluate(self, value):
        return value


def _sqlite_rows(tmp_path, rows: int, payload: str):
    url = f"sqlite:///{tmp_path / f'wide-{rows}.db'}"
    engine = sqlalchemy.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE TABLE t (id INTEGER, payload TEXT)"))
        conn.execute(
            sqlalchemy.text("INSERT INTO t VALUES (:i, :p)"),
            [{"i": i, "p": payload} for i in range(rows)],
        )
    engine.dispose()
    return url


def _node(url, monkeypatch, **fields):
    monkeypatch.setattr(database, "get_credential", lambda name: {"url": url})
    return SqlQueryNode(id="q", credential="cred", **fields)


def test_large_rows_small_budget_peak_is_bounded_by_one_row(tmp_path, monkeypatch):
    """负控主场景：300×128 KiB、max_bytes=1024 → 峰值 ~0.4 MiB（旧 32.3 MiB）。"""
    url = _sqlite_rows(tmp_path, 300, "x" * (128 * 1024))
    node = _node(url, monkeypatch, query="SELECT id, payload FROM t",
                 max_bytes=1024)

    tracemalloc.start()
    try:
        with pytest.raises(SqlResultLimitError) as excinfo:
            node.execute(_Exec())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # 首行即超限：fail-closed 语义与计数不变
    assert excinfo.value.observed_rows == 1
    assert excinfo.value.observed_bytes > 1024
    # 32.3 MiB（pre-fix）→ 单行 ~128 KiB + SQLAlchemy 开销；2 MiB 留足平台余量
    assert peak < 2 * 1024 * 1024, (
        f"峰值 {peak / 1024 / 1024:.3f} MiB：仍在批量物化大行结果集"
    )


def test_peak_does_not_scale_with_row_count(tmp_path, monkeypatch):
    """行数翻倍不改变峰值——批大小由剩余字节预算决定，而非固定 256 行。"""
    peaks = []
    for rows in (300, 600):
        url = _sqlite_rows(tmp_path, rows, "y" * (128 * 1024))
        node = _node(url, monkeypatch, query="SELECT payload FROM t",
                     max_bytes=1024)
        tracemalloc.start()
        try:
            with pytest.raises(SqlResultLimitError):
                node.execute(_Exec())
            peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    assert max(peaks) < 2 * 1024 * 1024
    assert max(peaks) < 2 * min(peaks), f"峰值随行数增长：{peaks}"


def test_under_budget_large_rows_still_return_all(tmp_path, monkeypatch):
    """上限内结果逐字不变：自适应批大小不改变输出。"""
    payload = "z" * (16 * 1024)
    url = _sqlite_rows(tmp_path, 40, payload)
    node = _node(url, monkeypatch, query="SELECT id, payload FROM t ORDER BY id",
                 max_bytes=1024 * 1024)
    out = node.execute(_Exec())
    assert out["rowcount"] == 40
    assert [r["id"] for r in out["rows"]] == list(range(40))
    assert all(r["payload"] == payload for r in out["rows"])
