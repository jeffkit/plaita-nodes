"""A2R7-06 回归：IM webhook 的 HTTP 200 + 业务错误码必须判失败。

``_post_json`` 原先只判 HTTP 状态码；四个通道在 200 响应体里报错时全部被
当成成功，通知静默丢失且不触发 ``retryTimes``：

- Feishu ``{"code": 19003}``（token 无效）
- WeCom ``{"errcode": 93000}``（webhook url 无效）
- Slack ``{"ok": false, "error": "invalid_token"}``
- DingTalk ``{"errcode": 310000}``（签名不匹配）

契约：
- 非 0 业务码 → 具名 ``WebhookBusinessError``（确定性，不重试重复群消息）；
- 限流业务码（Feishu 9499 / WeCom 45009 / DingTalk 130101 / Slack
  ``rate_limited``）→ ``WebhookTransientBusinessError``，保留退避重试；
- 成功码（0 / ok=true / 缺字段的兼容响应）输出形状不变。

负控（回退 ``_check_business_code`` 调用）：业务错误码用例全部「未抛错」，
Flow 级用例 send 计数停在 1 且流程成功。
"""
from __future__ import annotations

import pytest

from plaita.core.executor import FlowExecution
from plaita.core.flow import Flow
from plaita.core.runner import _is_retryable_error

import plaita_nodes
import plaita_nodes.notify_backends as nb
from plaita_nodes.notify_backends import (
    WebhookBusinessError,
    WebhookTransientBusinessError,
)
from plaita_nodes.webhooks import (
    DingtalkWebhookNode,
    FeishuWebhookNode,
    SlackWebhookNode,
    WecomWebhookNode,
)

plaita_nodes.register_all()

_URL = "https://stub.invalid/hook"


class _Resp:
    def __init__(self, body):
        self.status_code = 200
        self._body = body
        self.text = body if isinstance(body, str) else str(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _Exec:
    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


def _patch_post(monkeypatch, body):
    monkeypatch.setattr(nb.requests, "post", lambda *a, **k: _Resp(body))
    monkeypatch.setattr(nb, "get_credential", lambda name: {"url": _URL})


@pytest.mark.parametrize("node_cls,body,needle", [
    (FeishuWebhookNode, {"code": 19003, "msg": "invalid webhook token"}, "19003"),
    (WecomWebhookNode, {"errcode": 93000, "errmsg": "invalid webhook url"}, "93000"),
    (SlackWebhookNode, {"ok": False, "error": "invalid_token"}, "invalid_token"),
    (DingtalkWebhookNode, {"errcode": 310000, "errmsg": "sign not match"}, "310000"),
])
def test_business_error_body_raises_named_deterministic_error(monkeypatch, node_cls, body, needle):
    _patch_post(monkeypatch, body)
    with pytest.raises(WebhookBusinessError) as caught:
        node_cls(id="n", credential="c", text="hello").execute(_Exec())
    text = str(caught.value)
    assert needle in text
    assert node_cls.node_type in text
    assert _is_retryable_error(caught.value) is False


@pytest.mark.parametrize("node_cls,body", [
    (FeishuWebhookNode, {"code": 0, "msg": "success"}),
    (WecomWebhookNode, {"errcode": 0, "errmsg": "ok"}),
    (SlackWebhookNode, {"ok": True}),
    (DingtalkWebhookNode, {"errcode": 0, "errmsg": "ok"}),
    # 字段缺失的兼容响应（既有测试桩/网关只回 {"ok": true}）
    (DingtalkWebhookNode, {"ok": True}),
])
def test_business_success_body_shape_unchanged(monkeypatch, node_cls, body):
    _patch_post(monkeypatch, body)
    out = node_cls(id="n", credential="c", text="hello").execute(_Exec())
    assert out == {"status": 200, "response": body}


def test_slack_plain_text_ok_unchanged(monkeypatch):
    """Slack Incoming Webhook 成功返回纯文本 ``ok``（非 JSON）。"""
    _patch_post(monkeypatch, "ok")
    out = SlackWebhookNode(id="n", credential="c", text="hello").execute(_Exec())
    assert out == {"status": 200, "response": "ok"}


def test_business_error_message_scrubs_echoed_url(monkeypatch):
    """业务错误响应体若回显 webhook URL，异常文案同样打码（A2R8-01 纪律）。"""
    body = {"code": 19003, "msg": f"invalid token {_URL}?token=SECRET123"}
    _patch_post(monkeypatch, body)
    with pytest.raises(WebhookBusinessError) as caught:
        FeishuWebhookNode(id="n", credential="c", text="hello").execute(_Exec())
    assert "SECRET123" not in str(caught.value)


def test_rate_limited_business_code_stays_retryable(monkeypatch):
    _patch_post(monkeypatch, {"code": 9499, "msg": "too many request"})
    with pytest.raises(WebhookTransientBusinessError) as caught:
        FeishuWebhookNode(id="n", credential="c", text="hello").execute(_Exec())
    assert _is_retryable_error(caught.value) is True


def _flow(node_type: str, retry_times: int = 2) -> Flow:
    return Flow.model_validate({
        "nodes": [
            {"type": "start", "id": "start", "next": "n"},
            {"type": node_type, "id": "n", "credential": "c", "text": "hello",
             "next": "end",
             "errorHandler": {"strategy": "abort", "retryTimes": retry_times}},
            {"type": "end", "id": "end", "resultType": "success",
             "output": "$NODE.n"},
        ]
    })


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _noop(_delay):
        return None

    monkeypatch.setattr("plaita.core.runner._backoff_sleep", _noop)


def test_deterministic_business_error_sent_once_despite_retries(monkeypatch):
    """凭据/签名错误：retry_times=2 仍只发送 1 次，流程具名失败。"""
    calls = {"n": 0}

    def _post(*args, **kwargs):
        calls["n"] += 1
        return _Resp({"code": 19003, "msg": "invalid webhook token"})

    monkeypatch.setattr(nb.requests, "post", _post)
    monkeypatch.setattr(nb, "get_credential", lambda name: {"url": _URL})
    with pytest.raises(Exception) as caught:  # NodeExecutionError
        FlowExecution().run_compatible(_flow("feishu_webhook"), False)
    assert calls["n"] == 1
    assert isinstance(caught.value.__cause__, WebhookBusinessError)


def test_rate_limited_business_code_retried_to_limit(monkeypatch):
    """限流业务码：1 次首发 + 2 次重试 = 3 次请求（负控下停在 1 次）。"""
    calls = {"n": 0}

    def _post(*args, **kwargs):
        calls["n"] += 1
        return _Resp({"code": 9499, "msg": "too many request"})

    monkeypatch.setattr(nb.requests, "post", _post)
    monkeypatch.setattr(nb, "get_credential", lambda name: {"url": _URL})
    with pytest.raises(Exception) as caught:
        FlowExecution().run_compatible(_flow("feishu_webhook"), False)
    assert calls["n"] == 3
    assert isinstance(caught.value.__cause__, WebhookTransientBusinessError)
