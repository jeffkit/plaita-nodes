"""DecisionNode —— 结构化决策原子：封闭决策空间 → 类型化选择 + 置信度。

理念对标 "System One" 决策模型（如 TypeSafe 的 Jev）：流程里的路由/分类/
打分是单步、决策空间封闭的判断，输出应是类型化决策而非生成文本——
choice 必须落在决策空间内（可校验、不幻觉出第三选项），confidence 用于
门控（低于阈值可升级 HITL 或走默认项），引擎可替换（provider 注册表）。

与 llm 的边界（原子性原则的延续）：
- ``llm``：开放文本生成（摘要/改写/抽取），输出是自然语言；
- ``decision``：封闭决策空间判断（路由/分类/评分），输出是结构化决策。

provider 注册表 ``DECISION_PROVIDERS``（仿 agentproc EXECUTORS，开放注册）：
- ``"llm"``：OpenAI 兼容 /chat/completions（json_mode + 约束提示词，
  temperature 固定 0）。端点解析与 LlmNode 同链：字段显式值 > provider
  bundle > LLM_API_BASE/LLM_API_KEY/LLM_MODEL 环境变量；
- ``"jev"``：Jev 线协议（``POST {base}/v1/systemone``）。官方 TypeSafe Jev
  与开源替代 OpenJev 同说这套协议（state + choice 型 question 的 criteria
  决策空间 → answers 里取 {choice, probabilities, confidence}）；端点由
  ``api_base`` 字段或 ``JEV_API_BASE`` 环境变量给出（官方 early-access API
  或自托管 OpenJev 均可）；
- 自定义：``register_decision_provider(name, fn)``，``fn(**request)`` 返回
  ``{"choice", "confidence", "model", "raw"}``（测试与确定性流程用）；
- ``"jevlike"``：本地打分器（开源替代，jevlike 仓，MIT）。``model`` 字段 =
  checkpoint 路径；torch/jevlike 懒加载（缺包时给出安装提示，不影响其他
  provider），checkpoint 按路径缓存避免每步重载。

输出：``{"choice", "confidence", "provider", "model", "low_confidence",
"raw", "dry_run"}``。``low_confidence`` 如实记录是否低于 ``min_confidence``
（即使随后被 default_choice 替换也保持 True，供下游分支与审计）。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional

import requests
from pydantic import Field

from plaita import Node

try:
    from .llm import resolve_llm_endpoint
except ImportError:  # 平铺布局兜底
    from plaita_nodes.llm import resolve_llm_endpoint  # type: ignore[no-redef]


class DecisionError(RuntimeError):
    pass


def _normalize_choices(raw: Any) -> list[dict]:
    """决策空间归一 → [{"value", "description"}]。接受字符串列表、
    {value, description} 列表、{值: 说明} 映射三种写法。"""
    if raw is None:
        raise DecisionError("decision 节点缺少 choices（决策空间不能为空）")
    items: list[dict] = []
    if isinstance(raw, dict):
        for key, desc in raw.items():
            items.append({"value": str(key),
                          "description": None if desc is None else str(desc)})
    elif isinstance(raw, list):
        for el in raw:
            if isinstance(el, str):
                items.append({"value": el, "description": None})
            elif isinstance(el, dict):
                value = el.get("value")
                if value is None:
                    raise DecisionError(f"choices 元素缺少 value: {el!r}")
                items.append({"value": str(value),
                              "description": None if el.get("description") is None else str(el["description"])})
            else:
                raise DecisionError(f"choices 元素类型不支持: {el!r}")
    else:
        raise DecisionError("choices 需为字符串列表、{value,description} 列表或 {值: 说明} 映射")
    if not items:
        raise DecisionError("决策空间为空（choices 不能是空列表）")
    return items


def _render_payload(payload: Any) -> str:
    if payload is None:
        return "(空)"
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except TypeError:
        return str(payload)


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)  # 兼容 ```json 围栏与前后缀文字
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise DecisionError(f"LLM 未返回 JSON 决策对象: {text[:200]}")


# ---- 内置 provider：签名 (question, choices, input, api_base, api_key, model, timeout_secs) ----

def _provider_llm(*, question: Any, choices: list[dict], input: Any,
                  api_base: Optional[str], api_key: Optional[str],
                  model: Optional[str], timeout_secs: int) -> dict:
    base, key, resolved_model = resolve_llm_endpoint(api_base, api_key, model)
    lines = [f"- {c['value']}" + (f"：{c['description']}" if c["description"] else "")
             for c in choices]
    messages = [
        {"role": "system",
         "content": ('你是结构化决策引擎。只输出一个 JSON 对象，格式 '
                     '{"choice": "<决策，必须是候选之一>", "confidence": <0到1的小数>}，'
                     "不要输出任何其他文字。")},
        {"role": "user",
         "content": (f"## 判定问题\n{question or '（未提供，按内容直接判定）'}\n\n"
                     f"## 候选决策\n" + "\n".join(lines) + "\n\n"
                     f"## 待判定内容\n{_render_payload(input)}")},
    ]
    resp = requests.post(f"{base}/chat/completions",
                         headers={"Authorization": f"Bearer {key}"},
                         json={"model": resolved_model, "messages": messages,
                               "temperature": 0.0,
                               "response_format": {"type": "json_object"}},
                         timeout=timeout_secs)
    if resp.status_code >= 400:
        raise DecisionError(f"LLM 决策请求失败 {resp.status_code}: {resp.text[:300]}")
    try:
        text = resp.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise DecisionError(f"LLM 响应缺少 choices[].message.content: {resp.text[:200]}") from exc
    space = {c["value"] for c in choices}
    data = _extract_json(text)
    choice = str(data.get("choice", "")).strip()
    if choice not in space:
        raise DecisionError(f"LLM 返回的 choice {choice!r} 不在决策空间内: {sorted(space)}")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise DecisionError(f"confidence 非数值: {data.get('confidence')!r}") from exc
    return {"choice": choice, "confidence": max(0.0, min(1.0, confidence)),
            "model": resolved_model, "raw": text[:2000]}


def _provider_jev(*, question: Any, choices: list[dict], input: Any,
                  api_base: Optional[str], api_key: Optional[str],
                  model: Optional[str], timeout_secs: int) -> dict:
    """Jev 线协议：POST {base}/v1/systemone。

    state = 待判定内容；单个 choice 型 question，criteria 即决策空间。
    官方 TypeSafe Jev（early access）与自托管 OpenJev 同说这套协议。
    """
    base = str(api_base or os.environ.get("JEV_API_BASE", "")).rstrip("/")
    if not base:
        raise DecisionError("jev provider 需要 api_base 字段或 JEV_API_BASE 环境变量")
    key = str(api_key or os.environ.get("JEV_API_KEY", ""))
    body: dict = {
        "state": _render_payload(input),
        "questions": {"decision": {
            "type": "choice",
            "instructions": str(question or "Choose the best option."),
            "criteria": {c["value"]: (c["description"] or c["value"]) for c in choices},
        }},
    }
    if model:
        body["model"] = str(model)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = requests.post(f"{base}/v1/systemone", headers=headers, json=body,
                         timeout=timeout_secs)
    if resp.status_code >= 400:
        raise DecisionError(f"systemone 请求失败 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise DecisionError(f"systemone 响应缺少 answers 对象: {str(data)[:200]}")
    answer = data["answers"].get("decision")
    if not isinstance(answer, dict) or "choice" not in answer:
        raise DecisionError(f"systemone answers.decision 缺少 choice: {str(data)[:200]}")
    try:
        confidence = float(answer.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise DecisionError(f"confidence 非数值: {answer.get('confidence')!r}") from exc
    return {"choice": str(answer["choice"]), "confidence": confidence,
            "model": data.get("model"),
            "raw": json.dumps(data, ensure_ascii=False)[:2000]}


DecisionProvider = Callable[..., dict]

# jevlike checkpoint 缓存：{resolved_path: (model, collator, device)}
_JEVLIKE_CACHE: dict[str, tuple] = {}


def _provider_jevlike(*, question: Any, choices: list[dict], input: Any,
                      api_base: Optional[str], api_key: Optional[str],
                      model: Optional[str], timeout_secs: int) -> dict:
    """本地 jevlike 打分器（https://github.com/vinnylarouge/jevlike，MIT）。

    ``model`` 字段 = checkpoint 路径（jevlike 仓训练产物，如
    ``jevlike-train … --output runs/x.pt``）。单次前向传播给每个选项打分，
    无文本生成。torch/jevlike 懒加载：未安装时抛带安装提示的 DecisionError，
    不影响 llm/jev provider。
    """
    if not model:
        raise DecisionError("jevlike provider 需要 model 字段指向 checkpoint 路径")
    try:
        import torch
        from jevlike.data import ChoiceExample
        from jevlike.model import load_checkpoint, select_device
        from jevlike.train import move
    except ImportError as exc:
        raise DecisionError(
            "jevlike provider 需要 jevlike + torch"
            "（git clone https://github.com/vinnylarouge/jevlike && pip install -e jevlike）"
        ) from exc

    checkpoint = Path(str(model)).expanduser().resolve()
    if not checkpoint.is_file():
        raise DecisionError(f"jevlike checkpoint 不存在: {checkpoint}")
    cache_key = f"{checkpoint}:{select_device('auto')}"
    cached = _JEVLIKE_CACHE.get(cache_key)
    if cached is None:
        device = select_device("auto")
        net, collator, _ = load_checkpoint(checkpoint, device)  # 第三项为 checkpoint 元数据，弃用
        cached = (net, collator, device)
        _JEVLIKE_CACHE[cache_key] = cached
    net, collator, device = cached

    state = _render_payload(input)
    if question:
        state = f"{question}\n{state}"
    batch = move(collator([ChoiceExample(state, tuple(c["value"] for c in choices), 0)]),
                 device)
    net.eval()
    with torch.no_grad():
        probs = net(batch).softmax(-1)[0, :len(choices)].cpu().tolist()
    best = max(range(len(choices)), key=lambda i: probs[i])
    return {"choice": choices[best]["value"], "confidence": float(probs[best]),
            "model": checkpoint.name,
            "raw": json.dumps({c["value"]: round(p, 4) for c, p in zip(choices, probs)},
                              ensure_ascii=False)}


DECISION_PROVIDERS: dict[str, DecisionProvider] = {
    "llm": _provider_llm,
    "jev": _provider_jev,
    "jevlike": _provider_jevlike,
}


def register_decision_provider(name: str, fn: DecisionProvider) -> None:
    """注册自定义 decision provider（覆盖同名内置项）。"""
    DECISION_PROVIDERS[name] = fn


class DecisionNode(Node):
    """封闭决策空间内的单步判定，返回类型化选择 + 置信度。

    JSON 字段：
    - ``question``: 判定问题（如"这条短信是否垃圾短信"）
    - ``choices``: 决策空间——字符串列表 / [{value, description}] / {值: 说明} 映射
    - ``input``: 待判定内容（文本或对象）
    - ``provider``: ``llm``（默认）/ ``jev`` / 自定义注册名
    - ``api_base`` / ``api_key`` / ``model``: 端点覆盖（llm/jev 共用；
      llm 端点另有 provider bundle 与 LLM_* 环境变量回退）
    - ``min_confidence``: 置信阈值；低于时 ``low_confidence=True`` 并按
      ``on_low_confidence`` 处理：``passthrough``（默认，仅标记）/
      ``default``（取 ``default_choice``）/ ``error``（抛错，交由容错策略）
    - ``dry_run``: 为 true（或 globalContext.dry_run）时不请求，返回首选项

    输出：``{"choice", "confidence", "provider", "model", "low_confidence",
    "raw", "dry_run"}``。
    """

    node_type: ClassVar[str] = "decision"
    node_name: ClassVar[str] = "结构化决策"

    question: Optional[Any] = None
    choices: Optional[Any] = None
    input: Optional[Any] = None
    provider: Optional[Any] = None
    api_base: Optional[Any] = None
    api_key: Optional[Any] = None
    model: Optional[Any] = None
    timeout_secs: int = Field(default=30)
    min_confidence: Optional[float] = None
    on_low_confidence: Literal["passthrough", "default", "error"] = "passthrough"
    default_choice: Optional[Any] = None
    dry_run: bool = False

    def validate(self) -> None:
        """构建期拦截：字面量决策空间/端点配置问题尽早失败。"""
        if isinstance(self.choices, (list, dict)):
            try:
                _normalize_choices(self.choices)
            except DecisionError as exc:
                raise ValueError(f"decision 节点配置非法: {exc}") from exc
        provider = self.provider
        if isinstance(provider, str) and provider not in DECISION_PROVIDERS:
            raise ValueError(f"decision 节点 provider {provider!r} 未注册"
                             f"（已注册：{sorted(DECISION_PROVIDERS)}）")
        if self.on_low_confidence == "default" and self.default_choice is None:
            raise ValueError("on_low_confidence=default 需要同时配置 default_choice")

    def execute(self, execution: Any) -> dict:
        choices = _normalize_choices(execution.evaluate(self.choices))
        provider_name = str(execution.evaluate(self.provider) or "llm")
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            return {"choice": choices[0]["value"], "confidence": 1.0,
                    "provider": provider_name, "model": "",
                    "low_confidence": False, "raw": "[dry-run]", "dry_run": True}

        fn = DECISION_PROVIDERS.get(provider_name)
        if fn is None:
            raise DecisionError(f"decision provider {provider_name!r} 未注册"
                                f"（已注册：{sorted(DECISION_PROVIDERS)}）")
        ev = lambda v: execution.evaluate(v) if v is not None else None  # noqa: E731
        out = fn(question=ev(self.question),
                 choices=choices,
                 input=ev(self.input),
                 api_base=ev(self.api_base),
                 api_key=ev(self.api_key),
                 model=ev(self.model),
                 timeout_secs=self.timeout_secs)

        choice = str(out.get("choice", ""))
        if choice not in {c["value"] for c in choices}:
            raise DecisionError(f"provider 返回的 choice {choice!r} 不在决策空间内")
        try:
            confidence = float(out.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise DecisionError(f"provider 返回的 confidence 非数值: {out.get('confidence')!r}") from exc
        low = self.min_confidence is not None and confidence < float(self.min_confidence)
        if low and self.on_low_confidence == "error":
            raise DecisionError(
                f"置信度 {confidence:.2f} 低于阈值 {self.min_confidence}（choice={choice!r}）")
        if low and self.on_low_confidence == "default":
            default = execution.evaluate(self.default_choice)
            if default is None:
                raise DecisionError("on_low_confidence=default 需要配置 default_choice")
            choice = str(default)
        return {"choice": choice, "confidence": confidence,
                "provider": provider_name, "model": str(out.get("model") or ""),
                "low_confidence": low, "raw": str(out.get("raw") or "")[:2000],
                "dry_run": False}
