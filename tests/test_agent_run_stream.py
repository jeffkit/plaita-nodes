"""recursive_stream_turn 流式辅助路径回归：stderr 背压死锁 + 超时击杀。

历史实现（评审 C3-4）：
- ``stderr=subprocess.PIPE`` 却无人消费——agent 写满管道缓冲（~64KB）即
  阻塞，stdout 永不 EOF → 调用方永久挂死；
- ``timed_out`` 在 ``proc.wait()`` 之后才判且无任何击杀——进程挂死时
  deadline 形同虚设，调用方永久阻塞。

修复后：stderr 排水线程 + 看门狗线程（deadline 到 killpg 整个进程组）+
EOF 后受 deadline 约束的 wait；逐行 yield 流式语义不变。

#19：消费方提前关闭生成器（SSE 断连 / ``gen.close()``）时，旧实现只清
遗言锁不杀 agent，留下「进程存活 + 锁已删」——下一次 preflight 看到 clean
放行即并发写同一 workspace。现在 close/异常路径先 SIGKILL 进程组并确认
终态，再清锁；无法确认终态则保留锁让 preflight 清场。正常完成路径不变。

用 shell 桩脚本当"recursive"（经 RECURSIVE_BIN 注入，同 test_agent_run.py
的做法）；用看门狗线程收割生成器，把"挂死"转化为可断言的失败而非卡死测试。
"""
from __future__ import annotations

import json
import os
import signal
import threading

import pytest

from agentproc.run_lock import _pid_alive, lock_path_for
from plaita_nodes import agent_run as agent_run_mod
from plaita_nodes.agent_run import (
    AgentRunError,
    _run_lock_key,
    preflight_workspace,
    recursive_stream_turn,
)


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


