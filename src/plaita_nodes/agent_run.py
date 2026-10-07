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
- ``details=true`` 时 recursive 走 stream-json 输出，节点解析内部事件
  （工具调用、文本轮）为 ``observations`` 列表附进输出，供观测侧在
  agent span 下建子 observation——打开"agent 内部循环"的可见性。

日志脱敏：任何路径都不打印 apiKey / ANTHROPIC_AUTH_TOKEN。

沙箱执行（docs/sandbox-drivers-design.md v2.1）：节点声明 ``workspace``（infra 注册表
``.plaita/sandboxes.json`` 里的名字，可表达式）即进沙箱——driver 产包裹 argv，
agentproc runner 仍是唯一执行者；密钥只经 envfile 进容器，回传经 canary 脱敏。
``repo`` 直跑路径与无 workspace 的存量 flow 行为逐字节不变。

多租户 fail-closed（#5）：租户上下文非 default 时 ``repo`` 直跑默认拒绝
（``PLAITA_ALLOW_HOST_AGENT_RUN=1`` 由部署方显式放开）——直跑是 agent CLI 在
worker 宿主上免审批执行，只有沙箱才有 VM 边界。流式辅助路径
``recursive_stream_turn`` 的宿主 env 同样经白名单重建。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from typing import Any, ClassVar, Optional

from pydantic import Field

from plaita import Node

from ._subprocess_env import build_subprocess_env
from .config import EXECUTOR_ALIASES, resolve_agent


class AgentRunError(RuntimeError):
    pass


def _ap_exit_cancelled() -> int:
    """agentproc 的 EXIT_CANCELLED（125）。agentproc 缺席时回退字面量——本常
    量在运行器契约层固定，直跑与沙箱两条路径共用。"""
    try:
        from agentproc.runner import EXIT_CANCELLED
        return int(EXIT_CANCELLED)
    except Exception:  # noqa: BLE001 — 可选依赖缺席
        return 125


_log = logging.getLogger(__name__)

# 多租户直跑 fail-closed（#5）：租户任务的 agent 默认只能进沙箱（``workspace``），
# 宿主直跑要部署方显式放开——直跑 = agent CLI 在 worker 宿主上免审批执行，
# 能读平台凭据与其它租户的产物。
HOST_RUN_OPT_OUT_ENV = "PLAITA_ALLOW_HOST_AGENT_RUN"
_opt_out_warned = False


def _multi_tenant_scope() -> Optional[str]:
    """多租户档的当前租户 id；单租户（无租户上下文 / default 租户）→ None。"""
    try:
        from plaita.tenant_context import DEFAULT_TENANT_ID, current_tenant
    except ImportError:  # plaita 0.5.x 无租户上下文 = 单租户
        return None
    tenant = current_tenant() or DEFAULT_TENANT_ID
    return None if tenant == DEFAULT_TENANT_ID else tenant


def _host_run_denied() -> Optional[str]:
    """宿主直跑应被拒时返回租户 id（None = 放行）。

    单机信任部署可显式设 ``PLAITA_ALLOW_HOST_AGENT_RUN=1`` 回到历史行为
    （每次进程生命周期内告警一次）。
    """
    tenant = _multi_tenant_scope()
    if tenant is None:
        return None
    if os.environ.get(HOST_RUN_OPT_OUT_ENV, "").strip() == "1":
        global _opt_out_warned
        if not _opt_out_warned:
            _opt_out_warned = True
            _log.warning("多租户 agent 宿主直跑被 %s=1 放行（tenant=%s）："
                         "agent 及其子进程可读宿主凭据，仅限单机信任部署",
                         HOST_RUN_OPT_OUT_ENV, tenant)
        return None
    return tenant


def _run_lock_key(workspace: str, run_key: Optional[str] = None) -> str:
    """遗言锁键 = workspace 绝对路径 (+ run 作用域)。

    同 workspace 的并发 run 各持一把锁：后开工者的 preflight 看不到兄弟 run
    的锁，不会把对方仍存活的 agent 当孤儿 killpg。
    """
    ws = os.path.abspath(workspace)
    return f"{ws}#{run_key}" if run_key else ws


