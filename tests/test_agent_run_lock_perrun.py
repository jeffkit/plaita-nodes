"""issue #2 — 遗言锁键带 run 作用域：同 workspace 并发 run 不再互相当孤儿杀。

契约（issue #2 验收）：
1. 同 workspace 两 run：run B 的 preflight 放行后 run A 的 live agent 仍存活（未被
   killpg）、run A 的遗言锁记录仍在（pid 未被覆盖/删除），且 run B 自身正常开工；
2. preflight 只清「归属本 run」的锁记录，清理留痕（warning log）带出被清 pid 与命令；
3. 存疑不盲杀不回归（既有 test_agent_run_lock.py::test_busy_refuses_to_start 看护）。

run 作用域取 NodeExecutionContext 的公开属性 execution_id（plaita/core/node_context.py:19），
锁键 = workspace 绝对路径 + "#" + run 作用域（src/plaita_nodes/agent_run.py::_run_lock_key）；
既有无 execution_id 的执行桩（tests/test_agent_run_lock.py 的 _EXEC）回退裸 workspace 键，
既有用例因此仍全绿。
"""
from __future__ import annotations

import json
import logging
import subprocess

import pytest

from agentproc import run_lock
from plaita_nodes.agent_run import AgentRunNode


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(run_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))
    return tmp_path / "locks"


class _Run:
    """NodeExecutionContext 桩：本用例用到的公开面 + execution_id（run 级身份）。"""

    def __init__(self, execution_id: str):
        self.execution_id = execution_id

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


def _spawn_session_leader(secs=60):
    """真实会话领袖（start_new_session）：pid==pgid，才能被 killpg 打中。"""
    return subprocess.Popen(["sleep", str(secs)], start_new_session=True)


def _run_lock_key(monkeypatch, node, execution) -> str:
    """截获本次 run 实际用的遗言锁键——不对键格式做假设（看行为，不看实现形状）。"""
    import agentproc.runner as ap_runner

    real_run = ap_runner.run
    captured = {}

    def _spy(profile, options):
        captured["key"] = options.run_lock_key
        return real_run(profile, options)

    monkeypatch.setattr(ap_runner, "run", _spy)
    node.execute(execution)
    key = captured.get("key")
    assert key, "节点未向 agentproc 传 run_lock_key（遗言锁断线）"
    return key


class TestConcurrentRunsSameWorkspace:
    def test_sibling_live_run_survives_preflight(
            self, agent_config_repo, register_test_echo_executor, monkeypatch):
        """run B 的 preflight 不得动兄弟 run A 的 live agent 与锁（基线：killpg 后放行）。"""
        ws = str(agent_config_repo)
        node_a = AgentRunNode(id="a", agent="echo", prompt="a", repo=ws)
        key_a = _run_lock_key(monkeypatch, node_a, _Run("run-a"))

        proc_a = _spawn_session_leader()
        try:
            run_lock.write_run_lock(key_a, proc_a.pid, ["sleep", "60"])

            node_b = AgentRunNode(id="b", agent="echo", prompt="hello", repo=ws)
            out = node_b.execute(_Run("run-b"))

            assert "msg=hello" in out["text"]          # 仅 run B 自身正常开工
            assert proc_a.poll() is None               # 兄弟 live agent 未被 killpg
            record = json.loads(run_lock.lock_path_for(key_a).read_text(encoding="utf-8"))
            assert record["pid"] == proc_a.pid         # 兄弟 live run 的锁未被删/覆盖
        finally:
            if proc_a.poll() is None:
                proc_a.kill()
            proc_a.wait(timeout=15)

    def test_own_run_live_orphan_still_cleaned_with_evidence(
            self, agent_config_repo, register_test_echo_executor, monkeypatch, caplog):
        """归属本 run 的遗言锁仍要清（别修成「谁都不清」），且留痕带出 pid 与命令。"""
        ws = str(agent_config_repo)
        node = AgentRunNode(id="t", agent="echo", prompt="hello", repo=ws)
        key_b = _run_lock_key(monkeypatch, node, _Run("run-b"))

        proc_b = _spawn_session_leader()
        try:
            run_lock.write_run_lock(key_b, proc_b.pid, ["sleep", "60"])

            with caplog.at_level(logging.WARNING, logger="plaita_nodes.agent_run"):
                out = node.execute(_Run("run-b"))

            assert "msg=hello" in out["text"]
            assert proc_b.poll() is not None
            assert not run_lock.lock_path_for(key_b).exists()
            assert str(proc_b.pid) in caplog.text
            assert "sleep" in caplog.text
        finally:
            if proc_b.poll() is None:
                proc_b.kill()
            proc_b.wait(timeout=15)
