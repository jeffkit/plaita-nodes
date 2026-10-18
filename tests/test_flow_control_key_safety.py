"""plaita#201：rate_limit key / report token 必须留在存储命名空间内。

修法 = **白名单单个文件名分量**（``_safe_path.safe_component``）+ resolve 后
``is_relative_to`` 二道防线。本文件钉住三件事：

1. 黑名单绕过面（``....//``、URL 编码、Unicode 同形字符、``..\\\\``、绝对路径）
   全部被拒绝，且**不落盘**；
2. 错误消息不回显原始 key/token（只给长度 + 指纹）；
3. 既有正常 key/token（``xhs-publish`` / ``run-1``）的文件名与输出逐字不变。
"""
from __future__ import annotations

import pytest

from fake_exec import FakeExecution

from plaita_nodes._safe_path import UnsafePathName, resolve_child, safe_component
from plaita_nodes.rate_limit import RateLimitNode
from plaita_nodes.report import ReportNode, append_entry, read_entries, report_path

#: 攻击面：每一个都必须 fail-closed。第 2 项正是 issue 里的沙箱注册表路径。
BAD_NAMES = [
    ("parent", "../x"),
    ("sandbox_registry", "../../.plaita/sandboxes"),
    ("absolute", "/tmp/plaita-201-escape"),
    ("nested", "a/b"),
    ("dot", "."),
    ("dotdot", ".."),
    ("backslash", "..\\..\\.plaita\\sandboxes"),
    ("urlencoded", "%2e%2e%2f%2e%2e%2f.plaita%2fsandboxes"),
    ("unicode_dots", "\uff0e\uff0e/\uff0e\uff0e/.plaita/sandboxes"),
    ("dotdot_slash", "....//....//.plaita/sandboxes"),
    ("nul", "a\x00b"),
    ("space", "a b"),
    ("too_long", "a" * 201),
]


@pytest.mark.parametrize("label,raw", BAD_NAMES, ids=[x[0] for x in BAD_NAMES])
def test_rate_limit_rejects_escaping_key(tmp_path, label, raw):
    ex = FakeExecution()
    node = RateLimitNode(id="r", action="record", key=raw, store_dir=str(tmp_path))
    with pytest.raises(UnsafePathName, match="rate_limit key 非法"):
        node.execute(ex)
    # fail-closed：拒绝发生在落盘之前（包括 mkdir 父目录）
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("label,raw", BAD_NAMES, ids=[x[0] for x in BAD_NAMES])
def test_report_rejects_escaping_token(tmp_path, label, raw):
    ex = FakeExecution()
    node = ReportNode(id="w", action="write", token=raw,
                      base_dir=str(tmp_path), entry={"pwned": True})
    with pytest.raises(UnsafePathName, match="report token 非法"):
        node.execute(ex)
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("label,raw", BAD_NAMES, ids=[x[0] for x in BAD_NAMES])
def test_report_path_helpers_reject_escaping_token(tmp_path, label, raw):
    with pytest.raises(UnsafePathName, match="report token 非法"):
        report_path(str(tmp_path), raw)
    with pytest.raises(UnsafePathName, match="report token 非法"):
        append_entry(str(tmp_path), raw, {"x": 1})
    with pytest.raises(UnsafePathName, match="report token 非法"):
        read_entries(str(tmp_path), raw)
    assert list(tmp_path.rglob("*")) == []


def test_acquire_rejects_before_creating_lock_file(tmp_path):
    """acquire 的 ``open(path, "a+")`` 同样在净化之后，非法 key 不建文件。"""
    with pytest.raises(UnsafePathName):
        RateLimitNode(id="a", action="acquire", key="../escape",
                      store_dir=str(tmp_path)).execute(FakeExecution())
    assert list(tmp_path.rglob("*")) == []


def test_error_message_does_not_echo_raw_key(tmp_path):
    marker = "SECRET-201-marker"
    with pytest.raises(UnsafePathName) as ei:
        safe_component(f"{marker}/../x", kind="rate_limit key")
    message = str(ei.value)
    assert marker not in message
    assert "长度" in message and "指纹" in message
    assert "[A-Za-z0-9][A-Za-z0-9._-]{0,199}" in message


def test_normal_keys_are_verbatim(tmp_path):
    """白名单内的名字逐字保留：文件名、返回值与修复前一致。"""
    ex = FakeExecution()
    for key in ("xhs-publish", "run-1", "A_1", "a", "a" * 200):
        out = RateLimitNode(id="r", action="record", key=key,
                            store_dir=str(tmp_path)).execute(ex)
        assert out["key"] == key
        assert (tmp_path / f"{key}.json").is_file()

    for token in ("run-42", "rt-42", "report_v2"):
        out = ReportNode(id="w", action="write", token=token,
                         base_dir=str(tmp_path), entry={"ok": 1}).execute(ex)
        assert out == {"written": True, "entry": {"ok": 1}}
        assert (tmp_path / f"{token}.jsonl").is_file()


def test_safe_component_is_idempotent_for_valid_names():
    assert safe_component("xhs-publish", kind="k") == "xhs-publish"


def test_safe_component_fails_closed_when_value_is_not_stringifiable():
    class Broken:
        def __str__(self):
            raise RuntimeError("boom")

    with pytest.raises(UnsafePathName, match="无法字符串化"):
        safe_component(Broken(), kind="rate_limit key")


def test_resolve_child_blocks_traversal_even_without_whitelist(tmp_path):
    """第二道防线独立生效：绕过 safe_component 直接拼接也必须被拦。"""
    base = tmp_path / "store"
    base.mkdir()
    with pytest.raises(UnsafePathName, match="越出存储目录"):
        resolve_child(base, "../escape", kind="test", suffix=".json")


def test_resolve_child_blocks_symlink_escape(tmp_path):
    """store 内的同名 symlink 指向外部文件：resolve 后越界 ⇒ 拒绝写。"""
    base = tmp_path / "store"
    outside = tmp_path / "outside.json"
    base.mkdir()
    outside.write_text("do-not-overwrite")
    (base / "linked.json").symlink_to(outside)
    with pytest.raises(UnsafePathName, match="越出存储目录"):
        resolve_child(base, "linked", kind="test", suffix=".json")
    assert outside.read_text() == "do-not-overwrite"