def preflight_workspace(workspace: str, *, run_key: Optional[str] = None,
                        grace_secs: float = 10.0) -> dict:
    """kill-before-start：开工前清理同 workspace 的孤儿 agent 进程组（G3）。

    孤儿 = worker 被 SIGKILL/OOM 硬死后遗存、仍在写 worktree 的 agent CLI
    进程组，经 agentproc 遗言锁（run_lock）定位。任一新 agent 开工前都应走
    这里——时序上保证「旧的死了新的才开工」，根除并发写。busy（疑似占用但
    身份无法核实）→ AgentRunError：宁可不开工也不并发写。
    供 AgentRunNode 直跑路径与 keeper reaper（WIP 快照前）复用。

    ``run_key`` 非空时锁键带 run 作用域：只清本 run 的锁，同 workspace 其它
    run 的 live agent 与锁一律不碰。
    """
    from agentproc.run_lock import RunLockBusy, cleanup_stale_run

    try:
        info = cleanup_stale_run(_run_lock_key(workspace, run_key), grace_secs=grace_secs)
    except RunLockBusy as exc:
        raise AgentRunError(f"workspace 孤儿清场未完成，拒绝开工：{exc}") from exc
    if info.get("action") in ("killed", "stale"):
        _log.warning("preflight 清理孤儿 agent：workspace=%s run_key=%s action=%s pid=%s command=%s",
                     os.path.abspath(workspace), run_key or "", info.get("action"),
                     info.get("pid"), info.get("command") or "")
    return info


# 沙箱模式 runner 超时 = 沙箱内墙钟 + 余量（设计 §6.4：宿主兜底，击杀权在沙箱内）
_RUNNER_TIMEOUT_MARGIN = 120


