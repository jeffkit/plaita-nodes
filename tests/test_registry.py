"""entry_points 注册冒烟：默认 registry 能发现 plaita-nodes 全部节点。"""
from __future__ import annotations


def test_entry_points_discover_all_nodes():
    from plaita.node import get_default_registry

    registry = get_default_registry()
    known = set(registry.list_types())
    for node_type in ("agentrun", "capture", "hitl", "notify", "writefile"):
        assert node_type in known, f"{node_type} 未被 entry_points 发现"
