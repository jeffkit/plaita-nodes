"""A2R7-05 回归：计时参数三态统一为构造期 fail-closed。

历史三态互不一致：

* gate ``timeout_secs=0`` → ``or 600`` 静默变成 600s；
* capture ``timeout_secs=0`` → ``communicate(timeout=0)`` 立即 kill；
* api/generic ``timeout_ms=0/-1`` → 拖到 requests 连接前才抛。

统一为：字面量在**构造期**具名拒绝（pydantic ValidationError，ValueError
子类）；DSL 表达式（gate 的 ``$INPUT.x``）在求值后、spawn/连网前立即拒绝。

负控（回退生产改动）：构造期用例不再抛错；gate 表达式求值 0 会启动命令。
"""
from __future__ import annotations

import pytest

from plaita_nodes.api import ApiRequestNode, GenericWebhookNode
from plaita_nodes.capture import CaptureNode
from plaita_nodes.gate import GateNode


class _Exec:
    def __init__(self, values=None):
        self.values = values or {}

    def evaluate(self, value):
        return self.values.get(value, value)

    def get_global_variable(self, key, default=None):
        return default


@pytest.mark.parametrize("bad", [0, -1, -0.5, float("nan"), float("inf"), True, "-1"])
def test_gate_timeout_rejected_at_construction(bad):
    with pytest.raises(ValueError) as caught:
        GateNode(id="g", command=["true"], timeout_secs=bad)
    assert "gate timeout_secs" in str(caught.value)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_capture_timeout_rejected_at_construction(bad):
    with pytest.raises(ValueError) as caught:
        CaptureNode(id="c", command=["true"], timeout_secs=bad)
    assert "capture timeout_secs" in str(caught.value)


@pytest.mark.parametrize("node_cls,name", [(ApiRequestNode, "api_request"),
                                           (GenericWebhookNode, "generic_webhook")])
@pytest.mark.parametrize("bad", [0, -1, True])
def test_api_timeout_ms_rejected_at_construction(node_cls, name, bad):
    with pytest.raises(ValueError) as caught:
        node_cls(id="a", timeout_ms=bad)
    assert f"{name} timeout_ms" in str(caught.value)


def test_gate_expression_is_deferred_and_validated_before_spawn(monkeypatch):
    node = GateNode(id="g", command=["true"],
                    timeout_secs="$INPUT.gate_timeout_secs")

    def boom(*args, **kwargs):
        raise AssertionError("timeout 校验失败时不得 spawn 子进程")

    monkeypatch.setattr("plaita_nodes.gate.subprocess.Popen", boom)
    with pytest.raises(ValueError) as caught:
        node.execute(_Exec({"$INPUT.gate_timeout_secs": 0}))
    assert "gate timeout_secs" in str(caught.value)
    assert "0 and negative values are rejected" in str(caught.value)


def test_gate_expression_positive_value_still_runs():
    out = GateNode(id="g", command=["true"],
                   timeout_secs="$INPUT.gate_timeout_secs").execute(
                       _Exec({"$INPUT.gate_timeout_secs": "30"}))
    assert out["passed"] is True and out["exit_code"] == 0


def test_gate_default_none_falls_back_to_600():
    # 显式 None 仍表示「未设置」——保持既有 600s 默认（不是 0 预算）。
    out = GateNode(id="g", command=["true"], timeout_secs=None).execute(_Exec())
    assert out["passed"] is True
