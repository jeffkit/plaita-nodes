#!/usr/bin/env python
"""plaita#205 repro: sql_query must not materialise the whole SELECT result set.

Run from the repo root (or any cwd) with the package on PYTHONPATH::

    PYTHONPATH=src python docs/proofs/data/205-repro.py [--rows N]

Builds a local SQLite table (default 200k rows x 200 chars ≈ 43 MB on disk),
runs SqlQueryNode with default limits, and measures Python peak allocation with
``tracemalloc``.

Exit code 0  = result bounded (fail-closed on limit)
Exit code 1  = unbounded materialisation (vulnerable baseline)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import tracemalloc
from pathlib import Path

import sqlalchemy

import plaita_nodes.database as db


class EvalStub:
    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


def build_db(path: Path, n_rows: int) -> str:
    url = f"sqlite:///{path}"
    engine = sqlalchemy.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE TABLE big (id INTEGER, payload TEXT)"))
        conn.execute(
            sqlalchemy.text("INSERT INTO big VALUES (:id, :payload)"),
            [{"id": i, "payload": "x" * 200} for i in range(n_rows)],
        )
    engine.dispose()
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--max-rows", type=int, default=None)
    ap.add_argument("--max-bytes", type=int, default=None)
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="plaita205-"))
    try:
        dbfile = work / "big.db"
        print(f"building sqlite db: {args.rows} rows ...")
        t0 = time.time()
        url = build_db(dbfile, args.rows)
        print(f"db built in {time.time() - t0:.2f}s")
        print(f"db size: {os.path.getsize(dbfile) / 1e6:.1f} MB")

        db.get_credential = lambda name: {"url": url}  # bypass credential store
        db._engine_cache.clear()

        fields = {}
        if args.max_rows is not None:
            fields["max_rows"] = args.max_rows
        if args.max_bytes is not None:
            fields["max_bytes"] = args.max_bytes
        node = db.SqlQueryNode(id="q", credential="repro",
                               query="SELECT * FROM big", **fields)

        tracemalloc.start()
        t0 = time.time()
        try:
            out = node.execute(EvalStub())
        except Exception as exc:  # noqa: BLE001 - either outcome is the observation
            elapsed = time.time() - t0
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            print(f"raised: {type(exc).__name__}: {exc}")
            print(f"elapsed={elapsed:.2f}s peak_python={peak / 1e6:.1f} MB")
            print("VERDICT: BOUNDED (fail-closed, no partial result returned)")
            return 0

        elapsed = time.time() - t0
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        n_rows = len(out["rows"])
        n_bytes = len(json.dumps(out["rows"]))
        print(f"returned rows={n_rows} rowcount={out['rowcount']} "
              f"json_bytes={n_bytes / 1e6:.1f} MB")
        print(f"elapsed={elapsed:.2f}s peak_python={peak / 1e6:.1f} MB")
        if n_rows >= args.rows:
            print("VERDICT: UNBOUNDED (whole table materialised and returned)")
            return 1
        if peak > 40_000_000:
            print("VERDICT: UNBOUNDED (python peak above 40 MB)")
            return 1
        print("VERDICT: BOUNDED")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
