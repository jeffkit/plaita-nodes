"""A2R8-02/03 回归：capture/gate/hitl 消费 ``cancel_event``。

引擎在节点预算超时时置位 ``execution.cancel_event`` 并遗弃节点线程；
``FlowExecution.cancel()`` 还置位粘滞的 ``cancel_requested``。不消费这两个
标志的节点会在流程已经返回后继续跑子进程 / 轮询，最长到自身
``timeout_secs``（capture 120s / gate 600s×(1+retries) / hitl 3600s）。

契约：
- 取消后 1s 内子进程退出（killpg 整组），1.2s 后才会写的 sentinel 不出现；
- gate 取消后不再进入下一轮重试；
- hitl 用可唤醒等待（``cancel_event.wait``），不等满 ``poll_interval`` 即停。

负控（回退生产改动）：``test_flow_timeout_kills_capture_child`` 的子进程存活且
sentinel 落盘；hitl 用例的 polls 在取消后继续增长。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plaita.core.executor import FlowExecution
from plaita.core.flow import Flow

import plaita_nodes
from plaita_nodes._cancel import NodeCancelledError
from plaita_nodes.capture import CaptureNode
from plaita_nodes.gate import GateNode
from plaita_nodes.hitl import HitlNode

plaita_nodes.register_all()


class _Exec:
    """最小执行桩 + 可置位取消事件。"""

    def __init__(self, cancel_event=None):
        self.cancel_event = cancel_event

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _side_effect_script(pid_file, sentinel, *, append: bool = False) -> str:
    mode = "a" if append else "w"
    return (
        "import os, time\n"
        f"with open({str(pid_file)!r}, {mode!r}) as f:\n"
        "    f.write(str(os.getpid()) + '\\n')\n"
        "time.sleep(1.2)\n"
        f"with open({str(sentinel)!r}, 'w') as f:\n"
        "    f.write('written after cancel')\n"
    )


def _run_in_thread(fn):
    result = {}

    def target():
        try:
            result["out"] = fn()
        except BaseException as exc:  # noqa: BLE001 — 断言保留异常对象
            result["exc"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, result


@pytest.mark.parametrize("node_cls", [CaptureNode, GateNode])
def test_cancel_kills_subprocess_and_prevents_late_side_effect(tmp_path, node_cls):
    pid_file = tmp_path / "child.pid"
    sentinel = tmp_path / "late.txt"
    script = _side_effect_script(pid_file, sentinel)
    cancel = threading.Event()
    exec_ = _Exec(cancel)
    fields = {"id": "n", "command": [sys.executable, "-c", script],
              "timeout_secs": 5}
    if node_cls is GateNode:
        fields["max_retries"] = 2
    node = node_cls(**fields)

    thread, result = _run_in_thread(lambda: node.execute(exec_))
    assert _wait_for(pid_file.exists), "子进程未启动"
    pid = int(pid_file.read_text().split()[0])

    cancel.set()
    thread.join(1.0)
    assert not thread.is_alive(), "取消后 1s 内节点线程未退出"
    assert isinstance(result.get("exc"), NodeCancelledError)
    assert _wait_for(lambda: not _pid_alive(pid), 1.0), "取消后 1s 内子进程未退出"

    time.sleep(1.4)  # 越过脚本 1.2s 的写盘点
    assert not sentinel.exists(), "取消后仍产生了副作用文件"
    if node_cls is GateNode:
        # 重试环不再进入：脚本只落一行 pid
        assert len(pid_file.read_text().split()) == 1


def test_flow_timeout_kills_capture_child(tmp_path):
    """真实引擎节点超时（cancel_event）→ 子进程随之终止、sentinel 不落盘。"""
    pid_file = tmp_path / "child.pid"
    sentinel = tmp_path / "late.txt"
    script = _side_effect_script(pid_file, sentinel)
    flow = Flow.model_validate({
        "nodes": [
            {"type": "start", "id": "start", "next": "n"},
            {"type": "capture", "id": "n",
             "command": [sys.executable, "-c", script],
             "timeout_secs": 30, "next": "end", "timeout": "PT0.3S"},
            {"type": "end", "id": "end", "resultType": "success",
             "output": "$NODE.n"},
        ]
    })
    started = time.monotonic()
    with pytest.raises(Exception):  # NodeTimeoutError
        FlowExecution().run_compatible(flow, False)
    assert time.monotonic() - started < 2.0

    assert _wait_for(pid_file.exists), "子进程未启动"
    pid = int(pid_file.read_text().split()[0])
    assert _wait_for(lambda: not _pid_alive(pid), 1.0), \
        "flow 返回后 1s 内子进程未退出"

    remaining = 1.7 - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
    assert not sentinel.exists(), "flow 超时返回后子进程仍写盘"


class _HitlStub:
    def __init__(self):
        self.polls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                data = json.dumps({"success": True, "session_id": "s1"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.polls += 1
                data = json.dumps({"has_reply": False, "replies": [],
                                   "status": "waiting"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_hitl_polling_stops_on_cancel():
    stub = _HitlStub()
    cancel = threading.Event()
    node = HitlNode(id="h", message="ok?", base_url=stub.url,
                    timeout_secs=8, poll_interval=5.0)
    try:
        thread, result = _run_in_thread(lambda: node.execute(_Exec(cancel)))
        assert _wait_for(lambda: stub.polls >= 1, 2.0)
        cancel.set()
        # poll_interval=5s：能被 cancel_event 唤醒才可能在 1s 内退出
        thread.join(1.0)
        assert not thread.is_alive(), "取消后 hitl 未停止轮询"
        assert result.get("out", {}).get("status") == "cancel"
        polls_at_cancel = stub.polls
        time.sleep(0.6)
        assert stub.polls == polls_at_cancel, "取消后仍在轮询 hitl-server"
    finally:
        stub.close()
