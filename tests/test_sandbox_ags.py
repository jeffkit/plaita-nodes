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


def test_root_execution_id_without_parent_and_without_id():
    class _Stub:
        execution_id = "solo"

    assert sb.root_execution_id(_Stub()) == "solo"      # 无 parent = 自己是根
    assert sb.root_execution_id(object()) == ""          # 无 id → 空串（调用方 fail-closed）
