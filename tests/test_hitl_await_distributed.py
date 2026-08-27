"""HitlAwaitNode 的 Distributed 挂起/恢复闭环（ADR phase 2 验收）。

场景：微信确认等待期间进程崩溃/重启 → context 从落盘恢复 → poller 模式
（外部进程向 EventBus 发布 hitl_reply）→ 流程续跑完成。
HitlAwaitNode 的发送走 stub hitl-server；事件由测试内直接 publish（模拟 poller）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import plaita_nodes  # noqa: F401  # entry_points 注册（含 hitl_await）
from plaita import Flow
from plaita.core.executor import FlowExecution
from plaita.core.strategies import ExecutionMode


class StubHitlServer:
    """只实现 /api/send（成功返回 session）；轮询由 poller 概念替代。"""

    def __init__(self):
        self.received: dict = {}
        handler = self._build()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _build(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                data = b'{"success": true, "session_id": "sess-42"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        return H


@pytest.fixture
def hitl_stub(monkeypatch):
    stub = StubHitlServer()
    monkeypatch.delenv("HITL_URL", raising=False)
    yield stub
    server = stub.server
    server.shutdown()
    server.server_close()


def test_crash_during_hitl_wait_recovers(tmp_path, hitl_stub):
    from unittest.mock import MagicMock

    flow = Flow.from_string(json.dumps({
        "flow_id": "hitl-await-demo",
        "inputType": {"dataType": "object"},
        "nodes": [
            {"type": "start", "id": "start", "next": "prepare"},
            {"type": "capture", "id": "prepare",
             "command": ["echo", "dispatch"], "next": "confirm"},
            {"type": "hitl_await", "id": "confirm",
             "message": "发布确认：回复同意", "timeout_secs": 600,
             "base_url": "$INPUT.hitl_url",
             "event_type": "hitl_reply",
             "next": "record"},
            {"type": "writefile", "id": "record",
             "path": "$INPUT.out",
             "content": "{% $F.concat('决定：', $NODE.confirm.status, ' / ', $NODE.confirm.replies) %}",
             "next": "end"},
            {"type": "end", "id": "end", "output": "$NODE.record", "resultType": "success"},
        ],
    }))
    bus = MagicMock()
    bus.register_subscription = MagicMock(return_value="sub-hitl-1")

    def persist(context: dict) -> None:
        (tmp_path / "ctx.json").write_text(json.dumps(context, ensure_ascii=False),
                                           encoding="utf-8")

    def load() -> dict:
        return json.loads((tmp_path / "ctx.json").read_text(encoding="utf-8"))

    params = {"out": str(tmp_path / "decision.txt"), "hitl_url": hitl_stub.url}

    # Step1: prepare 完成
    r1 = FlowExecution.run(flow, params=params, mode=ExecutionMode.DISTRIBUTED, event_bus=bus)
    assert r1["id"] == "prepare" and not r1["is_end"]
    persist(r1["context"])

    # Step2: 进入 confirm（真发消息到 stub hitl-server）→ 挂起
    r2 = FlowExecution.run(flow, mode=ExecutionMode.DISTRIBUTED, event_bus=bus,
                           context=load())
    assert r2["id"] == "confirm" and r2.get("is_suspend") is True
    assert bus.register_subscription.called  # 内核已向 bus 注册订阅（防僵尸挂起）
    persist(r2["context"])

    # —— 此处模拟进程崩溃重启：新 FlowExecution + 从盘恢复 context ——
    # poller（外部进程）从 hitl-server 轮询到回复 → 向 bus 发布 hitl_reply
    crash_note = {"session_id": "sess-42"}

    r3 = FlowExecution.run(flow, mode=ExecutionMode.DISTRIBUTED, event_bus=bus,
                           context=load(), resume_type="event",
                           resume_data={"status": "replied", "replies": ["同意"],
                                        "session_id": crash_note["session_id"]})
    assert r3["id"] == "confirm" and not r3.get("is_suspend")
    persist(r3["context"])

    r4 = FlowExecution.run(flow, mode=ExecutionMode.DISTRIBUTED, event_bus=bus,
                           context=load())
    assert r4["id"] == "record"
    persist(r4["context"])
    r5 = FlowExecution.run(flow, mode=ExecutionMode.DISTRIBUTED, event_bus=bus,
                           context=load())
    assert r5["is_end"] is True

    content = (tmp_path / "decision.txt").read_text(encoding="utf-8")
    assert "决定：replied" in content and "同意" in content
    # stub hitl-server 确实收到了确认消息（真发送发生）
    assert "发布确认" in hitl_stub.received.get("message", "")
    assert hitl_stub.received.get("wait_reply") is True
