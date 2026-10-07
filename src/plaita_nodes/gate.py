"""GateNode —— 质量门原子：跑验证命令并判定 pass/fail。

对标 flowcast 的 runGate：命令 → 退出码 → 结构化 pass/fail。
与 capture 的边界：capture 返回原始 exit_code 由流程分支处理；gate 语义化
为 passed 布尔 + gate 名，并支持 ``max_retries`` 内嵌重试（退出码非零时自动
重试最多 N 次）。

输出：``{"passed", "gate", "exit_code", "stdout", "stderr", "retries"}``。
stdout/stderr 超阈值（4000 / 2000 字符）时做**头尾保留**并插入
``…[省略 N 字符]…`` 标记：诊断（``failures:`` / ``test result:``）几乎总在尾部，
纯头部切片会把唯一有用的信息丢掉。

子进程 env 经公共层白名单重建（``plaita.subprocess_env``，见
``_subprocess_env``）：宿主凭据不进被执行的命令。
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node

from ._subprocess_env import build_subprocess_env, clip_output

_STDOUT_CAP = 4000
_STDERR_CAP = 2000


class GateNode(Node):
    """跑验证命令并判定 pass/fail。

    JSON 字段：
    - ``command``: shell 字符串或参数列表（元素支持 {% %} 表达式）
    - ``gate_name``: 门名称（用于报告）
    - ``cwd``: 工作目录
    - ``timeout_secs``: 超时秒数（默认 600）
    - ``max_retries``: 失败自动重试次数（默认 0）；每次重试**重新执行命令**，
      ``retries`` 输出实际重试次数（0 = 首次即过/未重试）
    - ``dry_run``: 为 true（或 globalContext.dry_run）时返回 passed=True

    输出：``{"passed", "gate", "exit_code", "stdout", "stderr", "retries"}``。
    超阈值时 stdout/stderr 做头尾保留并标注 ``…[省略 N 字符]…``（详见模块 docstring）。
    """

    node_type: ClassVar[str] = "gate"
    node_name: ClassVar[str] = "质量门"

    command: Optional[Any] = None
    gate_name: str = Field(default="quality-gate")
    cwd: Optional[Any] = None
    # Any 而非 int：DSL 传参下是 "$INPUT.gate_timeout_secs" 表达式串（pydantic
    # 会在构造期拒收 str 进 int 字段），execute 内求值后再转 int
    timeout_secs: Any = Field(default=600)
    max_retries: int = Field(default=0)
    dry_run: bool = False

    def execute(self, execution: Any) -> dict:
        if self.command is None:
            raise ValueError("gate 节点缺少 command")
        raw = self.command if isinstance(self.command, list) else [self.command]
        parts: list[str] = [str(execution.evaluate(el)) for el in raw]
        cmd = shlex.split(" ".join(parts)) if len(parts) == 1 else parts
        if not cmd:
            raise ValueError("gate 命令为空")
        cwd = str(execution.evaluate(self.cwd) or "") if self.cwd else None
        # timeout_secs 经 DSL 传入时是表达式串（issue-pipeline v0.3 起 per-repo
        # 门预算走 INPUT），必须求值——与 git_publish.merge_mode 同一批坑。
        timeout_secs = int(execution.evaluate(self.timeout_secs) or 600)
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            return {"passed": True, "gate": self.gate_name, "exit_code": 0,
                    "stdout": "[dry-run]", "stderr": "", "retries": 0, "dry_run": True}

        env = build_subprocess_env()
        exit_code, stdout, stderr = 1, "", ""
        attempts = 1 + max(0, self.max_retries)
        for attempt in range(attempts):
            # 每轮重新 Popen——重试语义 = 重新执行命令。（历史 bug：Popen 在
            # 循环外，communicate 复用已退出进程，失败后从未真正重跑。）
            proc = subprocess.Popen(cmd, cwd=cwd or None, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
            timed_out = False
            try:
                stdout, stderr = proc.communicate(timeout=timeout_secs)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                stdout, stderr = proc.communicate()
            exit_code = 124 if timed_out else (proc.returncode or 0)
            if exit_code == 0:
                break
        return {"passed": exit_code == 0, "gate": self.gate_name,
                "exit_code": exit_code, "stdout": clip_output(stdout or "", _STDOUT_CAP),
                "stderr": clip_output(stderr or "", _STDERR_CAP),
                "retries": max(0, attempt), "dry_run": False}
