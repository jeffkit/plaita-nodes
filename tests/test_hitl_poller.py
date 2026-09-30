"""hitl_poller 冒烟测试：poll_once 事件产出 + main 默认参数求值路径回归。

（历史 bug：main() 的 --hitl-url 默认值用了 os.environ 但模块未 import os，
不传 --hitl-url 直接 NameError——test_main_default_hitl_url_uses_env 看护。）
"""
from __future__ import annotations

import requests

from plaita_nodes.hitl_poller import main, poll_once


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_poll_once_replied_timeout_and_skip(monkeypatch):
    responses = {
        "sess-a": {"has_reply": True, "replies": [{"content": "同意"}]},
        "sess-b": {"has_reply": False, "status": "timeout"},
        "sess-c": {"has_reply": False, "status": "waiting"},   # 未决 → 无事件
        "sess-d": {"has_reply": True, "replies": [{"text": "ok-text"}]},  # text 兜底
    }

    def fake_get(url, timeout=None):
        return _Resp(responses[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr("plaita_nodes.hitl_poller.requests.get", fake_get)

    events = poll_once({
        "exec-a": {"session_id": "sess-a"},
        "exec-b": {"session_id": "sess-b"},
        "exec-c": {"session_id": "sess-c"},
        "exec-d": {"session_id": "sess-d"},
        "exec-e": {},  # 缺 session_id → 跳过
    }, "http://hitl")

    by_exec = {e["execution_id"]: e["data"] for e in events}
    assert by_exec["exec-a"] == {"status": "replied", "replies": ["同意"],
                                 "session_id": "sess-a"}
    assert by_exec["exec-b"] == {"status": "timeout", "replies": [],
                                 "session_id": "sess-b"}
    assert by_exec["exec-d"]["replies"] == ["ok-text"]
    assert "exec-c" not in by_exec and "exec-e" not in by_exec


def test_poll_once_survives_network_error(monkeypatch):
    """单 session 轮询失败不炸整轮（continue 语义）。"""

    def fake_get(url, timeout=None):
        raise requests.RequestException("boom")

    monkeypatch.setattr("plaita_nodes.hitl_poller.requests.get", fake_get)
    assert poll_once({"exec-a": {"session_id": "s"}}, "http://hitl") == []


def test_main_default_hitl_url_uses_env(monkeypatch, tmp_path):
    """main() 默认 --hitl-url 走 os.environ 求值（回归：缺 os import 的 NameError）。"""
    monkeypatch.delenv("HITL_URL", raising=False)
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text("{}", encoding="utf-8")  # 空 → 打印退出，不进轮询循环
    assert main(["--sessions", str(sessions_file)]) == 0
