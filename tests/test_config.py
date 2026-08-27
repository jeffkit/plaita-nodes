"""config.py：flowcast 兼容配置层的行为测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from plaita_nodes.config import (
    AgentConfigError,
    interpolate,
    load_merged_config,
    resolve_agent,
    resolve_provider,
)


def test_load_merged_config_reads_repo_level(agent_config_repo: Path):
    merged = load_merged_config("agents", repo=str(agent_config_repo))
    assert "glm-52" in merged["agents"]
    assert merged["agents"]["glm-52"]["executor"] == "recursive"


def test_load_merged_config_missing_returns_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("plaita_nodes.config.Path.home", lambda: tmp_path)
    assert load_merged_config("agents", repo=str(tmp_path)) == {}


def test_resolve_provider_interpolates_env(agent_config_repo: Path, monkeypatch):
    providers = load_merged_config("providers", repo=str(agent_config_repo))
    monkeypatch.setenv("TEST_API_KEY", "sk-test-123")
    bundle = resolve_provider("glm-52", providers)
    assert bundle["type"] == "anthropic"
    assert bundle["apiBase"] == "https://open.bigmodel.cn/api/anthropic"
    assert bundle["apiKey"] == "sk-test-123"
    assert bundle["model"] == "GLM-5.2"


def test_interpolate_missing_var_fails_fast():
    with pytest.raises(AgentConfigError, match="NOPE_VAR"):
        interpolate("${NOPE_VAR}", environ={})


def test_interpolate_nested_structures():
    out = interpolate({"a": ["${V1}", 2], "b": {"c": "${V2}"}}, environ={"V1": "x", "V2": "y"})
    assert out == {"a": ["x", 2], "b": {"c": "y"}}


def test_resolve_agent_recursive_env_translation(agent_config_repo: Path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk-live")
    profile = resolve_agent("glm-52", repo=str(agent_config_repo))
    assert profile["executor"] == "recursive"
    assert profile["model"] == "GLM-5.2"
    assert profile["max_steps"] == 30
    # provider 翻译
    assert profile["env"]["RECURSIVE_PROVIDER_TYPE"] == "anthropic"
    assert profile["env"]["RECURSIVE_API_BASE"] == "https://open.bigmodel.cn/api/anthropic"
    assert profile["env"]["RECURSIVE_API_KEY"] == "sk-live"
    assert profile["env"]["RECURSIVE_MODEL"] == "GLM-5.2"
    # profile.env 透传（flowcast 会丢弃——行为修正点）
    assert profile["env"]["RECURSIVE_MAX_TOKENS"] == "65536"


def test_resolve_agent_locked_executor_rejects_provider(agent_config_repo: Path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk-x")
    agents = json.loads((agent_config_repo / ".flowcast" / "agents.json").read_text())
    agents["agents"]["bad"] = {"executor": "codex", "provider": "glm-52"}
    (agent_config_repo / ".flowcast" / "agents.json").write_text(json.dumps(agents))
    with pytest.raises(AgentConfigError, match="锁定型"):
        resolve_agent("bad", repo=str(agent_config_repo))


def test_resolve_agent_missing(agent_config_repo: Path):
    with pytest.raises(AgentConfigError, match="no-such"):
        resolve_agent("no-such", repo=str(agent_config_repo))
