"""agent 宿主直跑的多租户 fail-closed（#5）。

租户上下文非 default 时 ``repo`` 直跑（agent CLI 在 worker 宿主上免审批执行）
默认拒绝；部署方显式 ``PLAITA_ALLOW_HOST_AGENT_RUN=1`` 才放行。单租户
（default 租户）行为不变。
"""
from __future__ import annotations

import pytest
from plaita.tenant_context import reset_current_tenant, set_current_tenant

from fake_exec import FakeExecution
from plaita_nodes.agent_run import HOST_RUN_OPT_OUT_ENV, AgentRunError, AgentRunNode


@pytest.fixture
def multi_tenant():
    token = set_current_tenant("acme")
    yield "acme"
    reset_current_tenant(token)


class TestMultiTenantHostRunGuard:
    def test_host_run_denied_for_tenant(self, multi_tenant, agent_config_repo):
        node = AgentRunNode(id="a", agent="echo", prompt="hi", repo=str(agent_config_repo))
        with pytest.raises(AgentRunError, match="宿主直跑"):
            node.execute(FakeExecution())

    def test_denial_message_names_tenant_and_opt_out(self, multi_tenant, agent_config_repo):
        node = AgentRunNode(id="a", agent="echo", prompt="hi", repo=str(agent_config_repo))
        with pytest.raises(AgentRunError) as exc:
            node.execute(FakeExecution())
        assert "acme" in str(exc.value)
        assert HOST_RUN_OPT_OUT_ENV in str(exc.value)

    def test_explicit_opt_out_allows_host_run(self, multi_tenant, agent_config_repo,
                                              register_test_echo_executor, monkeypatch):
        monkeypatch.setenv(HOST_RUN_OPT_OUT_ENV, "1")
        node = AgentRunNode(id="a", agent="echo", prompt="hi", repo=str(agent_config_repo))
        out = node.execute(FakeExecution())
        assert out["dry_run"] is False
        assert "msg=hi" in out["text"]

    def test_dry_run_not_denied_for_tenant(self, multi_tenant, agent_config_repo):
        node = AgentRunNode(id="a", agent="echo", prompt="hi", repo=str(agent_config_repo),
                            dry_run=True)
        assert node.execute(FakeExecution())["dry_run"] is True

    def test_single_tenant_host_run_unaffected(self, agent_config_repo,
                                               register_test_echo_executor):
        node = AgentRunNode(id="a", agent="echo", prompt="hi", repo=str(agent_config_repo))
        out = node.execute(FakeExecution())
        assert out["dry_run"] is False
        assert "msg=hi" in out["text"]
