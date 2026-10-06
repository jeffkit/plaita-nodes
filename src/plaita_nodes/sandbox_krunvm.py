"""krunvm microVM 沙箱 driver（本地 libkrun/Hypervisor.framework，实验档）。

与 docker driver 同为「本地 argv 型」：runner 仍是唯一执行者，本 driver 只产包裹
argv——``krunvm start <vm> -- timeout <T> <agent argv…>``。

与 docker 的三点差异（都是 microVM 带来的）：

1. **击杀语义更简单**：libkrun 的 VMM 就在调用进程内——宿主进程组被杀，VM 随之
   消灭，天然无孤儿（docker 需要三段击杀）；``enforce`` 只做按名 pkill 兜底。
2. **git 数据面在宿主侧**：workspace 数据层是宿主目录（bind mount 进 VM），
   ``git()`` 直接在宿主数据目录上执行 git——密钥零入 VM（envfile 参数收下但
   忽略），代价是数据面不隔离（设计文档 §5 实验档口径）。
3. **macOS 前置**：krunvm 要求专用大小写敏感 APFS 卷（``/Volumes/krunvm``，
   ``diskutil apfs addVolume diskX "Case-sensitive APFS" krunvm``）并通过
   ``krunvm list`` 完成首跑配置；VM 配置写 ``~/.config/krunvm/config.json``
   （跨 workspace 并发 ensure 存在配置文件竞争，v1 假定单 worker）。

镜像契约：内含 ``timeout``（coreutils 或 busybox）与 agent CLI；无网络要求
（git 数据面在宿主侧），``alpine:latest`` 即满足。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .sandbox import (
    SANDBOX_PATH,
    Redactor,
    write_sidecar,
    SandboxConfigError,
    SandboxError,
    WorkspaceHandle,
    WorkspaceSpec,
    ensure_image_pinned,
    handle_id,
    register_driver,
    resource_name,
)

__all__ = ["KrunvmDriver", "wrap_argv_from_env"]

# krunvm CLI / 宿主 git 的调用超时（含 create 时的镜像拉取）
_KRUNVM_TIMEOUT = 900


def _default_run(argv: List[str], timeout: float, cwd: Optional[str] = None
                 ) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)


class KrunvmDriver:
    """本地 microVM 沙箱（krunvm/libkrun）。``run_fn`` 可注入供单测。"""

    name = "krunvm"

    def __init__(self, bin_name: Optional[str] = None,
                 data_root: Optional[str] = None,
                 run_fn: Optional[Callable[..., subprocess.CompletedProcess]] = None,
                 redactor: Optional[Redactor] = None):
        self.bin = bin_name or os.environ.get("PLAITA_KRUNVM_BIN", "krunvm")
        # None = 每次 ensure 时解析（Path.home 可被测试隔离；import 期固化会泄漏真实 HOME）
        self._data_root: Optional[str] = data_root
        self._run = run_fn or _default_run
        self.redactor = redactor

    @property
    def data_root(self) -> Path:
        if self._data_root is not None:
            return Path(self._data_root)
        # env 每次调用解析（E2E 隔离 + colima 挂载白名单：数据根须在 ~ 下）
        env = os.environ.get("PLAITA_KRUNVM_DATA_ROOT")
        if env:
            return Path(env)
        return Path.home() / ".plaita" / "sandbox-data"

    # ── 内部 ────────────────────────────────────────────────────────────
    def _krunvm(self, args: List[str], timeout: float = _KRUNVM_TIMEOUT
                ) -> subprocess.CompletedProcess:
        result = self._run([self.bin, *args], timeout)
        if self.redactor is not None:
            result = subprocess.CompletedProcess(
                args=result.args, returncode=result.returncode,
                stdout=self.redactor.redact(result.stdout or ""),
                stderr=self.redactor.redact(result.stderr or ""))
        return result

    def _datadir(self, execution_id: str, ws_key: str) -> Path:
        path = self.data_root / resource_name(execution_id, ws_key, "data")
        path.mkdir(parents=True, exist_ok=True)
        return path
    @staticmethod
    def _listed_vms(listing: subprocess.CompletedProcess) -> List[str]:
        """``krunvm list`` 输出里的顶层 VM 名（无缩进行）。"""
        return [line.strip() for line in listing.stdout.splitlines()
                if line.strip() and not line[:1].isspace()]

    def _fail(self, result: subprocess.CompletedProcess, what: str) -> SandboxError:
        tail = ((result.stderr or "") or (result.stdout or "")).strip()[-400:]
        return SandboxError(f"krunvm driver: {what} 失败：{tail or '无输出'}")

    # ── 协议实现 ────────────────────────────────────────────────────────
    def ensure(self, spec: WorkspaceSpec, execution_id: str,
               ws_key: str) -> WorkspaceHandle:
        ensure_image_pinned(spec)
        if not spec.image:
            raise SandboxConfigError(f"sandbox '{spec.name}'：krunvm driver 需要 image")
        vm = resource_name(execution_id, ws_key, "vm").lower()
        datadir = self._datadir(execution_id, ws_key)

        git = (spec.provision or {}).get("git") or {}
        if git.get("repo"):  # 数据面在宿主侧：provision 也是宿主 git（无网络要求）
            probe = self._run(["git", "rev-parse", "--is-inside-work-tree"],
                              60, cwd=str(datadir))
            if probe.returncode != 0:  # 未 provision → clone（幂等 attach）
                clone_args = ["clone"]
                if git.get("branch"):
                    clone_args += ["--branch", str(git["branch"]), "--single-branch"]
                clone_args += [str(git["repo"]), "."]
                cloned = self._run(["git", *clone_args], 600, cwd=str(datadir))
                if cloned.returncode != 0:
                    raise self._fail(cloned, f"provision clone（{git.get('repo')}）")

        listing = self._krunvm(["list"], timeout=120)
        if listing.returncode != 0:
            raise self._fail(listing, "krunvm list（检查安装与首跑配置）")
        if vm not in self._listed_vms(listing):  # 幂等 attach：同名即复用
            create = ["create", spec.image, "--name", vm]
            resources = spec.resources or {}
            if resources.get("cpus"):
                create += ["--cpus", str(resources["cpus"])]
            if resources.get("mem"):
                create += ["--mem", str(resources["mem"])]
            create += ["-v", f"{datadir}:{SANDBOX_PATH}", "-w", SANDBOX_PATH]
            created = self._krunvm(create)
            if created.returncode != 0:
                raise self._fail(created, f"create microVM {vm}")

        write_sidecar(datadir, self.name, execution_id, ws_key)  # provision 后（clone 目标须为空目录）

        return WorkspaceHandle(
            driver=self.name,
            id=handle_id(execution_id, ws_key),
            path=SANDBOX_PATH,
            ws_key=ws_key,
            execution_id=execution_id,
            data={"vm": vm, "datadir": str(datadir), "image": spec.image},
        )

    def wrap_argv(self, handle: WorkspaceHandle, agent_argv: List[str],
                  timeout_secs: int, envfile: Optional[Path] = None) -> List[str]:
        # envfile 收下但忽略：krunvm 数据面在宿主侧，密钥零入 VM（模块 docstring §2）
        return [self.bin, "start", str(handle.data["vm"]), "--",
                "timeout", str(int(timeout_secs)), *agent_argv]

    def enforce(self, handle: WorkspaceHandle, force: bool = True) -> None:
        """按 vm 名兜底 pkill。正常情况宿主 killpg 已连带 VM（VMM 在进程内）。"""
        vm = str(handle.data.get("vm", ""))
        if vm:
            self._run(["pkill", "-f", f"krunvm start {vm}"], 30)

    def git(self, handle: WorkspaceHandle, args: List[str],
            envfile: Optional[Path] = None) -> subprocess.CompletedProcess:
        """数据面在宿主数据目录上执行 git（cwd=datadir）。"""
        result = self._run(["git", *args], 600,
                           cwd=str(handle.data.get("datadir") or "."))
        if self.redactor is not None:
            result = subprocess.CompletedProcess(
                args=result.args, returncode=result.returncode,
                stdout=self.redactor.redact(result.stdout or ""),
                stderr=self.redactor.redact(result.stderr or ""))
        return result

    def release(self, handle: WorkspaceHandle, keep_data: bool = True) -> None:
        vm = str(handle.data.get("vm", ""))
        if vm:
            self._krunvm(["delete", vm])
        if not keep_data:
            datadir = handle.data.get("datadir")
            if datadir:
                shutil.rmtree(datadir, ignore_errors=True)


# ── build_args 包装层的 krunvm 实现（经 sandbox.wrap_agent_argv_from_env 分派）───

def wrap_argv_from_env(env: Dict[str, str], agent_argv: List[str]) -> List[str]:
    """从 PLAITA_SANDBOX_* 旋钮构造 krunvm 包裹 argv；缺关键旋钮拒绝裸跑。"""
    vm = env.get("PLAITA_SANDBOX_NAME")
    timeout = env.get("PLAITA_SANDBOX_TIMEOUT")
    if not vm or not timeout:
        raise SandboxConfigError("沙箱旋钮缺失（NAME/TIMEOUT）——拒绝裸跑 agent argv")
    bin_name = os.environ.get("PLAITA_KRUNVM_BIN", "krunvm")
    return [bin_name, "start", vm, "--",
            "timeout", str(int(timeout)), *agent_argv]


# ── 枚举（reaper 专用，设计 §6.3）：扫数据目录 sidecar ──────────────────

def enumerate_workspaces(data_root: Optional[str] = None) -> List[Dict[str, str]]:
    """枚举本机 krunvm 沙箱数据目录（sidecar 元数据）。reaper 专用。"""
    if data_root:
        root = Path(data_root)
    else:
        root = (KrunvmDriver().data_root)
    if not root.is_dir():
        return []
    out: List[Dict[str, str]] = []
    for sidecar in sorted(root.glob(f"*/.plaita-sandbox.json")):
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("driver") != "krunvm":
            continue
        out.append({"driver": "krunvm",
                    "name": str(sidecar.parent.name),
                    "execution_id": str(meta.get("execution_id", "")),
                    "ws_key": str(meta.get("ws_key", ""))})
    return out


# import 即注册（进程内路径；与 sandbox_docker 同惯例）
register_driver("krunvm", KrunvmDriver())
