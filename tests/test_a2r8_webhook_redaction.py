"""A2R8-01 回归：IM webhook 传输异常不得带出 URL 里的机器人 token。

缺陷：``_post_json`` 直接放行 ``requests`` 异常——``ConnectionError`` /
``Timeout`` 的文本带完整 URL path/query（Feishu/Slack token 在 path，
DingTalk ``access_token`` 在 query），经 ``NodeExecutionError`` 进
``state.error`` / worker 日志。

契约：
- 传输异常统一包成清洗后的 ``RuntimeError``（token/path/query 打码，host 保留）；
- HTTP 4xx（408/425/429 除外）是确定性错误（不重试），5xx 保持可重试；
- 非 200 的自建错误文案同样过 ``_scrub_url_text``。

负控（回退生产改动）：``test_connection_error_text_scrubs_bot_token`` 会拿到
原始 ``ConnectionError`` 且文本含 token；负控记录见
``docs/proofs/38-a2r8-webhook-token-redaction.md``。
"""
from __future__ import annotations

import socket

import pytest
import requests

import plaita_nodes.notify_backends as nb
from plaita.core.runner import _is_retryable_error

TOKEN = "FEISHU_HOOK_SECRET_9f3a"


class _Exec:
    def get_global_variable(self, key, default=None):
        return default

    def evaluate(self, value):
        return value


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _cred_url(monkeypatch, url: str) -> None:
    monkeypatch.setattr(nb, "get_credential", lambda name: {"url": url})


def _feishu_exec(url: str, monkeypatch) -> _Exec:
    _cred_url(monkeypatch, url)
    return _Exec()


def _node():
    return type("N", (), {"credential": "bot"})()


def test_connection_error_text_scrubs_bot_token(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError(
            "HTTPConnectionPool(host='open.feishu.cn', port=443): Max retries "
            f"exceeded with url: /open-apis/bot/v2/hook/{TOKEN} "
            "(Caused by NewConnectionError(...))")

    monkeypatch.setattr(nb.requests, "post", boom)
    _cred_url(monkeypatch, f"https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}")
    with pytest.raises(RuntimeError) as caught:
        nb.get_notify_backend("feishu_webhook")(
            _Exec(), _node(), "hello")
    text = str(caught.value)
    assert TOKEN not in text
    # 相对 URL 的 path/query 已逐段打码（requests 的 "with url:" 形态）
    assert "Max retries exceeded with url: /***/***/***/***/***" in text
    # 诊断价值保留：endpoint host 仍可定位
    assert "open.feishu.cn" in text


def test_real_connection_refused_scrubs_token(monkeypatch):
    """真实 requests 路径（关闭端口）：异常文本不含 token。"""
    url = f"http://127.0.0.1:{_closed_port()}/open-apis/bot/v2/hook/{TOKEN}"
    with pytest.raises(RuntimeError) as caught:
        nb.get_notify_backend("feishu_webhook")(
            _feishu_exec(url, monkeypatch), _node(), "hello")
    text = str(caught.value)
    assert TOKEN not in text
    assert "127.0.0.1" in text


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else str(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def test_http_4xx_is_deterministic_and_does_not_retry(monkeypatch):
    monkeypatch.setattr(nb.requests, "post",
                        lambda *a, **k: _Resp(401, {"code": 19003, "msg": "bad token"}))
    _cred_url(monkeypatch, f"https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}")
    with pytest.raises(ValueError) as caught:  # DeterministicRuntimeError
        nb.get_notify_backend("feishu_webhook")(
            _Exec(), _node(), "hello")
    assert "HTTP 401" in str(caught.value)
    assert _is_retryable_error(caught.value) is False


def test_http_5xx_stays_retryable(monkeypatch):
    monkeypatch.setattr(nb.requests, "post",
                        lambda *a, **k: _Resp(503, "upstream unavailable"))
    _cred_url(monkeypatch, f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={TOKEN}")
    with pytest.raises(RuntimeError) as caught:
        nb.get_notify_backend("wecom_webhook")(
            _Exec(), _node(), "hello")
    assert _is_retryable_error(caught.value) is True


def test_http_error_message_scrubs_echoed_url(monkeypatch):
    """响应体若回显 URL/凭据，自建错误文案也必须打码。"""
    echoed = f"invalid hook https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}"
    monkeypatch.setattr(nb.requests, "post", lambda *a, **k: _Resp(400, echoed))
    _cred_url(monkeypatch, f"https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}")
    with pytest.raises(RuntimeError) as caught:
        nb.get_notify_backend("feishu_webhook")(_Exec(), _node(), "hello")
    assert TOKEN not in str(caught.value)
