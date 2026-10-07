"""GateNode —— 质量门原子：跑验证命令并判定 pass/fail。

对标 flowcast 的 runGate：命令 → 退出码 → 结构化 pass/fail。
与 capture 的边界：capture 返回原始 exit_code 由流程分支处理；gate 语义化
为 passed 布尔 + gate 名，并支持 ``max_retries`` 内嵌重试（退出码非零时自动
重试最多 N 次）。

输出：``{"passed", "gate", "exit_code", "stdout", "stderr", "retries"}``。
stdout/stderr 超阈值（4000 / 2000 字符）时做**头尾保留**并插入
``…[省略 N 字符]…`` 标记：诊断（``failures:`` / ``test result:``）几乎总在尾部，
纯头部切片会把唯一有用的信息丢掉。
"""
from __future__ import annotations

import os
import shlex
import signal
import logging
import subprocess
import time
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node

_log = logging.getLogger(__name__)

# 宿主工件注入的沙箱落点前缀（模块级：pydantic 基类会把类属性当字段处理）
_SBX_ARTIFACT_DIR = "/tmp/plaita-gate"

_STDOUT_CAP = 4000
_STDERR_CAP = 2000


def _clip_tail(text: str, cap: int) -> str:
    """超阈值时头 1/4 + 尾 3/4 保留：诊断（failures:/test result:）在尾部。"""
    if len(text) <= cap:
        return text
    head_len = cap // 4
    return f"{text[:head_len]}…[省略 {len(text) - cap} 字符]…{text[-(cap - head_len):]}"


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
    # 沙箱执行（可选）：填 `.plaita/sandboxes.json` 里的 spec 名即**在沙箱内
    # 跑该门禁**（复用 agent 的实例与工作区、经代理通道执行）。留空 = 宿主执行，
    # 既有行为逐字节不变。填了之后 cwd 语义变为沙箱内路径（通常与 agent 的
    # workspace 一致——从而「在 agent 改过的同一棵树上」验证）。
    sandbox: Optional[Any] = None
    ws_key: Optional[Any] = None
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
        # sandbox 同样可能是表达式串（如 childflow 里传 `$INPUT.sbx_spec`）：
        # **必须求值后再判分支**——用原始字段判会因"非 None"恒真而误入沙箱，
        # 或在未求值态传入 driver（实测：sandbox='$INPUT.sbx_spec' 字符串直入）
        sandbox_spec = (str(execution.evaluate(self.sandbox) or "").strip()
                        if self.sandbox is not None else "")
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            return {"passed": True, "gate": self.gate_name, "exit_code": 0,
                    "stdout": "[dry-run]", "stderr": "", "retries": 0, "dry_run": True}

        # 沙箱分支：sandbox 求值非空即在沙箱内执行（同实例、同工作区、代理通道）
        if sandbox_spec:
            return self._execute_in_sandbox(execution, cmd, timeout_secs, sandbox_spec)

        env = os.environ.copy()
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
                "exit_code": exit_code, "stdout": _clip_tail(stdout or "", _STDOUT_CAP),
                "stderr": _clip_tail(stderr or "", _STDERR_CAP),
                "retries": max(0, attempt), "dry_run": False}

    @classmethod
    def _inject_host_artifacts(cls, driver, handle, cmd: list) -> tuple:
        """把 argv 里引用的**宿主绝对路径文件**上传到沙箱并改写为沙箱路径。

        只处理「存在且是文件」的绝对路径参数——既覆盖 `gate_runner.py` 这类
        脚本，也覆盖 `--spec /tmp/.../gates.json` 这类数据文件。上传用 driver 的
        分块通道（与仓库供给同款，带 sha 校验）。返回 (改写后的 argv, 注入表)。
        """
        from pathlib import Path

        injected: dict = {}
        out: list = []
        for token in cmd:
            text = str(token)
            cand = Path(text) if text.startswith("/") else None
            if cand is None or not cand.is_file():
                out.append(token)
                continue
            remote = f"{_SBX_ARTIFACT_DIR}/{cand.name}"
            if remote not in injected.values():
                try:
                    driver.client.put_bytes(handle.id, remote, cand.read_bytes())
                    injected[str(cand)] = remote
                except Exception as exc:  # noqa: BLE001 — 注入失败保留原令牌（让错误可见）
                    _log.warning("gate: 宿主工件注入失败 %s: %s", cand, exc)
                    out.append(token)
                    continue
            out.append(remote)
        return out, injected

    def _execute_in_sandbox(self, execution: Any, cmd: list, timeout_secs: int,
                            sandbox_name: str = "") -> dict:
        """在沙箱内执行门禁命令（复用 SANDBOX_AGENT 的实例与工作区）。

        为什么值得：门禁（pytest/cargo/clippy）是流水线最重的部分——宿主执行
        意味着每台 worker 都要备齐工具链与足够算力；下沉沙箱后宿主只做编排，
        重活与依赖都在镜像里，机器更轻、并发可更高。

        实现走既有代理通道（与 SANDBOX_AGENT 同一套 driver/实例/环境注入），
        因此天然共享同一工作区——**在 agent 改过的同一棵树上跑门禁**，无需补丁往返。
        """
        from . import sandbox as sb

        ws_key = str(execution.evaluate(self.ws_key) or "main").strip()
        # 身份用**根执行** id（不是本节点所在 childflow 的上下文 id）：gate 多在
        # gate_once 子流程里跑，用子流程 id 会 ensure() 不到 agent 的实例而新建
        # 空沙箱——门禁就在空目录里跑成 changed_files=0 / "no tests ran"（2026-10-07
        # 实测 9 次全灭的根因）。
        execution_id = sb.root_execution_id(execution)
        if not execution_id:
            raise ValueError("gate 沙箱执行缺 execution_id（无法定位实例）")
        spec = sb.load_sandboxes().get(sandbox_name)
        if spec is None:
            raise ValueError(f"gate 沙箱 '{sandbox_name}' 未注册（fail-closed）")
        driver = sb.get_driver(spec.driver)
        if driver is None:
            raise ValueError(f"gate 沙箱 driver '{spec.driver}' 未注册")

        handle = driver.ensure(spec, execution_id, ws_key)   # 幂等 attach 同一实例
        cwd = str(execution.evaluate(self.cwd) or handle.path or "")
        envs = {str(k): str(v) for k, v in (spec.env or {}).items()}

        # 宿主脚本/产物注入：门禁命令常引用**宿主路径**的脚本与 spec 文件
        # （如 `python3 <宿主>/flows/gates/gate_runner.py --spec /tmp/xxx/gates.json`）。
        # 沙箱里没有这些宿主路径——把它们上传到沙箱同构位置并改写命令，
        # 门禁才能在沙箱内原样执行（否则报 "can't open file"）。
        cmd, injected = self._inject_host_artifacts(driver, handle, cmd)
        # --cwd 归一：命令里的 `--cwd .` 依赖进程工作目录，而 gate_runner 判定
        # 「本次改动」用的是 `--cwd` 指向的**仓库根**。沙箱里必须指向沙箱工作区
        # （否则 changed_files=0、门在空目录里跑 → "no tests ran"）。
        sbx_cwd = cwd or handle.path
        cmd = [str(t) for t in cmd]
        for i, tok in enumerate(cmd):
            if tok == "--cwd" and i + 1 < len(cmd) and str(cmd[i + 1]) in (".", ""):
                cmd[i + 1] = sbx_cwd

        attempts = 1 + max(0, self.max_retries)
        exit_code, stdout, stderr, attempt = 1, "", "", 0
        for attempt in range(attempts):
            try:
                exit_code, stdout, stderr = driver.client.exec_argv(
                    handle.id, ["bash", "-c", " ".join(shlex.quote(c) for c in cmd)],
                    envs=envs or None, cwd=cwd or None, timeout=timeout_secs)
            except Exception as exc:  # noqa: BLE001 — 基础设施错误按门失败上报
                exit_code, stdout, stderr = 1, "", f"gate sandbox exec error: {exc}"
            if exit_code == 0:
                break
        return {"passed": exit_code == 0, "gate": self.gate_name,
                "exit_code": exit_code, "stdout": _clip_tail(stdout or "", _STDOUT_CAP),
                "stderr": _clip_tail(stderr or "", _STDERR_CAP),
                "retries": max(0, attempt), "dry_run": False, "sandbox": handle.id}
