"""CaptureNode —— 跑本地命令并捕获输出（对标 flowcast spawnCapture）。

与 spawnCapture 的语义对齐：**命令失败不是节点失败**——返回
``{"ok", "exit_code", "stdout", "stderr"}``，由流程用分支/错误策略决定去向
（如 minimax 封面失败仍入池）。只有配置错误（缺 command）才抛异常。

超时：整组进程 kill（POSIX 用进程组 SIGKILL，对标 flowcast killProcessTree）。

子进程 env 经公共层白名单重建（``plaita.subprocess_env``，见 ``_subprocess_env``）：
宿主凭据不进被执行的命令。stdout/stderr 超阈值（4000 / 2000 字符）时头尾保留并标注
``…[省略 N 字符]…``——节点输出会进 checkpoint / 事件流，全量塞进去对低权限
viewer 也是泄露面。
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
from typing import Any, ClassVar, Optional

from plaita import Node

from ._subprocess_env import build_subprocess_env, clip_output

_STDOUT_CAP = 4000
_STDERR_CAP = 2000


class CaptureConfigError(RuntimeError):
    pass


class CaptureNode(Node):
    """执行本地命令并捕获输出。

    JSON 字段：
    - ``command``: 字符串（shlex 切分）或字符串列表（元素支持 ``{% %}`` 表达式）
    - ``cwd``: 工作目录（默认进程 cwd）
    - ``timeout_secs``: 超时秒数，默认 120；超时 kill 进程组后返回 exit_code=124
    - ``env``: 附加环境变量（表达式，求值后叠加到白名单重建的子进程 env 上）
    - ``stdin``: 可选，传给子进程的标准输入文本（表达式；长文本走 stdin 避开 ARG_MAX）
    - ``dry_run``: 为 true（或 globalContext.dry_run）时返回 fake 结果不执行

    输出：``{"ok", "exit_code", "stdout", "stderr", "dry_run"}``；stdout/stderr
    超阈值时头尾保留并标注省略量（详见模块 docstring）。
    """

    node_type: ClassVar[str] = "capture"
    node_name: ClassVar[str] = "命令执行"

    command: Optional[Any] = None
    cwd: Optional[Any] = None
    timeout_secs: int = 120
    env: Optional[Any] = None
    stdin: Optional[Any] = None
    dry_run: bool = False

    def execute(self, execution: Any) -> dict:
        if self.command is None:
            raise CaptureConfigError("capture 节点缺少 command 字段")
        if isinstance(self.command, list):
            # 混合字面量/表达式的元素列表
            cmd = [str(execution.evaluate(el)) for el in self.command]
        else:
            # 字符串：可能是 "$NODE.x.argv" 这类求值为列表的表达式，或 shlex 字符串
            value = execution.evaluate(self.command)
            if isinstance(value, (list, tuple)):
                cmd = [str(x) for x in value]
            else:
                cmd = shlex.split(str(value))
        if not cmd:
            raise CaptureConfigError("capture 命令为空")

        cwd = execution.evaluate(self.cwd) if self.cwd else os.getcwd()
        stdin_text = str(execution.evaluate(self.stdin)) if self.stdin is not None else None
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        display = " ".join(cmd)
        if dry:
            return {"ok": True, "exit_code": 0, "stdout": f"[dry-run] would exec: {display}",
                    "stderr": "", "dry_run": True}

        # 白名单重建 env：宿主凭据不进子进程；``env`` 字段是显式声明的 extra
        extra_env = {}
        if self.env is not None:
            extra_env = {str(k): str(v)
                         for k, v in (execution.evaluate(self.env) or {}).items()}
        env = build_subprocess_env(extra_env)

        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, text=True,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=self.timeout_secs)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()

        exit_code = 124 if timed_out else (proc.returncode or 0)
        return {"ok": exit_code == 0, "exit_code": exit_code,
                "stdout": clip_output(stdout or "", _STDOUT_CAP),
                "stderr": clip_output(stderr or "", _STDERR_CAP), "dry_run": False}
