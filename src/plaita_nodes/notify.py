"""NotifyNode —— 通知外发（terminal 后端，对标 flowcast setHitlBackend('terminal')）。

flowcast 的 terminal 后端就是打到 stdout（``[notify] <message>``）；微信/企微
通道后续经 hitl-server 或 mcp2cli 扩展（新增 channel 字段即可，不破坏兼容）。
"""
from __future__ import annotations

from typing import Any, ClassVar, Optional

from plaita import Node


class NotifyNode(Node):
    """发一条通知。

    JSON 字段：
    - ``message``: 文本（支持表达式）
    - ``channel``: 当前仅 ``terminal``（默认），打印到 stdout

    输出：``{"ok": True, "channel"}``。
    """

    node_type: ClassVar[str] = "notify"
    node_name: ClassVar[str] = "通知"

    message: Optional[Any] = None
    channel: str = "terminal"

    def execute(self, execution: Any) -> dict:
        message = str(execution.evaluate(self.message) or "")
        if self.channel == "terminal":
            print(f"[notify] {message}")
        else:
            raise ValueError(f"未知通知 channel: {self.channel}（当前支持 terminal）")
        return {"ok": True, "channel": self.channel}
