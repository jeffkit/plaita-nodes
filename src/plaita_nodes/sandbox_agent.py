"""SandboxAgentNode —— AGENTRUN 的沙箱变体（AGS 远程执行）。

与 ``AgentRunNode`` 的关系与差异：

- **接口同形**：字段/输出与 AGENTRUN 一致（``text`` / ``usage`` /
  ``session_id`` / ...），flow 里把 ``AGENTRUN(...)`` 换成
  ``SANDBOX_AGENT(...)`` 即可并列跑灰度——两边共用同一套门禁/发布节点，
  唯一变量是「agent 在哪执行」。
- **repo 语义**：仍是宿主工作区路径，但它是**同步源**（sync_in 源 +
  sync_out 落点），不是 agent 的工作目录；agent 在沙箱内工作。
- **复用同一沙箱**：``(execution_id, ws_key)`` 派生实例身份——同一 flow
  里 implement / fix / review 多次调用默认 ws_key="main"，**共享同一实例**
  （driver.ensure 幂等 attach）；implement 留下的工作区，review 直接接着看。
- **同步纪律**（补丁双向，宿主索引零触碰）：
  - ``sync_in``：宿主工作区脏改动（临时索引产 patch）→ 沙箱 ``reset --hard
    基线`` + apply → 提交 "plaita-sync-in"（沙箱树 == 宿主树）；
  - agent 在沙箱里干活；
  - ``sync_out``：沙箱增量 patch → 宿主 ``git apply``（不改索引）→ 沙箱
    提交归一，保持干净。

宿主前置（worker env）：``E2B_DOMAIN`` + ``E2B_API_KEY``；沙箱镜像须自带
envd/expose 49983（见 ``sandbox_ags`` 模块 docstring）。``e2b`` 为可选依赖。

已知边界（v1）：``session`` 续跑暂未接线（每轮全新 run；prompt 自带上下文，
与 flow 的 fix-loop 提示词形态兼容）。
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar, Optional

from pydantic import Field

from .agent_run import (
    AgentRunError,
    AgentRunNode,
    _ap_exit_cancelled,
    register_recursive_direct,
)
from .config import EXECUTOR_ALIASES, resolve_agent

_log = logging.getLogger(__name__)


class SandboxAgentNode(AgentRunNode):
    """在 AGS 沙箱里跑 agent；宿主工作区经补丁双向同步。

    JSON 字段（在 AGENTRUN 基础上）：
    - ``sandbox``: ``.plaita/sandboxes.json`` 里的 spec 名（默认 ``ags``），
      支持表达式；spec.driver 必须是 ``ags``
    - ``ws_key``: 沙箱实例键（默认 ``main``）；同一 flow 内多次调用用同一
      键即共享同一实例（中断重投递后经元数据 attach 回来）

    输出与 AGENTRUN 同形（``workspace`` 快照额外带实例 id）。
    """

    node_type: ClassVar[str] = "sandbox_agent"
    node_name: ClassVar[str] = "沙箱 Agent 运行"

    sandbox: Optional[Any] = "ags"
    ws_key: Optional[Any] = None
    # 本节点 repo+sandbox 并存是常态（repo=同步源），覆盖父类互斥校验
    timeout_secs: Any = Field(default=3600)

    def validate(self) -> None:  # noqa: D102 - 覆盖父类互斥（见类 docstring）
        if self.prompt is None:
            raise AgentRunError("sandbox_agent 需要 prompt")

    def execute(self, execution: Any) -> dict:
        agent_name = str(execution.evaluate(self.agent)) if self.agent is not None else "glm-52"
        prompt = execution.evaluate(self.prompt) if self.prompt is not None else ""
        repo = str(execution.evaluate(self.repo)) if self.repo else ""
        session_id = str(execution.evaluate(self.session) or "") if self.session is not None else ""
        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            preview = str(prompt)[:80] + ("…" if len(str(prompt)) > 80 else "")
            return {"text": f"[dry-run] {agent_name} would run (ags sandbox): {preview}",
                    "cli": agent_name, "model": None, "session_id": "",
                    "usage": None, "dry_run": True}
        return self._execute_ags(execution, agent_name, str(prompt), repo, session_id)

    # ── 分流释放（失败/取消路径）：可按需改成保现场 ───────────────────────

    @staticmethod
    def _preserve_scene(driver, handle) -> None:
        """失败/取消路径**一律 pause 保现场**（不再按 synced 分流 kill）。

        两个理由（2026-10-08 修正）：
        1. **重启 drain 打断的 run 会回来续跑**——pause 让它原地接力（实测
           ``connect(paused)`` 自动恢复、工作区完好），旧的「取消即 kill」让每次
           重启都白丢沙箱并重建供给；
        2. 用户口径「失败不能都 kill 掉」——即便改动已 sync_out 回 Git，pause 的
           成本是**零计算费**（暂停不计算力费），保留现场仍值。

        清理责任在别处：成功路径终态释放 kill（flow_worker._release_sandboxes）、
        超龄暂停实例由 ``AgsDriver.sweep_paused`` 定期扫（配额安全带）。
        任何异常都吞掉（回收失败不能掩盖原始失败原因；AGS 侧 timeout 兜底）。
        """
        try:
            drv = getattr(driver, "client", None)
            if drv is not None and hasattr(drv, "pause"):
                drv.pause(handle.id)
            else:  # 无 pause 能力的 driver（本地 docker 等）退回 release 保数据
                driver.release(handle, keep_data=True)
            _log.info("sandbox_agent: 已 pause 保留现场 %s（可被续跑 attach 回来）",
                      handle.id)
        except Exception as exc:  # noqa: BLE001 — 尽力而为
            _log.warning("sandbox_agent: 失败路径资源处置异常：%s", exc)

    # ── AGS 执行主链 ────────────────────────────────────────────────────

    def _execute_ags(self, execution: Any, agent_name: str, prompt: str,
                     repo: str, session_id: str) -> dict:
        from . import sandbox as sb
        from .sandbox_ags import AgsDriver  # noqa: F401  # import 即注册 ags driver

        sandbox_name = str(execution.evaluate(self.sandbox) or "ags").strip()
        ws_key = str(execution.evaluate(self.ws_key) or "main").strip()
        # 身份用**根执行** id：childflow 上下文每次新铸 $EXECUTION_ID，直接取
        # execution.execution_id 会让子流程里的节点另建空实例（见 sandbox.
        # root_execution_id 的说明）。
        execution_id = sb.root_execution_id(execution)
        if not execution_id:
            raise AgentRunError("execution 缺少 execution_id，无法派生沙箱实例身份")

        # 注册表搜索：~/.plaita（worker 级）→ <repo>/.plaita（仓级覆盖）
        spec = sb.load_sandboxes(repo=repo or None).get(sandbox_name)
        if spec is None:
            raise AgentRunError(
                f"sandbox '{sandbox_name}' 未注册（fail-closed）：请在 .plaita/sandboxes.json 定义")
        if spec.driver != "ags":
            raise AgentRunError(
                f"sandbox '{sandbox_name}' 的 driver='{spec.driver}'，本节点只走 ags")
        driver = sb.get_driver("ags")
        if driver is None:  # pragma: no cover - import 即注册，防御
            raise AgentRunError("ags driver 未注册")

        profile = resolve_agent(agent_name, repo=None)
        executor = profile["executor"]
        register_recursive_direct()
        from agentproc import EXECUTORS as AP_EXECUTORS

        ap_executor = EXECUTOR_ALIASES.get(executor, executor)
        if ap_executor not in AP_EXECUTORS:
            raise AgentRunError(f"执行器 '{executor}' 未接入（agentproc 可用：{sorted(AP_EXECUTORS)}）")
        sandbox_executor = sb.register_sandbox_executor(ap_executor)

        from agentproc.runner import RunOptions
        from agentproc.runner import run as agentproc_run

        wall_secs = int(execution.evaluate(self.timeout_secs) or 3600)
        runner_timeout = wall_secs + 120

        # 凭据/env 白名单：profile 翻译 env + 注册表 env（密钥只进 envfile）
        env_whitelist = dict(profile["env"])
        for key, value in (spec.env or {}).items():
            env_whitelist[str(key)] = self._resolve_spec_env(value)
        redactor = sb.Redactor(env_whitelist)

        handle = driver.ensure(spec, execution_id, ws_key)
        # 续期：本轮预算 + 收尾余量（见 sandbox.renewal_timeout_secs 的长注释）——
        # 不是固定 4h；空闲长尾靠显式释放，不靠 TTL 兜长。
        driver.client.set_timeout(handle.id, sb.renewal_timeout_secs(wall_secs))

        if repo:
            # 首次：把宿主工作区供给到沙箱（幂等；之后轮次跳过）
            driver.ensure_repo(handle, repo,
                               mode=str(spec.resources.get("provision_mode") or "bundle"))
            driver.sync_in(handle, repo)

        lease = sb.WorkspaceLease(
            sb.default_lease_store(), sb.handle_id(execution_id, ws_key),
            ttl=runner_timeout + 60.0)
        lease.acquire()
        envfile = sb.write_envfile(env_whitelist)
        protocol_lines: list[str] = []

        def _on_protocol_line(line: str) -> None:
            protocol_lines.append(redactor.redact(line))

        try:
            lease.start_heartbeat()
            extra_env = sb.sandbox_extra_env(handle, spec, wall_secs, envfile, execution_id)
            extra_env["RECURSIVE_WORKSPACE"] = handle.path
            for knob in ("RECURSIVE_MODEL", "RECURSIVE_MAX_STEPS"):
                if profile["env"].get(knob):
                    extra_env[knob] = profile["env"][knob]
            want_details = bool(execution.evaluate(self.details)) if self.details is not None else False
            if want_details and executor == "recursive":
                extra_env["RECURSIVE_OUTPUT_FORMAT"] = "stream-json"
            model = profile.get("model") or extra_env.get("RECURSIVE_MODEL") or agent_name

            result = agentproc_run(
                {"executor": sandbox_executor},
                RunOptions(message=prompt, extra_env=extra_env,
                           timeout_secs=runner_timeout,
                           cancel_event=getattr(execution, "cancel_event", None),
                           on_protocol_line=_on_protocol_line),
            )
            if getattr(result, "exit_code", 0) == _ap_exit_cancelled():
                # 取消（含 worker 重启 drain 打断）：先尽力把沙箱侧已有改动捞回宿主
                # （取消前 agent 可能已改了不少），再 pause 保现场——重投后 ensure()
                # 按 metadata 找回暂停实例并自动恢复，原地接力。
                if repo:
                    try:
                        driver.sync_out(handle, repo)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning("sandbox_agent: 取消路径 sync_out 未完成：%s", exc)
                self._preserve_scene(driver, handle)
                raise AgentRunError("agent 沙箱执行被取消（cancel_event 命中）")
            if result.error or result.exit_code != 0:
                # 失败保现场：先尽力把沙箱侧改动同步回宿主（与直跑语义一致），
                # 然后**一律 pause**（见 _preserve_scene：续跑接力 + 零计算费）
                if repo:
                    try:
                        driver.sync_out(handle, repo)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning("sandbox_agent: 失败路径 sync_out 未完成：%s", exc)
                self._preserve_scene(driver, handle)
                raise AgentRunError(
                    redactor.redact(result.error or f"{agent_name} 退出码 {result.exit_code}"))

            if repo:
                driver.sync_out(handle, repo)
            text, usage, observations = self._interpret_result(
                executor=executor, result=result, protocol_lines=protocol_lines,
                redactor=redactor, want_details=want_details)
        finally:
            sb.burn_envfile(envfile)
            lease.release()

        out = {"text": text, "cli": executor, "model": model,
               "session_id": result.session_id, "usage": usage, "dry_run": False}
        if observations is not None:
            out["observations"] = observations
        out["workspace"] = handle.snapshot(env_names=env_whitelist.keys())
        out["sandbox_instance"] = handle.id
        return out
