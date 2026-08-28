"""plaita-nodes —— plaita 通用节点集（infra 级，ADR-2026-08-27）。

节点通过 pyproject 的 ``[project.entry-points."plaita.nodes"]`` 自动注册；
若以源码方式引入（未经 pip 安装），可显式调用 :func:`register_all`。
"""
from .agent_run import AgentRunError, AgentRunNode, extract_recursive_result, register_recursive_direct
from .capture import CaptureConfigError, CaptureNode
from .config import (
    EXECUTOR_ALIASES,
    AgentConfigError,
    interpolate,
    load_merged_config,
    resolve_agent,
    resolve_provider,
)
from .hitl import HitlError, HitlNode
from .gate import GateNode
from .hitl_await import HitlAwaitNode
from .llm import LlmError, LlmNode
from .rate_limit import RateLimitNode
from .report import ReportNode, report_append, report_read
from .notify import NotifyNode
from .write_file import WriteFileNode

__version__ = "0.1.0"

_ALL_NODES = [AgentRunNode, CaptureNode, GateNode, HitlAwaitNode, HitlNode, LlmNode, NotifyNode, RateLimitNode, ReportNode, WriteFileNode]


def register_all() -> None:
    """显式注册全部节点到默认 registry（entry_points 之外的手动路径）。"""
    from plaita.node import get_default_registry

    registry = get_default_registry()
    for node_cls in _ALL_NODES:
        registry.register(node_cls)


__all__ = [
    "AgentRunNode", "AgentRunError", "extract_recursive_result", "register_recursive_direct",
    "CaptureNode", "CaptureConfigError",
    "HitlNode", "HitlAwaitNode", "HitlError", "LlmNode", "LlmError", "ReportNode", "report_append", "report_read",
    "NotifyNode",
    "WriteFileNode",
    "register_all",
    "load_merged_config", "resolve_agent", "resolve_provider", "interpolate",
    "AgentConfigError", "EXECUTOR_ALIASES",
    "__version__",
]
