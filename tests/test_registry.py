"""entry_points 注册冒烟：默认 registry 能发现 plaita-nodes 全部节点。"""
from __future__ import annotations


def test_entry_points_discover_all_nodes():
    from plaita.node import get_default_registry

    registry = get_default_registry()
    known = set(registry.list_types())
    for node_type in ("agentrun", "capture", "hitl", "notify", "writefile"):
        assert node_type in known, f"{node_type} 未被 entry_points 发现"


def test_all_nodes_matches_entry_points():
    """本仓 entry-points（模块前缀 plaita_nodes.）与 _ALL_NODES 必须一一对应
    （防两份手工清单漂移）。注意 plaita 内核自己也注册 plaita.nodes 组的
    服务节点（delay/kafka_queue 等），不在本仓清单内，按模块前缀排除。"""
    import importlib.metadata

    import plaita_nodes

    ep_classes = {ep.load() for ep in importlib.metadata.entry_points(group="plaita.nodes")
                  if ep.value.startswith("plaita_nodes.")}
    assert set(plaita_nodes._ALL_NODES) == ep_classes


def test_all_exports_resolve():
    """__all__ 里每个名字都必须真实存在（防导出名漂移，如 report_append 事故）。"""
    import plaita_nodes

    missing = [name for name in plaita_nodes.__all__ if not hasattr(plaita_nodes, name)]
    assert missing == []
