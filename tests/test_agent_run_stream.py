"""recursive_stream_turn 流式辅助路径回归：stderr 背压死锁 + 超时击杀。

历史实现（评审 C3-4）：
- ``stderr=subprocess.PIPE`` 却无人消费——agent 写满管道缓冲（~64KB）即
  阻塞，stdout 永不 EOF → 调用方永久挂死；
- ``timed_out`` 在 ``proc.wait()`` 之后才判且无任何击杀——进程挂死时
  deadline 形同虚设，调用方永久阻塞。

修复后：stderr 排水线程 + 看门狗线程（deadline 到 killpg 整个进程组）+
EOF 后受 deadline 约束的 wait；逐行 yield 流式语义不变。

用 shell 桩脚本当"recursive"（经 RECURSIVE_BIN 注入，同 test_agent_run.py
的做法）；用看门狗线程收割生成器，把"挂死"转化为可断言的失败而非卡死测试。
"""
from __future__ import annotations

import json
import threading

import pytest

from plaita_nodes.agent_run import recursive_stream_turn


def _drain_generator(gen, timeout: float = 15.0) -> list:
    """驱动生成器到结束；超时未结束 = 挂死回归，显式失败而非卡死测试。"""
    outcome: dict = {}

    def run():
        try:
            outcome["events"] = list(gen)
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(f"recursive_stream_turn 未在 {timeout}s 内结束（疑似挂死）")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["events"]


def _register_stub_agent(agent_config_repo, stub_path, name: str) -> None:
    agents = json.loads(
        (agent_config_repo / ".flowcast" / "agents.json").read_text())
    agents["agents"][name] = {
        "executor": "recursive",
        "env": {"RECURSIVE_BIN": str(stub_path)},
    }
    (agent_config_repo / ".flowcast" / "agents.json").write_text(
        json.dumps(agents))


class TestRecursiveStreamTurnRobustness:
    def test_stderr_flood_does_not_deadlock(self, agent_config_repo, tmp_path):
        """子进程 stderr 写满管道缓冲（>64KB）不挂死，正常产出结果。"""
        stub = agent_config_repo / "stub-flood-stderr.sh"
        # 先往 stderr 灌 200KB（远超 Linux 管道缓冲 ~64KB），再输出结果对象
        stub.write_text(
            "#!/bin/sh\n"
            "head -c 200000 /dev/zero | tr '\\0' 'x' >&2\n"
            "printf '%s\\n' '{\"result\": \"ok\", \"is_error\": false}'\n"
        )
        stub.chmod(0o755)
        _register_stub_agent(agent_config_repo, stub, "rec-flood")

        events = _drain_generator(recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-flood", timeout_secs=30))
        # 结果 JSON 行本身也作为 line 事件产出（既有契约），随后是 done
        assert events[-1]["type"] == "done"
        assert events[-1]["ok"] is True
        assert events[-1]["result"] == "ok"
        assert [e["text"] for e in events[:-1]] == \
            ['{"result": "ok", "is_error": false}']

    def test_hung_process_killed_at_deadline(self, agent_config_repo, tmp_path):
        """子进程无输出挂死：deadline 到被杀，调用方拿到超时错误而非永久阻塞。"""
        stub = agent_config_repo / "stub-hang.sh"
        stub.write_text("#!/bin/sh\nsleep 30\n")
        stub.chmod(0o755)
        _register_stub_agent(agent_config_repo, stub, "rec-hang")

        events = _drain_generator(recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-hang", timeout_secs=1),
            timeout=15)
        assert len(events) == 1
        done = events[0]
        assert done["type"] == "done"
        assert done["ok"] is False
        assert "超时" in done["error"]

    def test_streaming_lines_then_done(self, agent_config_repo, tmp_path):
        """流式语义保留：逐行 yield line 事件，末尾 done 携带结果。"""
        stub = agent_config_repo / "stub-stream.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "echo 'step 1'\n"
            "echo 'step 2'\n"
            "printf '%s\\n' '{\"result\": \"完成\", \"is_error\": false}'\n"
        )
        stub.chmod(0o755)
        _register_stub_agent(agent_config_repo, stub, "rec-stream")

        events = _drain_generator(recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-stream", timeout_secs=30))
        assert events == [
            {"type": "line", "text": "step 1"},
            {"type": "line", "text": "step 2"},
            {"type": "line", "text": '{"result": "完成", "is_error": false}'},
            {"type": "done", "ok": True, "result": "完成", "error": ""},
        ]

    def test_output_after_deadline_reports_timeout(self, agent_config_repo, tmp_path):
        """有输出但拖过 deadline：击杀后调用方拿到超时错误（不再假装没超时）。"""
        stub = agent_config_repo / "stub-slow.sh"
        # 每行间隔 1s 共 10 行，timeout 2s → 输出中途被击杀
        stub.write_text(
            "#!/bin/sh\n"
            "i=0\n"
            "while [ $i -lt 10 ]; do echo \"line $i\"; sleep 1; i=$((i+1)); done\n"
        )
        stub.chmod(0o755)
        _register_stub_agent(agent_config_repo, stub, "rec-slow")

        events = _drain_generator(recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-slow", timeout_secs=2),
            timeout=15)
        # 前若干行已流式产出，终止事件是超时
        assert [e["type"] for e in events[:-1]] == ["line"] * (len(events) - 1)
        assert events[-1]["type"] == "done"
        assert events[-1]["ok"] is False
        assert "超时" in events[-1]["error"]
        assert len(events) - 1 < 10  # 没等到全部行就被杀
