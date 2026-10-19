"""生命周期回调（lifecycle.py）与 status-aware reaper（sandbox_reaper.py）单测。"""
from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import plaita_nodes.sandbox as sb
from plaita_nodes.lifecycle import SandboxLifecycleCallback
from plaita_nodes.sandbox import WorkspaceSpec, handle_id
from plaita_nodes.sandbox_reaper import decide, run_cycle

from fake_exec import FakeExecution
from test_sandbox import RecordingDriver

NOW = _dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
SPEC = WorkspaceSpec(name="main", driver="recording",
                     image="reg/img@sha256:abc",
                     provision={"git": {"repo": "https://x/y.git"}})


def _snapshot(exec_id="e1", ws="main"):
    return {"ws_key": ws, "driver": "recording", "id": handle_id(exec_id, ws),
            "path": "/work", "env_names": []}


def _dirty_driver():
    return RecordingDriver(git_results=[
        subprocess.CompletedProcess([], 0, stdout=" M f.py\n", stderr=""),
    ])


# ── SandboxLifecycleCallback ────────────────────────────────────────────

class TestLifecycleCallback:
    def test_suspend_dirty_releases_with_wip(self):
        driver = _dirty_driver()
        cb = SandboxLifecycleCallback(sandboxes={"main": SPEC}, drivers={"recording": driver})
        cb.on_node_end(flow=None, node=None, result={"text": "x", "workspace": _snapshot()})
        cb.on_flow_suspend(flow=None)
        assert cb.events == [{"phase": "suspend", "id": handle_id("e1", "main"),
                              "outcome": "wip-pushed"}]
        actions = [c[0] for c in driver.calls]
        assert actions[0] == "ensure"
        assert "release" in actions and "enforce" not in actions  # 挂起不强制击杀

    def test_end_clean_releases_keep_data(self):
        driver = RecordingDriver()
        cb = SandboxLifecycleCallback(sandboxes={"main": SPEC}, drivers={"recording": driver})
        cb.on_node_end(flow=None, node=None, result={"workspace": _snapshot()})
        cb.on_flow_end(flow=None, result=None)
        assert cb.events[0]["phase"] == "end"
        assert cb.events[0]["outcome"] == "clean"
        assert driver.calls[-1] == ("release", handle_id("e1", "main"), True)

    def test_unregistered_workspace_left_to_reaper(self):
        driver = RecordingDriver()
        cb = SandboxLifecycleCallback(sandboxes={}, drivers={"recording": driver})
        cb.on_node_end(flow=None, node=None, result={"workspace": _snapshot()})
        cb.on_flow_end(flow=None)
        assert cb.events[0]["outcome"].startswith("no-spec")
        assert driver.calls == []

    def test_single_failure_does_not_block_others(self):
        class BoomDriver(RecordingDriver):
            def ensure(self, spec, execution_id, ws_key):
                if ws_key == "boom":
                    raise RuntimeError("boom")
                return super().ensure(spec, execution_id, ws_key)

        driver = BoomDriver()
        cb = SandboxLifecycleCallback(
            sandboxes={"main": SPEC, "boom": SPEC},
            drivers={"recording": driver})
        cb.on_node_end(flow=None, node=None, result={"workspace": _snapshot("e1", "boom")})
        cb.on_node_end(flow=None, node=None, result={"workspace": _snapshot("e1", "main")})
        cb.on_flow_end(flow=None)
        outcomes = {e["id"]: e["outcome"] for e in cb.events}
        assert outcomes[handle_id("e1", "boom")].startswith("error:")
        assert outcomes[handle_id("e1", "main")] == "clean"

    def test_non_workspace_results_ignored(self):
        cb = SandboxLifecycleCallback(sandboxes={}, drivers={})
        cb.on_node_end(flow=None, node=None, result={"text": "plain"})
        cb.on_flow_suspend(flow=None)
        assert cb.events == []


# ── reaper 决策表（纯函数）──────────────────────────────────────────────

