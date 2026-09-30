"""HitlAwaitNode —— HITL 确认的**挂起版**（Distributed 模式专用）。

与阻塞版 HitlNode 的分工：
- ``HitlNode``（Normal 模式）：execute 内轮询到底，进程内阻塞等待；
- ``HitlAwaitNode``（Distributed 模式）：execute 只发消息拿 session_id 即返回
  pending，内核随即挂起并快照 context——**等待微信回复期间进程可以崩溃/重启**，
  由外部 poller 轮询 hitl-server，把回复作为事件（event_type=hitl_reply）发布到
  EventBus 唤醒恢复。崩溃级恢复闭环见 ADR-2026-08-27 phase 2。

配套 poller：``python -m plaita_nodes.hitl_poller --hitl-url ... --sessions <file>
--redis-url ...``（轮询各 session，回复到达即向 Redis EventBus 发布事件）。
"""
from __future__ import annotations

from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node

from ._hitl_client import resolve_base_url, resolve_timeout_secs, send_message
from .hitl import HitlError  # noqa: F401  (再导出，保持既有导入路径)

EVENT_TYPE = "hitl_reply"


class HitlAwaitNode(Node):
    """发确认消息并**挂起等待**外部事件恢复（仅 Distributed 模式）。

    JSON 字段与 HitlNode 一致（message/images/base_url/timeout_secs），另含：
    - ``poll_after_send``: 语义占位——挂起版自身不轮询，由外部 poller 负责。

    execute：发消息（wait_reply=true）→ ``{status:"pending", session_id, ...}``
    resume：事件数据 ``{"status":"replied","replies":[...]}`` → 结构化输出；
            cancel/timeout → 对应状态。
    输出：``{"status": "replied"|"timeout"|"cancel", "replies", "session_id"}``。
    """

    node_type: ClassVar[str] = "hitl_await"
    node_name: ClassVar[str] = "人工确认(挂起)"
    is_suspending: ClassVar[bool] = True

    message: Optional[Any] = None
    images: Optional[Any] = None
    base_url: Optional[Any] = None
    timeout_secs: Optional[Any] = Field(default=3600)
    # 内核挂起时据此向 EventBus 注册订阅（poller 发布同型事件唤醒）
    event_type: str = EVENT_TYPE
    event_filter: dict = Field(default_factory=dict)
    dry_run: bool = False

    # ── 发送协议走 _hitl_client 共享层（与 HitlNode 一致，含图片探路降级）──

    def execute(self, execution: Any) -> dict:
        message = str(execution.evaluate(self.message) or "")
        images = execution.evaluate(self.images) if self.images is not None else []
        base = resolve_base_url(execution, self.base_url)
        timeout_secs = resolve_timeout_secs(execution, self.timeout_secs)
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))

        if dry:
            return {"status": "pending", "session_id": "dryrun",
                    "replies": [], "dry_run": True}
        session_id = send_message(base, message, [str(i) for i in images], timeout_secs)
        # 供外部 poller 定位：session → execution 映射由调用方持久化
        execution.set_state(f"{execution.express_prefix}HITL_SESSION", session_id)
        return {"status": "pending", "session_id": session_id,
                "replies": [], "timeout_secs": timeout_secs}

    def resume(self, execution: Any, resume_type, resume_data=None) -> dict:
        from plaita.core.errors import ResumeType

        data = resume_data if isinstance(resume_data, dict) else {}
        if resume_type is ResumeType.CANCEL:
            return {"status": "cancel", "replies": [],
                    "session_id": str(data.get("session_id") or "")}
        if resume_type is ResumeType.TIMEOUT:
            return {"status": "timeout", "replies": [],
                    "session_id": str(data.get("session_id") or "")}
        replies = [str(r) for r in (data.get("replies") or [])]
        return {"status": str(data.get("status") or "replied"),
                "replies": replies,
                "session_id": str(data.get("session_id") or "")}
