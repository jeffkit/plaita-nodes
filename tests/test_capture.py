"""CaptureNode：执行、失败不抛错、超时 kill、dry-run。"""
from __future__ import annotations

import sys
import time

import pytest

from plaita_nodes.capture import CaptureConfigError, CaptureNode


def test_simple_command(fake_execution):
    node = CaptureNode(id="t", command=[sys.executable, "-c", "print('hello')"])
    out = node.execute(fake_execution)
    assert out["ok"] is True and out["exit_code"] == 0
    assert out["stdout"].strip() == "hello"


def test_failure_is_not_exception(fake_execution):
    node = CaptureNode(id="t", command=[sys.executable, "-c", "print('bad'); raise SystemExit(3)"])
    out = node.execute(fake_execution)
    assert out["ok"] is False and out["exit_code"] == 3
    assert "bad" in out["stdout"]


def test_timeout_kills_and_returns_124(fake_execution):
    node = CaptureNode(id="t", command=[sys.executable, "-c", "import time; time.sleep(30)"],
                       timeout_secs=1)
    start = time.monotonic()
    out = node.execute(fake_execution)
    assert out["exit_code"] == 124 and out["ok"] is False
    assert time.monotonic() - start < 10


def test_dry_run(fake_execution):
    node = CaptureNode(id="t", command=["echo", "hi"], dry_run=True)
    out = node.execute(fake_execution)
    assert out["dry_run"] is True and out["ok"] is True
    assert "would exec" in out["stdout"]


def test_global_dry_run(fake_execution):
    from fake_exec import FakeExecution

    node = CaptureNode(id="t", command=["echo", "hi"])
    out = node.execute(FakeExecution(global_vars={"dry_run": True}))
    assert out["dry_run"] is True


def test_missing_command_raises():
    with pytest.raises(CaptureConfigError):
        CaptureNode(id="t").execute(type("E", (), {
            "evaluate": lambda self, v: v,
            "get_global_variable": lambda self, k, d=None: d,
        })())
