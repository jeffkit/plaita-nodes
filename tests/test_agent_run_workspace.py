"""AgentRunNode workspace 沙箱分支集成测试（P1）。

dry_run 零调用（先于注册表/凭据解析）· 互斥守卫 · fail-closed · None 静默守卫 ·
lease 冲突快速失败 · 失败路径 enforce · envfile 即焚 · canary 脱敏回传 ·
快照输出 · provision.branch 表达式求值。docker 不参与（RecordingDriver + 包装层替身）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from fake_exec import FakeExecution

import plaita_nodes.sandbox as sb
import plaita_nodes.sandbox_docker as sd
from plaita_nodes.agent_run import AgentRunError, AgentRunNode
from plaita_nodes.sandbox import (
    SandboxLeaseError,
    default_lease_store,
    handle_id,
)

from test_sandbox import RecordingDriver


# ── 桩与夹具 ────────────────────────────────────────────────────────────

class ExprExecution(FakeExecution):
    """支持 {% $NODE.x.y %} 求值的桩（其余透传）。"""

    def __init__(self, node=None, **kw):
        super().__init__(**kw)
        self._node = node or {}

    def evaluate(self, value):
        if isinstance(value, str):
            m = re.fullmatch(r"\{% \$NODE\.([\w.]+) %\}", value.strip())
            if m:
                cur: object = self._node
                for part in m.group(1).split("."):
                    cur = cur.get(part) if isinstance(cur, dict) else None
                return cur
        return value


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """HOME 隔离到 tmp（sandboxes 注册表 + 文件租约都在 tmp 下）。"""
    monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
    (tmp_path / ".plaita").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def sandbox_registry(sandbox_home):
    """注册 recording driver + main workspace。"""
    (sandbox_home / ".plaita" / "sandboxes.json").write_text(json.dumps({
        "sandboxes": {"main": {
            "driver": "recording",
            "image": "reg/img@sha256:abc",
            "env": {"GIT_TOKEN": "tok-abc-12345"},
        }}
    }), encoding="utf-8")
    driver = RecordingDriver()
    sb.register_driver("recording", driver)
    yield driver
    sb.SANDBOX_DRIVERS.pop("recording", None)


@pytest.fixture
def patched_wrap(monkeypatch):
    """包装层替身：读 envfile 首行并回显（证明 envfile 接通 + 触发脱敏）。"""
    captured: dict = {}

    def fake_wrap(env, argv):
        captured["env"] = dict(env)
        lines = Path(env["PLAITA_SANDBOX_ENVFILE"]).read_text(
            encoding="utf-8").splitlines()
        first_line = lines[0] if lines else "<empty-envfile>"
        return ["echo", f"ENVFILE<{first_line}> name={env['PLAITA_SANDBOX_NAME']}"]

    monkeypatch.setattr(sb, "wrap_agent_argv_from_env", fake_wrap)
    return captured


def _run_exec(**kw):
    return FakeExecution(**kw)


# ── 守卫：互斥 / fail-closed / None 静默 ────────────────────────────────

class TestGuards:
    def test_workspace_repo_mutual_exclusion_runtime(self, sandbox_home):
        node = AgentRunNode(id="t", agent="echo", prompt="hi",
                            workspace="main", repo="/some/repo")
        with pytest.raises(AgentRunError, match="互斥"):
            node.execute(_run_exec())

    def test_validate_raises_on_mutual_exclusion(self):
        node = AgentRunNode(id="t", agent="echo", prompt="hi",
                            workspace="main", repo="/some/repo")
        with pytest.raises(AgentRunError, match="互斥"):
            node.validate()

    def test_unregistered_name_fail_closed(self, sandbox_home, sandbox_registry):
        node = AgentRunNode(id="t", agent="echo", prompt="hi", workspace="nope")
        with pytest.raises(AgentRunError, match="未注册"):
            node.execute(_run_exec())

    def test_empty_workspace_hard_fail(self, sandbox_home, sandbox_registry):
        """$NODE 缺键静默 None 的防线：空名会让所有迭代共享同一沙箱。"""
        node = AgentRunNode(id="t", agent="echo", prompt="hi",
                            workspace="{% $NODE.missing %}")
        with pytest.raises(AgentRunError, match="求值为空"):
            node.execute(ExprExecution(node={}))

    def test_missing_execution_id_rejected(self, sandbox_home, sandbox_registry):
        class NoExecId(FakeExecution):
            execution_id = ""

        node = AgentRunNode(id="t", agent="echo", prompt="hi", workspace="main")
        with pytest.raises(AgentRunError, match="execution_id"):
            node.execute(NoExecId())


# ── dry_run 零调用（先于注册表解析与凭据组装，设计 §7.7）────────────────

class TestDryRunZeroCalls:
    def _patch_all_side_effects(self, monkeypatch):
        def _boom(*a, **kw):
            raise AssertionError("dry_run 下不应触达：" + repr(a[:1]))
        monkeypatch.setattr("plaita_nodes.agent_run.resolve_agent", _boom)
        monkeypatch.setattr(sb, "load_sandboxes", _boom)
        monkeypatch.setattr("plaita.credentials.get_credential", _boom)
        monkeypatch.setattr("subprocess.Popen", _boom)

    def test_node_level_dry(self, sandbox_home, sandbox_registry, monkeypatch):
        self._patch_all_side_effects(monkeypatch)
        node = AgentRunNode(id="t", agent="echo", prompt="hi", workspace="main",
                            dry_run=True)
        out = node.execute(_run_exec())
        assert out["dry_run"] is True

    def test_global_dry(self, sandbox_home, sandbox_registry, monkeypatch):
        self._patch_all_side_effects(monkeypatch)
        node = AgentRunNode(id="t", agent="echo", prompt="hi", workspace="main")
        out = node.execute(FakeExecution(global_vars={"dry_run": True}))
        assert out["dry_run"] is True


# ── happy path ──────────────────────────────────────────────────────────

class TestHappyPath:
    def test_full_flow(self, sandbox_home, sandbox_registry, patched_wrap,
                       agent_config_repo, register_test_echo_executor):
        node = AgentRunNode(id="t", agent="echo", prompt="hello", workspace="main")
        out = node.execute(_run_exec())

        # 脱敏回传：envfile 首行含密钥 → 输出只见 canary 标记
        assert "tok-abc-12345" not in out["text"]
        assert "[REDACTED:GIT_TOKEN]" in out["text"]
        assert "c-main" in out["text"]  # 容器名进 argv（非密钥）

        # 观测快照：极小、env 只含名字
        snap = out["workspace"]
        assert snap["ws_key"] == "main"
        assert snap["driver"] == "recording"
        assert snap["id"] == handle_id(FakeExecution.execution_id, "main")
        assert snap["env_names"] == ["GIT_TOKEN"]
        assert "GIT_TOKEN=tok-abc-12345" not in json.dumps(out)

        # driver 调用面：仅 ensure——workspace 跨节点共享，release 属 flow 生命周期
        # （设计 §6.1），失败路径才 enforce
        assert sandbox_registry.calls == [
            ("ensure", "main", FakeExecution.execution_id, "main"),
        ]

        # envfile 即焚
        envfile = patched_wrap["env"]["PLAITA_SANDBOX_ENVFILE"]
        assert not Path(envfile).exists()

        # 租约已释放：同 key 可再次获取
        assert default_lease_store().acquire(handle_id(FakeExecution.execution_id, "main"),
                                             "after", 60) is True
        default_lease_store().release(handle_id(FakeExecution.execution_id, "main"), "after")

    def test_secret_via_credential_ref(self, sandbox_home, sandbox_registry,
                                       patched_wrap, monkeypatch, agent_config_repo,
                                       register_test_echo_executor):
        (sandbox_home / ".plaita" / "sandboxes.json").write_text(json.dumps({
            "sandboxes": {"main": {
                "driver": "recording", "image": "reg/img@sha256:abc",
                "env": {"GIT_TOKEN": {"credential": "git-token", "field": "token"}},
            }}
        }), encoding="utf-8")
        monkeypatch.setattr("plaita.credentials.get_credential",
                            lambda name: {"token": "tok-from-credential-1"})

        node = AgentRunNode(id="t", agent="echo", prompt="hi", workspace="main")
        out = node.execute(_run_exec())
        assert "tok-from-credential-1" not in out["text"]
        assert "[REDACTED:GIT_TOKEN]" in out["text"]
        assert out["workspace"]["env_names"] == ["GIT_TOKEN"]

    def test_branch_expression_evaluated_at_first_reference(
            self, sandbox_home, sandbox_registry, patched_wrap, monkeypatch,
            agent_config_repo, register_test_echo_executor):
        (sandbox_home / ".plaita" / "sandboxes.json").write_text(json.dumps({
            "sandboxes": {"main": {
                "driver": "recording", "image": "reg/img@sha256:abc",
                "provision": {"git": {"repo": "https://x/y.git",
                                      "branch": "{% $NODE.commit.branch %}"}},
            }}
        }), encoding="utf-8")

        seen: list = []

        class SpecCap(RecordingDriver):
            def ensure(self, spec, execution_id, ws_key):
                seen.append(dict(spec.provision))
                return super().ensure(spec, execution_id, ws_key)

        sb.SANDBOX_DRIVERS["recording"] = SpecCap()
        try:
            node = AgentRunNode(id="t", agent="echo", prompt="hi", workspace="main")
            node.execute(ExprExecution(node={"commit": {"branch": "feature-x"}}))
        finally:
            sb.SANDBOX_DRIVERS["recording"] = sandbox_registry
        assert seen and seen[0]["git"]["branch"] == "feature-x"  # 表达式已求值


# ── 失败路径：lease 冲突 / 执行失败 → enforce ───────────────────────────

class TestFailurePaths:
    def test_lease_conflict_fails_fast(self, sandbox_home, sandbox_registry,
                                       agent_config_repo, register_test_echo_executor):
        key = handle_id(FakeExecution.execution_id, "main")
        assert default_lease_store().acquire(key, "someone-else", 60) is True
        node = AgentRunNode(id="t", agent="echo", prompt="hi", workspace="main")
        with pytest.raises(SandboxLeaseError, match="租约被他人持有"):
            node.execute(_run_exec())
        assert sandbox_registry.calls == []  # 未触达 driver

    def test_exec_failure_enforces(self, sandbox_home, sandbox_registry,
                                   patched_wrap, agent_config_repo,
                                   register_test_echo_executor, monkeypatch):
        from agentproc import EXECUTORS

        EXECUTORS["test-fail"] = {
            "cli_name": "false", "plain": True,
            "make_handlers": lambda: {"build_args": lambda m, s, e: ["false"]},
        }
        try:
            def failing_wrap(env, argv):
                patched_wrap["env"] = dict(env)  # 记录 envfile 路径供焚毁断言
                return ["false"]
            monkeypatch.setattr(sb, "wrap_agent_argv_from_env", failing_wrap)
            node = AgentRunNode(id="t", agent="echo", prompt="hi", workspace="main",
                                timeout_secs=30)
            with pytest.raises(AgentRunError):
                node.execute(_run_exec())
            assert ("enforce", handle_id(FakeExecution.execution_id, "main")) in sandbox_registry.calls
            # 节点路径不 release workspace（跨节点共享）；最后一步 = enforce
            assert sandbox_registry.calls[-1] == ("enforce", handle_id(FakeExecution.execution_id, "main"))
            assert not Path(patched_wrap["env"]["PLAITA_SANDBOX_ENVFILE"]).exists()
        finally:
            EXECUTORS.pop("test-fail", None)
            EXECUTORS.pop("test-fail-sandbox", None)
