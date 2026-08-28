"""AgentRunNode：recursive 结果提取、dry-run、agentproc in-process 链路。"""
from __future__ import annotations

import json

import pytest

from plaita_nodes.agent_run import AgentRunError, AgentRunNode, extract_recursive_result
from plaita_nodes.config import AgentConfigError


class TestExtractRecursiveResult:
    def test_plain(self):
        out = extract_recursive_result('{"result": "你好", "is_error": false}')
        assert out == {"result": "你好", "is_error": False}

    def test_with_noise_prefix_and_braces_in_strings(self):
        noisy = 'step logs {"note": "包含 { 花括号 \\" 与 \\\\ "} …\n{"result": "ok {braced}", "is_error": false}'
        out = extract_recursive_result(noisy)
        assert out["result"] == "ok {braced}"
        assert out["is_error"] is False

    def test_fallback_raw(self):
        out = extract_recursive_result("完全不是 JSON 的输出")
        assert out == {"result": "完全不是 JSON 的输出", "is_error": False, "raw": True}


class TestAgentRunNode:
    def test_dry_run_field(self, fake_execution):
        node = AgentRunNode(id="t", agent="glm-52", prompt="写一篇文章", dry_run=True)
        out = node.execute(fake_execution)
        assert out["dry_run"] is True
        assert "would run" in out["text"]
        assert "glm-52" in out["text"]  # dry 不解析 profile，cli 即 agent 名

    def test_dry_run_via_global(self, fake_execution):
        from fake_exec import FakeExecution

        node = AgentRunNode(id="t", agent="glm-52", prompt="hi")
        out = node.execute(FakeExecution(global_vars={"dry_run": True}))
        assert out["dry_run"] is True

    def test_missing_agent_raises(self, tmp_path):
        node = AgentRunNode(id="t", agent="no-such", prompt="x", repo=str(tmp_path))
        with pytest.raises(AgentConfigError):
            node.execute(type("E", (), {
                "evaluate": lambda self, v: v,
                "get_global_variable": lambda self, k, d=None: d,
            })())

    def test_runs_via_agentproc_executor(self, agent_config_repo, register_test_echo_executor):
        node = AgentRunNode(id="t", agent="echo", prompt="hello world", repo=str(agent_config_repo))
        out = node.execute(type("E", (), {
            "evaluate": lambda self, v: v,
            "get_global_variable": lambda self, k, d=None: d,
        })())
        assert out["dry_run"] is False
        assert out["cli"] == "test-echo"
        assert "msg=hello world" in out["text"]

    def test_recursive_error_result_raises(self, agent_config_repo, monkeypatch):
        """recursive 返回 is_error=true 时节点应抛错。"""
        from agentproc import EXECUTORS

        def _make():
            def build_args(message, session_id, env):
                return ["true"]  # /usr/bin/true，无输出
            return {"build_args": build_args}

        EXECUTORS["test-recursive-empty"] = {
            "cli_name": "true", "plain": True, "make_handlers": _make,
        }
        agents = json.loads((agent_config_repo / ".flowcast" / "agents.json").read_text())
        agents["agents"]["rec-err"] = {"executor": "test-recursive-empty"}
        (agent_config_repo / ".flowcast" / "agents.json").write_text(json.dumps(agents))

        node = AgentRunNode(id="t", agent="rec-err", prompt="x", repo=str(agent_config_repo))
        # plain 路径空输出 → agentproc 报 "returned empty output"
        with pytest.raises(AgentRunError):
            node.execute(type("E", (), {
                "evaluate": lambda self, v: v,
                "get_global_variable": lambda self, k, d=None: d,
            })())
