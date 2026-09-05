"""IM Webhook 连接器：飞书 / 企业微信 / Slack 群机器人。

共同模式：凭据按名引用（credential 字段 → plaita.credentials 解密读取
{"url": ...}），消息内容支持表达式（经 execution.evaluate 求值）。
新增一个连接器 ≈ 30 行：子类化 _WebhookNode，声明 payload 组装即可。
"""
import logging
from typing import Any, ClassVar, Dict, Optional

import requests

from plaita.credentials import get_credential
from plaita.node.basic import Node

_logger = logging.getLogger(__name__)


class _WebhookNode(Node):
    """Webhook 连接器基类：credential 解析 + POST 消息 + 统一错误。"""

    credential: str = ""
    text: Any = None

    def _endpoint(self) -> str:
        if not self.credential:
            raise ValueError("缺少 credential 字段：请填凭据名（编排台「凭据」页创建）")
        cred = get_credential(self.credential)
        url = cred.get("url")
        if not url:
            raise ValueError(f"凭据 {self.credential!r} 缺少 url 字段")
        return url

    def _payload(self, text: str) -> Dict[str, Any]:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def execute(self, execution):
        text = str(execution.evaluate(self.text) or "")
        url = self._endpoint()
        resp = requests.post(url, json=self._payload(text), timeout=15)
        ok = resp.status_code == 200
        body = None
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:200]
        if not ok:
            raise RuntimeError(f"webhook 调用失败 HTTP {resp.status_code}: {body}")
        return {"status": resp.status_code, "response": body}


class FeishuWebhookNode(_WebhookNode):
    """飞书群机器人：凭据数据为 {"url": "https://open.feishu.cn/open-apis/bot/v2/hook/..."}"""

    node_type: ClassVar[str] = "feishu_webhook"
    node_name: ClassVar[str] = "飞书通知"

    def _payload(self, text: str) -> Dict[str, Any]:
        return {"msg_type": "text", "content": {"text": text}}


class WecomWebhookNode(_WebhookNode):
    """企业微信群机器人：凭据数据为 {"url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."}"""

    node_type: ClassVar[str] = "wecom_webhook"
    node_name: ClassVar[str] = "企微通知"

    def _payload(self, text: str) -> Dict[str, Any]:
        return {"msgtype": "text", "text": {"content": text}}


class SlackWebhookNode(_WebhookNode):
    """Slack Incoming Webhook：凭据数据为 {"url": "https://hooks.slack.com/services/..."}"""

    node_type: ClassVar[str] = "slack_webhook"
    node_name: ClassVar[str] = "Slack 通知"

    def _payload(self, text: str) -> Dict[str, Any]:
        return {"text": text}
