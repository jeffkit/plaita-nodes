"""CaptureNode —— 跑本地命令并捕获输出（对标 flowcast spawnCapture）。

与 spawnCapture 的语义对齐：**命令失败不是节点失败**——返回
``{"ok", "exit_code", "stdout", "stderr"}``，由流程用分支/错误策略决定去向
（如 minimax 封面失败仍入池）。只有配置错误（缺 command）才抛异常。

超时：整组进程 kill（POSIX 用进程组 SIGKILL，对标 flowcast killProcessTree）。
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
from typing import Any, ClassVar, Optional

from plaita import Node


class CaptureConfigError(RuntimeError):
    pass


class CaptureNode(Node):
    """执行本地命令并捕获输出。

    JSON 字段：
    - ``command``: 字符串（shlex 切分）或字符串列表（元素支持 ``{% %}`` 表达式）
    - ``cwd``: 工作目录（默认进程 cwd）
    - ``timeout_secs``: 超时秒数，默认 120；超时 kill 进程组后返回 exit_code=124
    - ``env``: 附加环境变量（表达式，求值后 dict 合并进 os.environ）
    - ``dry_run``: 为 true（或 globalContext.dry_run）时返回 fake 结果不执行

    输出：``{"ok", "exit_code", "stdout", "stderr", "dry_run"}``。
    """

    node_type: ClassVar[str] = "capture"
    node_name: ClassVar[str] = "命令执行"

    command: Optional[Any] = None
    cwd: Optional[Any] = None
    timeout_secs: int = 120
    env: Optional[Any] = None
    dry_run: bool = False

    def execute(self, execution: Any) -> dict:
        if self.command is None:
            raise CaptureConfigError("capture 节点缺少 command 字段")
        raw = self.command if isinstance(self.command, list) else [self.command]
        parts: list[str] = []
        for element in raw:
            value = execution.evaluate(element)
            parts.append(str(value))
        cmd = shlex.split(" ".join(parts)) if len(parts) == 1 else parts
        if not cmd:
            raise CaptureConfigError("capture 命令为空")

        cwd = execution.evaluate(self.cwd) if self.cwd else os.getcwd()
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        display = " ".join(cmd)
        if dry:
            return {"ok": True, "exit_code": 0, "stdout": f"[dry-run] would exec: {display}",
                    "stderr": "", "dry_run": True}

        env = os.environ.copy()
        if self.env is not None:
            extra = execution.evaluate(self.env) or {}
            env.update({str(k): str(v) for k, v in extra.items()})

        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout_secs)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()

        exit_code = 124 if timed_out else (proc.returncode or 0)
        return {"ok": exit_code == 0, "exit_code": exit_code,
                "stdout": stdout or "", "stderr": stderr or "", "dry_run": False}
