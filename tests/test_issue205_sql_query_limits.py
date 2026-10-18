"""plaita#205：sql_query 结果必须有行数/字节上限（fail-closed，不静默截断）。

默认 ``max_rows=1000`` / ``max_bytes=4 MiB``：超过任一上限抛
:class:`SqlResultLimitError`（具名标记），**不返回部分结果**。小于上限的
SELECT 与现状逐字一致；写操作与 dry-run 不受影响。
"""
from __future__ import annotations

import tracemalloc

import pytest

pytest.importorskip("sqlalchemy")

import sqlalchemy

import plaita_nodes.database as database
from plaita_nodes.database import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ROWS,
    SqlQueryNode,
    SqlResultLimitError,
)


class _Exec:
    def get_global_variable(self, key, default=None):
        return default

    def evaluate(self, value):
        return value


def _sqlite(tmp_path, rows: int):
    url = f"sqlite:///{tmp_path / 'rows.db'}"
    engine = sqlalchemy.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE TABLE t (id INTEGER, name TEXT)"))
        if rows:
            conn.execute(
                sqlalchemy.text("INSERT INTO t VALUES (:i, :n)"),
                [{"i": i, "n": f"n{i}"} for i in range(rows)],
            )
    return url


def _node(url, monkeypatch, **fields):
    monkeypatch.setattr(database, "get_credential", lambda name: {"url": url})
    return SqlQueryNode(id="q", credential="cred", **fields)


def test_under_limit_result_is_unchanged(tmp_path, monkeypatch):
    url = _sqlite(tmp_path, 3)
    node = _node(url, monkeypatch, query="SELECT id, name FROM t ORDER BY id")
    assert (node.max_rows, node.max_bytes) == (DEFAULT_MAX_ROWS, DEFAULT_MAX_BYTES)
    assert node.execute(_Exec()) == {
        "rows": [{"id": 0, "name": "n0"}, {"id": 1, "name": "n1"}, {"id": 2, "name": "n2"}],
        "rowcount": 3,
    }


def test_exact_row_limit_is_allowed(tmp_path, monkeypatch):
    url = _sqlite(tmp_path, 5)
    node = _node(url, monkeypatch, query="SELECT id FROM t ORDER BY id", max_rows=5)
    out = node.execute(_Exec())
    assert out["rowcount"] == 5
    assert len(out["rows"]) == 5


def test_row_limit_fails_closed(tmp_path, monkeypatch):
    url = _sqlite(tmp_path, 6)
    node = _node(url, monkeypatch, query="SELECT id FROM t ORDER BY id", max_rows=5)
    with pytest.raises(SqlResultLimitError) as excinfo:
        node.execute(_Exec())
    assert "max_rows=5" in str(excinfo.value)
    assert excinfo.value.observed_rows == 6
    # fail-closed：异常而不是部分 rows
    assert not isinstance(excinfo.value, AssertionError)


def test_byte_limit_fails_closed(tmp_path, monkeypatch):
    url = _sqlite(tmp_path, 3)
    node = _node(url, monkeypatch, query="SELECT id, name FROM t", max_bytes=10)
    with pytest.raises(SqlResultLimitError) as excinfo:
        node.execute(_Exec())
    assert "max_bytes=10" in str(excinfo.value)
    assert excinfo.value.observed_bytes > 10


def test_write_path_unchanged(tmp_path, monkeypatch):
    url = _sqlite(tmp_path, 0)
    node = _node(url, monkeypatch, query="INSERT INTO t VALUES (1, 'x')")
    assert node.execute(_Exec()) == {"rowcount": 1}


def test_dry_run_unchanged(tmp_path, monkeypatch):
    url = _sqlite(tmp_path, 0)
    node = _node(url, monkeypatch, query="SELECT * FROM t", dry_run=True)
    assert node.execute(_Exec()) == {"rows": [], "rowcount": 0, "dry_run": True}


def test_100k_default_fails_closed_with_bounded_peak(tmp_path, monkeypatch):
    """验收主场景：10 万行默认上限 ⇒ 1001 行处 fail-closed，峰值有界。"""
    url = _sqlite(tmp_path, 100_000)
    node = _node(url, monkeypatch, query="SELECT * FROM t")
    tracemalloc.start()
    try:
        with pytest.raises(SqlResultLimitError) as excinfo:
            node.execute(_Exec())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert excinfo.value.observed_rows == DEFAULT_MAX_ROWS + 1
    assert peak < 20 * 1024 * 1024, (
        f"峰值 {peak / 1024 / 1024:.1f}MB 超过有界阈值（#205）；"
        f"说明结果仍在全量物化"
    )
