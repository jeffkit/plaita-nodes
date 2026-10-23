"""HitlAwaitNode 执行能力契约声明（plaita#330 §4 注册期不变量的消费面）。

版本无关设计：plaita-nodes 可装在**没有**注册期不变量的旧 plaita 上（声明是
无害 ClassVar），也可装在有不变量的新 plaita 上（注册过闸）。因此这里只断言
「声明存在且形状正确」与「注册成功」，不断言「裸挂起类会被拒」——后者是
plaita 仓 tests/unit/test_issue330_suspend_not_executable.py 的守备面。
"""

from plaita.node import NodeRegistry
from plaita.node.capabilities import (
    EXECUTION_CAPABILITY_TOKENS,
    SUSPEND_CAPABILITY_TOKENS,
)

from plaita_nodes import HitlAwaitNode


def test_hitl_await_declares_suspend_requirements():
    """is_suspending=True ⇒ execution_requirements 非空且含 suspend token。"""
    assert HitlAwaitNode.is_suspending is True
    reqs = tuple(HitlAwaitNode.execution_requirements)
    assert reqs, "hitl_await 缺 execution_requirements（不变量唯一漏网正主）"
    assert set(reqs) & SUSPEND_CAPABILITY_TOKENS
    assert set(reqs) <= EXECUTION_CAPABILITY_TOKENS
    assert "event_bus" in reqs  # 结构半边：poller 经事件总线发布 hitl_reply 唤醒


def test_hitl_await_registers_into_fresh_registry():
    """注册过闸（旧 plaita 无不变量同样通过——版本无关）。"""
    reg = NodeRegistry()
    reg.register(HitlAwaitNode)
    assert reg.get("hitl_await") is HitlAwaitNode
