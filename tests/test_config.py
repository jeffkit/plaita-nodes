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


# ── cursor：宿主锁定型 CLI 的 model 翻译（2026-10-10/11 实测回归）──────────
# 背景：cursor executor 的 build_args 只读**环境变量** `CURSOR_MODEL`，而
# resolve_agent 通用路径只把 model 放进返回值的 `model` 字段（该字段用于观测
# 归因，**不传给执行器**）。缺翻译时 `agents.json` 里写的 model 被静默忽略、
# 实际走 CLI 默认模型 —— 典型的「配置写了不生效」。


def _write_cursor_profile(root: Path, body: dict) -> None:
    p = root / ".plaita"
    p.mkdir(parents=True, exist_ok=True)
    (p / "agents.json").write_text(
        json.dumps({"agents": {"cur": body}}, ensure_ascii=False), encoding="utf-8")


def test_cursor_model_translated_without_provider(tmp_path: Path, monkeypatch):
    """无 provider（锁定型 CLI 的常态）也要把 model 落进 CURSOR_MODEL。

    这是管线实际用法：cursor 用本机登录态，`agents.json` 里通常**不写
    provider**——若只在 `if provider_name:` 分支翻译，就走不到这里。
    """
    monkeypatch.setattr("plaita_nodes.config.Path.home", lambda: tmp_path)
    _write_cursor_profile(tmp_path, {"executor": "cursor",
                                     "model": "claude-4.6-sonnet-medium"})
    prof = resolve_agent("cur", repo=str(tmp_path))
    assert prof["executor"] == "cursor"
    assert prof["env"].get("CURSOR_MODEL") == "claude-4.6-sonnet-medium", (
        "model 必须翻译成 CURSOR_MODEL——否则 agents.json 里的模型被静默忽略"
    )
    assert prof["model"] == "claude-4.6-sonnet-medium"


def test_cursor_model_translated_with_provider(tmp_path: Path, monkeypatch):
    """带 provider 时翻译 model + 可选端点/密钥。"""
    monkeypatch.setattr("plaita_nodes.config.Path.home", lambda: tmp_path)
    p = tmp_path / ".plaita"
    p.mkdir(parents=True, exist_ok=True)
    (p / "agents.json").write_text(
        json.dumps({"agents": {"cur": {"executor": "cursor", "provider": "px"}}}),
        encoding="utf-8")
    (p / "providers.json").write_text(
        json.dumps({"providers": {"px": {"type": "anthropic",
                                         "apiBase": "https://x.test",
                                         "apiKey": "k-1",
                                         "model": "claude-5-sonnet"}}}),
        encoding="utf-8")
    env = resolve_agent("cur", repo=str(tmp_path))["env"]
    assert env["CURSOR_MODEL"] == "claude-5-sonnet"      # provider 的 model 兜底
    assert env["CURSOR_API_ENDPOINT"] == "https://x.test"
    assert env["CURSOR_API_KEY"] == "k-1"


def test_recursive_without_provider_does_not_crash(tmp_path: Path, monkeypatch):
    """回归防线：recursive 无 provider 时不得因「模型翻译」抛 KeyError。

    历史事故：曾让 recursive 也走「无 provider 就翻译 model」的分支，而
    `_recursive_env` 需要 bundle 的 apiBase/apiKey，传空 dict 直接 KeyError，
    连锁 7 个用例失败。故 model-only 翻译必须**单列一表**，不共用
    `_ENV_TRANSLATORS`。
    """
    monkeypatch.setattr("plaita_nodes.config.Path.home", lambda: tmp_path)
    p = tmp_path / ".plaita"
    p.mkdir(parents=True, exist_ok=True)
    (p / "agents.json").write_text(
        json.dumps({"agents": {"r": {"executor": "recursive", "model": "m-1"}}}),
        encoding="utf-8")
    prof = resolve_agent("r", repo=str(tmp_path))     # 不得抛异常
    assert prof["env"] == {}, "recursive 无 provider 时不应产出 env"
