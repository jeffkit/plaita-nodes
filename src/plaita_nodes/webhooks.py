"""IM Webhook 连接器：飞书 / 企业微信 / Slack / 钉钉群机器人（薄壳节点）。

协议适配在 notify_backends 注册表（plaita-nodes#1：新通知渠道只加 backend、
不加节点）；本模块只保留节点外壳（type/字段/dry-run），按节点 type 委托 backend。
"""
from __future__ import annotations

from typing import Any, ClassVar

from plaita import Node

from .notify_backends import get_notify_backend


class _WebhookNode(Node):
    """Webhook 连接器基类：dry-run 判定 + text 求值 + 委托本节点 type 的 backend。"""

    credential: str = ""
    text: Any = None
    dry_run: bool = False

    def execute(self, execution):
        # dry-run 最先判：不解析凭据、不发请求（dry_run 本就用于无配置检查流程形状）
        if self.dry_run or bool(execution.get_global_variable("dry_run", False)):
            return {"status": None, "response": "[dry-run] would post", "dry_run": True}
        text = str(execution.evaluate(self.text) or "")
        return get_notify_backend(self.node_type)(execution, self, text)


class FeishuWebhookNode(_WebhookNode):
    """飞书群机器人：凭据数据为 {"url": "https://open.feishu.cn/open-apis/bot/v2/hook/..."}"""

    node_type: ClassVar[str] = "feishu_webhook"
    node_name: ClassVar[str] = "飞书通知"


class WecomWebhookNode(_WebhookNode):
    """企业微信群机器人：凭据数据为 {"url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."}"""

    node_type: ClassVar[str] = "wecom_webhook"
    node_name: ClassVar[str] = "企微通知"


class SlackWebhookNode(_WebhookNode):
    """Slack Incoming Webhook：凭据数据为 {"url": "https://hooks.slack.com/services/..."}"""

    node_type: ClassVar[str] = "slack_webhook"
    node_name: ClassVar[str] = "Slack 通知"


class DingtalkWebhookNode(_WebhookNode):
    """钉钉群机器人：凭据数据为 {"url": "...", "secret": "<加签密钥，可选>"}。
    配置了 secret 时自动加签（timestamp+HMAC-SHA256）。协议适配见 notify_backends。
    """

    node_type: ClassVar[str] = "dingtalk_webhook"
    node_name: ClassVar[str] = "钉钉通知"