class TestDecide:
    def test_matrix(self):
        def state(status, last_update=None):
            return SimpleNamespace(execution_id="e1", status=status,
                                   last_update_time=last_update)
        stale_ts = (NOW - _dt.timedelta(minutes=60)).isoformat()
        fresh_ts = (NOW - _dt.timedelta(minutes=1)).isoformat()
        assert decide(None, stale_minutes=30, now=NOW) == "reap-unknown"
        assert decide(state("suspended"), stale_minutes=30, now=NOW) == "skip-suspended"
        assert decide(state("running", fresh_ts), stale_minutes=30, now=NOW) == "skip-fresh"
        assert decide(state("running", stale_ts), stale_minutes=30, now=NOW) == "reap-zombie"
        assert decide(state("running"), stale_minutes=30, now=NOW) == "reap-zombie"  # 无时间戳=陈旧
        assert decide(state("completed"), stale_minutes=30, now=NOW) == "reap-terminal"
        assert decide(state("error"), stale_minutes=30, now=NOW) == "reap-terminal"


class FakeStorage:
    def __init__(self, states):
        self._states = states

    def list_executions(self, limit=100):
        return self._states


def _state(exec_id, status, minutes_ago=0):
    return SimpleNamespace(
        execution_id=exec_id, status=status,
        last_update_time=(NOW - _dt.timedelta(minutes=minutes_ago)).isoformat())


# ── run_cycle（决策 → 动作）─────────────────────────────────────────────

class TestRunCycle:
    def _resources(self):
        return [{"driver": "recording", "execution_id": "zombie", "ws_key": "main"},
                {"driver": "recording", "execution_id": "waiter", "ws_key": "main"},
                {"driver": "recording", "execution_id": "done", "ws_key": "main"},
                {"driver": "recording", "execution_id": "ghost", "ws_key": "ghost"}]

    def test_full_matrix_actions(self):
        driver = _dirty_driver()
        sb.register_driver("recording", driver)
        try:
            storage = FakeStorage([
                _state("zombie", "running", minutes_ago=120),    # 僵尸
                _state("waiter", "suspended", minutes_ago=120),  # 合法挂起
                _state("done", "completed", minutes_ago=5),      # 终态孤儿
            ])
            results = run_cycle(storage, self._resources(), stale_minutes=30, now=NOW,
                                spec_lookup=lambda key: SPEC if key == "main" else None)
        finally:
            sb.SANDBOX_DRIVERS.pop("recording", None)

        by_exec = {r["execution_id"]: r for r in results}
        assert by_exec["zombie"]["decision"] == "reap-zombie"
        assert by_exec["zombie"]["outcome"] == "reaped:wip-pushed"
        assert by_exec["waiter"]["decision"] == "skip-suspended"
        assert by_exec["waiter"]["outcome"] == ""
        assert by_exec["done"]["decision"] == "reap-terminal"
        assert by_exec["done"]["outcome"] == "reaped:clean"
        assert by_exec["ghost"]["decision"] == "reap-unknown"
        assert by_exec["ghost"]["outcome"].startswith("no-spec-skip")
        # 僵尸回收动作链：enforce 先断计算面，再 wip 留档，最后 release
        assert ("enforce", handle_id("zombie", "main")) in driver.calls
        assert driver.calls[-1][0] == "release"

    def test_dry_run_no_action(self):
        driver = RecordingDriver()
        sb.register_driver("recording", driver)
        try:
            storage = FakeStorage([_state("zombie", "running", minutes_ago=120)])
            results = run_cycle(storage, self._resources()[:1], stale_minutes=30,
                                now=NOW, spec_lookup=lambda key: SPEC, dry_run=True)
        finally:
            sb.SANDBOX_DRIVERS.pop("recording", None)
        assert results[0]["decision"] == "reap-zombie"
        assert driver.calls == []

    def test_storage_none_treats_all_unowned(self):
        driver = RecordingDriver()
        sb.register_driver("recording", driver)
        try:
            results = run_cycle(None, self._resources()[:1], stale_minutes=30,
                                now=NOW, spec_lookup=lambda key: SPEC)
        finally:
            sb.SANDBOX_DRIVERS.pop("recording", None)
        assert results[0]["decision"] == "reap-unknown"
        assert results[0]["outcome"] == "reaped:clean"


