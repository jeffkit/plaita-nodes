"""NotifyNode —— 通知外发（backend 注册表分发，对标 flowcast setHitlBackend('terminal')）。

channel 可为内置 ``terminal`` 或任何已注册通知 backend（feishu_webhook 等，
见 notify_backends；新渠道经 register_notify_backend 注册，**不加节点**，
plaita-nodes#1）。
"""
from __future__ import annotations

from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node

from .notify_backends import get_notify_backend


class NotifyNode(Node):
    """发一条通知。

    JSON 字段：
    - ``message``: 文本（支持表达式）
    - ``channel``: ``terminal``（默认，打印到 stdout）或已注册通知 backend
      （如 ``feishu_webhook``；届时需配 ``credential``）
    - ``credential``: 凭据名（channel 需要时）
    - ``dry_run``: 为 true（或 globalContext.dry_run）时不外发，返回 fake 结果

    输出：terminal → ``{"ok": True, "channel"}``；其余 channel 见对应 backend。
    """

    node_type: ClassVar[str] = "notify"
    node_name: ClassVar[str] = "通知"

    message: Optional[Any] = None
    channel: str = Field(default="terminal", description="terminal | 已注册通知 backend")
    credential: str = ""
    dry_run: bool = False

    def execute(self, execution: Any) -> dict:
        # dry-run 最先判：不解析凭据、不外发
        if self.dry_run or bool(execution.get_global_variable("dry_run", False)):
            return {"ok": True, "channel": self.channel, "dry_run": True}
        text = str(execution.evaluate(self.message) or "")
        return get_notify_backend(self.channel)(execution, self, text)