class TestConsumerCloseTerminatesAgent:
    """#19：消费方提前关闭（SSE 断连 / ``gen.close()``）必须先终止 agent。

    旧实现 ``finally`` 只清遗言锁、不杀进程：close 后 agent 仍存活且锁已
    删除，下一次 ``preflight_workspace`` 看到 clean 放行 → 同 workspace
    无锁并发写（窗口 = 剩余 timeout_secs，最长 1800s）。
    """

    def _register_sleeper(self, agent_config_repo, tmp_path, name, pidfile) -> None:
        """agent 桩：写自身 pid 到文件，吐一行后长时间运行。"""
        stub = agent_config_repo / f"stub-{name}.sh"
        stub.write_text(
            "#!/bin/sh\n"
            f"echo $$ > '{pidfile}'\n"
            "echo started\n"
            "sleep 30\n"
        )
        stub.chmod(0o755)
        _register_stub_agent(agent_config_repo, stub, name)

    @staticmethod
    def _lock_file(tmp_path, run_key: str):
        key = _run_lock_key(os.path.abspath(str(tmp_path)), run_key)
        return lock_path_for(key)

    def test_close_kills_agent_before_lock_release(
            self, agent_config_repo, tmp_path, monkeypatch):
        """close 返回时 agent 已终态、锁已清（先杀组后清锁）。"""
        monkeypatch.setenv("AGENTPROC_RUN_LOCK_DIR", str(tmp_path / "run-locks"))
        pidfile = tmp_path / "agent.pid"
        self._register_sleeper(agent_config_repo, tmp_path, "rec-close", pidfile)
        lock_file = self._lock_file(tmp_path, "t-close")

        gen = recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-close",
            timeout_secs=60, run_key="t-close")
        try:
            assert next(gen) == {"type": "line", "text": "started"}
            agent_pid = int(pidfile.read_text().strip())
            assert _pid_alive(agent_pid)
            assert lock_file.exists(), "运行中应有遗言锁"
        finally:
            gen.close()

        assert not _pid_alive(agent_pid), "close 后 agent 进程组必须已终止"
        assert not lock_file.exists(), "确认终态后才清锁"

    def test_disconnect_never_leaves_unlocked_live_agent(
            self, agent_config_repo, tmp_path, monkeypatch):
        """负控（pre-fix 形态）：断连后不得出现「agent 存活 + 锁已删」。

        旧实现在本用例上精确红：close 后 agent 仍活、锁文件不存在，下一次
        preflight 返回 clean 并放行并发写。
        """
        monkeypatch.setenv("AGENTPROC_RUN_LOCK_DIR", str(tmp_path / "run-locks"))
        pidfile = tmp_path / "agent.pid"
        self._register_sleeper(agent_config_repo, tmp_path, "rec-neg", pidfile)
        lock_file = self._lock_file(tmp_path, "t-neg")

        gen = recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-neg",
            timeout_secs=60, run_key="t-neg")
        try:
            assert next(gen) == {"type": "line", "text": "started"}
            agent_pid = int(pidfile.read_text().strip())
        finally:
            gen.close()

        alive = _pid_alive(agent_pid)
        lock_exists = lock_file.exists()
        # 不变式：锁被清掉时 agent 必须已确认终态；否则 preflight 会放行。
        assert not (alive and not lock_exists), (
            f"断连后 agent 仍存活(pid={agent_pid}) 且遗言锁已删除："
            "下一次 preflight 将放行并发写")
        assert not alive, "close 路径应终止 agent"

        info = preflight_workspace(str(tmp_path), run_key="t-neg")
        assert info["action"] in ("clean", "stale")

    def test_unkillable_agent_keeps_lock_and_preflight_refuses(
            self, agent_config_repo, tmp_path, monkeypatch):
        """收尾无法确认终态时保锁——preflight 必须拒绝新 run（fail-closed）。"""
        monkeypatch.setenv("AGENTPROC_RUN_LOCK_DIR", str(tmp_path / "run-locks"))
        pidfile = tmp_path / "agent.pid"
        self._register_sleeper(agent_config_repo, tmp_path, "rec-stuck", pidfile)
        lock_file = self._lock_file(tmp_path, "t-stuck")

        procs = []

        def _pretend_kill_failed(proc):
            procs.append(proc)
            return False

        monkeypatch.setattr(agent_run_mod, "_terminate_stream_process",
                            _pretend_kill_failed)

        gen = recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-stuck",
            timeout_secs=60, run_key="t-stuck")
        try:
            assert next(gen) == {"type": "line", "text": "started"}
            agent_pid = int(pidfile.read_text().strip())
            gen.close()

            assert _pid_alive(agent_pid)
            assert lock_file.exists(), "agent 未确认终态时不得清锁"
            with pytest.raises(AgentRunError):
                preflight_workspace(str(tmp_path), run_key="t-stuck")
        finally:
            proc = procs[0] if procs else None
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001 — 清理不掩盖断言失败
                    pass
            lock_file.unlink(missing_ok=True)

    def test_normal_completion_releases_lock_without_kill(
            self, agent_config_repo, tmp_path, monkeypatch):
        """正常完成路径零变化：不触发击杀，完成后释放锁。"""
        monkeypatch.setenv("AGENTPROC_RUN_LOCK_DIR", str(tmp_path / "run-locks"))
        stub = agent_config_repo / "stub-normal.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' '{\"result\": \"ok\", \"is_error\": false}'\n"
        )
        stub.chmod(0o755)
        _register_stub_agent(agent_config_repo, stub, "rec-normal")
        lock_file = self._lock_file(tmp_path, "t-normal")

        killed = []
        monkeypatch.setattr(agent_run_mod, "_kill_process_group",
                            lambda proc: killed.append(proc))

        events = _drain_generator(recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-normal",
            timeout_secs=30, run_key="t-normal"))
        assert events[-1]["type"] == "done"
        assert events[-1]["ok"] is True
        assert killed == [], "正常完成不应击杀进程"
        assert not lock_file.exists(), "正常完成仍按原语义释放锁"
