"""邮件发送连接器：经 SMTP 发送（stdlib smtplib，无额外依赖）。

凭据数据::

    {"host": "smtp.example.com", "port": 465,
     "username": "...", "password": "...", "use_ssl": true, "use_tls": false}
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any, ClassVar, List

from plaita.credentials import get_credential
from plaita.node.basic import Node


class EmailSendNode(Node):
    """邮件发送：凭据提供 SMTP 配置，节点描述收件人与内容。"""

    node_type: ClassVar[str] = "email_send"
    node_name: ClassVar[str] = "邮件发送"

    credential: str = ""
    to: Any = None            # 收件人：字符串、逗号分隔或列表（支持表达式）
    subject: str = ""
    body: Any = None
    body_is_html: bool = False

    def execute(self, execution):
        if not self.credential:
            raise ValueError("缺少 credential 字段：请填凭据名（编排台「凭据」页创建）")
        cred = get_credential(self.credential)
        host = cred.get("host")
        if not host:
            raise ValueError(f"凭据 {self.credential!r} 缺少 host 字段")
        port = int(cred.get("port", 465 if cred.get("use_ssl") else 587))

        recipients: List[str] = execution.evaluate(self.to) or []
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        body = str(execution.evaluate(self.body) or "")

        msg = EmailMessage()
        msg["From"] = cred.get("username") or "plaita"
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = str(execution.evaluate(self.subject) or "")
        msg.set_content(body)
        if self.body_is_html:
            msg.add_alternative(body, subtype="html")

        if cred.get("use_ssl"):
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
        try:
            if cred.get("use_tls") and not cred.get("use_ssl"):
                smtp.starttls()
            if cred.get("username") and cred.get("password"):
                smtp.login(cred["username"], cred["password"])
            smtp.send_message(msg)
        finally:
            smtp.quit()
        return {"status": "sent", "to": recipients}