def _kill_process_group(proc: subprocess.Popen) -> None:
    """超时兜底：SIGKILL 整个进程组并回收（同 capture/gate 的击杀形态）。

    Popen 侧 start_new_session 使子进程自成进程组，killpg 连 agent 拉起的
    子树一起清；Windows 无 killpg（AttributeError），退回单进程 kill。
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (AttributeError, ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# ── recursive 直调执行器（语义 = flowcast runRecursiveDirect）──────────

def _make_recursive_handlers():
    def build_args(message: str, session_id: str, env: dict) -> list:
        bin_name = env.get("RECURSIVE_BIN", "recursive")
        args = [bin_name, "--workspace", env.get("RECURSIVE_WORKSPACE", "."),
                "--output-format", env.get("RECURSIVE_OUTPUT_FORMAT", "json"),
                # 无头自动化语义：recursive-direct 无 permission 桥接，
                # 不显式放行时工具调用会发 control_request 等待批准直至挂起
                "--permission-mode", "auto"]
        if env.get("RECURSIVE_MODEL"):
            args += ["--model", env["RECURSIVE_MODEL"]]
        if env.get("RECURSIVE_MAX_STEPS"):
            args += ["--max-steps", str(env["RECURSIVE_MAX_STEPS"])]
        if session_id:
            # 会话续跑（L2）：resume 是独立子命令（--resume 旗标不能与子命令
            # 同用），message 走 -p/--message。子进程无 TTY = resume 的
            # orphan tool_call 走默认 abort——会话由我方管线写入，不应有孤儿；
            # headless/输出格式等全局旗标与 run 共用（前置已加）。
            args += ["resume", session_id, "--message", message]
        else:
            args += ["run", message]
        return args

    return {"build_args": build_args}


# ── stream-json 内部事件 → 观测 observations ────────────────────────────
# recursive --output-format stream-json 是 Claude 兼容 NDJSON：
#   {"type":"system","subtype":"init",...}（跳过）
#   {"type":"stream_event",...}（原始增量，跳过）
#   {"type":"assistant","message":{content:[{type:"tool_use",name,id,input}|
#                                         {type:"text",text}], model, ...}}
#   {"type":"user","message":{content:[{type:"tool_result",tool_use_id,content}]}}
#   {"type":"result",...,"usage":{...}}（终态对象，聚合 usage 由它出）

_DETAILS_CAP = 50  # 单次 agent 运行的观测条数上限，防超长会话撑爆 trace


def parse_stream_details(lines) -> list[dict]:
    """把 recursive stream-json 行流解析为观测 observations 列表。

    - ``assistant`` 的 tool_use 与后续 ``user`` 的 tool_result 按 tool_use_id
      配对 → ``{"type":"span","name":"tool:<名>","input","output"}``
    - ``assistant`` 的 text 块 → ``{"type":"generation","name":"turn:<n>",
      "model","output"}``（逐轮 usage 本仓 provider 不上报，留空）
    - system / stream_event / result 行跳过；超出 _DETAILS_CAP 截断。
    """
    observations: list[dict] = []
    pending_tools: dict[str, dict] = {}  # tool_use_id → tool_use block
    turn_no = 0
    for raw in lines:
        line = raw.strip() if isinstance(raw, str) else ""
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "assistant":
            message = event.get("message") or {}
            model = message.get("model")
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    pending_tools[block.get("id")] = block
                elif btype == "text" and (block.get("text") or "").strip():
                    turn_no += 1
                    observations.append({
                        "type": "generation", "name": f"turn:{turn_no}",
                        "model": model, "output": block.get("text"),
                    })
        elif etype == "user":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use = pending_tools.pop(block.get("tool_use_id"), None)
                if tool_use is None:
                    continue
                result = block.get("content")
                if isinstance(result, list):  # content block 数组 → 拼文本
                    result = "\n".join(
                        b.get("text", "") for b in result
                        if isinstance(b, dict) and b.get("type") == "text")
                observations.append({
                    "type": "span", "name": f"tool:{tool_use.get('name')}",
                    "input": tool_use.get("input"),
                    "output": result,
                })
        if len(observations) >= _DETAILS_CAP:
            break
    return observations[:_DETAILS_CAP]


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
    - ``workspace``: 沙箱 workspace 名（``.plaita/sandboxes.json`` 注册表，支持表达式；
      与 ``repo`` 互斥——workspace=沙箱执行，repo=宿主直跑）。未注册名 fail-closed；
      求值为空硬失败（$INPUT 缺键静默 None 防线，设计 §4）
    - ``timeout_secs``: 超时秒数（默认 1800；flowcast 的 recursive 直路径无超时，
      这里是行为改进）
    - ``details``: 为 true 时收集 agent 内部事件（工具调用、文本轮）为输出
      里的 ``observations`` 列表，供观测侧建子 observation——recursive 切
      stream-json 后解析全文；claude-code 本走 stream-json，经协议行回调收集
    - ``dry_run``: 为 true（或流程 globalContext.dry_run=true）时不真正调用，
      返回 fake 文本
    - ``session``: 会话 id（支持表达式）。非空时 recursive 走 ``resume <sid>
      --message <prompt>`` 续会话而非 ``run``——断点续跑（L2）的接线面：前序
      run 的输出 ``session_id`` / 会话存储里检索到的 id 喂回来即续上下文。
      空/None = 全新 run（行为不变）。

    输出：``{"text", "cli", "model", "session_id", "usage", "dry_run"}``；
    ``details=true`` 且 recursive 时额外带 ``observations``。
    """

    node_type: ClassVar[str] = "agentrun"
    node_name: ClassVar[str] = "Agent 运行"

    agent: Optional[Any] = "glm-52"
    prompt: Optional[Any] = None
    repo: Optional[Any] = None
    workspace: Optional[Any] = None
    # 会话 id（支持表达式）：非空 → recursive 走 resume 续会话（L2 断点续跑）
    session: Optional[Any] = None
    # Any 而非 int：DSL 传参下是表达式串（issue-pipeline v0.3 per-repo 预算），
    # execute 内求值后转 int（pydantic 构造期会拒收 str 进 int 字段）
    timeout_secs: Any = Field(default=1800)
    details: bool = False
    dry_run: bool = False

    def validate(self) -> None:
        """构建期互斥校验（由 FlowBuilder.validate 调用；JSON 直载路径是 warning
        降级，运行期另有硬守卫）。"""
        if self.workspace is not None and self.repo is not None:
            raise AgentRunError(
                "workspace 与 repo 互斥：workspace=沙箱执行，repo=宿主直跑路径，二选一")

    def execute(self, execution: Any) -> dict:
        agent_name = str(execution.evaluate(self.agent)) if self.agent is not None else "glm-52"
        prompt = execution.evaluate(self.prompt) if self.prompt is not None else ""
        repo = execution.evaluate(self.repo) if self.repo else None
        # 会话续跑（L2）：空/None 退化为全新 run；非空 → executor 侧 resume 形态
        session_id = str(execution.evaluate(self.session) or "") if self.session is not None else ""
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))

        if dry:
            preview = prompt[:80] + ("…" if len(prompt) > 80 else "")
            return {"text": f"[dry-run] {agent_name} would run: {preview}",
                    "cli": agent_name, "model": None, "session_id": "",
                    "usage": None, "dry_run": True}

        # 沙箱分支（设计 §4/§8）：声明 workspace 即进沙箱；dry 最先判已保证
        # dry 下不解析注册表、不解析凭据、零 driver 调用。
        if self.workspace is not None:
            if repo:
                raise AgentRunError(
                    "workspace 与 repo 互斥：workspace=沙箱执行，repo=宿主直跑路径，二选一")
            return self._execute_sandboxed(execution, agent_name, str(prompt))

        denied_tenant = _host_run_denied()
        if denied_tenant:
            raise AgentRunError(
                f"多租户（tenant={denied_tenant}）不允许 agent 宿主直跑：节点声明 "
                f"workspace 走沙箱执行（VM 边界 + 密钥只经 envfile 进容器）；"
                f"单机信任部署可显式设 {HOST_RUN_OPT_OUT_ENV}=1 放行")

        profile = resolve_agent(agent_name, repo=repo)
        executor = profile["executor"]

        # kill-before-start（G3）：直跑路径的 repo 即 agent 的工作 worktree，
        # 开工前清掉同 workspace 的孤儿；本次 spawn 也落遗言锁供下轮清理。
        # 沙箱路径不走此门：VM 边界 + WorkspaceLease 已各管一摊。
        run_lock_key: Optional[str] = None
        if repo:
            # run 作用域取 execution_id（每 dispatch 唯一）⇒ 原子锁文件按 run 分开；
            # 无 execution_id 的执行桩回退裸 workspace 键（今日行为）。同 _execute_sandboxed。
            run_scope = str(getattr(execution, "execution_id", "") or "")
            run_lock_key = _run_lock_key(str(repo), run_scope or None)
            preflight_workspace(str(repo), run_key=run_scope or None)

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
        # details：recursive 切 stream-json 输出（plain 路径 reply=完整 stdout，
        # 全文后解析）；claude-code 本就走 stream-json（NDJSON 路径），经
        # on_protocol_line 收集原始行。两者事件同构（Claude 兼容），共用解析器。
        want_details = bool(execution.evaluate(self.details)) if self.details is not None else False
        if want_details and executor == "recursive":
            extra_env["RECURSIVE_OUTPUT_FORMAT"] = "stream-json"
        # 协议行始终收集（纯内存 append）：executor 在进程内 NDJSON 路径不回调
        # 通用 usage 捕获（claude-code 的 parse_event 不产 usage），节点的
        # usage/observations 都从这里取。
        protocol_lines: list[str] = []

        def _on_protocol_line(line: str) -> None:
            protocol_lines.append(line)

        # 观测归因（Langfuse generation 需 model 字段）：profile 显式 model →
        # provider 翻译出的执行器模型 env → agent 名兜底
        model = (profile.get("model")
                 or extra_env.get("RECURSIVE_MODEL")
                 or extra_env.get("CLAUDE_MODEL")
                 or agent_name)
        result = agentproc_run(
            {"executor": ap_executor},
            RunOptions(message=str(prompt), session_id=session_id,
                       extra_env=extra_env,
                       run_lock_key=run_lock_key,
                       timeout_secs=int(execution.evaluate(self.timeout_secs) or 1800),
                       # 协作式取消（2026-10 波次③步内中断）：worker 取消监听命中
                       # 标志键即置位 execution.cancel_event，agentproc 在等待子
                       # 进程期间观察到置位则分级击杀 agent 进程组（SIGTERM →
                       # kill_grace → SIGKILL），不再白等满 timeout_secs（默认
                       # 1800s）。无 cancel_event 的执行（单测/local 旧路径）→
                       # None，退化为纯超时驱动（现状）。
                       cancel_event=getattr(execution, "cancel_event", None),
                       on_protocol_line=_on_protocol_line),
        )
        if getattr(result, "exit_code", 0) == _ap_exit_cancelled():
            raise AgentRunError("agent 执行被取消（cancel_event 命中）")
        if result.error or result.exit_code != 0:
            raise AgentRunError(result.error or f"{agent_name} 退出码 {result.exit_code}")

        text, usage, observations = self._interpret_result(
            executor=executor, result=result, protocol_lines=protocol_lines,
            want_details=want_details)

        out = {"text": text, "cli": executor, "model": model,
               "session_id": result.session_id,
               "usage": usage, "dry_run": False}
        if observations is not None:
            out["observations"] = observations
        return out

    def _interpret_result(self, *, executor: str, result: Any,
                          protocol_lines: list, redactor: Any = None,
                          want_details: bool = False):
        """agentproc RunResult → (text, usage, observations)。

        直跑与沙箱两条路径共用；``redactor`` 非 None 时（沙箱路径）reply 与
        错误文案先过 canary 脱敏再解析（设计 §7.1）。协议行在追加时已脱敏。
        """
        red = (lambda t: t) if redactor is None else redactor.redact
        reply = red(result.reply)
        text = reply
        usage = result.usage
        observations: Optional[list[dict]] = None
        if executor == "recursive":
            parsed = extract_recursive_result(reply)
            if parsed.get("is_error"):
                raise AgentRunError(
                    f"recursive is_error: {red(str(parsed.get('result'))[:300])}")
            text = str(parsed.get("result") or "")
            # plain 直调路径 agentproc 解析不到 NDJSON 事件，usage 兜底取自
            # 结果对象本体（recursive --output-format json 自带 usage 字段）
            usage = result.usage or parsed.get("usage")
            if want_details:
                observations = parse_stream_details(reply.splitlines())
        elif protocol_lines:
            # claude-code 等流式执行器：终态 result 事件的 usage 兜底
            for raw in protocol_lines:
                stripped = raw.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result" and isinstance(event.get("usage"), dict):
                    usage = result.usage or event["usage"]
                    break
            if want_details:
                observations = parse_stream_details(protocol_lines)
        return text, usage, observations

    @staticmethod
    def _resolve_spec_env(value: Any) -> str:
        """注册表 env 值：字面量（加载时已 ${VAR} 插值）或凭据引用
        ``{"credential": 名, "field": 字段(默认 token)}``（经 plaita.credentials）。"""
        if isinstance(value, dict) and value.get("credential"):
            from plaita.credentials import CredentialError, get_credential
            try:
                bundle = get_credential(str(value["credential"]))
            except CredentialError as exc:
                raise AgentRunError(f"沙箱凭据解析失败：{exc}") from exc
            field_name = str(value.get("field") or "token")
            resolved = bundle.get(field_name)
            if resolved is None:
                raise AgentRunError(
                    f"凭据 '{value['credential']}' 缺少字段 {field_name}")
            return str(resolved)
        return str(value)

    def _execute_sandboxed(self, execution: Any, agent_name: str, prompt: str) -> dict:
        """workspace 沙箱执行（设计 §3/§4/§6/§7）。

        顺序即纪律：注册表 fail-closed → lease 快速失败 → 凭据/env 白名单 →
        envfile(0600) → ensure（含抢占清残与幂等 provision）→ runner 执行 →
        失败 enforce → 脱敏回传；finally 焚 envfile、释放租约。
        """
        from . import sandbox as sb
        from .sandbox_docker import DockerDriver  # noqa: F401  # import 即注册 docker driver

        ws_key = execution.evaluate(self.workspace)
        ws_key = "" if ws_key is None else str(ws_key).strip()
        if not ws_key:
            raise AgentRunError(
                "workspace 求值为空——拒绝执行（$INPUT 缺键会静默 None，"
                "空名会让所有迭代共享同一沙箱，设计 §4 None 静默守卫）")
        execution_id = str(getattr(execution, "execution_id", "") or "")
        if not execution_id:
            raise AgentRunError("execution 缺少 execution_id，无法派生 workspace handle")

        spec = sb.load_sandboxes().get(ws_key)
        if spec is None:
            raise AgentRunError(
                f"workspace '{ws_key}' 未注册（fail-closed，运行期不自动创建）："
                f"请在 .plaita/sandboxes.json 定义后按名引用")
        # provision.git.branch 允许 flow 表达式（首个引用节点求值；须确定性，设计 §4）
        git = (spec.provision or {}).get("git")
        if isinstance(git, dict) and git.get("branch") is not None:
            git["branch"] = str(execution.evaluate(git.get("branch")) or "").strip() or None

        driver = sb.get_driver(spec.driver)
        if driver is None:
            raise AgentRunError(
                f"沙箱 driver '{spec.driver}' 未注册（可用：{sorted(sb.SANDBOX_DRIVERS)}）")

        profile = resolve_agent(agent_name, repo=None)
        executor = profile["executor"]
        register_recursive_direct()
        from agentproc import EXECUTORS as AP_EXECUTORS

        ap_executor = EXECUTOR_ALIASES.get(executor, executor)
        if ap_executor not in AP_EXECUTORS:
            raise AgentRunError(
                f"执行器 '{executor}' 未接入（agentproc 可用：{sorted(AP_EXECUTORS)}）；"
                f"如需其他 CLI 请在 agentproc executor 层扩展或加 EXECUTOR_ALIASES 映射"
            )
        sandbox_executor = sb.register_sandbox_executor(ap_executor)

        from agentproc.runner import RunOptions
        from agentproc.runner import run as agentproc_run

        wall_secs = int(execution.evaluate(self.timeout_secs) or 1800)
        runner_timeout = wall_secs + _RUNNER_TIMEOUT_MARGIN

        # env 白名单 = provider 翻译/agents.json env + 注册表 env；密钥只进 envfile
        env_whitelist = dict(profile["env"])
        for key, value in (spec.env or {}).items():
            env_whitelist[str(key)] = self._resolve_spec_env(value)
        redactor = sb.Redactor(env_whitelist)

        lease = sb.WorkspaceLease(
            sb.default_lease_store(), sb.handle_id(execution_id, ws_key),
            ttl=runner_timeout + 60.0)
        lease.acquire()  # 冲突 → SandboxLeaseError（快速失败，error 终态 + 指引重投）
        envfile = sb.write_envfile(env_whitelist)
        protocol_lines: list[str] = []

        def _on_protocol_line(line: str) -> None:
            protocol_lines.append(redactor.redact(line))

        try:
            lease.start_heartbeat()
            handle = driver.ensure(spec, execution_id, ws_key)

            extra_env = sb.sandbox_extra_env(handle, spec, wall_secs, envfile,
                                             execution_id)
            extra_env["RECURSIVE_WORKSPACE"] = handle.path  # 容器内路径（非宿主路径）
            # argv 旋钮（非密钥）：build_args 组装 argv 需要它们；密钥不走 extra_env
            for knob in ("RECURSIVE_MODEL", "RECURSIVE_MAX_STEPS"):
                if profile["env"].get(knob):
                    extra_env[knob] = profile["env"][knob]
            want_details = bool(execution.evaluate(self.details)) if self.details is not None else False
            if want_details and executor == "recursive":
                extra_env["RECURSIVE_OUTPUT_FORMAT"] = "stream-json"
            model = (profile.get("model")
                     or extra_env.get("RECURSIVE_MODEL")
                     or agent_name)

            result = agentproc_run(
                {"executor": sandbox_executor},
                RunOptions(message=prompt, extra_env=extra_env,
                           timeout_secs=runner_timeout,
                           # 协作式取消（波次③）：命中即击杀宿主侧包装进程组，
                           # 沙箱内的 driver.enforce 兜底最终击杀权（设计 §6.4）。
                           cancel_event=getattr(execution, "cancel_event", None),
                           on_protocol_line=_on_protocol_line),
            )
            if getattr(result, "exit_code", 0) == _ap_exit_cancelled():
                driver.enforce(handle)
                raise AgentRunError("agent 沙箱执行被取消（cancel_event 命中）")
            if result.error or result.exit_code != 0:
                driver.enforce(handle)  # 最终击杀权在 driver（设计 §6.4）
                raise AgentRunError(
                    redactor.redact(result.error or f"{agent_name} 退出码 {result.exit_code}"))

            text, usage, observations = self._interpret_result(
                executor=executor, result=result, protocol_lines=protocol_lines,
                redactor=redactor, want_details=want_details)
        finally:
            sb.burn_envfile(envfile)
            lease.release()

        out = {"text": text, "cli": executor, "model": model,
               "session_id": result.session_id,
               "usage": usage, "dry_run": False}
        if observations is not None:
            out["observations"] = observations
        out["workspace"] = handle.snapshot(env_names=env_whitelist.keys())
        return out


