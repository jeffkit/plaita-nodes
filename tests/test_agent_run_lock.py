"""AgentRunNode kill-before-start 门（G3）：孤儿清场 + 遗言锁接线。"""
from __future__ import annotations

import json
import subprocess

import pytest

from agentproc import run_lock
from plaita_nodes.agent_run import AgentRunError, AgentRunNode, preflight_workspace

_EXEC = type("E", (), {
    "evaluate": lambda self, v: v,
    "get_global_variable": lambda self, k, d=None: d,
})


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(run_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))
    return tmp_path / "locks"


def _spawn_session_leader(secs=60):
    return subprocess.Popen(["sleep", str(secs)], start_new_session=True)


class TestPreflightWorkspace:
    def test_clean_passes(self, tmp_path):
        info = preflight_workspace(str(tmp_path / "fresh-ws"))
        assert info["action"] == "clean"

    def test_kills_orphan_before_start(self, tmp_path):
        ws = str(tmp_path / "ws")
        proc = _spawn_session_leader()
        try:
            run_lock.write_run_lock(ws, proc.pid, ["sleep", "60"])
            info = preflight_workspace(ws)
            assert info["action"] == "killed"
            assert proc.poll() is not None
        finally:
            proc.wait(timeout=15)

    def test_busy_refuses_to_start(self, tmp_path, monkeypatch):
        ws = str(tmp_path / "ws")
        proc = _spawn_session_leader()
        try:
            run_lock.write_run_lock(ws, proc.pid, ["sleep", "60"])
            monkeypatch.setattr(run_lock, "_ps_fields", lambda pid: None)
            with pytest.raises(AgentRunError, match="拒绝开工"):
                preflight_workspace(ws)
            assert proc.poll() is None  # 存疑不盲杀
        finally:
            proc.kill()
            proc.wait(timeout=15)


class TestAgentRunNodePreflight:
    def _register_echo(self):
        from agentproc import EXECUTORS

        def _make():
            def build_args(message, session_id, env):
                return ["sh", "-c", f"echo msg={message}"]
            return {"build_args": build_args}

        EXECUTORS.setdefault("test-lock-echo", {
            "cli_name": "sh", "plain": True, "make_handlers": _make})

    def test_node_preflight_blocks_on_busy(self, agent_config_repo, monkeypatch):
        """直跑路径开工前过 preflight：busy → AgentRunError，agent 不被拉起。"""
        self._register_echo()
        ws = str(agent_config_repo)
        proc = _spawn_session_leader()
        try:
            run_lock.write_run_lock(ws, proc.pid, ["sleep", "60"])
            monkeypatch.setattr(run_lock, "_ps_fields", lambda pid: None)
            node = AgentRunNode(id="t", agent="echo", prompt="hi", repo=ws)
            with pytest.raises(AgentRunError):
                node.execute(_EXEC())
        finally:
            proc.kill()
            proc.wait(timeout=15)

    def test_node_cleans_orphan_then_runs_and_leaves_no_lock(
            self, agent_config_repo, register_test_echo_executor):
        """孤儿在场：preflight 杀掉 → 节点正常跑完 → 遗言锁收尾清零。"""
        ws = str(agent_config_repo)
        proc = _spawn_session_leader()
        try:
            run_lock.write_run_lock(ws, proc.pid, ["sleep", "60"])
            node = AgentRunNode(id="t", agent="echo", prompt="hello", repo=ws)
            out = node.execute(_EXEC())
            assert "msg=hello" in out["text"]
            assert proc.poll() is not None
            assert not run_lock.lock_path_for(ws).exists()
        finally:
            proc.wait(timeout=15)

    def test_node_run_lock_key_passed_to_agentproc(
            self, agent_config_repo, register_test_echo_executor, monkeypatch):
        """repo 绝对路径作为 run_lock_key 传给 agentproc（spawn 侧遗言落盘）。

        agent_run.execute 在调用时才 from-import agentproc.runner.run，
        打补丁到 runner 模块属性即可被拾取。
        """
        captured = {}
        import agentproc.runner as ap_runner

        real_run = ap_runner.run

        def _spy(profile, options):
            captured["key"] = options.run_lock_key
            return real_run(profile, options)

        monkeypatch.setattr(ap_runner, "run", _spy)
        self._register_echo()
        ws = str(agent_config_repo)
        node = AgentRunNode(id="t", agent="echo", prompt="hi", repo=ws)
        node.execute(_EXEC())
        assert captured["key"] == ws
