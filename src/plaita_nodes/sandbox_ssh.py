"""ssh 沙箱 driver（实验档）：远端 Linux VM（设计 §5）。

同为「本地 argv 型」：runner 仍是唯一执行者，包裹 argv 为::

    ssh <opts> <user@host> "cd <wsdir> && exec timeout <T> <quoted agent argv…>"

远端命令串由 driver 组装并 POSIX 转义（``shlex.quote``）——agent argv 的参数
边界不会因 ssh 的远端 shell 拼接而丢失；远端契约：POSIX sh + ``timeout``
（coreutils/busybox）+ ``git``（数据面）。

实验档口径（如实声明，设计 §5/§6.2）：

- **数据层与计算层同盘**：``release(keep_data=False)`` 才删远端目录；挂起期
  v1 不提供释放（持续占用远端盘）；
- **不做 env 白名单注入**：agent 使用远端盒子的默认环境，凭据应在远端配置
  或等后续 token broker（设计 §7.2）；envfile 参数收下但忽略；
- **枚举经远端 sidecar**：ensure 在远端工作目录写 ``.plaita-sandbox.json``，
  reaper 经 ``ssh cat`` 聚合（``enumerate_workspaces``）；
- 击杀：远端 ``timeout`` 墙钟为第一击杀权；``enforce`` 按 workspace 路径
  ``pkill -f``（命中的是会话 shell——命令串刻意不带 ``exec`` 以保住标记；
  shell 被杀后的孙进程由墙钟收尾，实验档口径）。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .sandbox import (
    Redactor,
    SandboxConfigError,
    SandboxError,
    WorkspaceHandle,
    WorkspaceSpec,
    handle_id,
    register_driver,
    resource_name,
)

__all__ = ["SshDriver", "wrap_argv_from_env", "enumerate_workspaces"]

_SSH_TIMEOUT = 900


def _default_run(argv: List[str], timeout: float, cwd: Optional[str] = None
                 ) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)


class SshDriver:
    """远端 VM 沙箱。``run_fn`` 可注入供单测。"""

    name = "ssh"

    def __init__(self, bin_name: Optional[str] = None,
                 run_fn: Optional[Callable[..., subprocess.CompletedProcess]] = None,
                 redactor: Optional[Redactor] = None):
        self.bin = bin_name or os.environ.get("PLAITA_SSH_BIN", "ssh")
        self._run = run_fn or _default_run
        self.redactor = redactor

    # ── 内部 ────────────────────────────────────────────────────────────
    def _ssh(self, spec_opts: List[str], target: str, remote_cmd: str,
             timeout: float = _SSH_TIMEOUT) -> subprocess.CompletedProcess:
        result = self._run([self.bin, *spec_opts, target, remote_cmd], timeout)
        if self.redactor is not None:
            result = subprocess.CompletedProcess(
                args=result.args, returncode=result.returncode,
                stdout=self.redactor.redact(result.stdout or ""),
                stderr=self.redactor.redact(result.stderr or ""))
        return result

    def _opts(self, spec: WorkspaceSpec) -> List[str]:
        """连接选项：私钥、端口、严格 host key（缺省用 ~/.ssh/known_hosts）。"""
        opts: List[str] = []
        if spec.identity:
            opts += ["-i", spec.identity, "-o", "IdentitiesOnly=yes"]
        if spec.port and spec.port != 22:
            opts += ["-p", str(spec.port)]
        if spec.known_hosts:
            opts += ["-o", f"UserKnownHostsFile={spec.known_hosts}"]
        opts += ["-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=10"]
        return opts

    @staticmethod
    def _target(spec: WorkspaceSpec) -> str:
        return f"{spec.user}@{spec.host}" if spec.user else str(spec.host)

    def _fail(self, result: subprocess.CompletedProcess, what: str) -> SandboxError:
        tail = ((result.stderr or "") or (result.stdout or "")).strip()[-400:]
        return SandboxError(f"ssh driver: {what} 失败：{tail or '无输出'}")

    # ── 协议实现 ────────────────────────────────────────────────────────
    def ensure(self, spec: WorkspaceSpec, execution_id: str,
               ws_key: str) -> WorkspaceHandle:
        if not spec.host:
            raise SandboxConfigError(f"sandbox '{spec.name}'：ssh driver 需要 host")
        opts = self._opts(spec)
        target = self._target(spec)
        wsdir = f"{spec.remote_root.rstrip('/')}/{resource_name(execution_id, ws_key, 'ws')}"

        mkdirs = self._ssh(opts, target, f"mkdir -p {shlex.quote(wsdir)}", timeout=120)
        if mkdirs.returncode != 0:
            raise self._fail(mkdirs, f"创建远端工作目录 {wsdir}")

        git = (spec.provision or {}).get("git") or {}
        if git.get("repo"):  # 数据面同盘：clone 在远端执行（远端需可达 repo）
            probe = self._ssh(opts, target,
                              f"cd {shlex.quote(wsdir)} && "
                              "git rev-parse --is-inside-work-tree", timeout=120)
            if probe.returncode != 0:
                # 远端工作目录归 driver 所有：clone 前清空（半截 clone/残留文件防呆）
                self._ssh(opts, target,
                          f"rm -rf {shlex.quote(wsdir)} && mkdir -p {shlex.quote(wsdir)}",
                          timeout=300)
                clone = ["clone"]
                if git.get("branch"):
                    clone += ["--branch", str(git["branch"]), "--single-branch"]
                clone += [str(git["repo"]), "."]
                quoted = " ".join(shlex.quote(a) for a in clone)
                cloned = self._ssh(opts, target,
                                   f"cd {shlex.quote(wsdir)} && git {quoted}")
                if cloned.returncode != 0:
                    raise self._fail(cloned, f"provision clone（{git.get('repo')}）")

        # 远端 sidecar 元数据（reaper 经 ssh cat 聚合）
        self._ssh(opts, target, "printf '%s\\n' "
                        f"{shlex.quote(json_sidecar(execution_id, ws_key))} > "
                        f"{shlex.quote(wsdir)}/.plaita-sandbox.json", timeout=120)

        return WorkspaceHandle(
            driver=self.name,
            id=handle_id(execution_id, ws_key),
            path=wsdir,
            ws_key=ws_key,
            execution_id=execution_id,
            data={"opts": opts, "target": target, "wsdir": wsdir,
                  "host": spec.host, "port": spec.port,
                  "identity": spec.identity,
                  "known_hosts": spec.known_hosts},
        )

    def wrap_argv(self, handle: WorkspaceHandle, agent_argv: List[str],
                  timeout_secs: int, envfile: Optional[Path] = None) -> List[str]:
        # envfile 收下但忽略（实验档：不做 env 注入，见模块 docstring）
        # 不用 `exec`：会话 shell 的 cmdline 携带 wsdir，是 enforce 的 pkill 标记
        # （exec 会让 marker 消失）；shell 被杀后的孙进程由远端墙钟兜底
        opts = list(handle.data.get("opts") or [])
        remote = (f"cd {shlex.quote(str(handle.data['wsdir']))} && timeout "
                  + " ".join(shlex.quote(part) for part in
                             [str(int(timeout_secs)), *agent_argv]))
        return [self.bin, *opts, str(handle.data["target"]), remote]

    def enforce(self, handle: WorkspaceHandle, force: bool = True) -> None:
        """按 workspace 路径 pkill 会话 shell（cmdline 携带 wsdir，见 wrap_argv）。

        ``[/]`` 括号技巧防 pkill 自匹配（调用壳的 cmdline 里是字面 ``[/]srv``,
        正则 ``[/]srv`` 不命中）；会话壳死后，孙进程由远端墙钟收尾（实验档口径，
        见模块 docstring）。
        """
        wsdir = str(handle.data.get("wsdir", ""))
        if not wsdir:
            return
        pattern = "[" + wsdir[0] + "]" + wsdir[1:]
        self._run([self.bin, *list(handle.data.get("opts") or []),
                   str(handle.data["target"]),
                   f"pkill -f {shlex.quote(pattern)} || true"], 60)

    def git(self, handle: WorkspaceHandle, args: List[str],
            envfile: Optional[Path] = None) -> subprocess.CompletedProcess:
        quoted = " ".join(shlex.quote(a) for a in args)
        return self._ssh(list(handle.data.get("opts") or []),
                         str(handle.data["target"]),
                         f"cd {shlex.quote(str(handle.data['wsdir']))} && git {quoted}")

    def release(self, handle: WorkspaceHandle, keep_data: bool = True) -> None:
        if not keep_data:
            wsdir = str(handle.data.get("wsdir", ""))
            if wsdir:
                self._ssh(list(handle.data.get("opts") or []),
                          str(handle.data["target"]),
                          f"rm -rf {shlex.quote(wsdir)}", timeout=300)
        # keep_data=True：同盘语义，目录保留（v1 无挂起期释放，见 docstring）


# ── build_args 包装层的 ssh 实现（经 sandbox.wrap_agent_argv_from_env 分派）─────

def wrap_argv_from_env(env: Dict[str, str], agent_argv: List[str]) -> List[str]:
    """从 PLAITA_SANDBOX_* 旋钮构造 ssh 包裹 argv；ssh 需要注册表携带连接面
    （旋钮里只有 wsdir/target 等派生件），缺关键件拒绝裸跑。"""
    wsdir = env.get("PLAITA_SANDBOX_WSDIR")
    timeout = env.get("PLAITA_SANDBOX_TIMEOUT")
    if not wsdir or not timeout:
        raise SandboxConfigError("沙箱旋钮缺失（WSDIR/TIMEOUT）——拒绝裸跑 agent argv")
    remote = (f"cd {shlex.quote(wsdir)} && exec timeout "
              + " ".join(shlex.quote(p) for p in [str(int(timeout)), *agent_argv]))
    opts = [opt for opt in env.get("PLAITA_SANDBOX_SSH_OPTS", "").split("\x1f") if opt]
    target = env.get("PLAITA_SANDBOX_TARGET", "")
    return [os.environ.get("PLAITA_SSH_BIN", "ssh"), *opts, target, remote]


def json_sidecar(execution_id: str, ws_key: str) -> str:
    return json.dumps({"driver": "ssh", "execution_id": execution_id,
                       "ws_key": ws_key,
                       "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def enumerate_workspaces() -> List[Dict[str, str]]:
    """ssh 资源枚举：v1 返回空——远端批量巡检需要连接面注入（运维侧执行，
    或后续 reaper 增加 --ssh-target 参数）。本机资源由 docker/krunvm 覆盖。"""
    return []


# import 即注册（进程内路径；与其它 driver 同惯例）
register_driver("ssh", SshDriver())
