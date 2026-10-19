"""节点级取消的协作实现（A2R8-02/03）。

plaita 引擎在**节点预算超时**时置位 ``execution.cancel_event``
（``plaita/core/runner.py::_run_sync_node``）并遗弃节点线程；``cancel()``
还会置位粘滞的 ``execution.cancel_requested``。不消费这两个标志的节点
（capture/gate 的子进程、hitl 的轮询循环）会在流程已经返回后继续产生
副作用，最长到节点自己的 ``timeout_secs``。

本模块把「等待可取消」收敛为一处：

* :func:`cancellation_requested` —— 同时看 ``cancel_event``（节点级）与
  ``cancel_requested``（执行级，父子共享），任一置位即视为取消；
* :func:`wait_cancellable` —— 可唤醒的 sleep（hitl 轮询间隔）；
* :func:`communicate_cancellable` —— 0.25s 步进的 Popen 等待；取消或超时都
  先 killpg 整组并有界回收管道，再返回/抛错，绝不留孤儿进程。

无 ``cancel_event`` 的执行桩（旧单测 / local 路径）退化为纯超时驱动，
与历史行为一致。
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Optional, Tuple

#: Popen 等待步进：与 plaita ``code._popen_wait_cancellable`` 同口径。
CANCEL_POLL_SECONDS = 0.25
#: kill 后回收管道的上界（组外残留进程握住管道时不能无限等）。
_DRAIN_TIMEOUT_SECONDS = 1.0


class NodeCancelledError(RuntimeError):
    """节点观察到取消信号；本地子进程/轮询已终止。"""


def cancellation_requested(execution: Any) -> bool:
    """``cancel_event`` 或 ``cancel_requested`` 已置位（缺失属性视为未取消）。"""
    for attr in ("cancel_event", "cancel_requested"):
        event = getattr(execution, attr, None)
        if event is None:
            continue
        try:
            if event.is_set():
                return True
        except Exception:  # noqa: BLE001 — 桩对象的事件实现不可依赖
            continue
    return False


def wait_cancellable(execution: Any, seconds: float) -> bool:
    """睡 ``seconds``；取消信号置位时立即返回 True（已取消）。"""
    if cancellation_requested(execution):
        return True
    event = getattr(execution, "cancel_event", None)
    wait = getattr(event, "wait", None)
    if callable(wait):
        # BranchCancelEvent.wait 同时轮询父级 Event，不会被父级置位卡住。
        try:
            return bool(wait(seconds))
        except Exception:  # noqa: BLE001 — 退化为切片 sleep
            pass
    deadline = time.monotonic() + seconds
    while True:
        if cancellation_requested(execution):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(CANCEL_POLL_SECONDS, remaining))


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """killpg 整组；组已消失 / 无权限时退回 ``proc.kill()``（A2R8-B6）。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        return
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _drain(proc: subprocess.Popen) -> Tuple[str, str]:
    """kill 后有界收回管道；组外残留进程握住管道时放弃输出（进程已死）。"""
    try:
        stdout, stderr = proc.communicate(timeout=_DRAIN_TIMEOUT_SECONDS)
        return stdout or "", stderr or ""
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return "", ""


def communicate_cancellable(
    proc: subprocess.Popen,
    timeout_secs: float,
    execution: Any,
    what: str,
    input_text: Optional[str] = None,
) -> Tuple[str, str, bool]:
    """等待子进程，返回 ``(stdout, stderr, timed_out)``。

    - 取消命中：killpg 整组 + 有界回收 → 抛 :class:`NodeCancelledError`；
    - 自身超时：killpg 整组 + 有界回收 → 返回 ``timed_out=True``（调用方
      保持 exit_code=124 的历史语义）。

    ``timeout_secs`` 由调用方先行校验为正数（A2R7-05）。
    """
    deadline = time.monotonic() + timeout_secs
    first = True
    while True:
        if cancellation_requested(execution):
            _kill_process_tree(proc)
            _drain(proc)
            raise NodeCancelledError(
                f"{what} cancelled via cancel_event; process tree killed")

        remaining = max(0.0, deadline - time.monotonic())
        try:
            if first:
                stdout, stderr = proc.communicate(
                    input=input_text, timeout=min(CANCEL_POLL_SECONDS, remaining))
                return stdout or "", stderr or "", False
            stdout, stderr = proc.communicate(
                timeout=min(CANCEL_POLL_SECONDS, remaining))
            return stdout or "", stderr or "", False
        except subprocess.TimeoutExpired:
            # stdin 只在首次调用传；超时续传是未定义行为（同主仓 code 节点）。
            first = False

        if time.monotonic() >= deadline:
            _kill_process_tree(proc)
            stdout, stderr = _drain(proc)
            return stdout, stderr, True
