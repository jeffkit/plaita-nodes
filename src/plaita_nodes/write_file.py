"""WriteFileNode —— 通用写文件节点（UTF-8，自动建父目录）。

``json=True`` 时把求值结果 ``json.dumps`` 后写入（用于产出
publish-payload.json 这类结构化文件）。

副作用说明：dry-run 下本节点**仍然写文件**（草稿目录本就是临时产物，
写出来便于人工检查）；不落盘的副作用（如 pool 入池）由各自节点尊重
globalContext.dry_run。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node


class WriteFileNode(Node):
    """写一个文件。

    JSON 字段：
    - ``path``: 目标路径（支持表达式）
    - ``content``: 内容（支持表达式）
    - ``json``: 为 true 时序列化为 JSON（默认 false）
    - ``indent``: JSON 缩进（默认 2）

    输出：``{"path", "size"}``。
    """

    node_type: ClassVar[str] = "writefile"
    node_name: ClassVar[str] = "写文件"

    path: Optional[Any] = None
    content: Optional[Any] = None
    json_mode: bool = Field(default=False, alias="json")
    indent: int = 2

    def execute(self, execution: Any) -> dict:
        if self.path is None:
            raise ValueError("writefile 节点缺少 path 字段")
        path = Path(str(execution.evaluate(self.path)))
        content = execution.evaluate(self.content) if self.content is not None else ""
        if self.json_mode:
            text = json.dumps(content, ensure_ascii=False, indent=self.indent)
        else:
            text = str(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"path": str(path), "size": len(text.encode("utf-8"))}
