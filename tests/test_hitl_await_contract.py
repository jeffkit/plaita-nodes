"""HitlAwaitNode 执行台阶契约声明（两语义终版，plaita fb36f085 配套）。

版本无关设计：plaita-nodes 可装在旧 plaita 上（声明是无害 ClassVar；旧版把
execution_requirements 按 legacy 折算），也可装在新 plaita 上（注册过闸）。
因此这里只断言「声明存在且形状正确」与「注册成功」，不断言「裸挂起类会被
拒」——后者是 plaita 仓 tests/unit/test_issue330_suspend_not_executable.py
的守备面。
"""

from plaita.node import NodeRegistry

from plaita_nodes import HitlAwaitNode


def _tier_vocab():
    """两值词汇（新 plaita）/ None（旧 plaita，未升级）——测试相应降级。"""
    import plaita.node.capabilities as caps

    return getattr(caps, "EXECUTION_TIERS", None)


def test_hitl_await_declares_managed_tier():
    """台阶=managed（会挂起、经事件总线唤醒）。

    读取必须走 :func:`declared_tier`（新 plaita）——execution_tier 是派生值
    **不写回类属性**（显式声明与 legacy 折算的优先级在函数里），直读类属性
    对 legacy 声明的节点会拿到 None。旧 plaita 无该函数时按声明形状降级。
    """
    try:
        from plaita.node.capabilities import declared_tier
    except ImportError:
        declared_tier = None
    if declared_tier is not None:
        assert declared_tier(HitlAwaitNode) == "managed"
    else:
        reqs = tuple(getattr(HitlAwaitNode, "execution_requirements", ()) or ())
        assert "suspend:root" in reqs  # 旧词汇形状
    # 事件总线半边：无 wake_service（poller 经通用事件总线发布 hitl_reply 唤醒，
    # 不依赖队列唤醒服务）。
    assert not getattr(HitlAwaitNode, "wake_service", None)
    if _tier_vocab() is not None and getattr(HitlAwaitNode, "execution_tier", None) is not None:
        # 仅显式声明形状下读派生值（legacy 折算形状不写回，跳过）
        assert HitlAwaitNode.is_suspending is True


def test_hitl_await_registers_into_fresh_registry():
    """注册过闸（旧 plaita 无不变量同样通过——版本无关）。"""
    reg = NodeRegistry()
    reg.register(HitlAwaitNode)
    assert reg.get("hitl_await") is HitlAwaitNode
