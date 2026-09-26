"""AgentRunNode —— 经 agentproc 调用 Agent CLI（recursive / claude / …）。

设计（ADR-2026-08-27）：
- 配置层复用 flowcast 的 agents.json / providers.json（见 config.py）；
- 执行层走 agentproc Python SDK 的 in-process executor 机制；
- ``recursive-direct`` 是本包注册的直调执行器，语义照搬 flowcast 的
  ``runRecursiveDirect``：``recursive --workspace . --output-format json
  [--model M] [--max-steps N] run "<prompt>"``，stdout 末尾为单个 JSON
  对象，取 ``.result`` 为回复、``.is_error`` 为失败。
- 输出：``{"text", "cli", "model", "session_id", "usage", "dry_run"}``，
  下游用 ``$NODE.<id>.text`` 引用。``model`` 供观测侧（plaita.obs 的
  generation 判定）归因 token 用量：profile.model → RECURSIVE_MODEL → agent 名。

日志脱敏：任何路径都不打印 apiKey / ANTHROPIC_AUTH_TOKEN。
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node

from .config import EXECUTOR_ALIASES, resolve_agent


class AgentRunError(RuntimeError):
    pass


# ── recursive 直调执行器（语义 = flowcast runRecursiveDirect）──────────

def _make_recursive_handlers():
    def build_args(message: str, session_id: str, env: dict) -> list:
        bin_name = env.get("RECURSIVE_BIN", "recursive")
        args = [bin_name, "--workspace", env.get("RECURSIVE_WORKSPACE", "."),
                "--output-format", "json"]
        if env.get("RECURSIVE_MODEL"):
            args += ["--model", env["RECURSIVE_MODEL"]]
        if env.get("RECURSIVE_MAX_STEPS"):
            args += ["--max-steps", str(env["RECURSIVE_MAX_STEPS"])]
        args += ["run", message]
        return args

    return {"build_args": build_args}


def register_recursive_direct() -> None:
    """注册 recursive-direct 执行器到 agentproc。

    agentproc 是可选依赖：只在调用本函数时（``AgentRunNode`` 执行前、
    或 :func:`recursive_stream_turn`）才 import。不在模块级调用，
    保证未安装 agentproc 时 ``import plaita_nodes`` 仍然可用。
    """
    from agentproc import EXECUTORS

    if "recursive-direct" not in EXECUTORS:
        EXECUTORS["recursive-direct"] = {
            "cli_name": "recursive",
            "install_hint": "brew install kongjie/tap/recursive（或 cargo build --release）",
            # plain：stdout 整体作为 reply（recursive --output-format json 输出单个
            # JSON 对象而非 NDJSON），由本节点再做 .result 提取。
            "plain": True,
            "make_handlers": _make_recursive_handlers,
        }


def extract_recursive_result(stdout: str) -> dict:
    """从 recursive 的 stdout 提取末尾 JSON 对象（``--output-format json`` 为
    “单个结果对象收尾”）。

    策略：优先取**行首 ``{`` 起始**的最后一个可解析对象（日志噪声多出现在行中）；
    退回首个 ``{"`` 的括号配对扫描（跳过字符串内花括号，与 flowcast
    extractRecursiveJson 同款）；全部失败则把原文当 result 返回。
    """
    line_starts: list[int] = []
    lines = stdout.split("\n")
    offset = 0
    for line in lines:
        if line.lstrip().startswith("{"):
            line_starts.append(offset)
        offset += len(line) + 1
    find_idx = stdout.find('{"')

    # 扫描顺序：行首对象从后往前优先（结果对象在末尾），find('{"') 兜底
    scan_order = list(reversed(line_starts))
    if find_idx >= 0:
        scan_order.append(find_idx)

    for start in scan_order:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(stdout)):
            ch = stdout[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stdout[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return {"result": stdout, "is_error": False, "raw": True}


class AgentRunNode(Node):
    """运行一个 Agent CLI 并返回其文本回复。

    JSON 字段：
    - ``agent``: agents.json 里的名字（默认 ``glm-52``），支持表达式
    - ``prompt``: prompt 文本，支持 ``{% ... %}`` 模板表达式
    - ``repo``: 工作目录（recursive 的 ``--workspace``；默认进程 cwd）
    - ``timeout_secs``: 超时秒数（默认 1800；flowcast 的 recursive 直路径无超时，
      这里是行为改进）
    - ``dry_run``: 为 true（或流程 globalContext.dry_run=true）时不真正调用，
      返回 fake 文本

    输出：``{"text", "cli", "model", "session_id", "usage", "dry_run"}``。
    """

    node_type: ClassVar[str] = "agentrun"
    node_name: ClassVar[str] = "Agent 运行"

    agent: Optional[Any] = "glm-52"
    prompt: Optional[Any] = None
    repo: Optional[Any] = None
    timeout_secs: int = Field(default=1800)
    dry_run: bool = False

    def execute(self, execution: Any) -> dict:
        agent_name = str(execution.evaluate(self.agent)) if self.agent is not None else "glm-52"
        prompt = execution.evaluate(self.prompt) if self.prompt is not None else ""
        repo = execution.evaluate(self.repo) if self.repo else None
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))

        if dry:
            preview = prompt[:80] + ("…" if len(prompt) > 80 else "")
            return {"text": f"[dry-run] {agent_name} would run: {preview}",
                    "cli": agent_name, "model": None, "session_id": "",
                    "usage": None, "dry_run": True}

        profile = resolve_agent(agent_name, repo=repo)
        executor = profile["executor"]

        # agentproc 延迟到执行时才 import（可选依赖，见 register_recursive_direct）
        register_recursive_direct()
        from agentproc import EXECUTORS as AP_EXECUTORS

        # 显式别名映射优先；否则若 agentproc 已内置同名 executor（或测试桩）直接使用
        ap_executor = EXECUTOR_ALIASES.get(executor, executor)
        if ap_executor not in AP_EXECUTORS:
            raise AgentRunError(
                f"执行器 '{executor}' 未接入（agentproc 可用：{sorted(AP_EXECUTORS)}）；"
                f"如需其他 CLI 请在 agentproc executor 层扩展或加 EXECUTOR_ALIASES 映射"
            )

        from agentproc.runner import RunOptions
        from agentproc.runner import run as agentproc_run

        extra_env = dict(profile["env"])
        extra_env.setdefault("RECURSIVE_WORKSPACE", repo or ".")
        # 观测归因（Langfuse generation 需 model 字段）：profile 显式 model →
        # provider 翻译出的 RECURSIVE_MODEL → agent 名兜底
        model = (profile.get("model") or extra_env.get("RECURSIVE_MODEL") or agent_name)
        result = agentproc_run(
            {"executor": ap_executor},
            RunOptions(message=str(prompt), extra_env=extra_env,
                       timeout_secs=self.timeout_secs),
        )
        if result.error or result.exit_code != 0:
            raise AgentRunError(result.error or f"{agent_name} 退出码 {result.exit_code}")

        text = result.reply
        usage = result.usage
        if executor == "recursive":
            parsed = extract_recursive_result(result.reply)
            if parsed.get("is_error"):
                raise AgentRunError(f"recursive is_error: {str(parsed.get('result'))[:300]}")
            text = str(parsed.get("result") or "")
            # plain 直调路径 agentproc 解析不到 NDJSON 事件，usage 兜底取自
            # 结果对象本体（recursive --output-format json 自带 usage 字段）
            usage = result.usage or parsed.get("usage")

        return {"text": text, "cli": executor, "model": model,
                "session_id": result.session_id,
                "usage": usage, "dry_run": False}


def recursive_stream_turn(task: str, *, workspace: str, profile: str = "glm-52",
                          model: Optional[str] = None,
                          max_steps: Optional[int] = None,
                          timeout_secs: int = 1800):
    """以流式方式跑一轮 recursive Agent（生成器：yield 事件 dict）。

    事件序列：
        {"type": "line", "text": str}            # agent 原始输出行（宿主可实时展示）
        {"type": "done", "ok": bool, "result": str, "error": str}
    供宿主进程（如 plaita-console 的 AI 流程生成）把编码 Agent 作为生成后端复用。
    """
    import time as _time

    register_recursive_direct()
    agent = resolve_agent(profile)
    env_extra = dict(agent["env"])
    env_extra.setdefault("RECURSIVE_WORKSPACE", workspace)
    if model:
        env_extra["RECURSIVE_MODEL"] = model
    handlers = _make_recursive_handlers()
    if agent.get("model"):
        env_extra.setdefault("RECURSIVE_MODEL", agent["model"])
    argv = handlers["build_args"](task, "", env_extra)
    # 子进程必须拿到 provider 凭证 env（否则无凭证运行得到空回复）
    proc_env = {**os.environ, **env_extra}
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=workspace, env=proc_env)
    lines: list[str] = []
    deadline = _time.monotonic() + timeout_secs
    for line in proc.stdout:  # type: ignore[union-attr]
        yield {"type": "line", "text": line.rstrip("\n")}
        lines.append(line)
    proc.wait()
    timed_out = _time.monotonic() > deadline
    stdout = "".join(lines)
    if timed_out:
        yield {"type": "done", "ok": False, "result": "", "error": f"agent 超时（>{timeout_secs}s）"}
        return
    parsed = extract_recursive_result(stdout)
    if parsed.get("is_error"):
        yield {"type": "done", "ok": False, "result": "",
               "error": f"recursive is_error: {str(parsed.get('result'))[:300]}"}
        return
    yield {"type": "done", "ok": True, "result": parsed.get("result") or "", "error": ""}
