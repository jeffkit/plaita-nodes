"""LlmNode —— 单次 LLM 补全原子（OpenAI 兼容 chat/completions）。

与 AgentRunNode 的边界（原子性原则）：
- ``agentrun``：多步 agentic 循环（模型可调用工具、自主多轮），重；
- ``llm``：一次补全请求，输入消息 → 输出文本，轻。流程中"摘要/改写/抽取/
  分类"这类单发生成用它，不要动用 agent。

凭证与端点（三选一，优先级从高到低）：
1. 显式 ``api_base`` + ``api_key`` + ``model`` 字段（api_key 支持 ${ENV} 插值）
2. ``provider``：providers.json（flowcast 同款格式）里的名字，自动取
   apiBase/apiKey/model
3. 环境变量 LLM_API_BASE / LLM_API_KEY / LLM_MODEL

输出：``{"text", "model", "usage"}``。
"""
from __future__ import annotations

import json
import os
from typing import Any, ClassVar, Optional

import requests
from pydantic import Field

from plaita import Node

try:
    from .config import load_merged_config, resolve_provider
except ImportError:  # 平铺布局兜底
    from plaita_nodes.config import (  # type: ignore[no-redef]
        load_merged_config, resolve_provider,
    )


class LlmError(RuntimeError):
    pass


def resolve_llm_endpoint(api_base: Optional[str], api_key: Optional[str],
                         model: Optional[str],
                         provider_name: Optional[str] = None) -> tuple[str, str, str]:
    """LLM 端点三级回退：字段显式值 > provider bundle > LLM_* 环境变量，
    → (api_base, api_key, model)。供 llm / decision 等节点共用。"""
    if provider_name:
        try:
            bundle = resolve_provider(provider_name, load_merged_config("providers"))
        except Exception as exc:  # noqa: BLE001
            raise LlmError(f"provider '{provider_name}' 解析失败: {exc}") from exc
        api_base = api_base or bundle.get("apiBase")
        api_key = api_key or bundle.get("apiKey")
        model = model or bundle.get("model")

    api_base = api_base or os.environ.get("LLM_API_BASE", "")
    api_key = api_key or os.environ.get("LLM_API_KEY", "")
    model = model or os.environ.get("LLM_MODEL", "")
    if not (api_base and api_key and model):
        raise LlmError(
            "LLM 端点不完整：需要 api_base/api_key/model（字段、provider 或 "
            "LLM_API_BASE/LLM_API_KEY/LLM_MODEL 环境变量）")
    return str(api_base).rstrip("/"), str(api_key), str(model)


class LlmNode(Node):
    """调用一次 LLM 补全，返回文本。

    JSON 字段：
    - ``messages``: 完整消息数组 [{"role","content"},...]（content 支持表达式）
    - ``prompt`` / ``system``: 简捷形式——拼成单条 user / system 消息
    - ``provider``: providers.json 中的名字（提供 apiBase/apiKey/model 默认值）
    - ``api_base`` / ``api_key`` / ``model``: 显式覆盖（model 必填兜底链末端）
    - ``temperature`` / ``max_tokens`` / ``json_mode``: 采样与输出格式
    - ``dry_run``: 为 true（或 globalContext.dry_run）时不请求，返回 fake 文本

    输出：``{"text", "model", "usage", "dry_run"}``。
    """

    node_type: ClassVar[str] = "llm"
    node_name: ClassVar[str] = "LLM 补全"

    messages: Optional[Any] = None
    prompt: Optional[Any] = None
    system: Optional[Any] = None
    provider: Optional[Any] = None
    api_base: Optional[Any] = None
    api_key: Optional[Any] = None
    model: Optional[Any] = None
    temperature: float = 0.2
    max_tokens: Optional[Any] = None
    json_mode: bool = False
    timeout_secs: int = Field(default=120)
    dry_run: bool = False

    def _resolve_endpoint(self, execution: Any) -> tuple[str, str, str]:
        ev = lambda v: execution.evaluate(v) if v is not None else None  # noqa: E731
        return resolve_llm_endpoint(
            ev(self.api_base), ev(self.api_key), ev(self.model),
            ev(self.provider) if self.provider is not None else None)

    def execute(self, execution: Any) -> dict:
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            model = str(execution.evaluate(self.model) or "") if self.model is not None else ""
            return {"text": "[dry-run] llm completion", "model": model,
                    "usage": None, "dry_run": True}

        if self.messages is not None:
            messages = execution.evaluate(self.messages)
        else:
            messages = []
            system = execution.evaluate(self.system) if self.system is not None else None
            if system:
                messages.append({"role": "system", "content": str(system)})
            prompt = execution.evaluate(self.prompt) if self.prompt is not None else ""
            messages.append({"role": "user", "content": str(prompt or "")})
        if not messages:
            raise LlmError("llm 节点缺少消息（prompt/messages）")

        api_base, api_key, model = self._resolve_endpoint(execution)
        body: dict = {"model": model, "messages": messages, "temperature": self.temperature}
        if self.max_tokens is not None:
            body["max_tokens"] = int(execution.evaluate(self.max_tokens))
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}

        resp = requests.post(f"{api_base}/chat/completions",
                             headers={"Authorization": f"Bearer {api_key}"},
                             json=body, timeout=self.timeout_secs)
        if resp.status_code >= 400:
            raise LlmError(f"LLM 请求失败 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"LLM 响应缺少 choices[].message.content: {str(data)[:200]}") from exc
        return {"text": text, "model": model,
                "usage": data.get("usage"), "dry_run": False}
