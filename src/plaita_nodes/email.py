"""邮件发送连接器（薄壳节点）：SMTP 协议适配在 notify_backends 注册表。

凭据数据::

    {"host": "smtp.example.com", "port": 465,
     "username": "...", "password": "...", "use_ssl": true, "use_tls": false}
"""
from __future__ import annotations

from typing import Any, ClassVar

from plaita import Node

from .notify_backends import get_notify_backend


class EmailSendNode(Node):
    """邮件发送：凭据提供 SMTP 配置，节点描述收件人与内容。"""

    node_type: ClassVar[str] = "email_send"
    node_name: ClassVar[str] = "邮件发送"

    credential: str = ""
    to: Any = None            # 收件人：字符串、逗号分隔或列表（支持表达式）
    subject: str = ""
    body: Any = None
    body_is_html: bool = False
    dry_run: bool = False

    def execute(self, execution):
        # dry-run 最先判：不解析凭据、不连 SMTP（收件人仍求值，dry 输出可检查）
        recipients = execution.evaluate(self.to) or []
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        if self.dry_run or bool(execution.get_global_variable("dry_run", False)):
            return {"status": "dry-run", "to": recipients, "dry_run": True}
        body = str(execution.evaluate(self.body) or "")
        return get_notify_backend(self.node_type)(execution, self, body)
