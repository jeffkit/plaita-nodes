"""RateLimitNode —— 频率限制原子（文件计数器，对标 flowcast rate-limiter）。

按 key 追踪调用频率：check 判断是否超限，record 记录一次调用。
存储：``<store_dir>/<key>.json`` 记录时间戳列表。

字段：
- ``action``: check | record | clear
- ``key``: 限频键（如 "xhs-publish"）
- ``daily``: 日限次（check 用）
- ``weekly``: 周限次（check 用）
- ``store_dir``: 计数器目录（默认 ~/.plaita-nodes/rate-limits）

输出（check）：``{allowed, reason, today_count, week_count}``。
输出（record）：``{recorded: True, key}``。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node


class RateLimitNode(Node):
    node_type: ClassVar[str] = "rate_limit"
    node_name: ClassVar[str] = "频率限制"

    action: str = Field(default="check", description="check | record | clear")
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
        if action == "record":
            stamps = self._load(path) + [now]
            self._save(path, stamps[-100:])
            return {"recorded": True, "key": key, "total": len(stamps)}
        if action == "clear":
            self._save(path, [])
            return {"cleared": True, "key": key}
        raise ValueError(f"未知 rate_limit action: {action}")
