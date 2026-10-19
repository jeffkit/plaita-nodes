"""通知 backend 注册表 —— 通知出口的协议适配层（先例：decision.DECISION_PROVIDERS）。

设计（plaita-nodes#1）：新通知渠道**只加 backend，不加节点 type**。

backend 契约::

    backend(execution, node, text: str) -> dict   # text 已由调用方 evaluate

- ``node``：调用方节点实例。backend 按需读取 ``node.credential`` 与渠道特有
  字段（如 email 的 to/subject/body_is_html，经 getattr 缺省安全读取）；
- 返回值即节点输出 dict；发送失败抛 RuntimeError 由节点透传。

内置 backend：``terminal`` / ``feishu_webhook`` / ``wecom_webhook`` /
``slack_webhook`` / ``dingtalk_webhook`` / ``email_send``。webhook×4 与
email_send 节点保留既有 type（DSL 兼容），内部为薄壳委托，输出形状不变。
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any, Callable, Dict, List

import requests

from plaita.credentials import get_credential

from ._redact import _scrub_exception_text, _scrub_url_text
from ._retry import DeterministicRuntimeError, is_retryable_http_status

NotifyBackend = Callable[[Any, Any, str], dict]

NOTIFY_BACKENDS: Dict[str, NotifyBackend] = {}


class WebhookBusinessError(DeterministicRuntimeError):
    """HTTP 200 但响应体业务码表示失败（A2R7-06）。

    凭据错误 / 签名错误 / 参数错误等：同一请求重跑必然以同样方式失败，
    不随 ``retryTimes`` 重放（避免重复群消息）。
    """


class WebhookTransientBusinessError(RuntimeError):
    """HTTP 200 + 限流类业务码：瞬态失败，保留 ``retryTimes`` 退避重试。"""


#: 各 IM 通道的「业务成功码」字段。字段缺省视为兼容（测试桩/网关只回
#: ``{"ok": true}``）；字段存在且非 0 即业务失败。
_BUSINESS_CODE_FIELDS = {
    "feishu_webhook": "code",
    "wecom_webhook": "errcode",
    "dingtalk_webhook": "errcode",
}

#: 限流类业务码（瞬态）：其余非 0 业务码一律确定性失败。
#: Feishu 9499 / 11232、WeCom 45009 / 45033、DingTalk 130101 均为官方
#: 「频率超限」族；Slack 的 ``rate_limited`` 在 body 的 error 字段。
_RETRYABLE_BUSINESS_CODES = {
    "feishu_webhook": frozenset({9499, 11232}),
    "wecom_webhook": frozenset({45009, 45033}),
    "dingtalk_webhook": frozenset({130101}),
}


def register_notify_backend(name: str, backend: NotifyBackend) -> None:
    """注册通知 backend（覆盖同名内置项；先例 register_decision_provider）。"""
    NOTIFY_BACKENDS[name] = backend


def get_notify_backend(name: str) -> NotifyBackend:
    try:
        return NOTIFY_BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"未知通知 channel: {name}"
            f"（可用：{', '.join(sorted(NOTIFY_BACKENDS))}；"
            f"自定义经 register_notify_backend 注册）"
        ) from None


# ── 内部共享 ──────────────────────────────────────────────────────

def _credential_data(node: Any) -> Dict[str, Any]:
    """按凭据名解密读取（编排台「凭据」页创建）。"""
    if not getattr(node, "credential", ""):
        raise ValueError("缺少 credential 字段：请填凭据名（编排台「凭据」页创建）")
    return get_credential(node.credential)


def _credential_url(node: Any) -> str:
    url = _credential_data(node).get("url")
    if not url:
        raise ValueError(f"凭据 {node.credential!r} 缺少 url 字段")
    return url


def _business_error(channel: str, code: Any, message: Any, *, retryable: bool) -> None:
    """把 200 + 业务错误码转成具名失败（A2R7-06）。

    Feishu ``code=19003`` / WeCom ``errcode=93000`` / DingTalk
    ``errcode=310000`` / Slack ``ok=false`` 都是 HTTP 200——只判状态码会让
    通知被静默丢弃。限流码保持可重试，其余为确定性失败（不重放群消息）。
    响应体可能回显 URL/凭据，异常文案同样过 ``_scrub_url_text``。
    """
    detail = _scrub_url_text(
        f"{channel} webhook 业务错误 code={code}: {str(message)[:200]}")
    if retryable:
        raise WebhookTransientBusinessError(detail)
    raise WebhookBusinessError(detail)


def _check_business_code(channel: str, body: Any) -> None:
    """按通道解析 200 响应体的业务码；缺字段视为兼容（视为成功）。"""
    if not isinstance(body, dict):
        return
    if channel == "slack_webhook":
        if body.get("ok") is False:
            error = str(body.get("error") or "ok=false")
            _business_error(channel, error,
                            body.get("error") or body,
                            retryable=error in ("rate_limited", "ratelimited"))
        return
    field = _BUSINESS_CODE_FIELDS.get(channel)
    if field is None or field not in body:
        return
    raw = body[field]
    try:
        code = int(raw)
    except (TypeError, ValueError):
        code = raw
    if code == 0:
        return
    _business_error(
        channel, raw, body.get("msg") or body.get("errmsg") or body,
        retryable=code in _RETRYABLE_BUSINESS_CODES.get(channel, ()))


def _post_json(url: str, payload: Dict[str, Any], *, channel: str) -> dict:
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as exc:
        # requests 的 ConnectionError/Timeout 文本带完整 URL path/query：
        # Feishu/Slack 的机器人 token 在 path，DingTalk 的 access_token 在
        # query。清洗成 ``scheme://host`` 后再抛出，绝不让 token 进
        # NodeExecutionError → state.error / worker 日志（A2R8-01）。
        raise RuntimeError(_scrub_exception_text(exc)) from None
    ok = resp.status_code == 200
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:200]
    if not ok:
        detail = _scrub_url_text(
            f"webhook 调用失败 HTTP {resp.status_code}: {body}")
        # 4xx（408/425/429 除外）是请求本身的确定性错误——不被 retryTimes
        # 放大成重复群消息；5xx/限流保持可重试（A2R7-04）。
        if is_retryable_http_status(resp.status_code):
            raise RuntimeError(detail)
        raise DeterministicRuntimeError(detail)
    _check_business_code(channel, body)
    return {"status": resp.status_code, "response": body}


# ── 内置 backend ─────────────────────────────────────────────────

def _backend_terminal(execution: Any, node: Any, text: str) -> dict:
    print(f"[notify] {text}")
    return {"ok": True, "channel": "terminal"}


def _backend_feishu_webhook(execution: Any, node: Any, text: str) -> dict:
    return _post_json(_credential_url(node), {"msg_type": "text", "content": {"text": text}},
                      channel="feishu_webhook")


def _backend_wecom_webhook(execution: Any, node: Any, text: str) -> dict:
    return _post_json(_credential_url(node), {"msgtype": "text", "text": {"content": text}},
                      channel="wecom_webhook")


def _backend_slack_webhook(execution: Any, node: Any, text: str) -> dict:
    return _post_json(_credential_url(node), {"text": text}, channel="slack_webhook")


def _backend_dingtalk_webhook(execution: Any, node: Any, text: str) -> dict:
    cred = _credential_data(node)
    url = cred.get("url")
    if not url:
        raise ValueError(f"凭据 {node.credential!r} 缺少 url 字段")
    secret = cred.get("secret")
    if secret:
        import base64
        import hashlib
        import hmac
        import time as _time
        import urllib.parse

        ts = str(round(_time.time() * 1000))
        digest = hmac.new(
            secret.encode(), f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"
    return _post_json(url, {"msgtype": "text", "text": {"content": text}},
                      channel="dingtalk_webhook")


def _backend_email_send(execution: Any, node: Any, text: str) -> dict:
    """SMTP 发送。text = 已 evaluate 的正文；to/subject/body_is_html 读节点字段。"""
    cred = _credential_data(node)
    host = cred.get("host")
    if not host:
        raise ValueError(f"凭据 {node.credential!r} 缺少 host 字段")
    port = int(cred.get("port", 465 if cred.get("use_ssl") else 587))

    recipients: List[str] = execution.evaluate(getattr(node, "to", None)) or []
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",") if r.strip()]
    subject = str(execution.evaluate(getattr(node, "subject", None)) or "")
    body_is_html = bool(getattr(node, "body_is_html", False))

    msg = EmailMessage()
    msg["From"] = cred.get("username") or "plaita"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(text)
    if body_is_html:
        msg.add_alternative(text, subtype="html")

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


register_notify_backend("terminal", _backend_terminal)
register_notify_backend("feishu_webhook", _backend_feishu_webhook)
register_notify_backend("wecom_webhook", _backend_wecom_webhook)
register_notify_backend("slack_webhook", _backend_slack_webhook)
register_notify_backend("dingtalk_webhook", _backend_dingtalk_webhook)
register_notify_backend("email_send", _backend_email_send)
