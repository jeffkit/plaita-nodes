"""notify backend 注册表：内置项守卫 / terminal 行为 / channel 分发 / 未知 channel 报错。"""
from __future__ import annotations

from fake_exec import FakeExecution

from plaita_nodes.notify import NotifyNode
from plaita_nodes.notify_backends import (
    NOTIFY_BACKENDS,
    get_notify_backend,
    register_notify_backend,
)


def test_builtin_backends_registered():
    """薄壳节点（webhook×4/email_send）委托的 backend 必须全部在册。"""
    for name in ("terminal", "feishu_webhook", "wecom_webhook", "slack_webhook",
                 "dingtalk_webhook", "email_send"):
        assert name in NOTIFY_BACKENDS, f"{name} backend 未注册"


def test_terminal_prints_and_dry_run_silent(capsys):
    ex = FakeExecution()
    out = NotifyNode(id="n", message="构建完成").execute(ex)
    assert out == {"ok": True, "channel": "terminal"}
    assert "[notify] 构建完成" in capsys.readouterr().out

    out = NotifyNode(id="n", message="机密", dry_run=True).execute(ex)
    assert out == {"ok": True, "channel": "terminal", "dry_run": True}
    assert capsys.readouterr().out == ""  # dry 不外发


def test_channel_dispatch_to_registered_backend():
    calls = []

    def _fake(execution, node, text):
        calls.append((node.channel, text, node.credential))
        return {"ok": True, "channel": node.channel, "via": "fake"}

    register_notify_backend("team_messenger", _fake)
    try:
        node = NotifyNode(id="n", message="发布通知", channel="team_messenger",
                          credential="team-bot")
        out = node.execute(FakeExecution())
    finally:
        NOTIFY_BACKENDS.pop("team_messenger", None)  # 注册表是全局态，测后清理

    assert out == {"ok": True, "channel": "team_messenger", "via": "fake"}
    assert calls == [("team_messenger", "发布通知", "team-bot")]


def test_unknown_channel_error_lists_registered():
    try:
        NotifyNode(id="n", message="x", channel="carrier_pigeon").execute(FakeExecution())
        raise AssertionError("未知 channel 应抛 ValueError")
    except ValueError as e:
        assert "carrier_pigeon" in str(e) and "terminal" in str(e)


def test_get_notify_backend_is_registry_lookup():
    assert get_notify_backend("terminal") is NOTIFY_BACKENDS["terminal"]
