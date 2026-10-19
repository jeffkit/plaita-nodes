"""通用 API 连接器：generic_webhook 与 api_request。

api_request 是覆盖面最广的 REST 连接器：凭据提供 base_url 与静态鉴权头
（如 {"base_url": "https://api.ex.com", "headers": {"Authorization": "Bearer xxx"}}），
节点字段描述请求本身（method/path/query/body），配合表达式可对接绝大多数
「静态 Header 鉴权」的开放 API，无需为每个 SaaS 单写节点。
"""
from __future__ import annotations

import re
from typing import Any, ClassVar, Dict, Optional

import requests
from pydantic import field_validator

from plaita.credentials import get_credential
from plaita.node.basic import Node

from ._redact import _scrub_exception_text, _scrub_url_text
from ._retry import DeterministicRuntimeError, is_retryable_http_status
from ._timeouts import positive_millis


_TOKEN_RE = re.compile(r"\$(?:INPUT|NODE|GLOBAL)(?:\.\w+)+")


def _interpolate(execution, text_value: str) -> str:
    """对长文本中的 $INPUT.x / $NODE.x / $GLOBAL.x token 做插值。

    引擎 evaluate 只解析「整串表达式」；URL 路径等场景是混合文本，
    这里逐 token 求值后拼回。
    """
    if not isinstance(text_value, str) or "$" not in text_value:
        return text_value

    def _sub(m: "re.Match[str]") -> str:
        return str(execution.evaluate(m.group(0)))

    return _TOKEN_RE.sub(_sub, text_value)


class GenericWebhookNode(Node):
    """通用 Webhook：把任意 JSON payload POST 到凭据指定的 URL。

    凭据数据 {"url": "..."}；payload 字段为表达式求值后的 JSON 请求体。
    """

    node_type: ClassVar[str] = "generic_webhook"
    node_name: ClassVar[str] = "Webhook 调用"

    credential: str = ""
    payload: Any = None
    timeout_ms: int = 15000
    dry_run: bool = False

    @field_validator("timeout_ms", mode="before")
    @classmethod
    def _validate_timeout_ms(cls, value: Any) -> Any:
        # 0/负值原先拖到 requests 连接前才抛；构造期具名拒绝（A2R7-05）。
        positive_millis("generic_webhook timeout_ms", value)
        return value

    def execute(self, execution):
        # dry-run 最先判：不解析凭据、不发请求
        if self.dry_run or bool(execution.get_global_variable("dry_run", False)):
            return {"status": None, "response": "[dry-run] would post", "dry_run": True}
        if not self.credential:
            raise ValueError("缺少 credential 字段：请填凭据名（编排台「凭据」页创建）")
        url = get_credential(self.credential).get("url")
        if not url:
            raise ValueError(f"凭据 {self.credential!r} 缺少 url 字段")
        body = execution.evaluate(self.payload)
        try:
            resp = requests.post(url, json=body, timeout=self.timeout_ms / 1000)
        except requests.RequestException as exc:
            # 传输异常文本会带完整 URL（凭据 query 里可能有 token）；清洗后抛出。
            raise RuntimeError(_scrub_exception_text(exc)) from None
        if resp.status_code >= 400:
            detail = _scrub_url_text(
                f"webhook 调用失败 HTTP {resp.status_code}: {resp.text[:200]}")
            if is_retryable_http_status(resp.status_code):
                raise RuntimeError(detail)
            raise DeterministicRuntimeError(detail)
        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text[:200]
        return {"status": resp.status_code, "response": data}


class ApiRequestNode(Node):
    """REST API 请求：凭据提供 base_url + 静态鉴权头，节点描述请求本身。

    凭据数据::

        {"base_url": "https://api.example.com",
         "headers": {"Authorization": "Bearer xxx", "X-Api-Key": "..."}}

    path 支持 $INPUT/$NODE 表达式（如 "repos/$INPUT.org/issues"）；
    非 2xx 默认抛错（raise_on_error=false 时返回原始响应交由下游处理）。
    """

    node_type: ClassVar[str] = "api_request"
    node_name: ClassVar[str] = "API 请求"

    credential: str = ""
    method: str = "GET"
    path: str = ""
    query: Optional[Dict[str, Any]] = None
    body: Any = None
    timeout_ms: int = 15000
    raise_on_error: bool = True
    dry_run: bool = False

    @field_validator("timeout_ms", mode="before")
    @classmethod
    def _validate_timeout_ms(cls, value: Any) -> Any:
        # 0/负值原先拖到 requests 连接前才抛；构造期具名拒绝（A2R7-05）。
        positive_millis("api_request timeout_ms", value)
        return value

    def execute(self, execution):
        # dry-run 最先判：不解析凭据、不发请求
        if self.dry_run or bool(execution.get_global_variable("dry_run", False)):
            return {"status": None, "data": "[dry-run] would request", "dry_run": True}
        if not self.credential:
            raise ValueError("缺少 credential 字段：请填凭据名（编排台「凭据」页创建）")
        cred = get_credential(self.credential)
        base_url = (cred.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError(f"凭据 {self.credential!r} 缺少 base_url 字段")

        path = str(_interpolate(execution, self.path)).lstrip("/")
        url = f"{base_url}/{path}" if path else base_url
        method = self.method.upper()
        headers = dict(cred.get("headers") or {})
        query = execution.evaluate(self.query) if self.query else None
        body = execution.evaluate(self.body) if self.body is not None else None
        kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": self.timeout_ms / 1000,
        }
        if query is not None:
            kwargs["params"] = query
        if body is not None and method not in ("GET", "HEAD"):
            kwargs["json"] = body

        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            # 传输异常文本带完整 URL（base_url/query 里可能有凭据）；清洗后抛出。
            raise RuntimeError(_scrub_exception_text(exc)) from None
        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text[:2000]
        if resp.status_code >= 400 and self.raise_on_error:
            detail = _scrub_url_text(
                f"API 请求失败 HTTP {resp.status_code}: {str(data)[:200]}")
            # 401/403/404 等确定性 4xx 不被 retryTimes 重复发请求（A2R7-04）。
            if is_retryable_http_status(resp.status_code):
                raise RuntimeError(detail)
            raise DeterministicRuntimeError(detail)
        return {"status": resp.status_code, "data": data}
