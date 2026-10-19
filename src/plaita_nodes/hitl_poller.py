"""hitl_poller —— 挂起版 HITL 的跨进程恢复桥。

职责：轮询 hitl-server 的各待确认 session，回复到达即向 EventBus 发布
``hitl_reply`` 事件，唤醒挂起的 HitlAwaitNode（Distributed 模式）。

session 映射文件（JSON）：``{execution_id: {"session_id": ..., "node_id": ...}}``，
由调用方在挂起后从 context 提取写入（或由 FlowWorker 自动落盘）。
**每轮轮询前重读文件**：启动后新增的挂起 session 下一轮即可见。发布成功后
只按本轮实际发布的键做增量删除，并以「同目录临时文件 + fsync + os.replace」
原子替换——既不覆盖并发写入者刚追加的条目，也不让读者看到半截 JSON。

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
import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

import requests

EVENT_TYPE = "hitl_reply"
TERMINAL_STATUSES = ("replied", "timeout")


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


def _load_sessions(path: Path) -> dict | None:
    """读取 session 映射（每轮调用）。

    文件缺失/为空 → 空映射；不可读/损坏 → ``None``（本轮读不到可信内容）。
    调用方拿到 ``None`` 必须跳过删除写回——不能把临时读失败当成空映射，
    否则会整份抹掉并发写入者的条目。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        print(f"⚠️ 读取 session 映射失败（{path}）：{exc}", file=sys.stderr)
        return None
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        print(f"⚠️ session 映射不是合法 JSON（{path}），本轮跳过：{exc}",
              file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"⚠️ session 映射顶层不是对象（{path}），本轮跳过", file=sys.stderr)
        return None
    return data


def _write_sessions_atomic(path: Path, sessions: dict) -> None:
    """同目录临时文件 + fsync + os.replace；失败清理临时文件、目标不变。"""
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(sessions, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if target.exists():
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _drop_published_sessions(path: Path, execution_ids: list[str]) -> None:
    """从**最新盘上内容**增量删除本轮已发布键，原子替换。

    重读发生在写回之前：并发写入者在本轮轮询期间追加的条目会保留下来；
    只 pop 本轮确实发布成功的 execution_id，不再整份覆盖。
    """
    current = _load_sessions(path)
    if current is None:
        print("⚠️ 无法重读 session 映射，已发布条目保留待下轮重试", file=sys.stderr)
        return
    changed = False
    for execution_id in execution_ids:
        if execution_id in current:
            current.pop(execution_id)
            changed = True
    if changed:
        _write_sessions_atomic(path, current)


def _publish(bus, data: dict) -> None:
    async def _run():
        await bus.publish(EVENT_TYPE, **data)

    # 单条发布在独立事件循环里完成（与任何总线实现的 async publish 契约一致）。
    asyncio.run(_run())


def poll_round(sessions_path: Path, hitl_url: str, bus) -> list[dict]:
    """执行一轮：重读映射 → 轮询 → 发布 → 按成功发布的键增量删除。"""
    sessions = _load_sessions(sessions_path)
    if sessions is None:
        return []
    events = poll_once(sessions, hitl_url)
    published: list[str] = []
    for event in events:
        execution_id, data = event["execution_id"], event["data"]
        print(f"  📨 {execution_id} → {data.get('status')}")
        try:
            _publish(bus, data)
        except Exception as exc:  # noqa: BLE001 — 单条失败不杀常驻进程，下轮重试
            print(f"⚠️ 发布 {EVENT_TYPE} 失败（{execution_id}）：{exc}",
                  file=sys.stderr)
            continue
        if data.get("status") in TERMINAL_STATUSES:
            published.append(execution_id)
    if published:
        _drop_published_sessions(sessions_path, published)
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hitl_poller", description=__doc__)
    parser.add_argument("--hitl-url", default=os.environ.get("HITL_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--sessions", required=True, help="session 映射 JSON 文件")
    parser.add_argument("--bus", default="memory", help="memory | redis://...")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    sessions_path = Path(args.sessions)
    sessions = _load_sessions(sessions_path)
    if sessions is None:
        return 1
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
        poll_round(sessions_path, args.hitl_url, bus)
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
