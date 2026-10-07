"""plaita-nodes —— plaita 通用节点集（infra 级，ADR-2026-08-27）。

节点通过 pyproject 的 ``[project.entry-points."plaita.nodes"]`` 自动注册；
若以源码方式引入（未经 pip 安装），可显式调用 :func:`register_all`。
"""
from .agent_run import AgentRunError, AgentRunNode, extract_recursive_result, register_recursive_direct
from .api import ApiRequestNode, GenericWebhookNode
from .database import SqlQueryNode
from .decision import DECISION_PROVIDERS, DecisionError, DecisionNode, register_decision_provider
from .email import EmailSendNode
from .capture import CaptureConfigError, CaptureNode
from .github_comment import GithubCommentNode, redact_text
from .git_publish import GitPublishNode
from .parse_json import ParseJsonNode
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
from .report import ReportNode, append_entry, read_entries
from .notify import NotifyNode
from .notify_backends import NOTIFY_BACKENDS, get_notify_backend, register_notify_backend
from .sandbox import (
    SANDBOX_DRIVERS,
    Redactor,
    SandboxConfigError,
    SandboxError,
    SandboxLeaseError,
    SandboxPinError,
    WorkspaceHandle,
    WorkspaceLease,
    WorkspaceSpec,
    get_driver,
    load_sandboxes,
    register_driver,
    register_sandbox_executor,
)
from .sandbox_ags import AgsClient, AgsDriver, AgsError
from .sandbox_agent import SandboxAgentNode
from .sandbox_docker import DockerDriver
from .sandbox_krunvm import KrunvmDriver
from .sandbox_ssh import SshDriver
from .webhooks import DingtalkWebhookNode, FeishuWebhookNode, SlackWebhookNode, WecomWebhookNode
from .write_file import WriteFileNode

__version__ = "0.8.0"

_ALL_NODES = [AgentRunNode, GithubCommentNode, GitPublishNode, ParseJsonNode, ApiRequestNode, CaptureNode, DecisionNode, DingtalkWebhookNode, EmailSendNode, FeishuWebhookNode, GateNode, GenericWebhookNode, HitlAwaitNode, HitlNode, LlmNode, NotifyNode, RateLimitNode, ReportNode, SandboxAgentNode, SlackWebhookNode, SqlQueryNode, WecomWebhookNode, WriteFileNode]


def register_all() -> None:
    """显式注册全部节点到默认 registry（entry_points 之外的手动路径）。"""
    from plaita.node import get_default_registry

    registry = get_default_registry()
    for node_cls in _ALL_NODES:
        registry.register(node_cls)


__all__ = [
    "AgentRunNode", "AgentRunError", "extract_recursive_result", "register_recursive_direct",
    "CaptureNode", "CaptureConfigError",
    "HitlNode", "HitlAwaitNode", "HitlError", "LlmNode", "LlmError",
    "DecisionNode", "DecisionError", "register_decision_provider", "DECISION_PROVIDERS",
    "ReportNode", "append_entry", "read_entries",
    "NotifyNode", "NOTIFY_BACKENDS", "register_notify_backend", "get_notify_backend",
    "WriteFileNode",
    "GithubCommentNode", "redact_text",
    "GitPublishNode",
    "SandboxAgentNode", "AgsDriver", "AgsClient", "AgsError",
    "ParseJsonNode",
    "GateNode", "RateLimitNode",
    "ApiRequestNode", "GenericWebhookNode", "SqlQueryNode", "EmailSendNode",
    "FeishuWebhookNode", "WecomWebhookNode", "SlackWebhookNode", "DingtalkWebhookNode",
    "register_all",
    "load_merged_config", "resolve_agent", "resolve_provider", "interpolate",
    "AgentConfigError", "EXECUTOR_ALIASES",
    "SANDBOX_DRIVERS", "register_driver", "get_driver", "load_sandboxes",
    "register_sandbox_executor", "DockerDriver", "KrunvmDriver", "SshDriver",
    "WorkspaceSpec", "WorkspaceHandle", "WorkspaceLease", "Redactor",
    "SandboxError", "SandboxConfigError", "SandboxPinError", "SandboxLeaseError",
    "__version__",
]
