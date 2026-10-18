"""日志安全渲染（plaita#151 纪律的本仓同类实现）回归。

节点日志/异常消息里不许出现 URL 的 path/query 凭据或裸 ``password=…`` 运行；
``_scrub_*`` 必须 **total**（任何输入不抛，失败退化为安全常量）。
"""
from __future__ import annotations

from plaita_nodes._redact import (
    PLACEHOLDER,
    UNPRINTABLE,
    _scrub_exception_text,
    _scrub_url_text,
)


def test_http_url_keeps_endpoint_only():
    scrubbed = _scrub_url_text(
        "connect failed: http://user:pw@example.com/a/b?access_token=SECRET"
    )
    assert "example.com" in scrubbed
    assert "SECRET" not in scrubbed
    assert "user:pw" not in scrubbed
    assert "?" + PLACEHOLDER in scrubbed


def test_non_http_scheme_degrades_to_constant():
    # DSN 的 userinfo 是口令形态，非 http(s) scheme 无从判断端点边界
    assert _scrub_url_text("postgresql://u:p@db:5432/x") == UNPRINTABLE


def test_bare_credential_run_is_masked_but_diagnostics_survive():
    text = _scrub_url_text("password=hunter2 code=1002 port=8080")
    assert "hunter2" not in text
    assert "code=1002" in text and "port=8080" in text


def test_scrub_url_text_is_total():
    assert isinstance(_scrub_url_text(None), str)
    assert isinstance(_scrub_url_text(12345), str)
    # 孤立代理字符写不进 utf-8 日志 ⇒ 安全常量，绝不原样返回
    assert _scrub_url_text("note \ud800") == UNPRINTABLE
    assert _scrub_url_text("http://exa\ud800mple.com/x") == UNPRINTABLE


def test_scrub_exception_text_scrubs_message_and_traceback():
    try:
        raise RuntimeError("GET http://user:pw@example.com/a?token=SECRET failed")
    except RuntimeError as exc:
        text = _scrub_exception_text(exc)
    assert "Traceback" in text  # 栈帧诊断保留
    assert "SECRET" not in text and "user:pw" not in text
    assert "example.com" in text
