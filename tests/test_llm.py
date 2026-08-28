"""LlmNode：单次补全原子——stub OpenAI 兼容端点 + provider 解析 + dry-run。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plaita_nodes.llm import LlmError, LlmNode


class FakeExecution:
    express_prefix = "$"
    context: dict = {}

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return self._globals.get(key, default)

    def __init__(self, global_vars=None):
        self._globals = global_vars or {}


class StubOpenAI:
    """OpenAI 兼容 /chat/completions。"""

    def __init__(self):
        self.last_body: dict = {}
        handler = self._build()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _build(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.last_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.dumps({
                    "choices": [{"message": {"content": "生成的文本"}}],
                    "usage": {"total_tokens": 42},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        return H


@pytest.fixture
def openai_stub(monkeypatch):
    stub = StubOpenAI()
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    yield stub
    server = stub.server
    server.shutdown()
    server.server_close()


def test_completion_with_explicit_endpoint(openai_stub):
    node = LlmNode(id="t", prompt="写一句诗", api_base=openai_stub.base,
                   api_key="sk-x", model="glm-5")
    out = node.execute(FakeExecution())
    assert out["text"] == "生成的文本"
    assert out["model"] == "glm-5"
    assert out["dry_run"] is False
    assert openai_stub.last_body["messages"][0]["content"] == "写一句诗"
    assert openai_stub.last_body["model"] == "glm-5"


def test_json_mode_and_max_tokens(openai_stub):
    node = LlmNode(id="t", prompt="p", api_base=openai_stub.base,
                   api_key="sk-t", model="m", json_mode=True, max_tokens=100)
    node.execute(FakeExecution())
    assert openai_stub.last_body["response_format"] == {"type": "json_object"}
    assert openai_stub.last_body["max_tokens"] == 100


def test_dry_run(openai_stub):
    node = LlmNode(id="t", prompt="p", api_base=openai_stub.base, dry_run=True)
    out = node.execute(FakeExecution())
    assert out["dry_run"] is True


def test_endpoint_resolution_error():
    node = LlmNode(id="t", prompt="p")
    with pytest.raises(LlmError, match="端点不完整"):
        node.execute(FakeExecution(global_vars={"dry_run": False})
                     if False else type("E", (), {
                         "evaluate": lambda self, v: v,
                         "get_global_variable": lambda self, k, d=None: False,
                     })())
