"""GateNode —— 质量门原子：跑验证命令并判定 pass/fail。

对标 flowcast 的 runGate：命令 → 退出码 → 结构化 pass/fail。
与 capture 的边界：capture 返回原始 exit_code 由流程分支处理；gate 语义化
为 passed 布尔 + gate 名，并支持 ``max_retries`` 内嵌重试（退出码非零时自动
重试最多 N 次）。

输出：``{"passed", "gate", "exit_code", "stdout", "stderr", "retries"}``。
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node


class GateNode(Node):
    """跑验证命令并判定 pass/fail。

    JSON 字段：
    - ``command``: shell 字符串或参数列表（元素支持 {% %} 表达式）
    - ``gate_name``: 门名称（用于报告）
    - ``cwd``: 工作目录
    - ``timeout_secs``: 超时秒数（默认 600）
    - ``max_retries``: 失败自动重试次数（默认 0）
    - ``dry_run``: 为 true（或 globalContext.dry_run）时返回 passed=True

    输出：``{"passed", "gate", "exit_code", "stdout", "stderr", "retries"}``。
    """

    node_type: ClassVar[str] = "gate"
    node_name: ClassVar[str] = "质量门"

    command: Optional[Any] = None
    gate_name: str = Field(default="quality-gate")
    cwd: Optional[Any] = None
    timeout_secs: int = Field(default=600)
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
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            return {"passed": True, "gate": self.gate_name, "exit_code": 0,
                    "stdout": "[dry-run]", "stderr": "", "retries": 0, "dry_run": True}

        env = os.environ.copy()
        proc = subprocess.Popen(cmd, cwd=cwd or None, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        timed_out = False
        for attempt in range(1 + max(0, self.max_retries)):
            try:
                stdout, stderr = proc.communicate(timeout=self.timeout_secs)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                stdout, stderr = proc.communicate()
            exit_code = 124 if timed_out else (proc.returncode or 0)
            if exit_code == 0:
                break
        return {"passed": exit_code == 0, "gate": self.gate_name,
                "exit_code": exit_code, "stdout": (stdout or "")[:4000],
                "stderr": (stderr or "")[:2000],
                "retries": max(0, attempt), "dry_run": False}
