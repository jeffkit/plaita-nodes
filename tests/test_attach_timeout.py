"""attach 有界超时（2026-10-11 impl 节点挂死 33 分钟的修复）。

契约：
- 沙箱失联导致 ``Sandbox.connect`` 挂死 → 超时后抛 ``AgsError``，不无限等待；
- ``connect`` 抛任意异常 → 归一为 ``AgsError``（节点只需处理一种失败）；
- 正常连接 → 原样返回，不引入额外开销。
"""
from __future__ import annotations

import time

import pytest

import plaita_nodes.sandbox_ags as sa


class _HangingSandbox:
    @staticmethod
    def connect(instance_id):
        time.sleep(30)  # 模拟 SDK 无超时挂死
        return object()


class _BrokenSandbox:
    @staticmethod
    def connect(instance_id):
        raise RuntimeError("Paused sandbox not found")


class _OkSandbox:
    @staticmethod
    def connect(instance_id):
        return ("attached", instance_id)


def _client_with(cls, monkeypatch):
    client = sa.AgsClient(domain="example.invalid", api_key="x")
    monkeypatch.setattr(client, "_sandbox_cls", lambda: cls)
    return client


def test_hanging_connect_times_out_as_ags_error(monkeypatch):
    monkeypatch.setenv("PLAITA_SANDBOX_ATTACH_TIMEOUT", "0.2")
    client = _client_with(_HangingSandbox, monkeypatch)
    t0 = time.monotonic()
    with pytest.raises(sa.AgsError) as ei:
        client.attach("sbx-hang")
    elapsed = time.monotonic() - t0
    assert elapsed < 5, f"超时应快速返回，实际 {elapsed:.1f}s"
    assert "超时" in str(ei.value)


def test_connect_exception_is_normalized_to_ags_error(monkeypatch):
    monkeypatch.setenv("PLAITA_SANDBOX_ATTACH_TIMEOUT", "5")
    client = _client_with(_BrokenSandbox, monkeypatch)
    with pytest.raises(sa.AgsError) as ei:
        client.attach("sbx-gone")
    assert "Paused sandbox not found" in str(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_healthy_connect_returns_handle(monkeypatch):
    monkeypatch.setenv("PLAITA_SANDBOX_ATTACH_TIMEOUT", "5")
    client = _client_with(_OkSandbox, monkeypatch)
    assert client.attach("sbx-ok") == ("attached", "sbx-ok")


def test_bad_timeout_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PLAITA_SANDBOX_ATTACH_TIMEOUT", "not-a-number")
    assert sa._attach_timeout_secs() == sa.AGS_DEFAULT_ATTACH_TIMEOUT
    monkeypatch.setenv("PLAITA_SANDBOX_ATTACH_TIMEOUT", "-3")
    assert sa._attach_timeout_secs() == sa.AGS_DEFAULT_ATTACH_TIMEOUT
