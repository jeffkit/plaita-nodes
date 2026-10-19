"""A2R11-08（nodes 面）：sessions 原子写的 SIGKILL 残留清扫 + 0600 权限。

``_write_sessions_atomic`` 的临时文件在 ``os.replace`` 前被 chmod 成目标
权限（常见 0644）；进程若在 chmod 后、replace 前被 SIGKILL，会留下带完整
内容且同权限的 ``.sessions.json.*.tmp``。旧实现只在 Python 异常路径 unlink，
启动/会话结束均不清扫。修复：临时文件保持 0600 直到替换，并在写回与
session 结束时清扫过龄残留。
"""
from __future__ import annotations

import json
import os
import stat
import time

from plaita_nodes import hitl_poller
from plaita_nodes.hitl_poller import _write_sessions_atomic, poll_round


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _RecordingBus:
    def __init__(self):
        self.publishes: list[tuple[str, dict]] = []

    async def publish(self, event_type, **kwargs):
        self.publishes.append((event_type, kwargs))
        return f"evt-{len(self.publishes)}"


def _age(path, seconds: float = 3600.0) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def test_atomic_write_keeps_target_private(tmp_path):
    """目标已存在且为 0644 时，原子替换后也必须收紧到 0600。"""
    target = tmp_path / "sessions.json"
    target.write_text("{}", encoding="utf-8")
    os.chmod(target, 0o644)

    _write_sessions_atomic(target, {"exec": {"session_id": "s"}})

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_sweeps_stale_tmp_siblings(tmp_path):
    """写回前清扫同目录下本文件的过龄临时文件（SIGKILL 残留）。"""
    target = tmp_path / "sessions.json"
    target.write_text("{}", encoding="utf-8")
    stale = tmp_path / f".{target.name}.dead.tmp"
    stale.write_text('{"exec": {"session_id": "secret"}}', encoding="utf-8")
    os.chmod(stale, 0o644)
    _age(stale)

    _write_sessions_atomic(target, {"exec": {"session_id": "s"}})

    assert not stale.exists(), "SIGKILL 残留的 sessions 临时文件应被清扫"


def test_sweep_keeps_recent_tmp(tmp_path):
    """年龄门槛：并发写者刚创建的临时文件不能被误删。"""
    target = tmp_path / "sessions.json"
    target.write_text("{}", encoding="utf-8")
    live = tmp_path / f".{target.name}.live.tmp"
    live.write_text("{}", encoding="utf-8")

    _write_sessions_atomic(target, {"exec": {"session_id": "s"}})

    assert live.exists(), "仍在写入窗口内的临时文件不得被清扫"


def test_terminal_session_sweeps_stale_tmp(tmp_path, monkeypatch):
    """session 结束路径也清扫残留（即使本轮没有触发写回）。"""
    sessions = tmp_path / "sessions.json"
    sessions.write_text(json.dumps({"exec": {"session_id": "s"}}), encoding="utf-8")
    stale = tmp_path / f".{sessions.name}.crashed.tmp"
    stale.write_text("{}", encoding="utf-8")
    os.chmod(stale, 0o644)
    _age(stale)

    def fake_get(url, timeout=None):
        # 会话已在盘上被清掉：本轮没有可删除键，_drop_published_sessions
        # 不会写回文件，只有 session 结束清扫能清掉残留。
        sessions.unlink()
        return _Resp({"has_reply": True, "replies": [{"content": "ok"}]})

    monkeypatch.setattr(hitl_poller.requests, "get", fake_get)
    bus = _RecordingBus()

    events = poll_round(sessions, "http://hitl", bus)

    assert events[0]["data"]["status"] == "replied"
    assert len(bus.publishes) == 1
    assert not stale.exists(), "session 结束时应清扫残留临时文件"
