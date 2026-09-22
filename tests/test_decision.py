"""DecisionNode：决策空间归一 + llm/jev provider + 置信度门控 + dry-run + 构建期校验。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plaita_nodes.decision import (
    DECISION_PROVIDERS,
    DecisionError,
    DecisionNode,
    register_decision_provider,
)
from plaita_nodes.llm import LlmNode


class FakeExecution:
    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return self._globals.get(key, default)

    def __init__(self, global_vars=None):
        self._globals = global_vars or {}


class StubOpenAIDecision:
    """OpenAI 兼容端点，content 固定返回一段 JSON 决策文本。"""

    def __init__(self, content: str):
        self.content = content
        self.last_body: dict = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.last_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = json.dumps({
                    "choices": [{"message": {"content": outer.content}}],
                    "usage": {"total_tokens": 7},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class StubJev:
    """Jev/OpenJev 线协议端点：POST /v1/systemone。"""

    def __init__(self, choice: str = "route_a", confidence: float = 0.9):
        self.choice = choice
        self.confidence = confidence
        self.last_body: dict = {}
        self.auth: str = ""
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.auth = self.headers.get("Authorization", "")
                outer.last_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                answers = {name: {"choice": outer.choice,
                                  "probabilities": {outer.choice: outer.confidence},
                                  "confidence": outer.confidence}
                           for name in outer.last_body.get("questions", {})}
                data = json.dumps({
                    "model": "openjev-0.1",
                    "answers": answers,
                    "usage": {"input_tokens": 11},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def openai_stub():
    stub = StubOpenAIDecision(json.dumps({"choice": "spam", "confidence": 0.97}))
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


@pytest.fixture
def jev_stub(monkeypatch):
    stub = StubJev()
    monkeypatch.setenv("JEV_API_BASE", stub.base)
    monkeypatch.setenv("JEV_API_KEY", "jev-secret")
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


def _node(**kw) -> DecisionNode:
    kw.setdefault("id", "t")
    kw.setdefault("api_base", kw.pop("_base"))
    return DecisionNode(**kw)


def test_llm_provider_full_chain(openai_stub):
    node = DecisionNode(id="t", question="判断短信类别", choices=["spam", "ham"],
                        input="恭喜您中奖", provider="llm",
                        api_base=openai_stub.base, api_key="sk-x", model="glm-5")
    out = node.execute(FakeExecution())
    assert out == {"choice": "spam", "confidence": 0.97, "provider": "llm",
                   "model": "glm-5", "low_confidence": False,
                   "raw": json.dumps({"choice": "spam", "confidence": 0.97}, ensure_ascii=False),
                   "dry_run": False}
    body = openai_stub.last_body
    assert body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_object"}
    user_msg = body["messages"][1]["content"]
    assert "判断短信类别" in user_msg
    assert "- spam" in user_msg and "- ham" in user_msg
    assert "恭喜您中奖" in user_msg


def test_llm_provider_fence_wrapped_response(openai_stub, monkeypatch):
    openai_stub.content = '```json\n{"choice": "ham", "confidence": 0.6}\n```'
    node = DecisionNode(id="t", choices={"spam": "垃圾短信", "ham": "正常"},
                        input="你好", api_base=openai_stub.base,
                        api_key="sk-x", model="m")
    out = node.execute(FakeExecution())
    assert out["choice"] == "ham"
    assert "ham：正常" in openai_stub.last_body["messages"][1]["content"]


def test_out_of_space_choice_raises(openai_stub):
    openai_stub.content = '{"choice": "other", "confidence": 0.99}'
    node = DecisionNode(id="t", choices=["spam", "ham"], input="x",
                        api_base=openai_stub.base, api_key="k", model="m")
    with pytest.raises(DecisionError, match="不在决策空间"):
        node.execute(FakeExecution())


def test_min_confidence_default_substitution(openai_stub):
    openai_stub.content = '{"choice": "spam", "confidence": 0.5}'
    node = DecisionNode(id="t", choices=["spam", "ham"], input="x",
                        min_confidence=0.9, on_low_confidence="default",
                        default_choice="ham",
                        api_base=openai_stub.base, api_key="k", model="m")
    out = node.execute(FakeExecution())
    assert out["choice"] == "ham"
    assert out["low_confidence"] is True


def test_min_confidence_error_mode(openai_stub):
    openai_stub.content = '{"choice": "spam", "confidence": 0.5}'
    node = DecisionNode(id="t", choices=["spam", "ham"], input="x",
                        min_confidence=0.9, on_low_confidence="error",
                        api_base=openai_stub.base, api_key="k", model="m")
    with pytest.raises(DecisionError, match="低于阈值"):
        node.execute(FakeExecution())


def test_min_confidence_passthrough_marks_low(openai_stub):
    openai_stub.content = '{"choice": "spam", "confidence": 0.5}'
    node = DecisionNode(id="t", choices=["spam", "ham"], input="x",
                        min_confidence=0.9,
                        api_base=openai_stub.base, api_key="k", model="m")
    out = node.execute(FakeExecution())
    assert out["choice"] == "spam"
    assert out["low_confidence"] is True


def test_jev_provider(jev_stub):
    node = DecisionNode(id="t", question="路由到哪个队列", provider="jev",
                        choices=[{"value": "route_a", "description": "普通队列"},
                                 {"value": "route_b"}],
                        input={"text": "工单内容"}, model="jev-latest")
    out = node.execute(FakeExecution())
    assert out["choice"] == "route_a"
    assert out["confidence"] == 0.9
    assert out["model"] == "openjev-0.1"
    assert out["provider"] == "jev"
    assert jev_stub.auth == "Bearer jev-secret"
    q = jev_stub.last_body["questions"]["decision"]
    assert q["type"] == "choice"
    assert q["instructions"] == "路由到哪个队列"
    assert q["criteria"] == {"route_a": "普通队列", "route_b": "route_b"}
    assert "工单内容" in jev_stub.last_body["state"]
    assert jev_stub.last_body["model"] == "jev-latest"


def test_jev_provider_requires_endpoint(monkeypatch):
    monkeypatch.delenv("JEV_API_BASE", raising=False)
    node = DecisionNode(id="t", choices=["a", "b"], input="x", provider="jev")
    with pytest.raises(DecisionError, match="JEV_API_BASE"):
        node.execute(FakeExecution())


def test_custom_provider_registration(openai_stub):
    def static_provider(**request):
        return {"choice": request["choices"][1]["value"], "confidence": 0.8,
                "model": "static", "raw": ""}

    register_decision_provider("static", static_provider)
    try:
        node = DecisionNode(id="t", choices=["a", "b"], input="x", provider="static")
        out = node.execute(FakeExecution())
        assert out["choice"] == "b"
        assert out["confidence"] == 0.8
        assert "static" in DECISION_PROVIDERS
    finally:
        DECISION_PROVIDERS.pop("static", None)


def test_jevlike_requires_model_path():
    node = DecisionNode(id="t", choices=["a", "b"], input="x", provider="jevlike")
    with pytest.raises(DecisionError, match="checkpoint"):
        node.execute(FakeExecution())


def test_jevlike_missing_package(monkeypatch):
    # torch 缺失（sys.modules 置 None 强制 import 失败）→ 守卫给出安装提示
    monkeypatch.setitem(__import__("sys").modules, "torch", None)
    node = DecisionNode(id="t", choices=["a", "b"], input="x", provider="jevlike",
                        model="/nonexistent.pt")
    with pytest.raises(DecisionError, match="jevlike \+ torch"):
        node.execute(FakeExecution())


def test_jevlike_checkpoint_missing():
    pytest.importorskip("torch")
    pytest.importorskip("jevlike")
    node = DecisionNode(id="t", choices=["a", "b"], input="x", provider="jevlike",
                        model="/nonexistent.pt")
    with pytest.raises(DecisionError, match="checkpoint 不存在"):
        node.execute(FakeExecution())


def test_llm_provider_batch(openai_stub):
    openai_stub.content = json.dumps({"results": [
        {"index": 0, "choice": "spam", "confidence": 0.97},
        {"index": 1, "choice": "ham", "confidence": 0.5},
        {"index": 2, "choice": "spam", "confidence": 0.99},
    ]})
    node = DecisionNode(id="t", question="逐条判断", choices=["spam", "ham"],
                        items=["恭喜中奖", "会议改到三点", "再赢一辆跑车"],
                        min_confidence=0.8,
                        api_base=openai_stub.base, api_key="sk-x", model="glm-5")
    out = node.execute(FakeExecution())
    assert [r["index"] for r in out["results"]] == [0, 1, 2]
    assert out["results"][0]["choice"] == "spam"
    assert out["results"][1]["low_confidence"] is True
    assert out["low_confidence"] is True
    assert out["dry_run"] is False
    user_msg = openai_stub.last_body["messages"][1]["content"]
    assert "[0] 恭喜中奖" in user_msg and "[2] 再赢一辆跑车" in user_msg


def test_llm_provider_batch_default_substitution(openai_stub):
    openai_stub.content = json.dumps({"results": [
        {"index": 0, "choice": "spam", "confidence": 0.4},
        {"index": 1, "choice": "ham", "confidence": 0.95},
    ]})
    node = DecisionNode(id="t", choices=["spam", "ham"],
                        items=["a", "b"], min_confidence=0.9,
                        on_low_confidence="default", default_choice="ham",
                        api_base=openai_stub.base, api_key="k", model="m")
    out = node.execute(FakeExecution())
    assert out["results"][0]["choice"] == "ham"       # 低置信 → 默认项
    assert out["results"][0]["low_confidence"] is True
    assert out["results"][1]["choice"] == "ham"
    assert out["results"][1]["low_confidence"] is False


def test_llm_provider_batch_error_mode(openai_stub):
    openai_stub.content = json.dumps({"results": [
        {"index": 0, "choice": "spam", "confidence": 0.95},
        {"index": 1, "choice": "ham", "confidence": 0.4},
    ]})
    node = DecisionNode(id="t", choices=["spam", "ham"],
                        items=["a", "b"], min_confidence=0.9,
                        on_low_confidence="error",
                        api_base=openai_stub.base, api_key="k", model="m")
    with pytest.raises(DecisionError, match="第 1 项"):
        node.execute(FakeExecution())


def test_llm_provider_batch_count_mismatch(openai_stub):
    openai_stub.content = json.dumps({"results": [{"index": 0, "choice": "spam",
                                                   "confidence": 0.9}]})
    node = DecisionNode(id="t", choices=["spam", "ham"], items=["a", "b"],
                        api_base=openai_stub.base, api_key="k", model="m")
    with pytest.raises(DecisionError, match="批量结果数不符"):
        node.execute(FakeExecution())


def test_jev_provider_batch(jev_stub):
    jev_stub.choice, jev_stub.confidence = "keep", 0.9
    node = DecisionNode(id="t", question="是否可交互", provider="jev",
                        choices={"keep": "可交互元素", "drop": "噪声"},
                        items=["BUTTON \"提交\" @e1", "TEXT 装饰横幅 @e2"])
    out = node.execute(FakeExecution())
    assert [r["choice"] for r in out["results"]] == ["keep", "keep"]
    qs = jev_stub.last_body["questions"]
    assert set(qs) == {"item_0", "item_1"}
    assert all(q["type"] == "choice" for q in qs.values())
    assert qs["item_0"]["criteria"] == {"keep": "可交互元素", "drop": "噪声"}


def test_batch_dry_run():
    node = DecisionNode(id="t", choices=["first", "second"], items=["a", "b", "c"],
                        dry_run=True)
    out = node.execute(FakeExecution())
    assert [r["choice"] for r in out["results"]] == ["first"] * 3
    assert out["dry_run"] is True


def test_items_must_be_nonempty_list():
    node = DecisionNode(id="t", choices=["a", "b"], items="not-a-list")
    with pytest.raises(DecisionError, match="非空列表"):
        node.execute(FakeExecution())
    node2 = DecisionNode(id="t", choices=["a", "b"], items=[])
    with pytest.raises(DecisionError, match="非空列表"):
        node2.execute(FakeExecution())


def test_jevlike_batch_local_checkpoint():
    pytest.importorskip("torch")
    pytest.importorskip("jevlike")
    checkpoint = "/tmp/jevlike-runs/badges.pt"
    if not __import__("os").path.isfile(checkpoint):
        pytest.skip("本机未训练 jevlike checkpoint")
    node = DecisionNode(id="t", question="", choices=["azure crane", "amber badger", "gold heron"],
                        items=["Choose the exact badge amber badger. Badge: amber badger.",
                               "Choose the exact badge gold heron. Badge: gold heron."],
                        provider="jevlike", model=checkpoint)
    out = node.execute(FakeExecution())
    assert out["results"][0]["choice"] == "amber badger"
    assert out["results"][1]["choice"] == "gold heron"


def test_unknown_provider():
    node = DecisionNode(id="t", choices=["a", "b"], input="x", provider="nope")
    with pytest.raises(DecisionError, match="未注册"):
        node.execute(FakeExecution())


def test_dry_run_global_and_local():
    node = DecisionNode(id="t", choices=["first", "second"], input="x", dry_run=True)
    out = node.execute(FakeExecution())
    assert out["choice"] == "first"
    assert out["dry_run"] is True

    node2 = DecisionNode(id="t", choices=["first", "second"], input="x")
    out2 = node2.execute(FakeExecution(global_vars={"dry_run": True}))
    assert out2["dry_run"] is True


def test_empty_choices_rejected():
    node = DecisionNode(id="t", choices=[], input="x", dry_run=True)
    with pytest.raises(DecisionError, match="决策空间为空"):
        node.execute(FakeExecution())


def test_validate_checks():
    with pytest.raises(ValueError, match="决策空间为空"):
        DecisionNode(id="t", choices=[], input="x").validate()
    with pytest.raises(ValueError, match="未注册"):
        DecisionNode(id="t", choices=["a"], provider="nope").validate()
    with pytest.raises(ValueError, match="default_choice"):
        DecisionNode(id="t", choices=["a"], on_low_confidence="default").validate()
    # 合法配置静默通过
    DecisionNode(id="t", choices=["a"], on_low_confidence="default",
                 default_choice="a").validate()


def test_llm_provider_bundle_endpoint(tmp_path, monkeypatch):
    """回归：provider bundle 路径此前少传 providers 参数（TypeError），修复后应打通。"""
    stub = StubOpenAIDecision(json.dumps({"choice": "spam", "confidence": 0.97}))
    monkeypatch.setattr("plaita_nodes.config.Path.home", lambda: tmp_path)
    flowcast = tmp_path / ".flowcast"
    flowcast.mkdir()
    (flowcast / "providers.json").write_text(json.dumps({
        "providers": {"stub": {"type": "openai", "apiBase": stub.base,
                               "model": "m1", "apiKey": "sk-p"}}
    }), encoding="utf-8")
    try:
        node = LlmNode(id="t", prompt="p", provider="stub")
        out = node.execute(FakeExecution())
        assert out["model"] == "m1"
        assert stub.last_body["model"] == "m1"
    finally:
        stub.server.shutdown()
        stub.server.server_close()
