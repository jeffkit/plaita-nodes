"""gate 大输出截断复现：尾部诊断被头部切片丢掉（issue #3）。

现状：``gate.py`` 返回语句对 stdout/stderr 做头部切片（``[:4000]`` / ``[:2000]``），
不留尾部、不标注省略量。验证命令的失败摘要几乎总在尾部
（``cargo test`` 的 ``failures:`` / pytest 的 ``test result:``），于是输出里只剩
无用的中段噪音——修复环拿不到任何测试名/断言文本。

验收对齐（issue-pipeline triage）：
1. >4000 字符且失败摘要在尾部的命令，节点输出中可直接读到该尾部摘要；
2. 截断处显式标注省略量（``…[省略 N 字符]…``）；
3. 小输出行为与现状一致（无省略标记、无新增文件），既有输出字段保持不变；
4. dry-run 不新增文件（AGENTS.md dry-run 契约）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fake_exec import FakeExecution
from plaita_nodes.gate import GateNode

OMIT_RE = re.compile(r"…\[省略 (\d+) 字符\]…")
STDOUT_CAP = 4000
STDERR_CAP = 2000
SUMMARY = ("FAILURES:\n"
           "  test_alpha::test_beta --- assertion `left == right` failed\n"
           "test result: FAILED. 1 failed; 2 passed; 0 ignored")


def _script(tmp_path: Path, body: str) -> list[str]:
    """把 emit 脚本落盘在 tmp_path 下，返回可执行命令（用当前解释器，免 PATH 依赖）。"""
    path = tmp_path / "script.py"
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def _big_tail_command(tmp_path: Path, stream: str) -> tuple[list[str], str]:
    """产出「超阈值填充 + 尾部失败摘要 + exit 1」的命令与全量输出。"""
    noise = "x" * (STDOUT_CAP + STDERR_CAP)
    body = (f"import sys\n"
            f"sys.{stream}.write({noise!r})\n"
            f"sys.{stream}.write(chr(10) + {SUMMARY!r})\n"
            f"sys.exit(1)\n")
    return _script(tmp_path, body), noise + "\n" + SUMMARY


def _new_files(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name != "script.py")


def _gate(tmp_path: Path, command, globals_: dict | None = None) -> dict:
    node = GateNode(id="g", command=command, gate_name="t", cwd=str(tmp_path))
    return node.execute(FakeExecution(globals_))


def test_gate_stdout_tail_failure_summary_visible(tmp_path):
    """stdout 超 4000 字符时，尾部 failures:/test result: 段必须可读且标注省略量。"""
    command, raw = _big_tail_command(tmp_path, "stdout")
    out = _gate(tmp_path, command)
    assert out["passed"] is False and out["exit_code"] == 1
    assert "test result: FAILED" in out["stdout"], f"尾部摘要丢失，实际尾部={out['stdout'][-80:]!r}"
    assert "test_alpha::test_beta" in out["stdout"], "测试名丢失（修复环无从下手）"
    marker = OMIT_RE.search(out["stdout"])
    assert marker, f"截断未标注省略量，实际尾部={out['stdout'][-80:]!r}"
    head, tail = out["stdout"].split(marker.group(0))
    assert int(marker.group(1)) == len(raw) - len(head) - len(tail), "省略量标注与实际不符"
    assert len(out["stdout"]) <= STDOUT_CAP + len(marker.group(0)), "保留量超出原阈值"


def test_gate_stderr_tail_failure_summary_visible(tmp_path):
    """stderr 超 2000 字符时同样保留尾部（编译错误/断言文本常走 stderr）。"""
    command, raw = _big_tail_command(tmp_path, "stderr")
    out = _gate(tmp_path, command)
    assert out["passed"] is False
    assert "test result: FAILED" in out["stderr"], f"尾部摘要丢失，实际尾部={out['stderr'][-80:]!r}"
    marker = OMIT_RE.search(out["stderr"])
    assert marker, "stderr 截断未标注省略量"
    head, tail = out["stderr"].split(marker.group(0))
    assert int(marker.group(1)) == len(raw) - len(head) - len(tail)
    assert len(out["stderr"]) <= STDERR_CAP + len(marker.group(0))


def test_gate_small_failure_output_unchanged_no_new_files(tmp_path):
    """小输出（低于阈值）行为与现状一致：原文照传、无省略标记、无新增文件。"""
    command = _script(tmp_path, "import sys\nsys.stdout.write('assert failed: 1 != 2\\n')\nsys.exit(1)\n")
    out = _gate(tmp_path, command)
    assert out["passed"] is False
    assert out["stdout"] == "assert failed: 1 != 2\n"
    assert OMIT_RE.search(out["stdout"]) is None
    assert _new_files(tmp_path) == [], "小输出不应产生任何新文件"


def test_gate_small_passing_output_unchanged_no_new_files(tmp_path):
    command = _script(tmp_path, "import sys\nsys.stdout.write('OK\\n')\n")
    out = _gate(tmp_path, command)
    assert out["passed"] is True
    assert out["stdout"] == "OK\n"
    assert OMIT_RE.search(out["stdout"]) is None
    assert _new_files(tmp_path) == [], "passed=True 不应产生新文件"


def test_gate_output_fields_unchanged(tmp_path):
    """既有输出字段（passed/gate/exit_code/stdout/stderr/retries）语义不变。"""
    command = _script(tmp_path, "import sys\nsys.stderr.write('warn\\n')\n")
    out = _gate(tmp_path, command)
    for key in ("passed", "gate", "exit_code", "stdout", "stderr", "retries"):
        assert key in out, f"既有字段 {key} 丢失"
    assert out["gate"] == "t" and out["exit_code"] == 0 and out["retries"] == 0
    assert out["stderr"] == "warn\n"


def test_gate_dry_run_writes_nothing(tmp_path):
    """dry-run 契约（AGENTS.md）：不执行命令、不新增文件。"""
    command, _raw = _big_tail_command(tmp_path, "stdout")
    out = _gate(tmp_path, command, {"dry_run": True})
    assert out["passed"] is True and out["stdout"] == "[dry-run]"
    assert _new_files(tmp_path) == [], "dry-run 下不应产生新文件"


# ── 沙箱执行分支（2026-10-07）：填 sandbox 走沙箱，不填行为不变 ─────────────

def test_gate_default_stays_host(monkeypatch, tmp_path):
    """不填 sandbox → 宿主执行（既有行为），不触任何沙箱依赖。"""
    from plaita_nodes.gate import GateNode

    class _Exec:
        execution_id = "e1"
        def evaluate(self, v): return v
        def get_global_variable(self, k, d=None): return False

    node = GateNode(id="g1", command="true", gate_name="t", cwd=str(tmp_path))
    out = node.execute(_Exec())
    assert out["passed"] is True
    assert "sandbox" not in out          # 宿主路径不产出该键
    assert out["exit_code"] == 0


def test_gate_sandbox_branch_dispatches_to_driver(monkeypatch, tmp_path):
    """填 sandbox → 经 driver 在沙箱内执行，且 cwd/实例来自同一 handle。"""
    from plaita_nodes import sandbox as sb
    from plaita_nodes.gate import GateNode
    from plaita_nodes.sandbox import WorkspaceHandle, WorkspaceSpec

    calls = {}

    class _FakeClient:
        def exec_argv(self, instance, argv, **kw):
            calls["instance"] = instance
            calls["argv"] = argv
            calls["cwd"] = kw.get("cwd")
            calls["timeout"] = kw.get("timeout")
            return 0, "all good\n", ""

    class _FakeDriver:
        client = _FakeClient()
        def ensure(self, spec, execution_id, ws_key):
            calls["ensure"] = (spec.name, execution_id, ws_key)
            return WorkspaceHandle(driver="ags", id="inst-9", path="/work",
                                   ws_key=ws_key, execution_id=execution_id)

    monkeypatch.setattr(sb, "load_sandboxes",
                        lambda repo=None: {"ags": WorkspaceSpec(name="ags", driver="ags", template="t")})
    monkeypatch.setattr(sb, "get_driver", lambda name: _FakeDriver())

    class _Exec:
        execution_id = "exec-42"
        def evaluate(self, v): return v
        def get_global_variable(self, k, d=None): return False

    node = GateNode(id="g2", command="python3 -m pytest -q", gate_name="tests",
                    sandbox="ags", cwd="/home/user/plaita-ws/repo")
    out = node.execute(_Exec())
    assert out["passed"] is True and out["sandbox"] == "inst-9"
    assert calls["ensure"] == ("ags", "exec-42", "main")     # 与 agent 同实例键
    assert calls["cwd"] == "/home/user/plaita-ws/repo"
    assert "pytest" in " ".join(calls["argv"])


def test_gate_sandbox_unregistered_fails_closed(monkeypatch):
    """spec 未注册 → 直接报错（不静默退化成宿主执行——那会让"沙箱化"变假象）。"""
    from plaita_nodes import sandbox as sb
    from plaita_nodes.gate import GateNode

    monkeypatch.setattr(sb, "load_sandboxes", lambda repo=None: {})

    class _Exec:
        execution_id = "e3"
        def evaluate(self, v): return v
        def get_global_variable(self, k, d=None): return False

    import pytest as _pytest
    with _pytest.raises(ValueError, match="未注册"):
        GateNode(id="g3", command="true", sandbox="nope").execute(_Exec())
