"""AGS driver —— 腾讯云 Agent Runtime（沙箱）E2B 兼容面远程执行。

设计要点（docs/sandbox-drivers-design.md v2.1 的 ags 后端）：

- **实例发现走元数据**：``Sandbox.list(SandboxQuery(metadata={...}))`` 按
  ``plaita_execution`` / ``plaita_ws`` 过滤（AGS 实测支持元数据过滤）——
  ``ensure`` 幂等且**机器无关**：任何 worker 都能 attach 回同一实例，
  这是「同一 flow 的多次 agent 执行共享一个沙箱」的实现点。
- **执行者仍是 agentproc runner**：本地 ``wrap_argv`` 产出**代理 argv**
  （``python -m plaita_nodes.ags_exec``），runner spawn 代理、代理经数据面
  把 argv 转发进沙箱执行、流式回传 stdout/stderr、退出码透传。协议形状
  与 docker/krunvm 的「宿主 spawn 包装命令」逐字节同构。
- **密钥只经 envfile**：实例创建**不带** envs；agent 环境每次执行经
  envfile（0600）→ 代理 → 数据面 ``envs=`` 注入进程，实例上不留凭据。
- **供给（provision）**：宿主 ``git bundle``（小仓）或 ``git archive``
  （大仓，``provision.mode=archive``）→ 分块 base64 上传 → 沙箱内 clone
  并记基线（``.git/plaita-baseline``）。宿主侧以 ``GIT_INDEX_FILE`` 临时
  索引产 patch，**不触碰宿主正常索引**。
- **生命周期**：``enforce``=kill（最终击杀权）；``release(keep_data=True)``
  默认 kill（AGS 数据在实例磁盘内，``provision.keep_release=pause`` 可改
  pause 保留）。

宿主前置：worker 环境需 ``E2B_DOMAIN`` + ``E2B_API_KEY``（AGS 控制台
「快速开始」的域名与 API Key）；沙箱镜像必须自带 envd 且暴露 49983
（见镜像配方）。``e2b`` SDK 为可选依赖，仅在实际调用时 import。
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .sandbox import (
    SANDBOX_ENV_FLAG,
    SandboxConfigError,
    SandboxError,
    WorkspaceHandle,
    WorkspaceSpec,
    register_driver,
)

_log = logging.getLogger(__name__)

# 沙箱内工作根（user 账号可写；镜像 `useradd -m user`）
AGS_SANDBOX_ROOT = "/home/user/plaita-ws"
# 元数据键（实例发现用；AGS 只存字符串）
META_EXECUTION = "plaita_execution"
META_WS = "plaita_ws"
# 默认实例时长（秒）；spec.resources.timeout_secs 可覆盖
AGS_DEFAULT_TIMEOUT = 7200
# 分块上传步长（base64 字符数；AGS 数据面实测 100KB/往返 ≈1.3s）
UPLOAD_CHUNK = 100_000


class AgsError(SandboxError):
    """AGS driver 基础设施错误。"""


def _require_e2b():
    try:
        from e2b_code_interpreter import Sandbox
        return Sandbox
    except ImportError as exc:  # pragma: no cover - 可选依赖
        raise AgsError(
            "e2b SDK 未安装（pip install e2b-code-interpreter）；"
            "AGS driver 为可选依赖，仅 ags workspace 需要") from exc


class AgsClient:
    """E2B 兼容面薄封装（懒 import——plaita-nodes 不硬依赖 e2b）。"""

    def __init__(self, domain: Optional[str] = None, api_key: Optional[str] = None):
        self.domain = domain or os.environ.get("E2B_DOMAIN", "")
        self.api_key = api_key or os.environ.get("E2B_API_KEY", "")

    # -- 连接层 ---------------------------------------------------------

    def _sandbox_cls(self):
        Sandbox = _require_e2b()
        if not self.domain or not self.api_key:
            raise AgsError(
                "缺少 E2B_DOMAIN / E2B_API_KEY（AGS 控制台「快速开始」下发；"
                "worker 环境需注入）")
        return Sandbox

    def find(self, execution_id: str, ws_key: str) -> Optional[str]:
        """按元数据找实例 id；未找到返回 None。"""
        Sandbox = self._sandbox_cls()
        from e2b.sandbox.sandbox_api import SandboxQuery

        try:
            pag = Sandbox.list(
                SandboxQuery(metadata={META_EXECUTION: execution_id, META_WS: ws_key}))
            items = pag.next_items() if hasattr(pag, "next_items") else list(pag)
        except Exception as exc:  # noqa: BLE001 - 网络/实现差异都当未找到（不炸）
            _log.warning("ags: 实例列举失败（视为未找到）：%s", exc)
            return None
        for item in items or []:
            sid = getattr(item, "sandbox_id", None) or getattr(item, "id", None)
            if sid:
                return str(sid)
        return None

    def create(self, spec: WorkspaceSpec, execution_id: str, ws_key: str) -> str:
        Sandbox = self._sandbox_cls()
        template = spec.template or spec.image
        if not template:
            raise AgsError(f"ags spec '{spec.name}' 缺 template/image（沙箱工具名或镜像）")
        timeout = int(spec.resources.get("timeout_secs", AGS_DEFAULT_TIMEOUT))
        # 注意：不传 envs——agent 环境经 envfile 每次执行注入（密钥不进实例配置）
        sbx = Sandbox.create(
            template=template,
            timeout=timeout,
            metadata={META_EXECUTION: execution_id, META_WS: ws_key,
                      **{str(k): str(v) for k, v in (spec.resources.get("metadata") or {}).items()}},
        )
        return str(sbx.sandbox_id)

    def attach(self, instance_id: str):
        Sandbox = self._sandbox_cls()
        return Sandbox.connect(instance_id)

    def kill(self, instance_id: str) -> None:
        try:
            self.attach(instance_id).kill()
        except Exception as exc:  # noqa: BLE001 - 尽力而为
            _log.warning("ags: kill(%s) 失败：%s", instance_id, exc)

    def pause(self, instance_id: str) -> None:
        try:
            self.attach(instance_id).pause()
        except Exception as exc:  # noqa: BLE001
            _log.warning("ags: pause(%s) 失败：%s", instance_id, exc)

    def set_timeout(self, instance_id: str, secs: int) -> None:
        try:
            self.attach(instance_id).set_timeout(secs)
        except Exception as exc:  # noqa: BLE001
            _log.warning("ags: set_timeout(%s) 失败：%s", instance_id, exc)

    # -- 数据面 ---------------------------------------------------------

    def exec_argv(self, instance_id: str, argv: list, *, envs: Optional[Dict[str, str]] = None,
                  cwd: Optional[str] = None, timeout: float = 600,
                  on_stdout: Optional[Callable[[str], None]] = None,
                  on_stderr: Optional[Callable[[str], None]] = None,
                  stdin_data: Optional[bytes] = None) -> tuple:
        """在沙箱内执行 argv（经 shell 引用拼串；envd 只收字符串命令）。

        返回 (exit_code, stdout, stderr)。stdin_data 非空时经 stdin 通道送入。
        """
        sbx = self.attach(instance_id)
        cmd = " ".join(shlex.quote(str(a)) for a in argv)
        out_buf: list = []
        err_buf: list = []

        def _wrap(buf, cb):
            def _sink(data):
                text = data if isinstance(data, str) else str(data)
                buf.append(text)
                if cb:
                    cb(text)
            return _sink

        kwargs: Dict[str, Any] = {
            "envs": envs or None,
            "cwd": cwd or None,
            "timeout": float(timeout),
        }
        try:
            if stdin_data is not None:
                # stdin 通道：background 拿句柄 → 送输入 → 关流 → 收结果
                proc = sbx.commands.run(cmd, stdin=True, background=True, **kwargs)
                try:
                    proc.send_stdin(stdin_data)
                finally:
                    proc.close_stdin()
                res = proc.wait(on_stdout=_wrap(out_buf, on_stdout),
                                on_stderr=_wrap(err_buf, on_stderr))
            else:
                res = sbx.commands.run(cmd, on_stdout=_wrap(out_buf, on_stdout),
                                       on_stderr=_wrap(err_buf, on_stderr), **kwargs)
            return int(res.exit_code), (res.stdout or "".join(out_buf)), \
                (res.stderr or "".join(err_buf))
        except Exception as exc:  # 非零退出 = CommandExitException（带结果字段）
            code = getattr(exc, "exit_code", None)
            if code is None:
                raise
            return (int(code), getattr(exc, "stdout", None) or "".join(out_buf),
                    getattr(exc, "stderr", None) or "".join(err_buf))

    def put_bytes(self, instance_id: str, remote_path: str, data: bytes,
                  chunk: int = UPLOAD_CHUNK) -> Dict[str, Any]:
        """分块 base64 上传（数据面单次 payload 有上限；files.write 大文件超时）。"""
        b64 = base64.b64encode(data).decode("ascii")
        tmp = f"{remote_path}.b64"
        # 目标目录先建（调用方给的路径可能落在还不存在的目录，如 /tmp/plaita-gate/）
        parent = str(remote_path).rsplit("/", 1)[0] or "/"
        self.exec_argv(instance_id, ["bash", "-c",
                                     f"mkdir -p {shlex.quote(parent)} && rm -f {shlex.quote(tmp)}"],
                       timeout=60)
        t0 = time.time()
        for i in range(0, len(b64), chunk):
            piece = b64[i:i + chunk]
            code, _, err = self.exec_argv(
                instance_id, ["bash", "-c", f"printf '%s' {shlex.quote(piece)} >> {shlex.quote(tmp)}"],
                timeout=120)
            if code != 0:
                raise AgsError(f"分块上传失败（块 {i//chunk}）：{err[:200]}")
        code, out, err = self.exec_argv(
            instance_id,
            ["bash", "-c", f"base64 -d {shlex.quote(tmp)} > {shlex.quote(remote_path)} "
                           f"&& rm -f {shlex.quote(tmp)} && sha256sum {shlex.quote(remote_path)}"],
            timeout=120)
        if code != 0:
            raise AgsError(f"上传落盘失败：{err[:200]}")
        remote_sha = out.strip().split()[0] if out.strip() else ""
        local_sha = hashlib.sha256(data).hexdigest()
        if remote_sha != local_sha:
            raise AgsError(f"上传 sha 不符 local={local_sha[:12]} remote={remote_sha[:12]}")
        return {"bytes": len(data), "secs": round(time.time() - t0, 1)}

    def put_text(self, instance_id: str, remote_path: str, text: str) -> None:
        sbx = self.attach(instance_id)
        sbx.files.write(remote_path, text)

    def get_text(self, instance_id: str, remote_path: str) -> str:
        sbx = self.attach(instance_id)
        data = sbx.files.read(remote_path)
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace")
        return str(data)


class AgsDriver:
    """AGS 远程执行 driver（SandboxDriver 协议）。

    ``ensure`` 幂等：元数据命中即 attach；未命中则创建 + 供给（bundle 上传
    + 沙箱内 clone + 记基线）。**同一 (execution_id, ws_key) 全流程共用一个
    实例**——implement/fix/review 轮次 attach 回来看到同一工作区。
    """

    name = "ags"

    def __init__(self, client: Optional[AgsClient] = None):
        self.client = client or AgsClient()

    # -- 协议：ensure ----------------------------------------------------

    def ensure(self, spec: WorkspaceSpec, execution_id: str, ws_key: str) -> WorkspaceHandle:
        if not (spec.template or spec.image):
            raise AgsError(f"ags spec '{spec.name}' 缺 template/image（沙箱工具名或镜像）")
        ws_path = str(spec.resources.get("workspace_path") or f"{AGS_SANDBOX_ROOT}/repo")
        instance = self.client.find(execution_id, ws_key)
        fresh = instance is None
        if fresh:
            instance = self.client.create(spec, execution_id, ws_key)
            _log.info("ags: 新建实例 %s（exec=%s ws=%s）", instance[:12], execution_id, ws_key)
        else:
            _log.info("ags: 复用实例 %s（exec=%s ws=%s）", instance[:12], execution_id, ws_key)

        handle = WorkspaceHandle(
            driver=self.name, id=str(instance), path=ws_path, ws_key=ws_key,
            execution_id=execution_id,
            data={"container": str(instance), "path": ws_path, "fresh": fresh},
        )
        git = (spec.provision or {}).get("git") or {}
        if git.get("repo"):
            self.ensure_repo(handle, str(git["repo"]),
                             rev=str(git.get("rev") or git.get("branch") or "HEAD"),
                             mode=str(git.get("mode") or "bundle"), force=fresh)
        else:
            # 无供给配置也要保证工作根存在（代理 cwd / RECURSIVE_WORKSPACE 依赖）
            self.client.exec_argv(handle.id, ["mkdir", "-p", ws_path], timeout=60)
        if fresh:
            # 实例寿命兜底续期（flow 全程可能超默认时长）
            self.client.set_timeout(str(instance), int(
                spec.resources.get("timeout_secs", AGS_DEFAULT_TIMEOUT)))
        return handle

    # -- 协议：wrap_argv / git / enforce / release -----------------------

    def wrap_argv(self, handle: WorkspaceHandle, agent_argv: list,
                  timeout_secs: int, envfile: Path) -> list:
        return _proxy_argv(handle.id, str(envfile), str(handle.path or AGS_SANDBOX_ROOT),
                           timeout_secs) + list(agent_argv)

    def git(self, handle: WorkspaceHandle, args: list,
            envfile: Optional[Path] = None) -> subprocess.CompletedProcess:
        argv = ["git", "-C", str(handle.path)] + [str(a) for a in args]
        code, out, err = self.client.exec_argv(handle.id, argv, timeout=300)
        return subprocess.CompletedProcess(argv, code, out, err)

    def enforce(self, handle: WorkspaceHandle, force: bool = True) -> None:
        """最终击杀权：实例级 kill（取消/失败路径）。"""
        self.client.kill(handle.id)

    # ── 暂停实例清扫（pause 保现场的安全带）──────────────────────────────

    def sweep_paused(self, max_age_secs: int = 6 * 3600) -> list:
        """回收**超龄仍在 PAUSED** 的自家实例，返回被清 id 列表。

        为什么需要：失败/取消路径改为一律 pause 保现场后（sandbox_agent.
        _preserve_scene），没人续跑的暂停实例会累积并占实例配额（AGS 配额 ~20）。
        暂停期间不计计算力费，但配额占满会让新建失败。**只清带自家 metadata 键
        （plaita_execution / plaita_ws）的实例**，同账号下他人的实例绝不碰。
        """
        try:
            from e2b_code_interpreter import Sandbox, SandboxQuery, SandboxState
        except Exception as exc:  # pragma: no cover — 缺 e2b 时静默跳过
            _log.warning("ags: 暂停清扫跳过（e2b 不可用）：%s", exc)
            return []
        try:
            items = Sandbox.list(SandboxQuery(state=[SandboxState.PAUSED])).next_items()
        except Exception as exc:  # noqa: BLE001 — 枚举失败不致命
            _log.warning("ags: 暂停实例枚举失败：%s", exc)
            return []
        now = _dt.datetime.now(_dt.timezone.utc)
        killed: list = []
        for entry in items:
            sid = str(getattr(entry, "sandbox_id", "") or "")
            started = getattr(entry, "started_at", None)
            if not should_sweep_paused(getattr(entry, "metadata", None), started,
                                       now=now, max_age_secs=max_age_secs):
                continue
            try:
                self.client.kill(sid)
                killed.append(sid)
                _log.info("ags: 清扫超龄暂停实例 %s（存活 %.1fh）", sid[:14],
                          (now - started).total_seconds() / 3600)
            except Exception as exc:  # noqa: BLE001 — 单点失败不拖累其它
                _log.warning("ags: 清扫暂停实例 %s 失败：%s", sid[:14], exc)
        return killed

    def release(self, handle: WorkspaceHandle, keep_data: bool = True,
                spec: Optional[WorkspaceSpec] = None) -> None:
        """释放计算层。**keep_data=True → pause（保实例磁盘，可恢复）；False → kill**。

        语义修正（2026-10-07）：此前由 spec 的 ``keep_release`` 单值决定，导致
        「谁调都一样」——即使失败路径想保现场，也被全局 kill 覆盖。现改为
        **以调用方的 keep_data 为准**（这正是 SandboxDriver 协议的原意）；
        spec 的 ``keep_release`` 仅作未显式指定时的缺省（缺省 kill）。
        调用方分流：正常完成（改动已 sync_out 回 Git）→ keep_data=False；
        失败/挂起（中间态只在沙箱）→ keep_data=True 保现场。
        """
        default_pause = str(((spec.resources if spec else {}) or {})
                            .get("keep_release") or "kill") == "pause"
        keep = bool(keep_data) or (spec is None and default_pause)
        if keep:
            self.client.pause(handle.id)
        else:
            self.client.kill(handle.id)

    # -- 供给（bundle/archive → 上传 → clone）----------------------------

    def ensure_repo(self, handle: WorkspaceHandle, repo: str,
                    rev: str = "HEAD", mode: str = "bundle", force: bool = False) -> bool:
        """幂等供给仓库到沙箱（宿主 bundle/archive → 上传 → clone + 记基线）。

        返回 True=本次做了供给；False=已有基线（跳过）。sync_in 依赖基线文件，
        首次调用必须完成供给。**repo 由调用方给**（flow 节点传宿主工作区），
        spec 不必写死仓库路径——按仓灰度只需一个通用 spec。
        """
        ws_path = handle.path

        # 已供给判断（幂等）：基线文件在 = 已 clone
        code, _, _ = self.client.exec_argv(
            handle.id, ["bash", "-c", f"test -f {shlex.quote(ws_path)}/.git/plaita-baseline"],
            timeout=60)
        if code == 0 and not force:
            return False

        payload, sha = _make_payload(repo, rev, mode)
        _log.info("ags: 供给 %s（%s，%.1fKB）→ %s", os.path.basename(repo), mode,
                  len(payload) / 1024, ws_path)
        remote_payload = f"/tmp/plaita-provision.{'tgz' if mode == 'archive' else 'bundle'}"
        self.client.put_bytes(handle.id, remote_payload, payload)

        if mode == "archive":
            cmds = (
                f"set -e; rm -rf {shlex.quote(ws_path)}; mkdir -p {shlex.quote(ws_path)}; "
                f"tar xzf {shlex.quote(remote_payload)} -C {shlex.quote(ws_path)}; "
                f"cd {shlex.quote(ws_path)}; git init -q; "
                f"git config user.email plaita@local; git config user.name plaita; "
                f"git add -A; git commit -qm baseline; ")
        else:
            cmds = (
                f"set -e; rm -rf {shlex.quote(ws_path)}; "
                f"git clone -q {shlex.quote(remote_payload)} {shlex.quote(ws_path)}; "
                f"cd {shlex.quote(ws_path)}; git config user.email plaita@local; "
                f"git config user.name plaita; "
                f"git checkout -q -f {shlex.quote(sha)} 2>/dev/null || true; ")
        cmds += (
            f"git -C {shlex.quote(ws_path)} rev-parse HEAD > "
            f"{shlex.quote(ws_path)}/.git/plaita-baseline; rm -f {shlex.quote(remote_payload)}")
        code, out, err = self.client.exec_argv(handle.id, ["bash", "-c", cmds], timeout=600)
        if code != 0:
            raise AgsError(f"沙箱供给失败（{mode}）：{err[:300] or out[:300]}")
        handle.data["baseline_sha"] = sha
        # 注：不写宿主 sidecar（数据在远端实例磁盘；reaper 走元数据列举，见模块 docstring）
        return True

    # -- 增量同步（host ⇄ sandbox 补丁）--------------------------------

    def sync_in(self, handle: WorkspaceHandle, host_repo: str) -> Dict[str, Any]:
        """宿主工作区 → 沙箱（应用宿主脏改动；**增量语义，不丢弃沙箱已有进展**）。

        ⚠️ 关键语义（2026-10-08 修正）：早期实现每次都 ``git reset --hard <基线>``
        ——那是**破坏性**的：沙箱 agent 的成果只存在于沙箱（宿主 worktree 是干净的），
        reset 会把它们连同后续门禁/评审看到的工作区一起抹掉（实测表现为门禁
        ``changed_files=0``、no tests ran）。正确做法是**基于沙箱当前 HEAD 应用
        宿主增量补丁**：宿主侧新出现的改动合进来，沙箱已有的提交保持不动。

        实现：先把宿主脏改动打成补丁（对比基线），在**沙箱当前树**上 ``git apply``
        （三方已有内容不重复应用），冲突则硬失败交上层——不静默丢改动。
        """
        baseline = self._baseline(handle)
        patch = _host_dirty_patch(host_repo)   # 宿主 vs 其 HEAD（含未跟踪）
        tmpp = "/tmp/plaita-sync-in.patch"
        if patch:
            self.client.put_bytes(handle.id, tmpp, patch)
        cmds = f"set -e; cd {shlex.quote(handle.path)}; "
        if patch:
            # 增量应用：只把宿主新改动叠加进来。已在树里的内容 apply 会报
            # "already applied" → 用 --3way 容忍，其余错误照实抛出。
            cmds += (f"(git apply --3way --whitespace=nowarn {shlex.quote(tmpp)} 2>&1 "
                     f"|| git apply --whitespace=nowarn {shlex.quote(tmpp)} 2>&1 "
                     f"|| echo 'PAITA_SYNC_IN_APPLY_FAILED'); rm -f {shlex.quote(tmpp)}; ")
        cmds += "git add -A; git commit -qm 'plaita-sync-in' --allow-empty; " \
                "git log --oneline -1; "
        code, out, err = self.client.exec_argv(handle.id, ["bash", "-c", cmds], timeout=300)
        if code != 0:
            raise AgsError(f"sync_in 失败：{err[:300] or out[:300]}")
        if "PAITA_SYNC_IN_APPLY_FAILED" in (out or ""):
            raise AgsError(f"sync_in 补丁应用失败（沙箱树与宿主不等价）：{out[:200]}")
        return {"patch_bytes": len(patch), "baseline": baseline[:12]}

    def sync_out(self, handle: WorkspaceHandle, host_repo: str) -> Dict[str, Any]:
        """沙箱 → 宿主工作区（本轮 agent 增量补丁；git apply 到宿主 worktree）。"""
        cmds = (
            f"set -e; cd {shlex.quote(handle.path)}; "
            f"git add -A; git diff --cached HEAD --binary;")
        code, out, err = self.client.exec_argv(handle.id, ["bash", "-c", cmds], timeout=300)
        if code != 0:
            raise AgsError(f"sync_out 取补丁失败：{err[:300]}")
        patch = out.encode("utf-8")
        if patch.strip():
            applied = _apply_patch(host_repo, patch)
        else:
            applied = False
        # 归一：沙箱侧提交本轮，保持工作树干净（下轮 sync_in 会 reset 基线）
        self.client.exec_argv(
            handle.id, ["bash", "-c", f"cd {shlex.quote(handle.path)} && "
                                      f"git commit -qm 'plaita-agent-turn' --allow-empty"],
            timeout=120)
        return {"patch_bytes": len(patch), "applied": applied}

    def _baseline(self, handle: WorkspaceHandle) -> str:
        if handle.data.get("baseline_sha"):
            return str(handle.data["baseline_sha"])
        code, out, _ = self.client.exec_argv(
            handle.id, ["cat", f"{handle.path}/.git/plaita-baseline"], timeout=60)
        if code != 0 or not out.strip():
            raise AgsError(f"沙箱缺基线文件（{handle.path}/.git/plaita-baseline）")
        return out.strip()


def _proxy_argv(instance_id: str, envfile: str, cwd: str, timeout_secs: int) -> list:
    return [sys.executable, "-m", "plaita_nodes.ags_exec",
            "--instance", str(instance_id), "--envfile", str(envfile),
            "--cwd", str(cwd), "--timeout", str(int(timeout_secs)), "--"]


# AGS 控制面凭据。代理（ags_exec）是**本地子进程**，其环境由 agentproc 组装：
# ``build_base_env()``（仅 INFRA 白名单 PATH/HOME/…）+ profile env + extra_env——
# **不继承宿主 env**（runner.py「The child env is built from exactly three layers」）。
# 所以凭据必须显式经 extra_env（本函数拿到的 env 正是这一层）下发，否则报
# 「ags_exec: 缺少 E2B_DOMAIN / E2B_API_KEY」（2026-10-08 远端实测 245 次，
# 整条沙箱流程空转）。密钥面：只进代理子进程，不进沙箱容器（容器 env 走 envfile）。
AGS_CREDENTIAL_ENV = ("E2B_DOMAIN", "E2B_API_KEY")


def should_sweep_paused(metadata: Optional[Dict[str, Any]], started_at: Any, *,
                        now: _dt.datetime, max_age_secs: int) -> bool:
    """纯决策：这条**暂停**实例该不该清（决策表测试专用，不碰网络）。

    ① 必须带自家 metadata 键（``plaita_execution`` / ``plaita_ws``）——同账号下
       他人的实例绝不碰；
    ② 必须能取到 ``started_at`` 且存活 ≥ ``max_age_secs``——刚暂停的实例可能马上
       被续跑 attach 回来（重投接力），不动。
    """
    md = metadata or {}
    if not (md.get(META_EXECUTION) or md.get(META_WS)):
        return False
    if started_at is None:
        return False
    if getattr(started_at, "tzinfo", None) is None:   # 容错：裸 datetime 按 UTC 读
        started_at = started_at.replace(tzinfo=_dt.timezone.utc)
    return (now - started_at).total_seconds() >= max_age_secs


def wrap_argv_from_env(env: Dict[str, str], agent_argv: list) -> list:
    """sandbox.py 分派入口：env 旋钮 → 代理 argv（与 docker/krunvm 同形）。

    副作用：把宿主 env 里的 AGS 凭据**补进** ``env``（= agentproc 的 extra_env
    层）；已有值不覆盖（spec/调用方显式指定优先）。
    """
    instance = env.get("PLAITA_SANDBOX_NAME") or ""
    if not instance:
        raise SandboxConfigError("ags 包装缺 PLAITA_SANDBOX_NAME（实例 id）")
    for key in AGS_CREDENTIAL_ENV:
        if not env.get(key) and os.environ.get(key):
            env[key] = os.environ[key]
    return _proxy_argv(
        instance,
        env.get("PLAITA_SANDBOX_ENVFILE") or "",
        env.get("PLAITA_SANDBOX_PATH") or AGS_SANDBOX_ROOT,
        int(env.get("PLAITA_SANDBOX_TIMEOUT") or 1800),
    ) + list(agent_argv)


# ── 宿主侧补丁工具（GIT_INDEX_FILE 临时索引，不触碰宿主正常索引）─────────

def _host_dirty_patch(host_repo: str) -> bytes:
    """宿主工作区未提交改动（含未跟踪）vs HEAD 的二进制补丁。

    用临时索引 ``git add -A`` 后 ``diff --cached``：宿主正常索引/工作区
    状态零触碰（评审同款纪律）。
    """
    with tempfile.TemporaryDirectory(prefix="plaita-ags-idx-") as td:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(td, "index")}
        add = subprocess.run(["git", "-C", host_repo, "add", "-A"],
                             env=env, capture_output=True)
        if add.returncode != 0:
            raise AgsError(f"宿主临时索引 add 失败：{add.stderr.decode('utf-8', 'replace')[:200]}")
        diff = subprocess.run(
            ["git", "-C", host_repo, "diff", "--cached", "HEAD", "--binary"],
            env=env, capture_output=True)
        if diff.returncode != 0:
            raise AgsError(f"宿主补丁生成失败：{diff.stderr.decode('utf-8', 'replace')[:200]}")
        return diff.stdout


def _apply_patch(host_repo: str, patch: bytes) -> bool:
    """把沙箱补丁应用到宿主 worktree（不改索引；冲突硬失败交上层）。"""
    with tempfile.NamedTemporaryFile(prefix="plaita-ags-out-", suffix=".patch",
                                     delete=False) as fh:
        fh.write(patch)
        path = fh.name
    try:
        res = subprocess.run(
            ["git", "-C", host_repo, "apply", "--whitespace=nowarn", path],
            capture_output=True)
        if res.returncode != 0:
            raise AgsError(
                f"sync_out 补丁应用到宿主失败：{res.stderr.decode('utf-8', 'replace')[:300]}")
        return True
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _make_payload(repo: str, rev: str, mode: str) -> tuple:
    """宿主产供给载荷：bundle（默认，含历史）或 archive（只树，大仓档）。

    返回 (bytes, rev_sha)。bundle 的 sha = rev 解析后的 commit（沙箱内 checkout）。
    """
    sha_res = subprocess.run(["git", "-C", repo, "rev-parse", rev],
                             capture_output=True, text=True)
    if sha_res.returncode != 0:
        raise AgsError(f"rev-parse {rev} 失败：{sha_res.stderr.strip()[:200]}")
    sha = sha_res.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="plaita-ags-payload-") as td:
        if mode == "archive":
            out = os.path.join(td, "ws.tgz")
            res = subprocess.run(
                ["bash", "-c",
                 f"git -C {shlex.quote(repo)} archive --format=tar.gz -o {shlex.quote(out)} "
                 f"{shlex.quote(sha)}"],
                capture_output=True)
            if res.returncode != 0:
                raise AgsError(f"git archive 失败：{res.stderr.decode('utf-8', 'replace')[:200]}")
        else:
            out = os.path.join(td, "ws.bundle")
            res = subprocess.run(
                ["git", "-C", repo, "bundle", "create", out, "--all"],
                capture_output=True)
            if res.returncode != 0:
                raise AgsError(f"git bundle 失败：{res.stderr.decode('utf-8', 'replace')[:200]}")
        data = Path(out).read_bytes()
    return data, sha


# import 即注册（与 docker 同先例；e2b 懒依赖）
register_driver("ags", AgsDriver())
