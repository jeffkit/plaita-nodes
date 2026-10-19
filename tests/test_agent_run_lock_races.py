"""A2R12-01/02 — stream 遗言锁生命周期竞态回归。

三条独立契约：

1. ``test_concurrent_stream_turns_second_is_gated``（A2R12-01 闸）：
   两个 stream 调用同 key 并发时，preflight→Popen→write_run_lock 必须原子；
   第二个在第一个落锁后到 preflight，看到存活的兄弟 agent 就具名拒绝，
   不得两个 agent 并发写同一 workspace。
2. ``test_late_clear_keeps_sibling_lock``（A2R12-01 清锁归属）：
   A 的 agent 已死、锁尚未清；B 的 preflight 按 stale 清掉旧锁并重建新锁后，
   A 的收尾不得无条件 unlink——必须校验锁记录 pid 归属。
3. ``test_stale_cleanup_does_not_delete_rebuilt_lock``（A2R12-02）：
   stale 清理的「读记录→判 pid→unlink」与随后重建新锁的写入串行化；慢清理者
   不得用旧快照删掉快者写入的新锁。

负控方式：只回退 ``src/plaita_nodes/agent_run.py``，保留本文件，三条用例
各自精确红（第二个 stream 未被闸 / 兄弟锁被删 / 新锁被迟到 unlink 删除）。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agentproc import run_lock
from plaita_nodes import agent_run as agent_run_mod
from plaita_nodes.agent_run import (
    AgentRunError,
    _run_lock_key,
    preflight_workspace,
    recursive_stream_turn,
)


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(run_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))
    return tmp_path / "locks"


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _register_stub(agent_config_repo, name: str, sleep_secs: float,
                   pidfile: Path | None = None) -> Path:
    """注册 shell 桩 agent（经 RECURSIVE_BIN 注入，同 test_agent_run_stream）。"""
    stub = agent_config_repo / f"stub-{name}.sh"
    lines = ["#!/bin/sh"]
    if pidfile is not None:
        lines.append(f"echo $$ > '{pidfile}'")
    lines += [
        "echo started",
        f"sleep {sleep_secs}",
        "printf '%s\\n' '{\"result\": \"ok\", \"is_error\": false}'",
    ]
    stub.write_text("\n".join(lines) + "\n")
    stub.chmod(0o755)
    agents = json.loads(
        (agent_config_repo / ".flowcast" / "agents.json").read_text(encoding="utf-8"))
    agents["agents"][name] = {
        "executor": "recursive",
        "env": {"RECURSIVE_BIN": str(stub)},
    }
    (agent_config_repo / ".flowcast" / "agents.json").write_text(
        json.dumps(agents), encoding="utf-8")
    return stub


def _wait(event: threading.Event, timeout: float, message: str) -> None:
    assert event.wait(timeout), message


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestConcurrentStreamTurnGate:
    def test_concurrent_stream_turns_second_is_gated(
            self, agent_config_repo, workspace, monkeypatch):
        """A/B 同时开工：A 落锁后 B 被 preflight 拒绝，B 的 agent 零 spawn。"""
        _register_stub(agent_config_repo, "race-a", 3)
        _register_stub(agent_config_repo, "race-b", 1)

        real_popen = subprocess.Popen
        a_in_popen = threading.Event()
        a_pid: list[int] = []
        spawned: list[str] = []
        spawned_lock = threading.Lock()

        def slow_popen(*args, **kwargs):
            argv0 = os.path.basename(args[0][0]) if args and args[0] else ""
            with spawned_lock:
                spawned.append(argv0)
            proc = real_popen(*args, **kwargs)
            if argv0.startswith("stub-race-a"):
                a_pid.append(proc.pid)
                a_in_popen.set()
                # 放大 Popen→write_run_lock 窗口：无互斥时 B 能在此窗口内也越过
                # preflight 并再 spawn 一个 agent。
                time.sleep(0.5)
            return proc

        monkeypatch.setattr(agent_run_mod.subprocess, "Popen", slow_popen)

        # CI 的 ps 按终端宽度截断 command 列（Linux 默认 80 列），pytest 的 tmp
        # 路径较长会把桩脚本 basename 截掉 → agentproc 误判 pid-reused 并删掉
        # live 锁（既有 test_agent_run_stream 同样钉住身份探测）。这里只钉 A 的
        # 身份，聚焦本仓契约：A 的 live 锁必须让 B 在 preflight 被拒。
        real_ps_fields = run_lock._ps_fields

        def ps_fields(pid):
            if a_pid and pid == a_pid[0]:
                return str(pid), "stub-race-a.sh"
            return real_ps_fields(pid)

        monkeypatch.setattr(run_lock, "_ps_fields", ps_fields)

        results: dict[str, dict] = {}

        def run(name: str) -> None:
            try:
                events = list(recursive_stream_turn(
                    "hi", workspace=str(workspace), profile=name,
                    timeout_secs=30, run_key="race"))
                results[name] = {"ok": events[-1].get("ok")}
            except BaseException as exc:  # noqa: BLE001 - 断言失败也要收集
                results[name] = {"error": exc}

        t_a = threading.Thread(target=run, args=("race-a",), daemon=True)
        t_a.start()
        _wait(a_in_popen, 10, "A 未进入 Popen（装置未就绪）")

        t_b = threading.Thread(target=run, args=("race-b",), daemon=True)
        t_b.start()
        t_a.join(timeout=20)
        t_b.join(timeout=20)

        assert results["race-a"].get("ok") is True, results["race-a"]
        assert isinstance(results["race-b"].get("error"), AgentRunError), (
            f"第二个 stream 未被闸：{results['race-b']}")
        assert "拒绝开工" in str(results["race-b"]["error"])
        with spawned_lock:
            assert not any("stub-race-b" in item for item in spawned), (
                f"第二个 stream 在被闸前仍 spawn 了 agent：{spawned}")


class TestClearRunLockOwnership:
    def test_late_clear_keeps_sibling_lock(
            self, agent_config_repo, workspace, monkeypatch):
        """A 收尾时锁已由 B 重建：只清自己的 pid，B 的锁与进程都存活。"""
        pidfile_b = workspace / "b.pid"
        _register_stub(agent_config_repo, "clear-a", 0)
        _register_stub(agent_config_repo, "clear-b", 30, pidfile_b)

        real_popen = subprocess.Popen
        pids: dict[str, int] = {}

        def recording_popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            name = os.path.basename(args[0][0]) if args and args[0] else ""
            pids[name] = proc.pid
            return proc

        monkeypatch.setattr(agent_run_mod.subprocess, "Popen", recording_popen)

        a_finalizing = threading.Event()
        release_a = threading.Event()
        real_terminate = agent_run_mod._terminate_stream_process

        def controlled_terminate(proc):
            if proc.pid == pids.get("stub-clear-a.sh"):
                a_finalizing.set()
                # 卡在「进程已死、锁未清」窗口：让 B 完成 stale 清理 + 重建锁。
                assert release_a.wait(15), "unblock late A finalizer"
            return real_terminate(proc)

        monkeypatch.setattr(agent_run_mod, "_terminate_stream_process",
                            controlled_terminate)

        errors: dict[str, BaseException] = {}

        def run_a() -> None:
            try:
                list(recursive_stream_turn(
                    "hi", workspace=str(workspace), profile="clear-a",
                    timeout_secs=30, run_key="race"))
            except BaseException as exc:  # noqa: BLE001
                errors["a"] = exc

        lock_key = _run_lock_key(str(workspace), "race")
        lock_file = run_lock.lock_path_for(lock_key)

        t_a = threading.Thread(target=run_a, daemon=True)
        t_a.start()
        _wait(a_finalizing, 15, "A 未进入收尾窗口")
        a_pid = pids["stub-clear-a.sh"]
        record = json.loads(lock_file.read_text(encoding="utf-8"))
        assert record["pid"] == a_pid
        assert not _alive(a_pid), "装置要求 A 的 agent 已死（锁成为 stale）"

        gen_b = recursive_stream_turn(
            "hi", workspace=str(workspace), profile="clear-b",
            timeout_secs=30, run_key="race")
        b_pid: int | None = None
        try:
            assert next(gen_b) == {"type": "line", "text": "started"}
            b_pid = int(pidfile_b.read_text().strip())
            record = json.loads(lock_file.read_text(encoding="utf-8"))
            assert record["pid"] == b_pid, "B 未能重建自己的遗言锁"

            release_a.set()
            t_a.join(timeout=15)
            assert not t_a.is_alive()
            assert "a" not in errors, errors

            assert lock_file.exists(), (
                "A 的迟到收尾把 B 重建的锁删了（B 的 agent 仍存活）")
            record = json.loads(lock_file.read_text(encoding="utf-8"))
            assert record["pid"] == b_pid, (
                "A 的迟到收尾把 B 重建的锁改了归属")
            assert _alive(b_pid), "B 的 agent 应仍存活"
        finally:
            release_a.set()
            gen_b.close()
            t_a.join(timeout=5)

        assert b_pid is not None
        assert not _alive(b_pid)
        assert not lock_file.exists(), "B 正常收尾应清掉自己的锁"


class TestStaleCleanupRace:
    def test_stale_cleanup_does_not_delete_rebuilt_lock(
            self, agent_config_repo, workspace, monkeypatch):
        """并发 preflight：慢清理者的迟到 unlink 不得删除快者重建的新锁。"""
        _register_stub(agent_config_repo, "stale-c", 3)
        lock_key = _run_lock_key(str(workspace), "race")
        lock_file = run_lock.lock_path_for(lock_key)

        dead = subprocess.Popen(["true"])
        dead.wait()
        run_lock.write_run_lock(lock_key, dead.pid, ["true"], spawner=os.getpid())
        assert lock_file.exists()

        real_alive = run_lock._pid_alive
        first_inside = threading.Event()
        release_slow = threading.Event()
        calls = {"n": 0}
        calls_lock = threading.Lock()

        def blocking_alive(pid):
            with calls_lock:
                calls["n"] += 1
                n = calls["n"]
            if n == 1 and pid == dead.pid:
                first_inside.set()
                assert release_slow.wait(15), "unblock slow preflight"
            return real_alive(pid)

        monkeypatch.setattr(run_lock, "_pid_alive", blocking_alive)

        slow: dict = {}

        def slow_preflight() -> None:
            slow.update(preflight_workspace(str(workspace), run_key="race"))

        t_slow = threading.Thread(target=slow_preflight, daemon=True)
        t_slow.start()
        _wait(first_inside, 10, "慢 preflight 未进入 pid 探测")

        fast_lock_written = threading.Event()
        fast_errors: dict[str, BaseException] = {}

        def fast_turn() -> None:
            try:
                for event in recursive_stream_turn(
                        "hi", workspace=str(workspace), profile="stale-c",
                        timeout_secs=30, run_key="race"):
                    if event["type"] == "line":
                        # 锁在首个 line 之前已落盘（Popen→write→yield）
                        fast_lock_written.set()
            except BaseException as exc:  # noqa: BLE001
                fast_errors["e"] = exc

        t_fast = threading.Thread(target=fast_turn, daemon=True)
        t_fast.start()

        # 修复后 fast 被 guard 挡在 preflight 之前；修复前它会在慢清理者暂停
        # 期间完成 unlink→spawn→write，随后被慢者的迟到 unlink 删掉。
        fast_lock_written.wait(timeout=1.0)
        release_slow.set()
        t_slow.join(timeout=15)
        assert not t_slow.is_alive()
        assert slow.get("reason") == "pid-gone", slow
        _wait(fast_lock_written, 15, "快 run 在慢 preflight 结束后仍未落锁")
        assert "e" not in fast_errors, fast_errors

        assert lock_file.exists(), (
            "慢 preflight 的迟到 unlink 删除了快 run 重建的新锁（活进程无锁）")
        record = json.loads(lock_file.read_text(encoding="utf-8"))
        fast_pid = record["pid"]
        assert _alive(fast_pid), "新锁必须指向存活的 agent"

        t_fast.join(timeout=15)
        assert not t_fast.is_alive()
        assert not lock_file.exists(), "快 run 正常收尾应清掉自己的锁"
