#!/usr/bin/env python3
"""plaita#205 repro / before-after probe (plaita-nodes).

Run with the tree under test on ``PYTHONPATH``::

    PYTHONPATH=/tmp/wt205-nodes/src <venv>/python docs/proofs/data/205-repro.py

Builds a 100k-row SQLite table, runs ``SqlQueryNode`` with default limits, and
reports returned rows / peak traced memory / whether max_rows/max_bytes exist.
Before the fix the node materialises all 100k rows; after the fix it fails
closed at row 1001 with ``SqlResultLimitError``.
"""
from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path

import sqlalchemy

import plaita_nodes.database as database
from plaita_nodes.database import SqlQueryNode


class _Exec:
    def get_global_variable(self, key, default=None):
        return default

    def evaluate(self, value):
        return value


def run() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="plaita205-"))
    url = f"sqlite:///{tmp / 'big.db'}"
    engine = sqlalchemy.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE TABLE t (id INTEGER, name TEXT)"))
        conn.execute(
            sqlalchemy.text("INSERT INTO t VALUES (:i, :n)"),
            [{"i": i, "n": f"n{i}"} for i in range(100_000)],
        )

    database.get_credential = lambda name: {"url": url}
    node = SqlQueryNode(id="q", credential="cred", query="SELECT * FROM t")
    fields = set(type(node).model_fields)
    result = None
    error = None
    observed_rows = None
    started = time.time()
    tracemalloc.start()
    try:
        result = node.execute(_Exec())
    except Exception as exc:  # noqa: BLE001 - probe records the named refusal
        error = type(exc).__name__
        observed_rows = getattr(exc, "observed_rows", None)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    elapsed = time.time() - started
    return {
        "plaita_nodes_file": database.__file__,
        "rows_total": 100_000,
        "rows_returned": len(result["rows"]) if isinstance(result, dict) else None,
        "rowcount": result.get("rowcount") if isinstance(result, dict) else None,
        "error": error,
        "observed_rows": observed_rows,
        "peak_mb": round(peak / 1024 / 1024, 2),
        "elapsed_s": round(elapsed, 3),
        "has_max_rows": "max_rows" in fields,
        "has_max_bytes": "max_bytes" in fields,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
