"""测试公共件：配置目录 fixture + 测试执行桩 re-export。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_exec import FakeExecution  # noqa: F401  # re-export 兼容

__all__ = ["FakeExecution", "agent_config_repo", "register_test_echo_executor"]

# 部署入口会把改变节点行为的变量注入进程：flow-worker 启动即注入
# PLAITA_NODES_WORKSPACE_ROOT（plaita#39 的 writefile jail），服务环境还常带
# LLM_API_BASE/LLM_API_KEY/LLM_MODEL。用例断言的是节点契约而非某个部署档，所以
# 每个用例前统一抹掉——否则在 worker 进程里跑 pytest（管线门禁就是这么跑的）会
# 随部署环境红绿。
_DEPLOYMENT_ENV_VARS = (
    "PLAITA_NODES_WORKSPACE_ROOT",
    "LLM_API_BASE",
    "LLM_API_KEY",
    "LLM_MODEL",
)


@pytest.fixture(autouse=True)
def hermetic_deployment_env(monkeypatch):
    """抹掉宿主注入的部署变量；用例内 monkeypatch.setenv 仍照常生效。"""
    for key in _DEPLOYMENT_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


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
            "echo-m": {"executor": "test-echo", "model": "TEST-MODEL"},
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
