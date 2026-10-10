"""sandbox_ags.py / ags_exec 单测（无网络：假 client + 真 git 的宿主侧函数）。

覆盖：代理 argv 形状（wrap_argv / wrap_argv_from_env）· ensure 幂等与供给 ·
供给载荷（bundle/archive，真 git）· 宿主补丁生成/应用（真 git，临时索引不
触碰宿主索引）· release 模式开关 · 节点字段互斥覆盖。AGS 实跑集成属
local/ags-poc 真机冒烟（smoke_driver.py），这里全用假 client。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from plaita_nodes import sandbox as sb
from plaita_nodes import sandbox_ags as sa
from plaita_nodes.sandbox import SandboxConfigError, WorkspaceSpec


# ── 假 client（记录调用，内存实例表）────────────────────────────────────

class FakeClient(sa.AgsClient):
    def __init__(self):
        super().__init__(domain="fake", api_key="fake")
        self.instances = {}
        self.calls = []
        self.exec_log = []

    def find(self, execution_id, ws_key):
        return self.instances.get((execution_id, ws_key))

    def create(self, spec, execution_id, ws_key):
        iid = f"fake-{len(self.instances) + 1}"
        self.instances[(execution_id, ws_key)] = iid
        self.calls.append(("create", iid, spec.template))
        return iid

    def kill(self, instance_id):
        self.calls.append(("kill", instance_id))

    def pause(self, instance_id):
        self.calls.append(("pause", instance_id))

    def set_timeout(self, instance_id, secs):
        self.calls.append(("set_timeout", instance_id, secs))

    def exec_argv(self, instance_id, argv, **kw):
        self.exec_log.append((instance_id, list(argv)))
        joined = " ".join(str(a) for a in argv)
        if "plaita-baseline" in joined and "test -f" in joined:
            return 0, "", ""      # 已供给
        return 0, "", ""

    def put_bytes(self, instance_id, remote_path, data, chunk=sa.UPLOAD_CHUNK):
        self.calls.append(("put_bytes", instance_id, remote_path, len(data)))
        return {"bytes": len(data), "secs": 0.0}


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def host_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.txt").write_text("two\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


# ── 代理 argv 形状 ─────────────────────────────────────────────────────

def test_proxy_argv_shape():
    argv = sa._proxy_argv("inst-1", "/tmp/e.env", "/work", 600)
    assert argv[:3] == [sys.executable, "-m", "plaita_nodes.ags_exec"]
    assert argv[argv.index("--instance") + 1] == "inst-1"
    assert argv[argv.index("--envfile") + 1] == "/tmp/e.env"
    assert argv[argv.index("--cwd") + 1] == "/work"
    assert argv[argv.index("--timeout") + 1] == "600"
    assert argv[-1] == "--"


def test_wrap_argv_appends_agent_argv():
    driver = sa.AgsDriver(client=FakeClient())
    handle = sb.WorkspaceHandle(driver="ags", id="inst-9", path="/work",
                                ws_key="main", execution_id="e1")
    argv = driver.wrap_argv(handle, ["recursive", "run", "hi"], 300, Path("/tmp/e.env"))
    assert argv[-3:] == ["recursive", "run", "hi"]
    assert "--instance" in argv and "inst-9" in argv


def test_wrap_argv_from_env_dispatch():
    env = {sb.SANDBOX_ENV_FLAG: "1", "PLAITA_SANDBOX_DRIVER": "ags",
           "PLAITA_SANDBOX_NAME": "inst-7", "PLAITA_SANDBOX_ENVFILE": "/tmp/x.env",
           "PLAITA_SANDBOX_PATH": "/work", "PLAITA_SANDBOX_TIMEOUT": "120"}
    argv = sb.wrap_agent_argv_from_env(env, ["recursive", "--version"])
    assert argv[1:3] == ["-m", "plaita_nodes.ags_exec"]
    assert argv[-2:] == ["recursive", "--version"]


def test_wrap_argv_from_env_requires_instance():
    env = {"PLAITA_SANDBOX_DRIVER": "ags"}
    with pytest.raises(SandboxConfigError):
        sb.wrap_agent_argv_from_env(env, ["recursive"])


def test_wrap_argv_from_env_injects_ags_credentials(monkeypatch):
    """代理进程跑在 agentproc 白名单子进程环境里（build_base_env 不继承宿主
    env）——E2B 凭据必须由 wrap 副作用补进 extra_env（2026-10-08 远端 245 次
    「ags_exec: 缺少 E2B_DOMAIN / E2B_API_KEY」的根因）。已有值不覆盖。"""
    monkeypatch.setenv("E2B_DOMAIN", "d.example")
    monkeypatch.setenv("E2B_API_KEY", "k-123")
    env = {"PLAITA_SANDBOX_DRIVER": "ags", "PLAITA_SANDBOX_NAME": "inst-1",
           "PLAITA_SANDBOX_PATH": "/work"}
    argv = sb.wrap_agent_argv_from_env(env, ["recursive", "--version"])
    assert argv[-2:] == ["recursive", "--version"]
    assert env["E2B_DOMAIN"] == "d.example" and env["E2B_API_KEY"] == "k-123"

    # 显式指定优先（未被宿主 env 覆盖）
    env2 = {"PLAITA_SANDBOX_DRIVER": "ags", "PLAITA_SANDBOX_NAME": "inst-2",
            "E2B_API_KEY": "explicit-key"}
    sb.wrap_agent_argv_from_env(env2, ["recursive"])
    assert env2["E2B_API_KEY"] == "explicit-key"


def test_wrap_argv_from_env_missing_creds_still_wraps(monkeypatch):
    """宿主也没有凭据时不在这里抛错——代理自身会给出可操作的报错，
    wrap 保持纯包装语义（避免破坏无需数据面的用例）。"""
    monkeypatch.delenv("E2B_DOMAIN", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    env = {"PLAITA_SANDBOX_DRIVER": "ags", "PLAITA_SANDBOX_NAME": "inst-3"}
    argv = sb.wrap_agent_argv_from_env(env, ["recursive"])
    assert "ags_exec" in " ".join(argv)
    assert "E2B_API_KEY" not in env


# ── ensure 幂等 / 供给 ─────────────────────────────────────────────────

def test_ensure_creates_then_attaches(host_repo):
    client = FakeClient()
    driver = sa.AgsDriver(client=client)
    spec = WorkspaceSpec(name="s", driver="ags", template="tpl",
                         provision={"git": {"repo": str(host_repo)}})
    h1 = driver.ensure(spec, "exec-1", "main")
    h2 = driver.ensure(spec, "exec-1", "main")
    assert h1.id == h2.id                       # 幂等 attach
    assert h1.data["fresh"] is True and h2.data["fresh"] is False
    creates = [c for c in client.calls if c[0] == "create"]
    assert len(creates) == 1                    # 只创建一次
    puts = [c for c in client.calls if c[0] == "put_bytes"]
    assert len(puts) == 1                       # 供给只上传一次


def test_ensure_without_provision_makes_workspace(tmp_path):
    client = FakeClient()
    driver = sa.AgsDriver(client=client)
    spec = WorkspaceSpec(name="s", driver="ags", template="tpl")
    driver.ensure(spec, "exec-2", "main")
    joined = [" ".join(str(a) for a in argv) for _, argv in client.exec_log]
    assert any("mkdir" in j for j in joined)


def test_ensure_missing_template_fails():
    client = FakeClient()
    driver = sa.AgsDriver(client=client)
    spec = WorkspaceSpec(name="s", driver="ags", template="")
    with pytest.raises(sa.AgsError, match="template"):
        driver.ensure(spec, "exec-3", "main")


# ── release 模式 ───────────────────────────────────────────────────────

def test_release_keep_data_drives_pause_vs_kill():
    """release 的 pause/kill **由调用方的 keep_data 决定**（协议原意）。

    语义修正（2026-10-07）：此前由 spec.keep_release 单值决定 → 失败路径想
    保现场也被全局 kill 覆盖。现：keep_data=True→pause（保实例磁盘可恢复）、
    False→kill；spec.keep_release 仅作无 spec 时的缺省。调用方据此分流：
    正常完成（已 sync_out 回 Git）→kill，失败/挂起（中间态只在沙箱）→pause。
    """
    client = FakeClient()
    driver = sa.AgsDriver(client=client)
    handle = sb.WorkspaceHandle(driver="ags", id="i1", path="/w", ws_key="main",
                                execution_id="e")
    spec_kill = WorkspaceSpec(name="s", driver="ags",
                              resources={"keep_release": "kill"})
    # keep_data=True 显式保数据 → 即便 spec 缺省是 kill，也必须 pause
    driver.release(handle, keep_data=True, spec=spec_kill)
    assert ("pause", "i1") in client.calls
    # keep_data=False（正常完成）→ kill
    driver.release(handle, keep_data=False, spec=spec_kill)
    assert ("kill", "i1") in client.calls
    # 无 spec 时退回 spec 缺省语义（无 spec → 走 code 里的缺省分支）
    driver.release(handle, keep_data=False, spec=None)
    assert ("kill", "i1") in client.calls


# ── 供给载荷（真 git）──────────────────────────────────────────────────

def test_make_payload_bundle_contains_history(host_repo):
    data, sha = sa._make_payload(str(host_repo), "HEAD", "bundle")
    assert data.startswith(b"# v2 git bundle")
    assert sha == _git(host_repo, "rev-parse", "HEAD").strip()
    # bundle 可克隆（含 HEAD 的 --all 形态，实测坑位回归）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        Path(td, "x.bundle").write_bytes(data)
        out = subprocess.run(["git", "clone", "-q", str(Path(td, "x.bundle")),
                              str(Path(td, "clone"))], capture_output=True)
        assert out.returncode == 0
        assert _git(Path(td, "clone"), "log", "--oneline").strip()


def test_make_payload_archive_tree_only(host_repo):
    data, sha = sa._make_payload(str(host_repo), "HEAD", "archive")
    assert data[:2] == b"\x1f\x8b"  # gzip
    assert len(sha) == 40


# ── 宿主补丁（真 git，临时索引不触碰宿主状态）───────────────────────

def test_host_dirty_patch_and_apply_roundtrip(host_repo):
    (host_repo / "a.txt").write_text("one\nchanged\n")
    (host_repo / "new.txt").write_text("brand new\n")
    patch = sa._host_dirty_patch(str(host_repo))
    assert b"a.txt" in patch and b"new.txt" in patch
    # 宿主索引零触碰：git status 仍显示未暂存
    status = _git(host_repo, "status", "--porcelain")
    assert " M a.txt" in status or "M a.txt" in status
    # 应用到另一份克隆（模拟宿主 worktree 落补丁）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "clone", "-q", str(host_repo), str(Path(td, "w"))],
                       check=True, capture_output=True)
        _git(Path(td, "w"), "reset", "-q", "--hard", "HEAD")
        sa._apply_patch(str(Path(td, "w")), patch)
        assert (Path(td, "w") / "new.txt").read_text() == "brand new\n"
        assert "changed" in (Path(td, "w") / "a.txt").read_text()
        # 应用不改索引/不自动提交
        assert _git(Path(td, "w"), "status", "--porcelain").count("\n") >= 2


def test_apply_patch_conflict_raises(host_repo):
    (host_repo / "a.txt").write_text("one\nchanged\n")
    patch = sa._host_dirty_patch(str(host_repo))
    # 目标侧同文件已有冲突内容
    (host_repo / "a.txt").write_text("one\nCONFLICTING\nchanged\n")
    with pytest.raises(sa.AgsError, match="应用"):
        sa._apply_patch(str(host_repo), patch)


# ── 节点面 ─────────────────────────────────────────────────────────────

def test_sandbox_agent_node_validate_and_dry():
    from plaita_nodes.sandbox_agent import SandboxAgentNode
    node = SandboxAgentNode(id="n1", prompt="do it", repo="/tmp/wt", sandbox="ags", dry_run=True)

    class _Exec:
        execution_id = "e1"
        cancel_event = None

        def evaluate(self, v):
            return v

        def get_global_variable(self, k, d=None):
            return True if k == "dry_run" else d

    out = node.execute(_Exec())
    assert out["dry_run"] is True
    assert "ags sandbox" in out["text"]

    with pytest.raises(Exception):
        SandboxAgentNode(id="n2", repo="/tmp/wt").validate()   # 缺 prompt


# ── 实例身份：子流程上下文必须上溯到根执行 ─────────────────────────────

def test_root_execution_id_walks_up_to_root():
    """真实引擎：childflow 上下文会新铸 ``$EXECUTION_ID``（差异真实存在），
    而沙箱身份 ``(execution_id, ws_key)`` 一律取**根执行** id——否则子流程里的
    ``ensure()`` 找不到 agent 已建的实例，转而新建空沙箱（2026-10-07 实测根因：
    gate_once 里的门禁在空工作区跑成 ``changed_files=0`` / "no tests ran"，
    且每调用泄漏一个实例）。"""
    pytest.importorskip("plaita", reason="plaita 引擎不可导入时跳过（本包测试可独立跑）")
    from plaita.core.executor import FlowExecution

    root = FlowExecution()
    root.clean()
    child = root.get_child_execution()

    assert child.execution_id != root.execution_id      # 子上下文确实另铸
    assert sb.root_execution_id(child) == root.execution_id
    assert sb.root_execution_id(root) == root.execution_id


def test_preserve_scene_pauses_instead_of_killing():
    """失败/取消路径**一律 pause**（用户口径「不能都 kill」+ 续跑原地接力）。

    实测依据：`Sandbox.connect(paused)` 自动恢复且工作区完好 ⇒ 重启/重投打断的 run
    能 attach 回同一实例继续；旧行为「synced 成功即 kill」会让每次重启都白丢沙箱。
    """
    from plaita_nodes.sandbox import WorkspaceHandle
    from plaita_nodes.sandbox_agent import SandboxAgentNode

    calls: list = []

    class _Client:
        def pause(self, iid):
            calls.append(("pause", iid))

    class _Driver:
        client = _Client()

        def release(self, handle, keep_data=True):   # 不该被调到
            calls.append(("release", handle.id, keep_data))

    handle = WorkspaceHandle(driver="ags", id="inst-1", path="/work",
                             ws_key="main", execution_id="e1")
    SandboxAgentNode._preserve_scene(_Driver(), handle)
    assert calls == [("pause", "inst-1")]

    # 无 pause 能力的 driver（本地 docker 等）→ release(keep_data=True) 保数据
    calls.clear()

    class _DriverNoPause:
        def release(self, handle, keep_data=True):
            calls.append(("release", handle.id, keep_data))

    SandboxAgentNode._preserve_scene(_DriverNoPause(), handle)
    assert calls == [("release", "inst-1", True)]


def test_should_sweep_paused_decision_table():
    """暂停清扫决策：只清「自家 metadata + 超龄」的实例（配额安全带）。

    背景：失败/取消路径改为一律 pause 保现场后，没人续跑的暂停实例会累积占配额
    （AGS ~20）。暂停不计计算力费，但配额满会让新建失败，所以要有这条清扫。
    """
    import datetime as dt

    now = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
    old = now - dt.timedelta(hours=7)
    young = now - dt.timedelta(minutes=5)
    mine = {sa.META_EXECUTION: "e1", sa.META_WS: "main"}

    assert sa.should_sweep_paused(mine, old, now=now, max_age_secs=6 * 3600) is True
    assert sa.should_sweep_paused(mine, young, now=now, max_age_secs=6 * 3600) is False
    assert sa.should_sweep_paused({"other": "x"}, old, now=now, max_age_secs=0) is False
    assert sa.should_sweep_paused(None, old, now=now, max_age_secs=0) is False
    assert sa.should_sweep_paused(mine, None, now=now, max_age_secs=0) is False
    naive = (now - dt.timedelta(hours=7)).replace(tzinfo=None)   # 裸 datetime 容错
    assert sa.should_sweep_paused(mine, naive, now=now, max_age_secs=6 * 3600) is True


def test_sweep_paused_kills_only_own_stale(monkeypatch):
    """sweep_paused 端到端（假 e2b 模块）：只 kill 自家超龄暂停实例。"""
    import datetime as dt
    import sys
    import types

    now = dt.datetime.now(dt.timezone.utc)
    entries = [
        types.SimpleNamespace(sandbox_id="mine-old", metadata={sa.META_EXECUTION: "e1"},
                              started_at=now - dt.timedelta(hours=7)),
        types.SimpleNamespace(sandbox_id="mine-new", metadata={sa.META_WS: "main"},
                              started_at=now - dt.timedelta(minutes=3)),
        types.SimpleNamespace(sandbox_id="theirs", metadata={"someone": "else"},
                              started_at=now - dt.timedelta(days=2)),
    ]

    class _FakePaginator:
        def next_items(self):
            return entries

    fake = types.ModuleType("e2b_code_interpreter")
    fake.Sandbox = types.SimpleNamespace(list=lambda q=None: _FakePaginator())
    fake.SandboxQuery = lambda **kw: kw
    fake.SandboxState = types.SimpleNamespace(PAUSED="paused", RUNNING="running")
    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", fake)

    client = FakeClient()
    driver = sa.AgsDriver(client=client)
    killed = driver.sweep_paused(max_age_secs=6 * 3600)
    assert killed == ["mine-old"]
    assert [c for c in client.calls if c[0] == "kill"] == [("kill", "mine-old")]


def test_renewal_timeout_is_budget_plus_slack_not_fixed_long_life():
    """续期 = 本次预算 + 余量（下限兜底），不是固定长寿命。

    2026-10-08 首夜实测 ~80% 沙箱花费是「跑完没释放」的闲置：旧式
    ``max(budget*2, 3600)`` 在 impl（7200s 预算）上把实例续成 **4 小时**；
    终态/挂起已能显式释放后，TTL 只该在崩溃兜底时兑现，故收紧为
    budget + slack。
    """
    assert sb.renewal_timeout_secs(7200) == 8100          # impl：2h 预算 → 2h15m
    assert sb.renewal_timeout_secs(600) == 1800           # 短预算吃下限
    assert sb.renewal_timeout_secs(60, slack_secs=600, floor_secs=900) == 900
    assert sb.renewal_timeout_secs(7200) < 4 * 3600       # 明显短于旧行为的 4h


def test_root_execution_id_without_parent_and_without_id():
    class _Stub:
        execution_id = "solo"

    assert sb.root_execution_id(_Stub()) == "solo"      # 无 parent = 自己是根
    assert sb.root_execution_id(object()) == ""          # 无 id → 空串（调用方 fail-closed）


def test_host_locked_executor_refused_in_sandbox():
    """宿主锁定型 CLI（cursor）必须被沙箱路径 fail-closed 拒绝。

    2026-10-11 评审发现：cursor 是「宿主 CLI + 本机登录态」，进沙箱必失败，
    且失败形态误导（容器没装 → command not found；或装了但无登录态；或
    沙箱 env 白名单丢 `CURSOR_MODEL` → 静默退回默认模型）。
    显式拒绝把「静默错跑」变成「响亮失败」。
    """
    from plaita_nodes.agent_run import _host_locked_executors

    locked = _host_locked_executors()
    assert "cursor" in locked, "cursor 必须被登记为宿主锁定型（进沙箱应拒绝）"
    # 沙箱可承载的 executor 不能误伤
    for ok in ("recursive", "claude-code", "codex"):
        assert ok not in locked, f"{ok} 不应被误列为宿主锁定型"


def test_host_locked_executors_env_override(monkeypatch):
    """env 可覆盖集合（测试/灰度用）。"""
    from plaita_nodes.agent_run import _host_locked_executors

    monkeypatch.setenv("PLAITA_HOST_LOCKED_EXECUTORS", "cursor,foo")
    assert _host_locked_executors() == frozenset({"cursor", "foo"})
