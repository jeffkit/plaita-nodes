"""测试公共件：最小 NodeExecutionContext 桩 + 配置目录 fixture。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


class FakeExecution:
    """满足节点用到的 NodeExecutionContext 窄接口：evaluate / get_global_variable。"""

    express_prefix = "$"
    context: dict = {}

    def __init__(self, global_vars: dict | None = None):
        self._globals = global_vars or {}

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return self._globals.get(key, default)


@pytest.fixture
def fake_execution():
    return FakeExecution()


@pytest.fixture
def agent_config_repo(tmp_path: Path, monkeypatch) -> Path:
    """建 <tmp>/.flowcast/{agents,providers}.json 并把 HOME 隔离到 tmp
   （防止读进机器上真实的 ~/.flowcast）。"""
    monkeypatch.setattr("plaita_nodes.config.Path.home", lambda: tmp_path)
    flowcast = tmp_path / ".flowcast"
    flowcast.mkdir()
    (flowcast / "agents.json").write_text(json.dumps({
        "agents": {
            "glm-52": {
                "executor": "recursive",
                "provider": "glm-52",
                "model": "GLM-5.2",
                "maxSteps": 30,
                "env": {"RECURSIVE_MAX_TOKENS": "65536"},
            },
            "echo": {"executor": "test-echo"},
        }
    }), encoding="utf-8")
    (flowcast / "providers.json").write_text(json.dumps({
        "providers": {
            "glm-52": {
                "type": "anthropic",
                "apiBase": "https://open.bigmodel.cn/api/anthropic",
                "model": "GLM-5.2",
                "apiKey": "${TEST_API_KEY}",
            }
        }
    }), encoding="utf-8")
    return tmp_path


@pytest.fixture
def register_test_echo_executor():
    """注册一个 plain 回显 executor，验证 agentproc in-process 执行链路。"""
    from agentproc import EXECUTORS

    def _make():
        def build_args(message, session_id, env):
            return ["echo", f"env={env.get('ECHO_MARKER', '')} msg={message}"]
        return {"build_args": build_args}

    EXECUTORS["test-echo"] = {
        "cli_name": "echo", "plain": True, "make_handlers": _make,
    }
    yield
    EXECUTORS.pop("test-echo", None)