def recursive_stream_turn(task: str, *, workspace: str, profile: str = "glm-52",
                          model: Optional[str] = None,
                          max_steps: Optional[int] = None,
                          timeout_secs: int = 1800,
                          run_key: Optional[str] = None):
    """以流式方式跑一轮 recursive Agent（生成器：yield 事件 dict）。

    事件序列：
        {"type": "line", "text": str}            # agent 原始输出行（宿主可实时展示）
        {"type": "done", "ok": bool, "result": str, "error": str}
    供宿主进程（如 plaita-console 的 AI 流程生成）把编码 Agent 作为生成后端复用。
    """
    import time as _time

    workspace_abs = os.path.abspath(workspace)
    # 无 execution 上下文的宿主（console）：默认以宿主进程为 run 身份，避免两个
    # console 进程同 workspace 互相当孤儿杀；需要跨重启清场时由宿主显式传稳定 run_key。
    scope = run_key or f"host-{os.getpid()}"
    lock_key = _run_lock_key(workspace_abs, scope)
    preflight_workspace(workspace_abs, run_key=scope)

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
    # 子进程必须拿到 provider 凭证 env（否则无凭证运行得到空回复）；宿主
    # 其余环境经公共层白名单过滤，不整份继承（见 _subprocess_env）
    proc_env = build_subprocess_env(env_extra)
    # 独立进程组：超时 killpg 连 agent 拉起的子树一起清（同 capture/gate 形态）
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=workspace, env=proc_env,
                            start_new_session=True)
    from agentproc import run_lock as _run_lock
    _run_lock.write_run_lock(lock_key, proc.pid, argv)
    try:
        yield from _stream_turn_body(proc, timeout_secs)
    finally:
        _run_lock.clear_run_lock(lock_key)


