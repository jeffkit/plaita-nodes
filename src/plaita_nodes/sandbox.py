"""沙箱执行基础设施（设计方案：docs/sandbox-drivers-design.md v2.1，P1）。

模块位置（评审约束）：独立模块，仅被 ``agent_run`` 引用；
不 import 本包 ``config.py``（env 白名单由 agent_run 解析后以参数传入）、
不 import ``plaita.server``（workspace lease 为自包含实现，可注入 Redis 后端）。

核心概念（见设计文档 §3/§4/§6/§7）：

- ``WorkspaceSpec``：``.plaita/sandboxes.json`` infra 注册表条目（driver/image/provision/
  resources/env/egress）——定义权收归 infra，flow 只按名引用；
- ``WorkspaceHandle``：``ensure`` 产物，id 由 ``(execution_id, ws_key)`` 确定性派生，
  不靠 context 存活，崩溃后按名重派生即可重连；
- ``SANDBOX_DRIVERS``：driver 注册表（同 ``DECISION_PROVIDERS``/``NOTIFY_BACKENDS`` 先例）；
- ``Redactor``：canary 脱敏——注入 env 的值不得出现于日志/观测/checkpoint/错误信息；
- ``WorkspaceLease``：workspace 级租约（execution lease 只在节点间续约，堵 15 倍裸奔窗口）；
- ``register_sandbox_executor``：把任意 agentproc executor 的 build_args 包上
  「PLAITA_SANDBOX=1 时产出沙箱包裹 argv」——runner 仍是唯一执行者（设计 §1.6）。

driver 自身的数据面只经 git（``driver.git`` 白名单方法），无通用文件传输 API。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets as _secrets
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Protocol

__all__ = [
    "SANDBOX_NAME_PREFIX", "SANDBOX_ENV_FLAG", "SANDBOX_PATH",
    "SandboxError", "SandboxConfigError", "SandboxPinError", "SandboxLeaseError",
    "WorkspaceSpec", "WorkspaceHandle",
    "sanitize_ws_key", "handle_id", "resource_name",
    "load_sandboxes", "ensure_image_pinned",
    "Redactor", "write_envfile", "burn_envfile",
    "FileLeaseStore", "default_lease_store", "WorkspaceLease",
    "SANDBOX_DRIVERS", "register_driver", "get_driver", "load_entrypoint_drivers",
    "register_sandbox_executor", "sandbox_extra_env",
    "collect_workspace_snapshots", "wip_push_if_dirty",
    "suspend_release",
]

# 派生资源名前缀 + 沙箱内工作路径（对上层恒定，`--resume`/cwd 依赖它）
SANDBOX_NAME_PREFIX = "plaita-ws"
SANDBOX_PATH = "/work"
# build_args 沙箱开关：runner 的 env 里出现该键且为 "1" 时，包装层才包裹 argv
SANDBOX_ENV_FLAG = "PLAITA_SANDBOX"

_DOCKER_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]")


# ── 错误 ────────────────────────────────────────────────────────────────

class SandboxError(RuntimeError):
    """沙箱基础设施错误基类。"""


class SandboxConfigError(SandboxError):
    """注册表/规格配置非法。"""


class SandboxPinError(SandboxConfigError):
    """镜像/template 未 pin（digest 且未显式白名单）。"""


class SandboxLeaseError(SandboxError):
    """workspace 租约被他人持有（快速失败：不排队，交由运维/DLQ 重投）。"""


# ── 命名派生（确定性 = 断点续跑免费，设计 §1.4）─────────────────────────

def sanitize_ws_key(ws_key: str) -> str:
    """ws_key → docker 资源名合法字符（``[a-zA-Z0-9_.-]``），其余替换为 '-'。"""
    cleaned = _DOCKER_NAME_RE.sub("-", str(ws_key)).strip("-.") or "default"
    return cleaned


def handle_id(execution_id: str, ws_key: str) -> str:
    """handle 稳定 id：``{execution_id}:{ws_key}``。"""
    return f"{execution_id}:{ws_key}"


def renewal_timeout_secs(budget_secs: int, slack_secs: int = 900,
                         floor_secs: int = 1800) -> int:
    """沙箱实例续期时长 = **本次用量预算 + 收尾余量**（下限 ``floor_secs``）。

    为什么不是固定长寿命（旧式 ``max(budget*2, 3600)``）：流程正常结束时终态/挂起
    都会**显式释放**实例（flow_worker._release_sandboxes），TTL 只剩「崩溃兜底」
    一个作用——固定 4 小时会把一次崩溃的长尾放大成"跑完还挂几小时"。2026-10-08
    首夜实测：**~80% 沙箱花费是未释放闲置**（单实例 ¥0.4716/时）。

    「谁用实例谁续期」：agent 按自己的 wall 预算续，门禁按自己的门预算续，
    长 flow 靠每次调用续命，不会被 TTL 收走；无人续期的空闲实例 10 分钟内自然回收。
    """
    return max(int(budget_secs) + int(slack_secs), int(floor_secs))


def root_execution_id(execution: Any) -> str:
    """沙箱实例身份必须取**根执行**的 execution_id（沿 parent 链上溯）。

    引擎语义：``ExecutionContext`` 每次构造（含 ``child()``）都新铸
    ``$EXECUTION_ID``（plaita core/context.py）。因此 childflow 里的节点
    ``execution.execution_id`` 与主流程**不同**——用它派生
    ``(execution_id, ws_key)`` 会让子流程节点 ``ensure()`` 找不到主流程已建的
    实例，转而**新建一个空实例**（2026-10-07 实测：门禁在空工作区里跑，
    ``changed_files=0`` / "no tests ran"，且每调用泄漏一个沙箱）。

    同一 flow 内「多次 Agent 执行共用同一沙箱」的前提就是身份对整个 flow 稳定，
    故一律以根执行 id 为准。上溯链条对 ``FlowExecution`` 与 ``ExecutionContext``
    同型（两者都有 ``parent`` / ``execution_id``）。
    """
    cur = execution
    for _ in range(64):  # parent 链环保护
        parent = getattr(cur, "parent", None)
        if parent is None:
            break
        cur = parent
    return str(getattr(cur, "execution_id", "") or "")


def resource_name(execution_id: str, ws_key: str, kind: str) -> str:
    """确定性派生 docker 资源名（volume / container），同 key 必同名。"""
    suffix = sanitize_ws_key(f"{execution_id}-{ws_key}")[:180]
    name = f"{SANDBOX_NAME_PREFIX}-{kind}-{suffix}"
    return name[:220]


# ── 规格与句柄 ──────────────────────────────────────────────────────────

@dataclass
class WorkspaceSpec:
    """infra 注册表（``.plaita/sandboxes.json``）里一个 workspace 的定义。

    driver/image/template/provision(git.repo)/egress 为 infra 常量；
    仅 ``provision.git.branch`` 允许 flow 表达式（由首个引用节点 evaluate）。
    """

    name: str
    driver: str = "docker"
    image: str = ""
    template: str = ""
    provision: Dict[str, Any] = field(default_factory=dict)
    resources: Dict[str, Any] = field(default_factory=dict)
    env: Dict[str, Any] = field(default_factory=dict)
    egress: str = "bridge"          # bridge | none（白名单档后续档位）
    allow_unpinned: bool = False    # 显式白名单：豁免 digest pin 校验
    # ssh driver 专有（远端 VM 连接面；实验档）
    host: str = ""                  # 远端主机
    user: str = ""                  # 登录用户（缺省当前用户）
    port: int = 22
    identity: str = ""              # 私钥文件路径（${VAR} 插值可用；凭据引用后续）
    known_hosts: str = ""           # 缺省用 ~/.ssh/known_hosts（strict 校验）
    remote_root: str = "/srv/plaita-ws"   # 远端 workspace 根目录


@dataclass
class WorkspaceHandle:
    """``ensure`` 的产物。id/path 对上层恒定；data 为 driver 私有件。"""

    driver: str
    id: str
    path: str
    ws_key: str
    execution_id: str
    data: Dict[str, Any] = field(default_factory=dict)

    def snapshot(self, env_names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """观测快照（进 checkpoint 的部分：极小，且 env 只含名字不含值）。"""
        return {
            "ws_key": self.ws_key,
            "driver": self.driver,
            "id": self.id,
            "path": self.path,
            "env_names": sorted(env_names or []),
        }


# ── 注册表加载（自带加载器：plaita 原生目录，无 flowcast 遗留）──────────

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value: Any, environ: Dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _interpolate(v, environ) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, environ) for v in value]
    if isinstance(value, str):
        def _sub(match: "re.Match[str]") -> str:
            var = match.group(1)
            if var not in environ:
                raise SandboxConfigError(f"sandboxes 配置引用了缺失的环境变量 ${{{var}}}")
            return environ[var]
        return _ENV_PATTERN.sub(_sub, value)
    return value


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_config_file(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise SandboxConfigError(f"读取 {path} 需要 pyyaml：pip install pyyaml") from exc
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raise SandboxConfigError(f"不支持的配置格式: {path}")


def _parse_spec(name: str, body: Any, environ: Dict[str, str]) -> WorkspaceSpec:
    if not isinstance(body, dict):
        raise SandboxConfigError(f"sandbox '{name}' 配置必须是对象")
    body = _interpolate(body, environ)
    driver = str(body.get("driver") or "docker")
    image = str(body.get("image") or "")
    template = str(body.get("template") or "")
    if not image and not template and driver != "ssh":
        # ssh 的远端 VM 是既定环境（无镜像概念）；host 必填由 driver ensure 校验
        raise SandboxConfigError(f"sandbox '{name}' 缺少 image/template")
    provision = body.get("provision") or {}
    if not isinstance(provision, dict):
        raise SandboxConfigError(f"sandbox '{name}' provision 必须是对象")
    git = provision.get("git")
    if git is not None:
        if not isinstance(git, dict) or not git.get("repo"):
            raise SandboxConfigError(f"sandbox '{name}' provision.git 缺少 repo")
    env = body.get("env") or {}
    if not isinstance(env, dict):
        raise SandboxConfigError(f"sandbox '{name}' env 必须是对象")
    return WorkspaceSpec(
        name=name, driver=driver, image=image, template=template,
        provision=provision,
        resources=body.get("resources") or {},
        env=env,
        egress=str(body.get("egress") or "bridge"),
        allow_unpinned=bool(body.get("allow_unpinned", False)),
        host=str(body.get("host") or ""),
        user=str(body.get("user") or ""),
        port=int(body.get("port") or 22),
        identity=str(body.get("identity") or ""),
        known_hosts=str(body.get("known_hosts") or ""),
        remote_root=str(body.get("remote_root") or "/srv/plaita-ws"),
    )


def load_sandboxes(repo: Optional[str] = None,
                   environ: Optional[Dict[str, str]] = None) -> Dict[str, WorkspaceSpec]:
    """加载 infra 注册表：搜索顺序 ``~/.plaita → <repo>/.plaita``，深合并，后者覆盖。

    ``${VAR}`` 插值 fail-fast（缺失即 SandboxConfigError）。
    """
    environ = os.environ if environ is None else environ
    dirs = [Path.home() / ".plaita"]
    if repo:
        dirs.append(Path(repo) / ".plaita")
    merged: Dict[str, Any] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for suffix in (".json", ".yaml", ".yml"):
            path = directory / f"sandboxes{suffix}"
            if path.is_file():
                merged = _deep_merge(merged, _read_config_file(path))
                break
    raw = (merged or {}).get("sandboxes", merged)
    if not isinstance(raw, dict):  # pragma: no cover - 防御
        raise SandboxConfigError("sandboxes 配置顶层必须是对象")
    return {str(name): _parse_spec(str(name), body, environ)
            for name, body in raw.items()}


def ensure_image_pinned(spec: WorkspaceSpec) -> None:
    """供应链 pin 强校验：digest pin 或显式 ``allow_unpinned`` 白名单，否则拒绝
    （设计 §7.5 / conformance 第 8 条）。无镜像的 driver（ssh：远端 VM 是既定
    环境）不适用此校验。"""
    if not spec.image or spec.allow_unpinned or "@sha256:" in spec.image:
        return
    raise SandboxPinError(
        f"sandbox '{spec.name}' 的镜像未 pin（需要 @sha256: digest，"
        f"或在注册表条目里显式 allow_unpinned=true）：{spec.image or spec.template}")


# ── canary 脱敏（设计 §7.1 / conformance 第 4 条）───────────────────────

class Redactor:
    """对注入 env 的 value 做 canary 式替换：命中 → ``[REDACTED:<NAME>]``。

    作用于 on_line 行、ExecResult.stdout/stderr、错误信息三处（此后才进
    checkpoint/观测/日志）。短于 6 字符的值跳过（防灾难性误替换）。
    """

    MIN_SECRET_LEN = 6

    def __init__(self, env: Dict[str, str]):
        pairs = [(v, k) for k, v in (env or {}).items()
                 if isinstance(v, str) and len(v) >= self.MIN_SECRET_LEN]
        # 长值优先替换，避免短值先吃掉长值的子串
        self._pairs = sorted(pairs, key=lambda kv: -len(kv[0]))

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value, name in self._pairs:
            text = text.replace(value, f"[REDACTED:{name}]")
        return text


# ── envfile（0600 即焚，禁 -e VAR=VAL 进 argv，设计 §7/§5）──────────────

def write_envfile(env: Dict[str, str], directory: Optional[str] = None) -> Path:
    """写 docker ``--env-file``（0600）。调用方负责 finally 里 :func:`burn_envfile`。"""
    fd, tmp = tempfile.mkstemp(prefix="plaita-sandbox-env-", suffix=".env", dir=directory)
    path = Path(tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for key, value in (env or {}).items():
                fh.write(f"{key}={value}\n")
    except Exception:
        burn_envfile(path)
        raise
    return path


def burn_envfile(path: Optional[Path]) -> None:
    """即焚：忽略一切清理错误。"""
    if path is None:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


# ── workspace 级租约（设计 §6.5；自包含，可注入 Redis 后端）────────────

class LeaseStore(Protocol):
    def acquire(self, key: str, holder: str, ttl: float) -> bool: ...
    def renew(self, key: str, holder: str, ttl: float) -> bool: ...
    def release(self, key: str, holder: str) -> bool: ...


class FileLeaseStore:
    """单机文件租约：O_EXCL 原子创建 + 到期可抢占（过期读→删→重抢一次）。

    多 worker 同机场景够用；跨机部署注入 Redis 版 store（同款 SET NX EX 语义）。
    """

    def __init__(self, directory: Optional[Path] = None):
        self.directory = Path(directory) if directory else (
            Path.home() / ".plaita" / "sandbox-leases")
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.lease"

    @staticmethod
    def _write(path: Path, holder: str, ttl: float) -> None:
        payload = json.dumps({"holder": holder, "expires_at": time.time() + ttl})
        path.write_text(payload, encoding="utf-8")

    def acquire(self, key: str, holder: str, ttl: float) -> bool:
        path = self._path(key)
        for attempt in (0, 1):  # 第二次 = 过期抢占重试
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"holder": holder, "expires_at": time.time() + ttl}))
                return True
            except FileExistsError:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if float(data.get("expires_at", 0)) > time.time():
                        return False
                    path.unlink(missing_ok=True)  # 过期 → 抢占
                except (OSError, ValueError):
                    return False
        return False

    def renew(self, key: str, holder: str, ttl: float) -> bool:
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("holder") != holder or float(data.get("expires_at", 0)) <= time.time():
            return False
        try:
            self._write(path, holder, ttl)
            return True
        except OSError:  # pragma: no cover
            return False

    def release(self, key: str, holder: str) -> bool:
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if data.get("holder") != holder:
            return False
        try:
            path.unlink()
            return True
        except OSError:  # pragma: no cover
            return False


def default_lease_store() -> LeaseStore:
    return FileLeaseStore()


def _new_holder() -> str:
    token = base64.b32encode(_secrets.token_bytes(8)).decode("ascii").rstrip("=")
    return f"{socket.gethostname()}:{os.getpid()}:{token}"


class WorkspaceLease:
    """workspace 租约：acquire 快速失败（SandboxLeaseError），心跳续约贯穿 exec。

    TTL 覆盖沙箱墙钟 + 余量；心跳间隔 = TTL/3。
    """

    def __init__(self, store: LeaseStore, key: str, ttl: float,
                 holder: Optional[str] = None):
        self.store = store
        self.key = key
        self.ttl = float(ttl)
        self.holder = holder or _new_holder()
        self.lost = False
        self._heartbeat: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def acquire(self) -> None:
        if not self.store.acquire(self.key, self.holder, self.ttl):
            raise SandboxLeaseError(
                f"workspace 租约被他人持有（key={self.key}）："
                f"同一 workspace 禁止并发 exec；如为残留，请排查后重投/DLQ 回灌")

    def start_heartbeat(self) -> None:
        if self._heartbeat is not None:
            return
        interval = max(self.ttl / 3.0, 0.05)

        def _beat() -> None:
            while not self._stop.wait(interval):
                if not self.store.renew(self.key, self.holder, self.ttl):
                    self.lost = True  # 丢租约不中断执行：沙箱墙钟兜底

        self._heartbeat = threading.Thread(target=_beat, daemon=True,
                                           name=f"ws-lease-{self.key[:24]}")
        self._heartbeat.start()

    def release(self) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=2)
            self._heartbeat = None
        self.store.release(self.key, self.holder)

    def __enter__(self) -> "WorkspaceLease":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ── driver 注册表（同 DECISION_PROVIDERS / NOTIFY_BACKENDS 先例）────────

class SandboxDriver(Protocol):
    """driver 协议（设计 §3.1）。本地 argv 型不拥有 agent 进程的 exec——
    只产包裹 argv；agent 进程的执行者始终是 agentproc runner。"""

    def ensure(self, spec: WorkspaceSpec, execution_id: str, ws_key: str) -> WorkspaceHandle: ...
    def wrap_argv(self, handle: WorkspaceHandle, agent_argv: list, timeout_secs: int,
                  envfile: Path) -> list: ...
    def enforce(self, handle: WorkspaceHandle, force: bool = True) -> None: ...
    def git(self, handle: WorkspaceHandle, args: list,
            envfile: Optional[Path] = None) -> subprocess.CompletedProcess: ...
    def release(self, handle: WorkspaceHandle, keep_data: bool = True) -> None: ...


SANDBOX_DRIVERS: Dict[str, SandboxDriver] = {}


def register_driver(name: str, driver: SandboxDriver) -> None:
    SANDBOX_DRIVERS[name] = driver


def get_driver(name: str) -> Optional[SandboxDriver]:
    return SANDBOX_DRIVERS.get(name)


def load_entrypoint_drivers(group: str = "plaita_nodes.sandbox_drivers",
                            warn=None) -> int:
    """加载外部包注册的 driver（entry_points 组）。加载失败降级告警不炸注册表
    （评审 A-C3/C5）。返回成功加载数。"""
    loaded = 0
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group=group)
    except Exception as exc:  # pragma: no cover - 老解释器差异
        if warn:
            warn(f"sandbox driver entry_points 不可用：{exc}")
        return 0
    for ep in eps:
        try:
            driver = ep.load()
            register_driver(ep.name, driver)
            loaded += 1
        except Exception as exc:
            if warn:
                warn(f"sandbox driver '{ep.name}' 加载失败（忽略）：{exc}")
    return loaded


# ── executor 包装：runner 仍是唯一执行者（设计 §1.6/§3）────────────────

def register_sandbox_executor(base_name: str) -> str:
    """复制一个 agentproc executor 为 ``<base>-sandbox``：其 build_args 在
    ``PLAITA_SANDBOX=1`` 时把 argv 交给 driver 包裹层（wrap_argv_from_env），
    否则逐字节透传。基 executor 本体永不改动（非沙箱路径零影响）。"""
    from agentproc import EXECUTORS

    sandbox_name = f"{base_name}-sandbox"
    if sandbox_name in EXECUTORS:
        return sandbox_name
    base = EXECUTORS.get(base_name)
    if base is None:
        raise SandboxConfigError(f"executor '{base_name}' 未注册，无法创建沙箱变体")

    def make_handlers():
        make = base.get("make_handlers")
        handlers = make() if callable(make) else base
        base_build = handlers.get("build_args")
        if not callable(base_build):
            raise SandboxConfigError(f"executor '{base_name}' 没有 build_args")

        # `_ctx` 是 agentproc 的**可选**第 4 参（`{"permission": ...}`，随
        # `efc95e7`「fail-closed permission posture」以**位置参数**传入）：
        #   `build_args_fn(msg, sid, env, ctx)`   ← runner.py:934-938
        # 故包装层必须显式接收并**原样转发**，否则抛
        #   build_args() takes 3 positional arguments but 4 were given
        # ⇒ 整个 `<base>-sandbox` executor 不可用（2026-10-11 生产实证：
        #   近 1h 内 Mac 72 次 / VM 65 次该报错，是当期最大失败源）。
        # 注：`agent_run.py:150` 的 recursive 直调 build_args 是同一坑的另一处，
        # 已先行修复（f8bd3af）；此处是**沙箱包装层**的等价缺陷。
        def build_args(message, session_id, env, _ctx=None):
            argv = base_build(message, session_id, env, _ctx)
            if env.get(SANDBOX_ENV_FLAG) == "1":
                argv = wrap_agent_argv_from_env(env, list(argv))
            return argv

        return {**handlers, "build_args": build_args}

    EXECUTORS[sandbox_name] = {**base, "make_handlers": make_handlers}
    return sandbox_name


def wrap_agent_argv_from_env(env: Dict[str, str], agent_argv: list) -> list:
    """按 ``PLAITA_SANDBOX_DRIVER`` 把包裹分派给对应 driver 的 env 包装实现
    （缺省 docker——向后兼容旧旋钮环境）。未知 driver 拒绝裸跑。"""
    driver = env.get("PLAITA_SANDBOX_DRIVER", "docker")
    if driver == "docker":
        from .sandbox_docker import wrap_argv_from_env
        return wrap_argv_from_env(env, agent_argv)
    if driver == "krunvm":
        from .sandbox_krunvm import wrap_argv_from_env
        return wrap_argv_from_env(env, agent_argv)
    if driver == "ssh":
        from .sandbox_ssh import wrap_argv_from_env
        return wrap_argv_from_env(env, agent_argv)
    if driver == "ags":
        # AGS（腾讯云）：远程执行——包装产出代理 argv（本地 spawn、数据面转发
        # 远端沙箱执行），见 sandbox_ags
        from .sandbox_ags import wrap_argv_from_env
        return wrap_argv_from_env(env, agent_argv)
    raise SandboxConfigError(f"driver '{driver}' 没有 env 包装实现，拒绝裸跑 agent argv")


def sandbox_extra_env(handle: WorkspaceHandle, spec: WorkspaceSpec,
                      timeout_secs: int, envfile: Path,
                      execution_id: str) -> Dict[str, str]:
    """宿主侧 extra_env：只含沙箱旋钮与相关性 id（非密钥）。密钥只经 envfile
    进容器——宿主进程 env 的密钥面不扩大（评审 B-C1/D-C6）。"""
    return {
        SANDBOX_ENV_FLAG: "1",
        "PLAITA_SANDBOX_DRIVER": handle.driver,
        # 执行单元名：docker=容器名 / krunvm=VM 名（各 driver 的 wrap 自行解释）
        "PLAITA_SANDBOX_NAME": str(handle.data.get("container")
                                   or handle.data.get("vm") or ""),
        "PLAITA_SANDBOX_IMAGE": spec.image,
        # 挂载源：docker=宿主数据目录（bind mount）/ krunvm=宿主数据目录 /
        # 兼容键名 VOLUME（docker wrap 仍从该键读挂载源）
        "PLAITA_SANDBOX_VOLUME": str(handle.data.get("datadir")
                                     or handle.data.get("volume") or ""),
        "PLAITA_SANDBOX_PATH": handle.path,
        "PLAITA_SANDBOX_ENVFILE": str(envfile),
        "PLAITA_SANDBOX_TIMEOUT": str(int(timeout_secs)),
        "PLAITA_SANDBOX_NETWORK": spec.egress,
        "PLAITA_SANDBOX_ENTRYPOINT": str((spec.resources or {}).get("entrypoint")
                                         or "timeout"),
        "PLAITA_SANDBOX_CPUS": str(spec.resources.get("cpus", "")),
        "PLAITA_SANDBOX_MEMORY": str(spec.resources.get("memory", "")),
        # ssh driver 专有旋钮（其余 driver 忽略）：连接面 + 远端工作目录
        "PLAITA_SANDBOX_WSDIR": str(handle.data.get("wsdir", "")),
        "PLAITA_SANDBOX_TARGET": str(handle.data.get("target", "")),
        "PLAITA_SANDBOX_SSH_OPTS": "\x1f".join(handle.data.get("opts") or []),
        "PLAITA_EXECUTION_ID": execution_id,
        "PLAITA_WORKSPACE": handle.ws_key,
    }


# ── 数据目录 sidecar（docker/krunvm 数据层统一枚举机制，设计 §6.3）──────

def write_sidecar(datadir: Path, driver: str, execution_id: str, ws_key: str) -> None:
    """在数据目录写沙箱元数据（reaper 枚举/反查 execution）。

    位置优先 ``<datadir>/.git/plaita-sandbox.json``——藏在 git 目录内，
    ``git status`` 永远看不到（否则 sidecar 会被 wip 纪律提交进用户仓库）；
    非仓库工作区退回 ``<datadir>/.plaita-sandbox.json``。
    """
    datadir = Path(datadir)
    datadir.mkdir(parents=True, exist_ok=True)
    git_dir = datadir / ".git"
    sidecar = (git_dir / "plaita-sandbox.json") if git_dir.is_dir() else (
        datadir / ".plaita-sandbox.json")
    if not sidecar.exists():
        import time
        sidecar.write_text(json.dumps({
            "driver": driver, "execution_id": execution_id, "ws_key": ws_key,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }), encoding="utf-8")


_SIDECAR_NAMES = (".plaita-sandbox.json", ".git/plaita-sandbox.json")


def iter_sidecars(roots: Iterable[Path]) -> List[Dict[str, str]]:
    """扫描数据根下所有 sidecar，返回 [{driver, execution_id, ws_key, path}]。"""
    out: List[Dict[str, str]] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for name in _SIDECAR_NAMES:
            for sidecar in sorted(root.glob(f"*/{name}")):
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                out.append({"driver": str(meta.get("driver", "")),
                            "execution_id": str(meta.get("execution_id", "")),
                            "ws_key": str(meta.get("ws_key", "")),
                            "path": str(sidecar.parent)})
    return out


# ── 挂起/结束的数据面纪律（设计 §6.1；回调类在 P2 接线）────────────────

def collect_workspace_snapshots(context: Dict[str, Any]) -> list:
    """从 checkpoint 上下文（``$NODE.<id>.workspace`` 快照）收集 handle 集合——
    release/审计数据源，不依赖进程内存。"""
    snapshots = []
    nodes = (context or {}).get("$NODE") or {}
    if not isinstance(nodes, dict):
        return snapshots
    for info in nodes.values():
        ws = (info or {}).get("workspace") if isinstance(info, dict) else None
        if isinstance(ws, dict) and ws.get("id"):
            snapshots.append(ws)
    return snapshots


def _git_check_args(spec: WorkspaceSpec) -> tuple:
    git = (spec.provision or {}).get("git") or {}
    return bool(git.get("repo"))


def wip_push_if_dirty(driver: SandboxDriver, handle: WorkspaceHandle,
                      spec: WorkspaceSpec, redactor: Optional[Redactor] = None,
                      envfile: Optional[Path] = None) -> str:
    """release 前置纪律：工作区脏则强制 commit+push 到
    ``plaita/wip/{execution_id}/{ws_key}``（不靠 prompt 自觉，设计 §6.1）。

    返回 "clean" | "wip-pushed" | "no-git"。
    """
    if not _git_check_args(spec):
        return "no-git"
    status = driver.git(handle, ["status", "--porcelain"], envfile=envfile)
    if status.returncode != 0:
        raise SandboxError(
            f"dirty-check 失败：{(status.stderr or status.stdout)[-400:]}")
    if not (status.stdout or "").strip():
        return "clean"
    branch = f"plaita/wip/{handle.execution_id}/{sanitize_ws_key(handle.ws_key)}"
    msg = f"plaita: wip snapshot {handle.execution_id}:{handle.ws_key}"
    # 显式身份：镜像可能没有 git user 配置（容器内 root@host 不可自动推断）
    identity = ["-c", "user.name=plaita-sandbox",
                "-c", "user.email=plaita-sandbox@invalid"]
    for args in (["add", "-A"],
                 [*identity, "commit", "-m", msg],
                 ["push", "origin", f"HEAD:{branch}"]):
        step = driver.git(handle, args, envfile=envfile)
        if step.returncode != 0:
            raise SandboxError(f"wip push 的 git {args[-1]} 失败：{(step.stderr or '')[-400:]}")
    return "wip-pushed"


def suspend_release(driver: SandboxDriver, handle: WorkspaceHandle,
                    spec: WorkspaceSpec, redactor: Optional[Redactor] = None,
                    envfile: Optional[Path] = None,
                    keep_data: bool = True) -> str:
    """挂起/结束路径：先 wip 纪律再 release（挂起释放计算层、数据层保留）。"""
    outcome = wip_push_if_dirty(driver, handle, spec, redactor=redactor, envfile=envfile)
    driver.release(handle, keep_data=keep_data)
    return outcome
