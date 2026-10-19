"""pn#10：hitl_poller 发布的 hitl_reply 必须带 correlation_id（= execution_id）。

历史缺陷：poller 只透传 ``data``（status/replies/session_id），``Event.correlation_id``
恒为 None。plaita 内核 ``EventFilter.handle_event`` 入口 ``if not
event.correlation_id: return`` 直接丢弃；即便绕过入口，挂起订阅按
``correlation_id=execution_id`` 匹配也必然不命中——人工回复双重到不了订阅端。

本组用例从真实 ``main()`` 循环捕获发布调用，再把该调用按 RedisEventBus 的
``str + kwargs`` 装配语义还原成 ``Event``，交给真实 ``EventFilter.handle_event``
（真实内存订阅存储 + 探针执行存储/队列）——pre-fix 在入口丢弃，
``load_execution_state`` 与入队均为 0 次。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from plaita.event.core import Event, EventSubscription
from plaita.event.memory import InMemoryEventSubscriptionStorage
from plaita.server.event_filter import EventFilter
from plaita.storage.base import ExecutionState
from plaita_nodes.hitl_poller import EVENT_TYPE, main


class _StopMain(Exception):
    """哨兵：让常驻 ``main()`` 循环跑完固定轮数后退出。"""


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


class _FakeRedis:
    """只实现 EventFilter 用到的两条命令：去重键 SET NX + 任务入队 XADD。"""

    def __init__(self):
        self.dedup_keys: list[str] = []
        self.xadds: list[tuple[str, dict]] = []

    def set(self, key, value, nx=False, ex=None):
        self.dedup_keys.append(key)
        return True

    def xadd(self, stream, fields):
        self.xadds.append((stream, fields))
        return "1-0"


class _ProbeExecutionStorage:
    def __init__(self, execution_id: str):
        self.execution_id = execution_id
        self.loaded: list[str] = []

    def load_execution_state(self, execution_id: str):
        self.loaded.append(execution_id)
        return ExecutionState(
            execution_id=execution_id,
            flow_id="flow-1",
            context={},
            status="suspended",
            tenant_id="default",
        )


def _run_one_round(monkeypatch, sessions_path, bus) -> None:
    def fake_get(url, timeout=None):
        return _Resp({"has_reply": True, "replies": [{"content": "同意"}]})

    def fake_sleep(_seconds):
        raise _StopMain

    monkeypatch.setattr("plaita.event.memory.InMemoryEventBus", lambda *a, **k: bus)
    monkeypatch.setattr("plaita_nodes.hitl_poller.requests.get", fake_get)
    monkeypatch.setattr("plaita_nodes.hitl_poller.time.sleep", fake_sleep)
    with pytest.raises(_StopMain):
        main([
            "--sessions", str(sessions_path),
            "--hitl-url", "http://hitl",
            "--bus", "memory",
            "--interval", "0.01",
        ])


def test_publish_carries_correlation_id(monkeypatch, tmp_path):
    """发布调用必须把 execution_id 作为 correlation_id 关键字参数带上。"""
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(json.dumps({
        "exec-a": {"session_id": "sess-a"},
    }), encoding="utf-8")

    bus = _RecordingBus()
    _run_one_round(monkeypatch, sessions_path, bus)

    assert bus.publishes == [(
        EVENT_TYPE,
        {
            "status": "replied",
            "replies": ["同意"],
            "session_id": "sess-a",
            "correlation_id": "exec-a",
        },
    )]


def test_reply_event_reaches_real_event_filter(monkeypatch, tmp_path):
    """按发布参数装配的事件必须通过真实 EventFilter 并命中挂起订阅。"""
    execution_id = "exec-a"
    sessions_path = tmp_path / "sessions.json"
    sessions_path.write_text(json.dumps({
        execution_id: {"session_id": "sess-a"},
    }), encoding="utf-8")

    bus = _RecordingBus()
    _run_one_round(monkeypatch, sessions_path, bus)
    event_type, kwargs = bus.publishes[0]

    # 与 RedisEventBus.publish(str, **kwargs) 的装配语义一致：
    # correlation_id 走关键字参数，其余 kwargs 进 data。
    event = Event(
        event_type=event_type,
        data={k: v for k, v in kwargs.items() if k != "correlation_id"},
        correlation_id=kwargs.get("correlation_id"),
    )

    subscription = EventSubscription(
        event_type=EVENT_TYPE,
        correlation_id=execution_id,
        flow_id="flow-1",
        node_id="confirm",
    )
    # 第二重不命中（订阅侧按 execution_id 匹配）的直接证据。
    assert subscription.matches_event(event, {}) is (
        event.correlation_id == execution_id
    )

    async def _drive():
        subscriptions = InMemoryEventSubscriptionStorage()
        await subscriptions.store_subscription(subscription)
        execution_storage = _ProbeExecutionStorage(execution_id)
        redis_client = _FakeRedis()
        event_filter = EventFilter(
            execution_storage=execution_storage,
            subscription_storage=subscriptions,
            redis_client=redis_client,
            event_bus=bus,
            enable_subscription_timeout_checker=False,
        )
        await event_filter.handle_event(event)
        return execution_storage, redis_client

    execution_storage, redis_client = asyncio.run(_drive())

    # 入口门禁放行：按 correlation_id 取到了执行状态。
    assert execution_storage.loaded == [execution_id]
    # 订阅侧匹配并已入队 resume 任务。
    assert len(redis_client.xadds) == 1
    stream, fields = redis_client.xadds[0]
    assert stream == "plaita:flow:queue"
    resume_task = json.loads(fields["payload"])
    assert resume_task["execution_id"] == execution_id
    assert resume_task["resume_type"] == "event"
    assert resume_task["data"]["subscription_id"] == subscription.subscription_id
    assert resume_task["data"]["event_type"] == EVENT_TYPE
