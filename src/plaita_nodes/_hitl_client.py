"""hitl 协议共享层 —— HitlNode（阻塞版）与 HitlAwaitNode（挂起版）共用的实现。

协议（iLink 通道，对标 mediaflow scripts/hitl.js）：
- ``POST {base}/api/send``  body ``{message, wait_reply, timeout, upstream:'ilink'}``
  → ``{success, session_id, error}``
- ``GET {base}/api/poll/{session_id}`` → ``{has_reply, replies:[{content|text}], status}``

图片：先发 ``wait_reply=false`` 探路消息，失败在正文附加降级提示（hitl.js parity）。
两版节点只保留模式差异（进程内轮询到底 vs 发完即挂起等事件），协议细节只改这里。
"""
from __future__ import annotations

import os
from typing import Any, List, Optional

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:8081"
DEFAULT_TIMEOUT_SECS = 3600


class HitlError(RuntimeError):
    pass


def resolve_base_url(execution: Any, base_url: Optional[Any]) -> str:
    """base_url 字段（支持表达式）> env HITL_URL > 默认本机 hitl-server。"""
    if base_url:
        return str(execution.evaluate(base_url)).rstrip("/")
    return str(os.environ.get("HITL_URL", DEFAULT_BASE_URL)).rstrip("/")


def resolve_timeout_secs(execution: Any, timeout_secs: Optional[Any]) -> int:
    """timeout_secs 表达式求值转 int；非法值回退默认（3600）。"""
    try:
        return int(execution.evaluate(timeout_secs))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECS


def send_message(base: str, message: str, images: Optional[List[str]] = None,
                 timeout_secs: int = DEFAULT_TIMEOUT_SECS,
                 wait_reply: bool = True) -> str:
    """发消息（含图片探路降级），返回 session_id；send 失败抛 HitlError。"""
    if images:
        image_failed = False
        try:
            probe = requests.post(f"{base}/api/send", json={
                "message": "（图片见下）", "images": list(images),
                "wait_reply": False, "upstream": "ilink",
            }, timeout=15)
            if not probe.json().get("success"):
                image_failed = True
        except requests.RequestException:
            image_failed = True
        if image_failed:
            message += "\n\n⚠️ 封面图发送失败（媒体窗口过期）：请先给 bot 发任意一条消息后说\"重发图\"。"

    resp = requests.post(f"{base}/api/send", json={
        "message": message, "wait_reply": wait_reply,
        "timeout": timeout_secs, "upstream": "ilink",
    }, timeout=30)
    payload = resp.json()
    if not payload.get("success"):
        raise HitlError(f"hitl-server 发送失败: {payload.get('error') or payload}")
    return str(payload.get("session_id") or "")