# ── 枚举（docker labels / krunvm sidecar）───────────────────────────────

class TestEnumerations:
    def test_docker_sidecar_enumeration(self, tmp_path):
        import plaita_nodes.sandbox_docker as sd

        data = tmp_path / "sandbox-data"
        wsdir = data / "plaita-ws-data-e1-main"
        wsdir.mkdir(parents=True)
        (wsdir / ".plaita-sandbox.json").write_text(json.dumps(
            {"driver": "docker", "execution_id": "e1", "ws_key": "main"}),
            encoding="utf-8")
        items = sd.enumerate_workspaces(data_root=str(data))
        assert items == [{"driver": "docker", "execution_id": "e1",
                          "ws_key": "main", "path": str(wsdir)}]

    def test_krunvm_sidecar(self, tmp_path):
        import plaita_nodes.sandbox_krunvm as sk

        data = tmp_path / "sandbox-data"
        wsdir = data / "plaita-ws-data-e1-main"
        wsdir.mkdir(parents=True)
        (wsdir / ".plaita-sandbox.json").write_text(json.dumps(
            {"driver": "krunvm", "execution_id": "e1", "ws_key": "main"}),
            encoding="utf-8")
        (data / "no-meta").mkdir()
        items = sk.enumerate_workspaces(data_root=str(data))
        assert items == [{"driver": "krunvm", "name": "plaita-ws-data-e1-main",
                          "execution_id": "e1", "ws_key": "main"}]

    def test_krunvm_ensure_writes_sidecar(self, tmp_path):
        from plaita_nodes.sandbox_krunvm import KrunvmDriver

        recorder: list = []

        def run(argv, timeout, cwd=None):
            recorder.append(argv)
            out = "krun-e2e\n" if argv[:2] == ["krunvm", "list"] else ""
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        driver = KrunvmDriver(data_root=str(tmp_path), run_fn=run)
        driver.ensure(WorkspaceSpec(name="main", driver="krunvm",
                                    image="alpine:latest", allow_unpinned=True),
                      "e1", "main")
        datadir = tmp_path / "plaita-ws-data-e1-main"
        meta = json.loads((datadir / ".plaita-sandbox.json").read_text(encoding="utf-8"))
        assert meta == {"driver": "krunvm", "execution_id": "e1", "ws_key": "main",
                        "created_at": meta["created_at"]}


# ── git_publish 沙箱绊线（设计 §11）─────────────────────────────────────

class TestGitPublishTripwire:
    def _node(self, tmp_path):
        from plaita_nodes.git_publish import GitPublishNode

        repo = tmp_path / "wt"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(repo)], capture_output=True)
        return GitPublishNode(id="g", worktree_dir=str(repo), branch_name="x",
                              merge_mode="none")  # 只 commit 不 push，仓库无提交即安全返回

    def test_warns_when_flow_has_workspace_snapshot(self, tmp_path):
        from plaita_nodes.git_publish import GitPublishNode

        class CtxExec(FakeExecution):
            context = {"$NODE": {"writer": {"workspace": _snapshot()}}}

        node = self._node(tmp_path)
        with pytest.warns(RuntimeWarning, match="沙箱 workspace 快照"):
            node.execute(CtxExec())

    def test_no_warning_without_workspace(self, tmp_path):
        import warnings as _warnings
        from plaita_nodes.git_publish import GitPublishNode

        class CtxExec(FakeExecution):
            context = {"$NODE": {"writer": {"text": "plain"}}}

        node = self._node(tmp_path)
        with _warnings.catch_warnings():
            _warnings.simplefilter("error")   # 任何 warning 都算失败
            node.execute(CtxExec())
