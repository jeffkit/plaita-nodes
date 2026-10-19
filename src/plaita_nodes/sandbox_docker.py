"""docker 沙箱 driver（设计 §5：本地 argv 型——runner 仍是唯一执行者）。

执行模型：per-exec 一次性容器（确定性命名 ``plaita-ws-ctr-{execution_id}-{ws_key}``），
宿主 argv 为 ``docker run --rm -i --init --name <ctr> --entrypoint timeout … <image> <T> <agent argv…>``。

击杀三段（设计 §6.4）：
1. 容器内 ``timeout`` 墙钟＝第一击杀权（宿主客户端被 SIGKILL 也不影响）；
2. 节点侧捕获超时/失败 → :meth:`DockerDriver.enforce`（``docker kill`` + ``rm -f`` 按派生名）；
3. runner 的 killpg/SIGKILL 只影响宿主 docker 客户端，不承担沙箱内击杀。

env 注入只经 ``--env-file``（0600 即焚），禁 ``-e VAR=VAL``（密钥进 argv，ps 可见）。
数据面（``git``）每次独立短命 ``docker run``，与 agent 执行容器互不影响。

**数据层 = 宿主数据目录 bind mount**（``~/.plaita/sandbox-data``，``PLAITA_DOCKER_DATA_ROOT``
可覆盖）——E2E 实测 colima 具名卷会被守护端静默销毁（create→mount→unmount→destroy），
宿主目录可靠且让 reaper 枚举与 krunvm 统一走 sidecar 元数据。隔离口径：挂载的是
专用数据根的子目录，非任意宿主路径。
镜像契约：内含 ``git`` 与 ``timeout``（coreutils 或 busybox）；**镜像 ENTRYPOINT 不
参与执行链**——包裹 argv 显式 ``--entrypoint timeout``（``resources.entrypoint`` 可
覆盖为绝对路径），git 凭据由镜像内的 credential helper 消费 envfile 注入的变量
（如 ``GIT_TOKEN``）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .sandbox import (
    SANDBOX_PATH,
    Redactor,
    SandboxConfigError,
    SandboxError,
    WorkspaceHandle,
    WorkspaceSpec,
    ensure_image_pinned,
    handle_id,
    register_driver,
    resource_name,
    write_sidecar,
)

__all__ = ["DockerDriver", "wrap_argv_from_env", "docker_run_fn"]

# 宿主侧 docker 客户端超时（CLI 调用本身；与沙箱墙钟无关）
_CLIENT_TIMEOUT = 600


def docker_run_fn(argv: List[str], timeout: float) -> subprocess.CompletedProcess:
    """默认宿主进程执行器（可注入替身供单测）。"""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class DockerDriver:
    """本地 Docker 沙箱。``run_fn`` 可注入（单测用假 runner 断言 argv）。"""

    name = "docker"

    def __init__(self, bin_name: Optional[str] = None,
                 data_root: Optional[str] = None,
                 run_fn: Optional[Callable[[List[str], float], subprocess.CompletedProcess]] = None,
                 redactor: Optional[Redactor] = None):
        self.bin = bin_name or os.environ.get("PLAITA_DOCKER_BIN", "docker")
        # None = 每次 ensure 时解析（Path.home 可被测试隔离）
        self._data_root: Optional[str] = data_root or os.environ.get("PLAITA_DOCKER_DATA_ROOT") or None
        self._run = run_fn or docker_run_fn
        self.redactor = redactor

    @property
    def data_root(self) -> Path:
        if self._data_root:
            return Path(self._data_root)
        # env 每次调用解析（E2E 隔离 + colima 挂载白名单：数据根须在 ~ 下）
        env = os.environ.get("PLAITA_DOCKER_DATA_ROOT")
        if env:
            return Path(env)
        return Path.home() / ".plaita" / "sandbox-data"

    def _datadir(self, execution_id: str, ws_key: str) -> Path:
        path = self.data_root / resource_name(execution_id, ws_key, "data")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── 内部 ────────────────────────────────────────────────────────────
    def _docker(self, args: List[str], timeout: float = _CLIENT_TIMEOUT
                ) -> subprocess.CompletedProcess:
        result = self._run([self.bin, *args], timeout)
        if self.redactor is not None:
            result = subprocess.CompletedProcess(
                args=result.args, returncode=result.returncode,
                stdout=self.redactor.redact(result.stdout or ""),
                stderr=self.redactor.redact(result.stderr or ""))
        return result

    @staticmethod
    def _fail(result: subprocess.CompletedProcess, what: str) -> SandboxError:
        tail = ((result.stderr or "") or (result.stdout or "")).strip()[-400:]
        return SandboxError(f"docker driver: {what} 失败：{tail or '无输出'}")

    # ── 协议实现 ────────────────────────────────────────────────────────
    def ensure(self, spec: WorkspaceSpec, execution_id: str,
               ws_key: str) -> WorkspaceHandle:
        ensure_image_pinned(spec)
        datadir = self._datadir(execution_id, ws_key)
        container = resource_name(execution_id, ws_key, "ctr")
        if not spec.image:
            raise SandboxConfigError(f"sandbox '{spec.name}'：docker driver 需要 image（template 暂不支持）")

        # 抢占清残：上一位持有者可能死亡后残留同名容器（确定性命名让这一步可行）
        stale = self._docker(["rm", "-f", "--force", container])
        if stale.returncode not in (0, 1) and stale.stderr:  # 1 = 不存在，正常
            raise self._fail(stale, f"清理残留容器 {container}")

        git = (spec.provision or {}).get("git") or {}
        if git.get("repo"):
            probe = self._docker(["run", "--rm", "--entrypoint", "git",
                                  "-v", f"{datadir}:{SANDBOX_PATH}",
                                  "-w", SANDBOX_PATH, spec.image,
                                  "rev-parse", "--is-inside-work-tree"])
            if probe.returncode != 0:  # 未 provision → clone（幂等 attach：已存在则跳过）
                # 数据目录按 (execution_id, ws_key) 派生、归 driver 所有——清空
                # 内容再 clone，避免"目录非空"拒绝。注意：清内容而非删目录——
                # colima/virtiofs 对删后重建的同名路径有陈旧句柄（E2E 实测）
                self._docker(["run", "--rm", "-v", f"{datadir}:{SANDBOX_PATH}",
                              "-w", SANDBOX_PATH, "--entrypoint", "sh",
                              spec.image, "-c", "rm -rf /work/.[!.]* /work/*"])
                clone_args = ["clone"]
                if git.get("branch"):
                    clone_args += ["--branch", str(git["branch"]), "--single-branch"]
                clone_args += [str(git["repo"]), "."]
                cloned = self._docker(["run", "--rm", "--entrypoint", "git",
                                       "-v", f"{datadir}:{SANDBOX_PATH}",
                                       "-w", SANDBOX_PATH, spec.image, *clone_args])
                if cloned.returncode != 0:
                    raise self._fail(cloned, f"provision clone（{git.get('repo')}）")

        write_sidecar(datadir, self.name, execution_id, ws_key)  # provision 后（clone 目标须为空目录）

        return WorkspaceHandle(
            driver=self.name,
            id=handle_id(execution_id, ws_key),
            path=SANDBOX_PATH,
            ws_key=ws_key,
            execution_id=execution_id,
            data={"datadir": str(datadir), "container": container,
                  "image": spec.image,
                  "entrypoint": str((spec.resources or {}).get("entrypoint")
                                    or "timeout")},
        )

    def wrap_argv(self, handle: WorkspaceHandle, agent_argv: List[str],
                  timeout_secs: int, envfile: Path) -> List[str]:
        return _build_wrap_argv(
            bin_name=self.bin, container=str(handle.data["container"]),
            volume=str(handle.data["datadir"]), path=handle.path,
            envfile=str(envfile), timeout=timeout_secs,
            image=str(handle.data["image"]),
            entrypoint=str(handle.data.get("entrypoint") or "timeout"),
            network="bridge", resources={}, agent_argv=agent_argv)

    def enforce(self, handle: WorkspaceHandle, force: bool = True) -> None:
        """最终击杀权：按派生名 kill + rm（容器可能已在，容错）。"""
        container = str(handle.data.get("container", ""))
        if not container:
            return
        self._docker(["kill", container])
        self._docker(["rm", "-f", "--force", container])

    def git(self, handle: WorkspaceHandle, args: List[str],
            envfile: Optional[Path] = None) -> subprocess.CompletedProcess:
        # --entrypoint git：数据面只经 git（conformance 6），且不受镜像 ENTRYPOINT 干扰；
        # safe.directory=*：bind mount 下仓库属主映射到宿主用户，容器内 root 会触发
        # dubious ownership 拒绝——git 的 -c 必须放在镜像之后（docker 的 -c 是 cpu-shares）
        argv = ["run", "--rm", "--entrypoint", "git",
                "-v", f"{handle.data.get('datadir')}:{handle.path}",
                "-w", handle.path]
        if envfile is not None:
            argv += ["--env-file", str(envfile)]
        argv += [str(handle.data.get("image", "")),
                 "-c", "safe.directory=*", *args]
        return self._docker(argv)

    def release(self, handle: WorkspaceHandle, keep_data: bool = True) -> None:
        container = str(handle.data.get("container", ""))
        if container:
            self._docker(["kill", container])
            self._docker(["rm", "-f", "--force", container])
        if not keep_data:
            import shutil
            datadir = handle.data.get("datadir")
            if datadir:
                shutil.rmtree(datadir, ignore_errors=True)
                if Path(datadir).exists():
                    # 容器以 root 写入的文件宿主用户删不动 → 容器内清理
                    self._docker(["run", "--rm", "-v", f"{datadir}:/data",
                                  "--entrypoint", "sh", str(handle.data.get("image", "")),
                                  "-c", "rm -rf /data/.[!.]* /data/*"])


# ── 枚举（reaper 专用，设计 §6.3）：labels 元数据 → (execution_id, ws_key) ────

def enumerate_workspaces(data_root: Optional[str] = None) -> List[Dict[str, str]]:
    """枚举本机 docker 沙箱数据目录（sidecar 元数据，与 krunvm 同机制）。"""
    from .sandbox import iter_sidecars
    root = data_root or os.environ.get("PLAITA_DOCKER_DATA_ROOT")
    if root:
        return [dict(item, driver="docker") for item in iter_sidecars([Path(root)])]
    return [dict(item, driver="docker") for item in iter_sidecars([Path.home() / ".plaita" / "sandbox-data"])]


# ── 包裹 argv（build_args 包装层的核心；被 sandbox.register_sandbox_executor 调用）

def _build_wrap_argv(*, bin_name: str, container: str, volume: str, path: str,
                     envfile: str, timeout: int, image: str,
                     entrypoint: str = "timeout", network: str = "bridge",
                     resources: Optional[Dict] = None,
                     agent_argv: List[str]) -> List[str]:
    argv = [bin_name, "run", "--rm", "-i", "--init", "--name", container,
            "--entrypoint", entrypoint,
            "-v", f"{volume}:{path}", "-w", path]
    if envfile:
        argv += ["--env-file", envfile]
    if network == "none":
        argv += ["--network", "none"]
    resources = resources or {}
    if resources.get("cpus"):
        argv += ["--cpus", str(resources["cpus"])]
    if resources.get("memory"):
        argv += ["--memory", str(resources["memory"])]
    # --entrypoint timeout：镜像后的参数直接是 timeout 的参数（墙钟 + agent argv），
    # 镜像自身 ENTRYPOINT（如 alpine/git 的 git）不参与执行链
    argv += [image, str(int(timeout)), *agent_argv]
    return argv


_ENV_KEYS = ("PLAITA_SANDBOX_IMAGE", "PLAITA_SANDBOX_NAME", "PLAITA_SANDBOX_VOLUME",
             "PLAITA_SANDBOX_PATH", "PLAITA_SANDBOX_ENVFILE", "PLAITA_SANDBOX_TIMEOUT")


def wrap_argv_from_env(env: Dict[str, str], agent_argv: List[str]) -> List[str]:
    """从 PLAITA_SANDBOX_* 旋钮构造包裹 argv（executor build_args 包装层调用）。

    缺关键旋钮即抛错——宁可失败也不裸跑（沙箱旁路是事故）。
    """
    missing = [key for key in _ENV_KEYS if not env.get(key)]
    if missing:
        raise SandboxConfigError(
            f"沙箱旋钮缺失（{', '.join(missing)}）——拒绝裸跑 agent argv")
    network = env.get("PLAITA_SANDBOX_NETWORK") or "bridge"
    resources: Dict[str, str] = {}
    if env.get("PLAITA_SANDBOX_CPUS"):
        resources["cpus"] = env["PLAITA_SANDBOX_CPUS"]
    if env.get("PLAITA_SANDBOX_MEMORY"):
        resources["memory"] = env["PLAITA_SANDBOX_MEMORY"]
    return _build_wrap_argv(
        bin_name=os.environ.get("PLAITA_DOCKER_BIN", "docker"),
        container=env["PLAITA_SANDBOX_NAME"],
        volume=env["PLAITA_SANDBOX_VOLUME"],
        path=env["PLAITA_SANDBOX_PATH"],
        envfile=env["PLAITA_SANDBOX_ENVFILE"],
        timeout=int(env["PLAITA_SANDBOX_TIMEOUT"]),
        image=env["PLAITA_SANDBOX_IMAGE"],
        entrypoint=env.get("PLAITA_SANDBOX_ENTRYPOINT") or "timeout",
        network=network,
        resources=resources,
        agent_argv=agent_argv,
    )


# import 即注册（进程内路径；entry_points 组供外部包，见 sandbox.load_entrypoint_drivers）
register_driver("docker", DockerDriver())
