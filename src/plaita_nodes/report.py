"""ReportNode —— run 级结果通道原子（通用 jsonl 追加/读取）。

用途：map 子流程（或任意分支）把结构化结果写入 run 级报告文件，主流程聚合
节点按同一 token 读取——绕开两个内核限制：map 子流程 end 的表达式递归限制、
if 块内赋值对块外不可见的作用域隔离。

**这是 workaround**：内核修复后业务流程应迁回原生表达、本节点降级/移除——
追踪 jeffkit/plaita#16。

文件布局：``<base_dir>/<token>.jsonl``，每行一个 JSON 对象。
base_dir 默认 ``<repo>/.flowcast/plaita-reports/``，可用 ``base_dir`` 字段覆盖。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node


def report_path(base_dir: str, token: str) -> Path:
    return Path(base_dir) / f"{token}.jsonl"


def append_entry(base_dir: str, token: str, entry: dict) -> None:
    path = report_path(base_dir, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_entries(base_dir: str, token: str) -> list[dict]:
    path = report_path(base_dir, token)
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


class ReportNode(Node):
    """写/读 run 级 jsonl 结果通道。

    JSON 字段：
    - ``action``: ``write``（默认）| ``read``
    - ``entry``: write 时要写入的对象（支持表达式）
    - ``base_dir``: 报告目录（默认 ``<repo>/.flowcast/plaita-reports``）
    - ``token``: run 令牌（默认 ``default``；建议主流程注入全局唯一值）

    输出：write → ``{"written": True, "entry"}``；read → ``{"entries": [...]}``。
    """

    node_type: ClassVar[str] = "report"
    node_name: ClassVar[str] = "结果通道"

    action: str = "write"
    entry: Optional[Any] = None
    repo: Optional[Any] = None
    base_dir: Optional[Any] = None
    token: Optional[Any] = None

    def _dir(self, execution: Any) -> str:
        base = execution.evaluate(self.base_dir) if self.base_dir is not None else None
        if base:
            return str(base)
        repo = execution.evaluate(self.repo) if self.repo is not None else ""
        return str(Path(str(repo or ".")) / ".flowcast" / "plaita-reports")

    def _token(self, execution: Any) -> str:
        token = execution.evaluate(self.token) if self.token is not None else None
        return str(token or execution.get_global_variable("run_token", "default") or "default")

    def execute(self, execution: Any) -> dict:
        base_dir = self._dir(execution)
        token = self._token(execution)
        if self.action == "read":
            return {"entries": read_entries(base_dir, token)}
        entry = execution.evaluate(self.entry) if self.entry is not None else {}
        if not isinstance(entry, dict):
            entry = {"value": entry}
        append_entry(base_dir, token, entry)
        return {"written": True, "entry": entry}
