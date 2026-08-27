"""hitl_poller —— 挂起版 HITL 的跨进程恢复桥。

职责：轮询 hitl-server 的各待确认 session，回复到达即向 EventBus 发布
``hitl_reply`` 事件，唤醒挂起的 HitlAwaitNode（Distributed 模式）。

session 映射文件（JSON）：``{execution_id: {"session_id": ..., "node_id": ...}}``，
由调用方在挂起后从 context 提取写入（或由 FlowWorker 自动落盘）。

用法：
    python -m plaita_nodes.hitl_poller \
        --hitl-url http://127.0.0.1:8081 \
        --sessions sessions.json \
        --bus redis://127.0.0.1:6379/0 \
        [--interval 5]

bus 协议：发布事件 ``hitl_reply``，data = {"status": "replied", "replies": [...],
"session_id": ...}；session 显示 timeout 时发布 {"status": "timeout"}。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

EVENT_TYPE = "hitl_reply"


def poll_once(sessions: dict, hitl_url: str) -> list[dict]:
    """轮询一轮，返回待发布事件列表 [{execution_id, data}]。"""
    events: list[dict] = []
    for execution_id, info in sessions.items():
        session_id = str(info.get("session_id") or "")
        if not session_id:
            continue
        try:
            poll = requests.get(f"{hitl_url}/api/poll/{session_id}", timeout=15).json()
        except (requests.RequestException, ValueError):
            continue
        if poll.get("has_reply"):
            replies = [str(r.get("content") or r.get("text") or "")
                       for r in (poll.get("replies") or [])]
            events.append({"execution_id": execution_id,
                           "data": {"status": "replied", "replies": replies,
                                    "session_id": session_id}})
        elif poll.get("status") == "timeout":
            events.append({"execution_id": execution_id,
                           "data": {"status": "timeout", "replies": [],
                                    "session_id": session_id}})
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hitl_poller", description=__doc__)
    parser.add_argument("--hitl-url", default=os.environ.get("HITL_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--sessions", required=True, help="session 映射 JSON 文件")
    parser.add_argument("--bus", default="memory", help="memory | redis://...")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    sessions = json.loads(Path(args.sessions).read_text(encoding="utf-8"))
    if not sessions:
        print("无可轮询 session，退出")
        return 0

    if args.bus.startswith("redis://"):
        from plaita.event.redis import RedisEventBus
        bus = RedisEventBus(url=args.bus)
    else:
        from plaita.event.memory import InMemoryEventBus
        bus = InMemoryEventBus()

    print(f"hitl_poller 启动：{len(sessions)} 个 session，间隔 {args.interval}s，bus={args.bus}")
    while True:
        for event in poll_once(sessions, args.hitl_url):
            execution_id, data = event["execution_id"], event["data"]
            print(f"  📨 {execution_id} → {data.get('status')}")
            import anyio

            async def _publish():
                await bus.publish(EVENT_TYPE, **data)

            anyio.run(_publish)
            if data.get("status") in ("replied", "timeout"):
                sessions.pop(execution_id, None)
                Path(args.sessions).write_text(
                    json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
