"""pn#20：``recursive_stream_turn`` 与非流式路径同构的租户闸。

缺口：``AgentRunNode`` 的宿主直跑路径对非 default 租户在 Popen 前具名拒绝，
但流式辅助路径 ``recursive_stream_turn`` 直接 Popen——同一宿主语义两套闸，
而流式面（console ``/api/flows/ai-generate/stream``）跑的是
``--permission-mode auto`` 的编码 Agent，漏掉的面更危险。

修法：流式生成器在**任何副作用之前**复用 ``_tenant_guard``
（``refuse_non_default_tenant``）——非 default 租户抛
``TenantContextPropagationError``（marker 具名），preflight / 凭据解析 /
Popen 零执行机会；default 租户语义不变。

负控：仅删生产代码里的 ``refuse_non_default_tenant("recursive_stream_turn")``
一行 ⇒ ``test_non_default_tenant_refused_before_popen`` 精确红（Popen 桩被
调用，或未抛出租户拒绝），default 正控保持绿。
"""
from __future__ import annotations

import json

import pytest
from plaita.tenant_context import reset_current_tenant, set_current_tenant

from plaita_nodes import agent_run as agent_run_mod
from plaita_nodes._tenant_guard import (
    TENANT_ISOLATION_MARKER,
    TenantContextPropagationError,
)
from plaita_nodes.agent_run import recursive_stream_turn


@pytest.fixture
def tenant_acme():
    token = set_current_tenant("acme")
    yield "acme"
    reset_current_tenant(token)


def _register_stub_agent(repo, stub, name: str) -> None:
    agents = json.loads((repo / ".flowcast" / "agents.json").read_text())
    agents["agents"][name] = {
        "executor": "recursive",
        "env": {"RECURSIVE_BIN": str(stub)},
    }
    (repo / ".flowcast" / "agents.json").write_text(json.dumps(agents))


def test_non_default_tenant_refused_before_popen(
        tenant_acme, agent_config_repo, tmp_path, monkeypatch):
    """非 default 租户：Popen / preflight / 凭据解析全部零执行。"""
    stub = agent_config_repo / "stub-issue20-deny.sh"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    _register_stub_agent(agent_config_repo, stub, "rec-issue20-deny")

    touched: list[str] = []

    def _popen(*args, **kwargs):  # noqa: ARG001
        touched.append("popen")
        raise AssertionError("Popen 不得在租户闸之前被调用")

    def _preflight(*args, **kwargs):  # noqa: ARG001
        touched.append("preflight")

    def _resolve(*args, **kwargs):  # noqa: ARG001
        touched.append("resolve_agent")

    monkeypatch.setattr(agent_run_mod.subprocess, "Popen", _popen)
    monkeypatch.setattr(agent_run_mod, "preflight_workspace", _preflight)
    monkeypatch.setattr(agent_run_mod, "resolve_agent", _resolve)

    with pytest.raises(TenantContextPropagationError) as exc:
        list(recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-issue20-deny"))

    assert TENANT_ISOLATION_MARKER in str(exc.value)
    assert tenant_acme in str(exc.value)
    assert touched == []


def test_guard_applies_even_if_host_run_opt_out_set(
        tenant_acme, agent_config_repo, tmp_path, monkeypatch):
    """流式闸独立于非流式的部署 opt-out：多租户下不会因环境变量放行。"""
    stub = agent_config_repo / "stub-issue20-optout.sh"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    _register_stub_agent(agent_config_repo, stub, "rec-issue20-optout")

    monkeypatch.setenv("PLAITA_ALLOW_HOST_AGENT_RUN", "1")
    monkeypatch.setattr(
        agent_run_mod.subprocess, "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Popen 不得被调用")),
    )

    with pytest.raises(TenantContextPropagationError, match=TENANT_ISOLATION_MARKER):
        list(recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-issue20-optout"))


def test_default_tenant_stream_still_runs(agent_config_repo, tmp_path):
    """default 租户正控：流式执行、逐行事件与 done 结果不变。"""
    stub = agent_config_repo / "stub-issue20-ok.sh"
    stub.write_text(
        "#!/bin/sh\n"
        "echo 'working'\n"
        "printf '%s\\n' '{\"result\": \"ok\", \"is_error\": false}'\n"
    )
    stub.chmod(0o755)
    _register_stub_agent(agent_config_repo, stub, "rec-issue20-ok")

    events = list(recursive_stream_turn(
        "hi", workspace=str(tmp_path), profile="rec-issue20-ok", timeout_secs=30))

    assert [e["type"] for e in events] == ["line", "line", "done"]
    assert events[0]["text"] == "working"
    assert events[-1]["ok"] is True
    assert events[-1]["result"] == "ok"
