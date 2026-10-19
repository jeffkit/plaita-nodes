"""A2R8-05：agent 协议行缓冲必须有行数/字节上界（超限截断 + 标记）。

旧实现 ``protocol_lines.append(...)`` 无界：20k 行 × 1 KiB 常驻约 19.8 MiB。
修复后任一上限触界即停止驻留，末尾追加非 JSON 标记行并打 WARNING；
直跑与沙箱两条路径共用同一个有界缓冲。
"""
from __future__ import annotations

import logging

from plaita_nodes.agent_run import (
    MAX_PROTOCOL_BYTES,
    MAX_PROTOCOL_LINES,
    AgentRunNode,
    _ProtocolLineBuffer,
    parse_stream_details,
)


def _exec():
    return type("E", (), {
        "evaluate": lambda self, v: v,
        "get_global_variable": lambda self, k, d=None: d,
    })()


def test_line_cap_truncates_and_marks(caplog):
    buf = _ProtocolLineBuffer(max_lines=5, max_bytes=1024 * 1024)
    with caplog.at_level(logging.WARNING, logger="plaita_nodes.agent_run"):
        for i in range(20):
            buf.append(f"line-{i}")

    assert buf.truncated is True
    assert buf.lines[:5] == [f"line-{i}" for i in range(5)]
    assert len(buf.lines) == 6, "上限之外至多一条标记行"
    assert buf.lines[-1].startswith("[plaita] protocol_lines truncated:")
    assert "kept 5 lines" in buf.lines[-1]
    assert "已截断" in caplog.text


def test_byte_cap_truncates_before_line_cap(caplog):
    buf = _ProtocolLineBuffer(max_lines=100, max_bytes=2500)
    with caplog.at_level(logging.WARNING, logger="plaita_nodes.agent_run"):
        for _ in range(10):
            buf.append("x" * 1000)

    assert buf.truncated is True
    assert len(buf.lines) == 3  # 2 行驻留 + 标记（第三行会超 2500 字节）
    assert sum(len(line.encode("utf-8")) for line in buf.lines[:-1]) <= 2500
    assert buf.lines[-1].startswith("[plaita] protocol_lines truncated:")


def test_under_caps_keeps_every_line_and_no_marker():
    buf = _ProtocolLineBuffer(max_lines=10, max_bytes=1024)
    lines = ["a", "b", "c"]
    for line in lines:
        buf.append(line)
    assert buf.truncated is False
    assert buf.lines == lines


def test_default_caps_bound_the_audit_scenario():
    """20k 行 × 1 KiB：驻留字节与行数都不得超过默认上界。"""
    buf = _ProtocolLineBuffer()
    for i in range(20_000):
        buf.append('{"payload":"' + "p" * 1013 + '"}')

    assert buf.truncated is True
    assert len(buf.lines) <= MAX_PROTOCOL_LINES + 1
    assert sum(len(line.encode("utf-8")) for line in buf.lines[:-1]) <= MAX_PROTOCOL_BYTES
    assert buf.lines[-1].startswith("[plaita] protocol_lines truncated:")


def test_marker_is_skipped_by_details_parser():
    buf = _ProtocolLineBuffer(max_lines=1, max_bytes=1024 * 1024)
    buf.append('{"type":"assistant","message":{"model":"M","content":['
               '{"type":"text","text":"hi"}]}}')
    buf.append('{"type":"assistant","message":{"model":"M","content":'
               '[{"type":"text","text":"lost"}]}}')

    observations = parse_stream_details(buf.lines)
    assert observations == [
        {"type": "generation", "name": "turn:1", "model": "M", "output": "hi"}
    ]


def test_direct_path_uses_bounded_buffer(agent_config_repo,
                                         register_test_echo_executor,
                                         monkeypatch):
    """直跑路径接线：20 条回调线在 max_lines=5 下截断且执行仍成功。"""
    import agentproc.runner as runner_mod

    import plaita_nodes.agent_run as agent_run_mod

    created = []
    original = agent_run_mod._ProtocolLineBuffer

    class Spy(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.append(self)

    monkeypatch.setattr(agent_run_mod, "_ProtocolLineBuffer", Spy)
    monkeypatch.setattr(agent_run_mod, "MAX_PROTOCOL_LINES", 5)
    monkeypatch.setattr(agent_run_mod, "MAX_PROTOCOL_BYTES", 1024 * 1024)

    class _Result:
        exit_code = 0
        error = None
        reply = '{"result":"ok","is_error":false}'
        usage = None
        session_id = "sess"

    def _fake_run(spec, options):
        for i in range(20):
            options.on_protocol_line(
                f'{{"type":"assistant","message":{{"content":'
                f'[{{"type":"text","text":"t{i}"}}]}}}}'
            )
        return _Result()

    monkeypatch.setattr(runner_mod, "run", _fake_run)

    node = AgentRunNode(id="t", agent="echo", prompt="x",
                        repo=str(agent_config_repo))
    out = node.execute(_exec())

    assert out["text"] == _Result.reply
    assert created, "execute 必须构造有界协议行缓冲"
    assert created[0].truncated is True
    assert len(created[0].lines) == 6
    assert created[0].lines[-1].startswith("[plaita] protocol_lines truncated:")
