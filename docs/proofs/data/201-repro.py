#!/usr/bin/env python
"""plaita#201 repro: rate_limit key / report token escape the storage namespace.

Run from the repo root (or any cwd) with the package on PYTHONPATH::

    PYTHONPATH=src python docs/proofs/data/201-repro.py

Each case gets an isolated ``<case>/store/{rate-limits,reports}`` tree; any file
created outside those two directories is an escape.  The classic payload
``../../.plaita/sandboxes`` lands directly on the sandbox-registry path
(``<case>/.plaita/sandboxes.json[.jsonl]``).

Exit code 0  = escape blocked (fixed)
Exit code 1  = escape succeeded (vulnerable baseline)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from plaita_nodes.rate_limit import RateLimitNode
from plaita_nodes.report import ReportNode


class EvalStub:
    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


ROOT = Path(tempfile.mkdtemp(prefix="plaita201-"))


def _outside(case_dir: Path, allowed: list[Path]) -> list[Path]:
    found = []
    for p in case_dir.rglob("*"):
        if not p.is_file():
            continue
        if any(a in p.parents for a in allowed):
            continue
        found.append(p)
    return found


def _case(name: str) -> tuple[Path, Path, Path]:
    case = ROOT / name
    store = case / "store" / "rate-limits"
    reports = case / "store" / "reports"
    store.mkdir(parents=True)
    reports.mkdir(parents=True)
    return case, store, reports


def main() -> int:
    ex = EvalStub()
    escaped: list[Path] = []

    payloads = [
        ("dotdot", "../../.plaita/sandboxes"),
        ("urlencoded", "%2e%2e%2f%2e%2e%2f.plaita%2fsandboxes"),
        ("unicode_dots", "\uff0e\uff0e/\uff0e\uff0e/.plaita/sandboxes"),
        ("dotdot_slash", "....//....//.plaita/sandboxes"),
        ("backslash", "..\\..\\.plaita\\sandboxes"),
        ("absolute", str(ROOT / "absolute-escape")),
        ("nested", "a/b"),
    ]

    for name, raw in payloads:
        case, store, reports = _case(name)
        for kind, call in (
            ("rate_limit", lambda: RateLimitNode(
                id="r", action="record", key=raw, store_dir=str(store)).execute(ex)),
            ("report", lambda: ReportNode(
                id="w", action="write", token=raw, base_dir=str(reports),
                entry={"pwned": True}).execute(ex)),
        ):
            try:
                out = call()
                print(f"[{name}/{kind}] accepted: {out}")
            except Exception as exc:  # noqa: BLE001 - repro reports either way
                print(f"[{name}/{kind}] rejected: {type(exc).__name__}: {exc}")
        outside = _outside(case, [store, reports])
        if Path(raw).is_absolute():  # Path(base) / "/abs" discards base entirely
            for suffix in (".json", ".jsonl"):
                p = Path(raw + suffix)
                if p.is_file():
                    outside.append(p)
        for p in outside:
            escaped.append(p)
            print(f"[{name}] ESCAPED -> {p.resolve()} :: {p.read_text()[:160]!r}")

    # sanity: normal keys/tokens still work in the configured namespace
    case, store, reports = _case("normal")
    normal_ok = True
    try:
        out = RateLimitNode(
            id="r", action="record", key="xhs-publish", store_dir=str(store)).execute(ex)
        normal_ok = normal_ok and (store / "xhs-publish.json").is_file()
        print(f"[normal/rate_limit] {out} file={(store / 'xhs-publish.json').is_file()}")
        out = ReportNode(
            id="w", action="write", token="run-42", base_dir=str(reports),
            entry={"ok": 1}).execute(ex)
        normal_ok = normal_ok and (reports / "run-42.jsonl").is_file()
        print(f"[normal/report] {out} file={(reports / 'run-42.jsonl').is_file()}")
    except Exception as exc:  # noqa: BLE001
        normal_ok = False
        print(f"[normal] broke: {type(exc).__name__}: {exc}")

    if escaped or not normal_ok:
        print(f"VERDICT: VULNERABLE (escaped={len(escaped)} file(s), normal_ok={normal_ok})")
        return 1
    print("VERDICT: SAFE (no path escaped; normal names unchanged)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
