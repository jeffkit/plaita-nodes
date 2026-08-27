"""HitlNode：stub hitl-server 的回复/超时/发图降级/dry-run。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plaita_nodes.hitl import HitlError, HitlNode


class StubHitlServer:
    """最小 hitl-server：/api/send + /api/poll。

    behavior: "reply"（第 poll_after 次轮询后回）、"timeout"、"send_fail"
    """

    def __init__(self, behavior="reply", poll_after=2):
        self.behavior = behavior
        self.poll_after = poll_after
        self.poll_count = 0
        self.received: dict = {}
        handler = self._build_handler()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server = server
        self.url = f"http://127.0.0.1:{server.server_port}"
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def _build_handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.received = body
                if outer.behavior == "send_fail":
                    payload = {"success": False, "error": "prepare failed"}
                else:
                    payload = {"success": True, "session_id": "sess-1"}
                self._json(payload)

            def do_GET(self):
                outer.poll_count += 1
                if outer.behavior == "reply" and outer.poll_count >= outer.poll_after:
                    payload = {"has_reply": True,
                               "replies": [{"content": "同意"}], "status": "ok"}
                else:
                    payload = {"has_reply": False, "replies": [], "status": "waiting"}
                self._json(payload)

            def _json(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        return H

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hitl_base(request):
    behavior = getattr(request, "param", "reply")
    with StubHitlServer(behavior=behavior) as stub:
        yield stub


def test_replied(hitl_base):
    node = HitlNode(id="t", message="确认发布？", base_url=hitl_base.url,
                    timeout_secs=5, poll_interval=0.05)
    out = node.execute(type("E", (), {"evaluate": lambda self, v: v,
                                      "get_global_variable": lambda self, k, d=None: d})())
    assert out["status"] == "replied"
    assert out["replies"] == ["同意"]
    assert out["session_id"] == "sess-1"
    # 微信通道固定走 iLink 上游
    assert hitl_base.received["upstream"] == "ilink"
    assert hitl_base.received["wait_reply"] is True


@pytest.mark.parametrize("hitl_base", ["timeout"], indirect=True)
def test_timeout_returns_not_raises(hitl_base):
    node = HitlNode(id="t", message="确认？", base_url=hitl_base.url,
                    timeout_secs=1, poll_interval=0.1)
    out = node.execute(type("E", (), {"evaluate": lambda self, v: v,
                                      "get_global_variable": lambda self, k, d=None: d})())
    assert out["status"] == "timeout"


@pytest.mark.parametrize("hitl_base", ["send_fail"], indirect=True)
def test_send_failure_raises(hitl_base):
    node = HitlNode(id="t", message="确认？", base_url=hitl_base.url)
    with pytest.raises(HitlError, match="发送失败"):
        node.execute(type("E", (), {"evaluate": lambda self, v: v,
                                    "get_global_variable": lambda self, k, d=None: d})())


def test_images_probe_failure_degrades(monkeypatch):
    """图片探路失败 → 正文附加降级提示，主消息仍发出。"""
    calls = []

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "body": json})
        if "images" in (json or {}):
            return FakeResp({"success": False})
        return FakeResp({"success": True, "session_id": "s"})

    monkeypatch.setattr("plaita_nodes.hitl.requests.post", fake_post)
    monkeypatch.setattr("plaita_nodes.hitl.requests.get", lambda url, timeout=None: FakeResp(
        {"has_reply": True, "replies": [{"text": "同意"}], "status": "ok"}))

    node = HitlNode(id="t", message="看图确认", images=["/tmp/cover.png"], base_url="http://x")
    out = node.execute(type("E", (), {"evaluate": lambda self, v: v,
                                      "get_global_variable": lambda self, k, d=None: d})())
    assert out["status"] == "replied"
    assert "封面图发送失败" in calls[-1]["body"]["message"]


def test_dry_run_simulates_agree(fake_execution):
    node = HitlNode(id="t", message="确认？", dry_run=True)
    out = node.execute(fake_execution)
    assert out == {"status": "replied", "replies": ["同意"], "session_id": "dryrun", "dry_run": True}
