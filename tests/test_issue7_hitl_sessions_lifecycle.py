"""pn#7：hitl_poller 的 sessions 映射生命周期。

历史缺陷：映射只在 ``main()`` 启动时读一次，之后每轮用陈旧内存快照整份覆盖写回。
后果：① 启动后新挂起的 HITL 永不被轮询；② 并发写入者刚追加的条目被静默抹掉。

本组用例驱动真实 ``main()`` 循环（``time.sleep`` 在固定轮数后抛哨兵退出），
在 ``requests.get`` 内模拟外部写入者追加条目——pre-fix 在两条上分别表现为
「从不请求新 session」与「写回把新条目整份抹掉」。
"""
from __future__ import annotations

import json

import pytest

from plaita_nodes.hitl_poller import main


class _StopMain(Exception):
    """哨兵：让常驻 ``main()`` 循环在固定轮数后退出。"""


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _RecordingBus:
    def __init__(self):
        self.publishes: list[tuple[str, dict]] = []

    async def publish(self, event_type, **kwargs):
        self.publishes.append((event_type, kwargs))
        return f"evt-{len(self.publishes)}"


def _install_bus(monkeypatch) -> _RecordingBus:
    bus = _RecordingBus()
    # main() 在函数内 import InMemoryEventBus；打桩源模块即可覆盖 pre/post 两种实现。
    monkeypatch.setattr("plaita.event.memory.InMemoryEventBus", lambda *a, **k: bus)
    return bus


def _run_rounds(monkeypatch, sessions_path, rounds: int, fake_get) -> None:
    """跑 ``rounds`` 轮后让 main() 以哨兵退出（每轮结束调用一次 sleep）。"""
    sleeps = {"n": 0}

    def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= rounds:
            raise _StopMain

    monkeypatch.setattr("plaita_nodes.hitl_poller.time.sleep", fake_sleep)
    monkeypatch.setattr("plaita_nodes.hitl_poller.requests.get", fake_get)
    with pytest.raises(_StopMain):
        main([
            "--sessions", str(sessions_path),
            "--hitl-url", "http://hitl",
            "--bus", "memory",
            "--interval", "0.01",
        ])


def test_runtime_added_session_is_polled_and_kept(monkeypatch, tmp_path):
    """启动后由外部写入者追加的 session，下一轮必须被轮询且条目保留。"""
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(json.dumps({
        "exec-1": {"session_id": "sess-a"},
    }), encoding="utf-8")

    requested: list[str] = []

    def fake_get(url, timeout=None):
        session_id = url.rsplit("/", 1)[-1]
        requested.append(session_id)
        if session_id == "sess-a":
            # 模拟并发写入者在 poller 运行期间追加一条新挂起的 HITL。
            current = json.loads(sessions_path.read_text(encoding="utf-8"))
            current["exec-2"] = {"session_id": "sess-b"}
            sessions_path.write_text(json.dumps(current), encoding="utf-8")
        return _Resp({"has_reply": False, "status": "waiting"})

    bus = _install_bus(monkeypatch)
    _run_rounds(monkeypatch, sessions_path, rounds=2, fake_get=fake_get)

    assert requested == ["sess-a", "sess-a", "sess-b"]
    assert bus.publishes == []
    on_disk = json.loads(sessions_path.read_text(encoding="utf-8"))
    assert on_disk == {
        "exec-1": {"session_id": "sess-a"},
        "exec-2": {"session_id": "sess-b"},
    }


def test_concurrent_append_survives_published_key_removal(monkeypatch, tmp_path):
    """删除已发布键时只能增量 pop，不能把并发追加的条目整份覆盖掉。"""
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(json.dumps({
        "exec-a": {"session_id": "sess-a"},
    }), encoding="utf-8")

    def fake_get(url, timeout=None):
        session_id = url.rsplit("/", 1)[-1]
        if session_id == "sess-a":
            # 写回发生在同一轮稍后；这里模拟写者在轮询期间追加 exec-b。
            current = json.loads(sessions_path.read_text(encoding="utf-8"))
            current["exec-b"] = {"session_id": "sess-b"}
            sessions_path.write_text(json.dumps(current), encoding="utf-8")
            return _Resp({"has_reply": True, "replies": [{"content": "同意"}]})
        return _Resp({"has_reply": False, "status": "waiting"})

    bus = _install_bus(monkeypatch)
    _run_rounds(monkeypatch, sessions_path, rounds=1, fake_get=fake_get)

    assert [kwargs["session_id"] for _, kwargs in bus.publishes] == ["sess-a"]
    on_disk = json.loads(sessions_path.read_text(encoding="utf-8"))
    assert on_disk == {"exec-b": {"session_id": "sess-b"}}
    # 原子替换不允许留下半截临时文件。
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []
