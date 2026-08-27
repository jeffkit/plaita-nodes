"""flowcast 兼容的 agents/providers 配置加载。

与 flowcast 的 provider.js / executor.js 保持同构：
- 搜索顺序 ``~/.flowx → ~/.flowcast → <repo>/.flowcast``，后者深合并覆盖前者；
- ``${VAR}`` 环境变量插值，缺失 fail-fast；
- provider bundle 翻译为各执行器的 env（recursive / claude）。

差异（有意为之，见 ADR-2026-08-27）：agents.json 里的 ``env`` 字段 flowcast
白名单会静默丢弃，这里按配置原样透传（如 glm-52 的 RECURSIVE_MAX_TOKENS）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class AgentConfigError(RuntimeError):
    pass


def _candidate_dirs(repo: Optional[str]) -> list[Path]:
    dirs = [Path.home() / ".flowx", Path.home() / ".flowcast"]
    if repo:
        dirs.append(Path(repo) / ".flowcast")
    return dirs


def _read_config_file(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AgentConfigError(f"读取 {path} 需要 pyyaml：pip install pyyaml") from exc
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raise AgentConfigError(f"不支持的配置格式: {path}")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_merged_config(basename: str, repo: Optional[str] = None) -> Dict[str, Any]:
    """按 flowcast 顺序加载并深合并一个配置名（如 agents / providers）。"""
    merged: Dict[str, Any] = {}
    for directory in _candidate_dirs(repo):
        if not directory.is_dir():
            continue
        for suffix in (".json", ".yaml", ".yml"):
            path = directory / f"{basename}{suffix}"
            if path.is_file():
                merged = _deep_merge(merged, _read_config_file(path))
                break
    return merged


def interpolate(value: Any, environ: Optional[Dict[str, str]] = None) -> Any:
    """递归展开 ``${VAR}``；缺失变量 fail-fast（与 flowcast interpolateEnv 一致）。"""
    env = environ if environ is not None else os.environ
    if isinstance(value, str):
        def _sub(match: re.Match) -> str:
            var = match.group(1)
            if var not in env:
                raise AgentConfigError(f"环境变量 {var} 未设置（配置里的 $({{{var}}}) 无法展开）")
            return env[var]
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, list):
        return [interpolate(v, env) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    return value


def resolve_provider(name: str, providers: Dict[str, Any], environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """解析 provider bundle：{name, type, apiBase, model, apiKey}。"""
    raw = (providers or {}).get("providers", providers).get(name)
    if not raw:
        raise AgentConfigError(f"provider '{name}' 不存在（检查 providers.json）")
    bundle = interpolate(raw, environ)
    return {
        "name": name,
        "type": bundle.get("type", "openai"),
        "apiBase": bundle.get("apiBase") or bundle.get("base") or "",
        "model": bundle.get("model"),
        "apiKey": bundle.get("apiKey") or (environ or {}).get(bundle.get("keyEnv") or "", ""),
    }


def _recursive_env(bundle: Dict[str, Any], model: Optional[str]) -> Dict[str, str]:
    env = {
        "RECURSIVE_PROVIDER_TYPE": bundle["type"],
        "RECURSIVE_API_BASE": bundle["apiBase"],
        "RECURSIVE_API_KEY": bundle["apiKey"],
    }
    resolved_model = model or bundle.get("model")
    if resolved_model:
        env["RECURSIVE_MODEL"] = resolved_model
    return env


def _claude_env(bundle: Dict[str, Any], model: Optional[str]) -> Dict[str, str]:
    env = {
        "ANTHROPIC_BASE_URL": bundle["apiBase"],
        "ANTHROPIC_AUTH_TOKEN": bundle["apiKey"],
    }
    resolved_model = model or bundle.get("model")
    if resolved_model:
        env["CLAUDE_MODEL"] = resolved_model
    return env


_ENV_TRANSLATORS = {"recursive": _recursive_env, "claude": _claude_env}

# flowcast CLI 名 → agentproc executor 名（executor/agentproc-adapter.js 的映射子集）
EXECUTOR_ALIASES = {
    "claude": "claude-code",
    "gemini": "gemini-cli",
    "recursive": "recursive-direct",  # plaita_nodes.agent_run 注册的直调执行器
}


def resolve_agent(name: str, repo: Optional[str] = None, environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """解析一个 agent profile → {name, executor, model, max_steps, timeout_ms, env, extra_args}。

    env = provider 翻译出的执行器 env + agents.json 里 profile.env（flowcast 会丢弃，
    这里透传——对 glm-52 的 RECURSIVE_MAX_TOKENS 是行为修正，见模块 docstring）。
    """
    agents = load_merged_config("agents", repo=repo)
    providers = load_merged_config("providers", repo=repo)
    raw = (agents or {}).get("agents", agents).get(name)
    if not raw:
        raise AgentConfigError(f"agent '{name}' 不存在（检查 agents.json）")
    profile = interpolate(raw, environ)

    executor = profile.get("executor")
    if not executor:
        raise AgentConfigError(f"agent '{name}' 缺少 executor 字段")

    env: Dict[str, str] = {}
    model = profile.get("model")
    provider_name = profile.get("provider")
    if provider_name:
        bundle = resolve_provider(provider_name, providers, environ)
        translator = _ENV_TRANSLATORS.get(executor)
        if translator is None:
            raise AgentConfigError(
                f"executor '{executor}' 是锁定型 CLI（不接受外部 provider）；"
                f"agent '{name}' 却声明了 provider '{provider_name}'"
            )
        env.update(translator(bundle, model))
        if not model:
            model = bundle.get("model")
    # profile.env 透传（flowcast 白名单会丢弃这里；见模块 docstring）
    for key, value in (profile.get("env") or {}).items():
        env[str(key)] = str(value)

    return {
        "name": name,
        "executor": executor,
        "model": model,
        "max_steps": profile.get("maxSteps"),
        "timeout_ms": profile.get("timeout"),
        "env": env,
        "extra_args": profile.get("extraArgs") or [],
    }
