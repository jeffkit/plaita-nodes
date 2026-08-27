"""HitlNode —— 直连 hitl-server 的微信人工确认（对标 mediaflow scripts/hitl.js）。

协议（iLink 通道）：
- ``POST {base}/api/send``  body ``{message, wait_reply, timeout, upstream:'ilink'}``
  → ``{success, session_id, error}``
- 轮询 ``GET {base}/api/poll/{session_id}`` → ``{has_reply, replies:[{content|text}], status}``
- 到 deadline 返回 ``status='timeout'`` 而非抛错（与 hitl.js 一致）；
  send 失败（success=false）才抛 HitlError。

图片：先发 ``wait_reply=false`` 的探路消息，失败在正文附加降级提示（hitl.js parity）。

断点续跑说明：本节点是阻塞版（Normal 模式直接轮询到底）。崩溃级恢复
（挂起快照 + 事件恢复）由 plaita Distributed 模式的 EventNode 模式承接，
见 ADR-2026-08-27 试点方案的 phase 2。
"""
from __future__ import annotations

import os
import time
from typing import Any, ClassVar, List, Optional

import requests
from pydantic import Field

from plaita import Node


class HitlError(RuntimeError):
    pass


class HitlNode(Node):
    """发消息给人工并等待回复。

    JSON 字段：
    - ``message``: 文本（支持表达式）
    - ``images``: 可选图片路径列表（表达式）
    - ``base_url``: hitl-server 地址（默认 env HITL_URL，再默认 127.0.0.1:8081）
    - ``timeout_secs``: 等待回复的 deadline（默认 3600）
    - ``poll_interval``: 轮询间隔秒（默认 5）
    - ``dry_run``: 为 true（或 globalContext.dry_run）时模拟一个「同意」回复

    输出：``{"status": "replied"|"timeout", "replies": [str], "session_id", "dry_run"}``。
    """

    node_type: ClassVar[str] = "hitl"
    node_name: ClassVar[str] = "人工确认"

    message: Optional[Any] = None
    images: Optional[Any] = None
    base_url: Optional[Any] = None
    timeout_secs: Optional[Any] = Field(default=3600)
    poll_interval: Optional[Any] = Field(default=5.0)
    wait_reply: bool = Field(default=True)
    dry_run: bool = False

    def execute(self, execution: Any) -> dict:
        message = str(execution.evaluate(self.message) or "")
        images = execution.evaluate(self.images) if self.images is not None else []
        base = (str(execution.evaluate(self.base_url)) if self.base_url
                else os.environ.get("HITL_URL", "http://127.0.0.1:8081")).rstrip("/")
        try:
            timeout_secs = int(execution.evaluate(self.timeout_secs))
        except (TypeError, ValueError):
            timeout_secs = 3600
        try:
            poll_interval = float(execution.evaluate(self.poll_interval))
        except (TypeError, ValueError):
            poll_interval = 5.0
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))

        if dry:
            if not self.wait_reply:
                return {"status": "sent", "replies": [], "session_id": "dryrun", "dry_run": True}
            return {"status": "replied", "replies": ["同意"],
                    "session_id": "dryrun", "dry_run": True}

        image_failed = False
        if images:
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
            "message": message, "wait_reply": self.wait_reply,
            "timeout": timeout_secs, "upstream": "ilink",
        }, timeout=30)
        payload = resp.json()
        if not payload.get("success"):
            raise HitlError(f"hitl-server 发送失败: {payload.get('error') or payload}")
        session_id = str(payload.get("session_id") or "")

        if not self.wait_reply:
            # 只发不等（如周报推送），对标 hitl.js waitReply:false
            return {"status": "sent", "replies": [], "session_id": session_id,
                    "dry_run": False}

        deadline = time.monotonic() + timeout_secs
        status, replies = "timeout", []
        while time.monotonic() < deadline:
            poll = requests.get(f"{base}/api/poll/{session_id}", timeout=15).json()
            if poll.get("has_reply"):
                status = "replied"
                replies = [
                    str(r.get("content") or r.get("text") or "")
                    for r in (poll.get("replies") or [])
                ]
                break
            poll_status = poll.get("status")
            if poll_status == "error":
                raise HitlError(f"hitl-server 轮询错误: {poll}")
            if poll_status == "timeout":
                status = "timeout"
                break
            time.sleep(poll_interval)

        return {"status": status, "replies": replies, "session_id": session_id,
                "dry_run": False}
