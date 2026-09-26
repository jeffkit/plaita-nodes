"""AgentRunNode：recursive 结果提取、dry-run、agentproc in-process 链路。"""
from __future__ import annotations

import json
import os

import pytest

from plaita_nodes.agent_run import (
    AgentRunError, AgentRunNode, extract_recursive_result, parse_stream_details,
)
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
        assert out["model"] == "echo"  # profile 无 model → agent 名兜底

    def test_output_model_from_profile(self, agent_config_repo, register_test_echo_executor):
        """输出带 model（Langfuse generation 归因用）：profile.model 优先。"""
        node = AgentRunNode(id="t", agent="echo-m", prompt="hi", repo=str(agent_config_repo))
        out = node.execute(type("E", (), {
            "evaluate": lambda self, v: v,
            "get_global_variable": lambda self, k, d=None: d,
        })())
        assert out["model"] == "TEST-MODEL"
        assert out["usage"] is None or isinstance(out["usage"], dict)

    def test_recursive_error_result_raises(self, agent_config_repo):
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

    def test_recursive_build_args_permission_mode_auto(self):
        """无头自动化必须显式放行工具权限：recursive-direct 无 permission
        桥接，default 模式下工具调用发 control_request 等批准直至挂起。"""
        from plaita_nodes.agent_run import _make_recursive_handlers

        argv = _make_recursive_handlers()["build_args"]("hi", "", {})
        assert "--permission-mode" in argv
        assert argv[argv.index("--permission-mode") + 1] == "auto"

    def test_parse_stream_details_pairs_tools_and_turns(self):
        lines = [
            '{"type":"system","subtype":"init"}',
            '{"type":"assistant","message":{"model":"GLM-5.2","content":['
            '{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls /tmp"}}]}}',
            '{"type":"user","message":{"content":['
            '{"type":"tool_result","tool_use_id":"t1","content":"a.txt\\nb.txt"}]}}',
            "not a json line",
            '{"type":"assistant","message":{"model":"GLM-5.2","content":['
            '{"type":"text","text":"完成"}]}}',
            '{"type":"result","result":"完成","usage":{"input_tokens":9,"output_tokens":2}}',
        ]
        obs = parse_stream_details(lines)
        assert obs == [
            {"type": "span", "name": "tool:Bash",
             "input": {"command": "ls /tmp"}, "output": "a.txt\nb.txt"},
            {"type": "generation", "name": "turn:1", "model": "GLM-5.2", "output": "完成"},
        ]

    def test_parse_stream_details_cap(self):
        from plaita_nodes.agent_run import _DETAILS_CAP
        lines = ['{"type":"assistant","message":{"model":"m","content":['
                 '{"type":"text","text":"t%d"}]}}' % i for i in range(_DETAILS_CAP + 10)]
        assert len(parse_stream_details(lines)) == _DETAILS_CAP

    def test_details_mode_collects_observations(self, agent_config_repo):
        """details=true：recursive 走 stream-json，输出带 observations。"""
        import json as _json
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"model": "M-1", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "echo hi"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "hi"}]}},
            {"type": "assistant", "message": {"model": "M-1", "content": [
                {"type": "text", "text": "搞定"}]}},
            {"type": "result", "result": "搞定", "is_error": False,
             "usage": {"input_tokens": 5, "output_tokens": 1}},
        ]
        stub = agent_config_repo / "stub-stream.sh"
        body = "\n".join(_json.dumps(e) for e in events)
        stub.write_text("#!/bin/sh\ncat <<'STREAM_EOF'\n" + body + "\nSTREAM_EOF\n")
        stub.chmod(0o755)
        agents = _json.loads((agent_config_repo / ".flowcast" / "agents.json").read_text())
        agents["agents"]["rec-stream"] = {
            "executor": "recursive", "model": "M-1",
            "env": {"RECURSIVE_BIN": str(stub)},
        }
        (agent_config_repo / ".flowcast" / "agents.json").write_text(_json.dumps(agents))

        node = AgentRunNode(id="t", agent="rec-stream", prompt="x",
                            repo=str(agent_config_repo), details=True)
        out = node.execute(type("E", (), {
            "evaluate": lambda self, v: v,
            "get_global_variable": lambda self, k, d=None: d,
        })())
        assert out["text"] == "搞定"
        assert out["usage"] == {"input_tokens": 5, "output_tokens": 1}
        assert out["observations"] == [
            {"type": "span", "name": "tool:Bash",
             "input": {"command": "echo hi"}, "output": "hi"},
            {"type": "generation", "name": "turn:1", "model": "M-1", "output": "搞定"},
        ]

    def test_details_false_has_no_observations_key(self, agent_config_repo):
        import json as _json
        stub = agent_config_repo / "stub-plain.sh"
        stub.write_text("#!/bin/sh\nprintf '%s' "
                        "'" + _json.dumps({"result": "ok", "is_error": False}) + "'")
        stub.chmod(0o755)
        agents = _json.loads((agent_config_repo / ".flowcast" / "agents.json").read_text())
        agents["agents"]["rec-plain"] = {
            "executor": "recursive", "env": {"RECURSIVE_BIN": str(stub)},
        }
        (agent_config_repo / ".flowcast" / "agents.json").write_text(_json.dumps(agents))

        node = AgentRunNode(id="t", agent="rec-plain", prompt="x",
                            repo=str(agent_config_repo))
        out = node.execute(type("E", (), {
            "evaluate": lambda self, v: v,
            "get_global_variable": lambda self, k, d=None: d,
        })())
        assert "observations" not in out

    def test_claude_details_via_protocol_lines(self, agent_config_repo, monkeypatch):
        """claude-code 执行器（NDJSON 路径）：经 on_protocol_line 收集原始行，
        同一解析器产出 observations。stub claude 注入 PATH。"""
        import json as _json

        bindir = agent_config_repo / "bin"
        bindir.mkdir()
        events = [
            {"type": "system", "subtype": "init", "session_id": "sess-c1"},
            {"type": "assistant", "message": {"model": "GLM-5.2", "content": [
                {"type": "tool_use", "id": "c1", "name": "Read",
                 "input": {"file_path": "/tmp/x"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "data"}]}},
            {"type": "assistant", "message": {"model": "GLM-5.2", "content": [
                {"type": "text", "text": "claude 完成"}]}},
            {"type": "result", "session_id": "sess-c1", "result": "claude 完成",
             "is_error": False,
             "usage": {"input_tokens": 11, "output_tokens": 4}},
        ]
        stub = bindir / "claude"
        stub.write_text("#!/bin/sh\ncat <<'STREAM_EOF'\n"
                        + "\n".join(_json.dumps(e) for e in events)
                        + "\nSTREAM_EOF\n")
        stub.chmod(0o755)
        monkeypatch.setenv("TEST_API_KEY", "sk-test")  # provider ${TEST_API_KEY} 插值
        monkeypatch.setenv("PATH", str(bindir) + os.pathsep +
                           os.environ["PATH"])

        agents = _json.loads((agent_config_repo / ".flowcast" / "agents.json").read_text())
        agents["agents"]["claude-glm"] = {
            "executor": "claude", "model": "GLM-5.2",
            "provider": "glm-52",
        }
        (agent_config_repo / ".flowcast" / "agents.json").write_text(_json.dumps(agents))

        node = AgentRunNode(id="t", agent="claude-glm", prompt="x",
                            repo=str(agent_config_repo), details=True)
        out = node.execute(type("E", (), {
            "evaluate": lambda self, v: v,
            "get_global_variable": lambda self, k, d=None: d,
        })())
        assert "claude 完成" in out["text"]
        assert out["cli"] == "claude"
        assert out["model"] == "GLM-5.2"
        assert out["usage"] == {"input_tokens": 11, "output_tokens": 4}
        assert out["observations"] == [
            {"type": "span", "name": "tool:Read",
             "input": {"file_path": "/tmp/x"}, "output": "data"},
            {"type": "generation", "name": "turn:1", "model": "GLM-5.2",
             "output": "claude 完成"},
        ]

    def test_recursive_usage_fallback_from_parsed(self, agent_config_repo):
        """recursive-direct 路径：agentproc 拿不到事件 usage 时，从结果对象
        本体兜底（recursive --output-format json 自带 usage 字段）。"""
        import json as _json
        from pathlib import Path

        # 桩"recursive 二进制"：输出带 usage 的结果对象（经 RECURSIVE_BIN 注入）
        stub = agent_config_repo / "stub-recursive.sh"
        payload = {"result": "ok", "is_error": False,
                   "usage": {"input_tokens": 36, "output_tokens": 3,
                             "cache_read_input_tokens": 100}}
        stub.write_text("#!/bin/sh\nprintf '%s' '" + _json.dumps(payload) + "'\n")
        stub.chmod(0o755)

        agents = _json.loads((agent_config_repo / ".flowcast" / "agents.json").read_text())
        agents["agents"]["rec-use"] = {
            "executor": "recursive", "model": "M-1",
            "env": {"RECURSIVE_BIN": str(stub)},
        }
        (agent_config_repo / ".flowcast" / "agents.json").write_text(_json.dumps(agents))

        node = AgentRunNode(id="t", agent="rec-use", prompt="x",
                            repo=str(Path("/tmp")))
        out = node.execute(type("E", (), {
            "evaluate": lambda self, v: v,
            "get_global_variable": lambda self, k, d=None: d,
        })())
        assert out["text"] == "ok"
        assert out["model"] == "M-1"
        assert out["usage"] == payload["usage"]
