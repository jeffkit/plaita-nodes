"""AgentRunNode × agentproc 取消链实弹（2026-10-26，取消语义收口配套）。

背景：波次③步内取消的节点级负控测试覆盖了 capture/gate/hitl
（test_a2r8_cancel.py——真子进程 + cancel_event + 1s 内死），唯独**最长
的节点族 agentrun**（agentproc 驱动，timeout_secs 默认 1800s）没有同款
实弹——「取消延迟=节点时长」的生产观测（10-08 的 28 分钟）正来自这一族。

链路：worker 取消监听（标志键 1s 轮询）→ ``execution.cancel_event`` 置位
→ agentproc runner 等待循环每轮探测（≤0.5s）→ 进程组分级击杀
（SIGTERM → kill_grace_secs → SIGKILL）→ ``EXIT_CANCELLED`` → 节点抛
``AgentRunError("agent 执行被取消")`` → worker 步界按取消标志终态化
cancelled。

本文件钉三格（全部真子进程，无 executor 桩短路）：
1. 取消命中：长跑 agent（60s sleep）在 cancel_event 置位后快速死亡，
   越期副作用（late sentinel）不落盘，节点抛取消语义异常；
2. 负控（回退生产改动）：不置位取消，agent 自然跑完、sentinel 落盘——
   证明「未落盘」断言来自取消而非脚本本身；
3. cancel_event 缺席（单测/local 旧路径形状）：退化为纯超时驱动，行为
   与无取消机制的旧版一致（不误杀）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from plaita_nodes.agent_run import AgentRunError, AgentRunNode

_SLEEP_SECS = 60
_START_WAIT_SECS = 10
_CANCEL_JOIN_SECS = 8


def _agent_script(pid_file: Path, late_file: Path, sleep_secs: float) -> str:
    return (
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"time.sleep({sleep_secs!r})\n"
        f"open({str(late_file)!r}, 'w').write('written after cancel')\n"
        "print('done')\n"  # plain executor 以 stdout 为 reply，空输出=error
    )


def _make_sleeper_executor(pid_file: Path, late_file: Path, sleep_secs: float):
    """注册 test-sleeper executor：真子进程长跑 + 越期副作用 sentinel。

    agentproc 恒以 4 个位置参数调用 build_args（第 4 参 = permission ctx），
    桩必须显式接收（见 conftest.register_test_echo_executor 同款注释）。
    """

    def build_args(message, session_id, env, _ctx=None):
        return [
            sys.executable, "-c",
            _agent_script(pid_file, late_file, sleep_secs),
        ]

    return {"cli_name": sys.executable, "plain": True, "build_args": build_args}


@pytest.fixture()
def sleeper_env(tmp_path: Path, monkeypatch):
    """agents.json 加 sleeper agent + 注册 executor；返回 sentinel 路径组。"""
    from agentproc import EXECUTORS

    pid_file = tmp_path / "child.pid"
    late_file = tmp_path / "late.txt"
    EXECUTORS["test-sleeper"] = _make_sleeper_executor(
        pid_file, late_file, _SLEEP_SECS
    )
    monkeypatch.setattr("plaita_nodes.config.Path.home", lambda: tmp_path)
    flowcast = tmp_path / ".flowcast"
    flowcast.mkdir(exist_ok=True)
    agents_file = flowcast / "agents.json"
    agents = {"agents": {"sleeper": {"executor": "test-sleeper"}}}
    if agents_file.exists():
        agents = {**json.loads(agents_file.read_text(encoding="utf-8")), **agents}
    agents_file.write_text(json.dumps(agents), encoding="utf-8")
    yield pid_file, late_file
    EXECUTORS.pop("test-sleeper", None)


def _run_node(node: AgentRunNode, cancel_event=None):
    """线程内执行节点；返回 (thread, result dict)。"""
    exec_stub = type("E", (), {
        "evaluate": lambda self, v: v,
        "get_global_variable": lambda self, k, d=None: d,
        "cancel_event": cancel_event,
    })()
    result: dict = {}

    def target():
        try:
            result["out"] = node.execute(exec_stub)
        except BaseException as exc:  # noqa: BLE001 — 断言保留异常对象
            result["exc"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, result


def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        import os
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_cancel_kills_agent_subprocess_before_timeout(sleeper_env, tmp_path):
    """取消命中：60s 长跑 agent 在 cancel_event 置位后快速死亡、副作用不落盘。"""
    pid_file, late_file = sleeper_env
    cancel = threading.Event()
    node = AgentRunNode(
        id="t", agent="sleeper", prompt="long run",
        repo=str(tmp_path), timeout_secs=_SLEEP_SECS,
    )

    started = time.monotonic()
    thread, result = _run_node(node, cancel_event=cancel)
    assert _wait_for(pid_file.exists, _START_WAIT_SECS), "agent 子进程未启动"
    pid = int(pid_file.read_text().strip())

    cancel.set()
    thread.join(_CANCEL_JOIN_SECS)
    elapsed = time.monotonic() - started
    assert not thread.is_alive(), "取消后节点线程未在窗口内退出"
    assert isinstance(result.get("exc"), AgentRunError), result
    assert "取消" in str(result["exc"])
    assert _wait_for(lambda: not _pid_alive(pid), 2.0), "取消后 agent 子进程未退出"
    time.sleep(0.5)  # 越过脚本若未被杀则会写 late 的时刻（60s 远未到，防御性）
    assert not late_file.exists(), "取消后 agent 仍完成了副作用"
    assert elapsed < _SLEEP_SECS, "取消耗时达到节点超时——取消链未生效"


def test_without_cancel_agent_completes_naturally(sleeper_env, tmp_path, monkeypatch):
    """负控：不置位取消，agent 自然跑完（短 sleep 版）——sentinel 落盘、正常返回。"""
    pid_file, late_file = sleeper_env
    # 短 sleep 版：脚本 1.5s 后写 late；timeout 足够宽
    from agentproc import EXECUTORS
    EXECUTORS["test-sleeper"] = _make_sleeper_executor(pid_file, late_file, 1.5)

    node = AgentRunNode(
        id="t", agent="sleeper", prompt="short run",
        repo=str(tmp_path), timeout_secs=30,
    )
    thread, result = _run_node(node, cancel_event=None)
    thread.join(30)
    assert not thread.is_alive()
    assert result.get("exc") is None, result
    out = result["out"]
    assert out["dry_run"] is False
    assert late_file.exists(), "负控：未取消时 agent 应自然完成副作用"


def test_missing_cancel_event_degrades_to_timeout_only(sleeper_env, tmp_path):
    """cancel_event 缺席（getattr 默认 None）：不误杀——退化为纯超时驱动。"""
    pid_file, late_file = sleeper_env
    node = AgentRunNode(
        id="t", agent="sleeper", prompt="no cancel event",
        repo=str(tmp_path), timeout_secs=_SLEEP_SECS,
    )
    # 无 cancel_event 属性的执行桩（旧路径形状）
    exec_stub = type("E", (), {
        "evaluate": lambda self, v: v,
        "get_global_variable": lambda self, k, d=None: d,
    })()
    result: dict = {}

    def target():
        try:
            result["out"] = node.execute(exec_stub)
        except BaseException as exc:  # noqa: BLE001
            result["exc"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    assert _wait_for(pid_file.exists, _START_WAIT_SECS), "agent 子进程未启动"
    # 给 2s：若节点误把「无 cancel_event」当「已取消」，子进程此刻已死
    time.sleep(2.0)
    pid = int(pid_file.read_text().strip())
    assert _pid_alive(pid), "无 cancel_event 时 agent 被误杀"
    # 外部终止收尾（模拟非取消死亡）：线程应随即退出、异常非取消语义——
    # 不等 60s 超时，套件保持秒级。
    import signal
    import os as _os
    _os.killpg(pid, signal.SIGTERM)
    thread.join(10)
    assert not thread.is_alive()
    exc = result.get("exc")
    assert exc is not None and "取消" not in str(exc)
