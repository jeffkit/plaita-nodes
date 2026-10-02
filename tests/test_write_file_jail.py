"""WriteFile 路径 jail 回归（2026-09 安全评审 P1-2）。"""
import json

import pytest

from plaita import Flow


@pytest.fixture()
def jailed_node():
    from plaita_nodes.write_file import WriteFileNode

    import tempfile
    root = tempfile.mkdtemp()
    old = WriteFileNode._WORKSPACE_ROOT_OVERRIDE
    WriteFileNode._WORKSPACE_ROOT_OVERRIDE = root
    yield WriteFileNode, root
    WriteFileNode._WORKSPACE_ROOT_OVERRIDE = old


def _run_write(path, content="x"):
    flow = Flow.from_string(json.dumps({
        "flow_id": "w",
        "nodes": [
            {"type": "start", "id": "s", "next": "w"},
            {"type": "writefile", "id": "w", "path": path, "content": content, "next": "e"},
            {"type": "end", "id": "e", "output": "$NODE.w.path", "resultType": "success"},
        ],
    }))
    return flow.run()


def test_relative_write_lands_in_jail(jailed_node):
    import os
    cls, root = jailed_node
    _run_write("out/ok.txt", "hello")
    assert os.path.exists(os.path.join(root, "out/ok.txt"))


def test_traversal_blocked(jailed_node):
    with pytest.raises(Exception, match="escapes workspace_root"):
        _run_write("../../escaped.txt")


def test_absolute_path_blocked(jailed_node):
    with pytest.raises(Exception, match="escapes workspace_root"):
        _run_write("/tmp/absolute-escape-by-writefile.txt")


def test_no_jail_keeps_legacy_behavior(jailed_node):
    """未设置 jail 时保持历史行为（任意路径）——兼容性契约。"""
    import os
    import tempfile
    cls, _ = jailed_node
    cls._WORKSPACE_ROOT_OVERRIDE = None  # 显式关闭 fixture 设置的 jail
    out = tempfile.mkdtemp()
    _run_write(os.path.join(out, "legacy.txt"))
    assert os.path.exists(os.path.join(out, "legacy.txt"))
