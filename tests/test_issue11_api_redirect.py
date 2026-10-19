"""pn#11：api_request 重定向不得把凭据头带到外部主机。

默认不跟随重定向；显式 ``allow_redirects=True`` 时同源透传、跨源剥离。
负控（还原为 requests 默认跟随）下，跨源目标会收到 ``X-Api-Key``/``Authorization``。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from fake_exec import FakeExecution
from plaita_nodes.api import ApiRequestNode


class _State:
    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.location: str | None = None
        self.body = {"ok": True}


def _serve(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            state.requests.append({
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": raw.decode() or None,
            })
            payload = json.dumps(state.body).encode()
            redirecting = state.location and self.path == "/start"
            self.send_response(state.status if redirecting else 200)
            if redirecting:
                self.send_header("Location", state.location)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = _handle

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def servers():
    a, b = _State(), _State()
    srv_a, base_a = _serve(a)
    srv_b, base_b = _serve(b)
    yield a, b, base_a, base_b
    srv_a.shutdown()
    srv_b.shutdown()


def _cred_env(monkeypatch, tmp_path, name: str, data: dict) -> str:
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    token = Fernet(key).encrypt(json.dumps(data).encode()).decode()
    cred_file = tmp_path / "creds.json"
    key_file = tmp_path / "creds.key"
    cred_file.write_text(json.dumps({name: {"type": "test", "data": token}}))
    key_file.write_text(key)
    monkeypatch.setenv("PLAITA_CREDENTIALS_FILE", str(cred_file))
    monkeypatch.setenv("PLAITA_CREDENTIALS_KEY_FILE", str(key_file))
    return name


def _node(monkeypatch, tmp_path, base: str, **fields) -> ApiRequestNode:
    cred = _cred_env(monkeypatch, tmp_path, "api", {
        "base_url": base,
        "headers": {"Authorization": "Bearer tok123", "X-Api-Key": "sekret"},
    })
    return ApiRequestNode(id="api", credential=cred, **fields)


def test_redirect_not_followed_by_default(monkeypatch, tmp_path, servers):
    a, b, base_a, base_b = servers
    a.status, a.location = 302, f"{base_b}/steal"

    out = _node(monkeypatch, tmp_path, base_a, path="start").execute(FakeExecution())

    assert out["status"] == 302
    assert b.requests == [], "默认配置下重定向目标不应收到任何请求"


def test_cross_origin_redirect_strips_credential_headers(monkeypatch, tmp_path, servers):
    a, b, base_a, base_b = servers
    a.status, a.location = 302, f"{base_b}/steal"

    out = _node(monkeypatch, tmp_path, base_a, path="start",
                allow_redirects=True).execute(FakeExecution())

    assert out["status"] == 200
    assert len(b.requests) == 1
    headers = b.requests[0]["headers"]
    assert "x-api-key" not in headers, "跨源重定向把 X-Api-Key 转发到了外部主机"
    assert "authorization" not in headers, "跨源重定向把 Authorization 转发到了外部主机"


def test_same_origin_redirect_keeps_credential_headers(monkeypatch, tmp_path, servers):
    a, _b, base_a, _base_b = servers
    a.status, a.location = 302, "/landing"

    out = _node(monkeypatch, tmp_path, base_a, path="start",
                allow_redirects=True).execute(FakeExecution())

    assert out["status"] == 200
    assert [r["path"] for r in a.requests] == ["/start", "/landing"]
    headers = a.requests[1]["headers"]
    assert headers["x-api-key"] == "sekret"
    assert headers["authorization"] == "Bearer tok123"
