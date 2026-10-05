"""RateLimitNode —— 频率限制原子（文件计数器，对标 flowcast rate-limiter）。

按 key 追踪调用频率：check 判断是否超限，record 记录一次调用。
存储：``<store_dir>/<key>.json`` 记录时间戳列表（上限 100 条，足够日/周窗口）。

字段：
- ``action``: check | record | clear | acquire
- ``acquire``: **原子 check+record**（flock 文件锁互斥）——发布前一键判定并占坑。
  分开写 check、record 两节点时，两步之间 crash/并发会超限双发；acquire 把
  判定与记账收进同一临界区。返回在 check 形状上多 ``recorded`` 布尔，且
  ``today_count``/``week_count`` 为占坑后的计数。
- ``key``: 限频键（如 "xhs-publish"）
- ``daily``: 日限次（check/acquire 用）
- ``weekly``: 周限次（check/acquire 用）
- ``store_dir``: 计数器目录（默认 ~/.plaita-nodes/rate-limits）

输出（check）：``{allowed, reason, today_count, week_count}``。
输出（acquire）：``{allowed, recorded, reason, today_count, week_count}``。
输出（record）：``{recorded: True, key}``。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node


class RateLimitNode(Node):
    node_type: ClassVar[str] = "rate_limit"
    node_name: ClassVar[str] = "频率限制"

    action: str = Field(default="check", description="check | record | clear | acquire")
    key: Optional[Any] = Field(default=None, description="限频键")
    daily: int = Field(default=1)
    weekly: int = Field(default=5)
    store_dir: Optional[Any] = Field(default=None)

    def _dir(self, execution: Any) -> Path:
        d = execution.evaluate(self.store_dir) if self.store_dir else None
        return Path(str(d)) if d else Path.home() / ".plaita-nodes" / "rate-limits"

    def _key(self, execution: Any) -> str:
        return str(execution.evaluate(self.key) or "default")

    def _load(self, path: Path) -> list[float]:
        if not path.exists():
            return []
        try:
            return [float(t) for t in json.loads(path.read_text())]
        except Exception:
            return []

    def _save(self, path: Path, timestamps: list[float]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(timestamps), encoding="utf-8")

    def execute(self, execution: Any) -> dict:
        action = self.action
        key = self._key(execution)
        path = self._dir(execution) / f"{key}.json"
        now = time.time()

        if action == "check":
            stamps = self._load(path)
            day = [t for t in stamps if now - t < 86400]
            week = [t for t in stamps if now - t < 7 * 86400]
            allowed = len(day) < self.daily and len(week) < self.weekly
            reason = f"日 {len(day)}/{self.daily}，周 {len(week)}/{self.weekly}"
            return {"allowed": allowed, "reason": reason,
                    "today_count": len(day), "week_count": len(week)}
        if action == "acquire":
            return self._acquire(path, now)
        if action == "record":
            stamps = self._load(path) + [now]
            self._save(path, stamps[-100:])
            return {"recorded": True, "key": key, "total": len(stamps)}
        if action == "clear":
            self._save(path, [])
            return {"cleared": True, "key": key}
        raise ValueError(f"未知 rate_limit action: {action}（check|record|clear|acquire）")

    def _acquire(self, path: Path, now: float) -> dict:
        """原子 check+record：flock 互斥内判定并写入，两步之间不会双发。

        flock 不可用的平台（非 POSIX）退化为非原子，行为等同 check+record 两步。
        """
        try:
            import fcntl
        except ImportError:  # pragma: no cover
            fcntl = None
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+") as f:
            try:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                stamps = self._load(path)
                day = [t for t in stamps if now - t < 86400]
                week = [t for t in stamps if now - t < 7 * 86400]
                allowed = len(day) < self.daily and len(week) < self.weekly
                if allowed:
                    # 经持锁句柄写回，保证互斥窗口覆盖读-改-写全程
                    stamps = stamps + [now]
                    f.seek(0)
                    f.truncate()
                    f.write(json.dumps(stamps[-100:]))
                    # 释放锁前必须 flush：下一个持锁者经 path 重新读盘，
                    # 缓冲未落盘会读到旧内容而重复放行
                    f.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        day_n, week_n = len(day) + (1 if allowed else 0), len(week) + (1 if allowed else 0)
        return {"allowed": allowed, "recorded": allowed,
                "reason": f"日 {day_n}/{self.daily}，周 {week_n}/{self.weekly}",
                "today_count": day_n, "week_count": week_n}
