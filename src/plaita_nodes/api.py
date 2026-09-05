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

from plaita.credentials import get_credential
from plaita.node.basic import Node


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

    def execute(self, execution):
        if not self.credential:
            raise ValueError("缺少 credential 字段：请填凭据名（编排台「凭据」页创建）")
        url = get_credential(self.credential).get("url")
        if not url:
            raise ValueError(f"凭据 {self.credential!r} 缺少 url 字段")
        body = execution.evaluate(self.payload)
        resp = requests.post(url, json=body, timeout=self.timeout_ms / 1000)
        if resp.status_code >= 400:
            raise RuntimeError(f"webhook 调用失败 HTTP {resp.status_code}: {resp.text[:200]}")
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

    def execute(self, execution):
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

        resp = requests.request(method, url, **kwargs)
        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text[:2000]
        if resp.status_code >= 400 and self.raise_on_error:
            raise RuntimeError(f"API 请求失败 HTTP {resp.status_code}: {str(data)[:200]}")
        return {"status": resp.status_code, "data": data}
