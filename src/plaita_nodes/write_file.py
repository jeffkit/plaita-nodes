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

    # 进程级 jail 覆盖：部署方在注册/启动脚本里设置
    # ``WriteFile._WORKSPACE_ROOT_OVERRIDE = "/data/sandbox"`` 即全局生效
    _WORKSPACE_ROOT_OVERRIDE: ClassVar[Optional[str]] = None

    # 路径 jail（2026-09 安全评审 P1-2）：默认 None 保持历史行为（任意路径，
    # 含绝对路径与 ../ 穿越）。多租户/不受信流程部署必须设置——注册时经
    # ``WORKSPACE_ROOT`` 设定，或环境变量 PLAITA_NODES_WORKSPACE_ROOT；
    # 设置后所有写入被约束在 jail 内。
    path: Optional[Any] = None
    content: Optional[Any] = None
    json_mode: bool = Field(default=False, alias="json")
    indent: int = 2

    workspace_root: Optional[str] = Field(
        default=None,
        description="写入根目录；设置后 path 被约束在其内（相对与绝对路径均收敛到 jail）",
    )

    @classmethod
    def _effective_root(cls) -> Optional[str]:
        import os
        return cls._WORKSPACE_ROOT_OVERRIDE or os.environ.get("PLAITA_NODES_WORKSPACE_ROOT") or None

    def execute(self, execution: Any) -> dict:
        if self.path is None:
            raise ValueError("writefile 节点缺少 path 字段")
        raw = str(execution.evaluate(self.path))
        root = self.workspace_root or self._effective_root()
        path = Path(raw)
        if root:
            root_path = Path(root).resolve()
            candidate = (root_path / path).resolve() if not path.is_absolute() else path.resolve()
            try:
                candidate.relative_to(root_path)
            except ValueError:
                raise ValueError(
                    f"writefile path {raw!r} escapes workspace_root {str(root_path)!r}; "
                    "refusing to write outside the jail (set per-node workspace_root or "
                    "env PLAITA_NODES_WORKSPACE_ROOT to configure the jail)"
                )
            path = candidate
        content = execution.evaluate(self.content) if self.content is not None else ""
        if self.json_mode:
            text = json.dumps(content, ensure_ascii=False, indent=self.indent)
        else:
            text = str(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"path": str(path), "size": len(text.encode("utf-8"))}