def _stream_turn_body(proc: subprocess.Popen, timeout_secs: int):
    import time as _time

    lines: list[str] = []
    deadline = _time.monotonic() + timeout_secs
    timed_out = {"flag": False}

    def _drain_stderr() -> None:
        # stderr 排水线程：stderr=PIPE 却无人消费时，子进程写满管道缓冲
        # （Linux 上 ~64KB）即阻塞，stdout 永不 EOF → 调用方永久挂死。
        try:
            for _ in proc.stderr:  # type: ignore[union-attr]
                pass
        except (ValueError, OSError):
            pass

    def _enforce_deadline() -> None:
        # 看门狗线程：deadline 到而进程仍在跑 → 击杀整个进程组。逐行读
        # stdout 只在行到达时才有机会检查 deadline，进程无输出挂死时
        # 只有这条路能保证调用方不永久阻塞。
        remaining = deadline - _time.monotonic()
        if remaining > 0:
            _time.sleep(remaining)
        if proc.poll() is None:
            timed_out["flag"] = True
            _kill_process_group(proc)

    threading.Thread(target=_drain_stderr, daemon=True).start()
    threading.Thread(target=_enforce_deadline, daemon=True).start()

    for line in proc.stdout:  # type: ignore[union-attr]
        yield {"type": "line", "text": line.rstrip("\n")}
        lines.append(line)

    # stdout EOF 后等退出，仍受 deadline 约束（历史实现 proc.wait() 无限等、
    # timed_out 在 wait 之后才判——进程挂死时调用方永久阻塞且超时形同虚设）。
    # 看门狗击杀后 stdout 同样 EOF，wait 会立即返回被杀退出码。
    try:
        proc.wait(timeout=max(deadline - _time.monotonic(), 0.0))
    except subprocess.TimeoutExpired:
        timed_out["flag"] = True
        _kill_process_group(proc)

    stdout = "".join(lines)
    if timed_out["flag"]:
        yield {"type": "done", "ok": False, "result": "", "error": f"agent 超时（>{timeout_secs}s）"}
        return
    parsed = extract_recursive_result(stdout)
    if parsed.get("is_error"):
        yield {"type": "done", "ok": False, "result": "",
               "error": f"recursive is_error: {str(parsed.get('result'))[:300]}"}
        return
    yield {"type": "done", "ok": True, "result": parsed.get("result") or "", "error": ""}
