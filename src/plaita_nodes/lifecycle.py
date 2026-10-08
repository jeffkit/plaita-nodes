"""沙箱生命周期回调（设计 §6.1）：挂起/结束 → dirty-check → wip push → release。

**best-effort 定位（如实声明）**：跨进程 resume 后，终结进程往往不是 ensure 进程，
本回调只能收割到本进程执行过的节点快照；status-aware reaper
（:mod:`plaita_nodes.sandbox_reaper`）才是孤儿资源的主兜底，两者互补。

用法（部署侧注册——plaita-nodes 不构造 FlowExecution）：

    from plaita_nodes.lifecycle import SandboxLifecycleCallback
    execution = FlowExecution(callback_handlers=[SandboxLifecycleCallback()])

句柄来源（评审 C4）：``on_node_end`` 的节点输出快照（``result["workspace"]``），
不依赖进程内存存活顺序；``ensure`` 幂等保证按名重建即是续接。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from plaita.core.callback import FlowCallback

from . import sandbox as sb

__all__ = ["SandboxLifecycleCallback"]

logger = logging.getLogger("plaita_nodes.sandbox.lifecycle")


class SandboxLifecycleCallback(FlowCallback):
    """随仓发布的沙箱生命周期回调。

    ``on_flow_suspend``：挂起（HITL/EventNode 等待）前 dirty-check + 强制 wip
    push + 释放计算层（数据层保留）——挂起期间不为闲置沙箱付费。
    ``on_flow_end``：同上（正常结束路径）。

    单个 workspace 的失败不影响其它（也受 CallbackManager 的异常吞并双保险）；
    全部结果记录在 :attr:`events` 供观测/测试断言。
    """

    def __init__(self, *, repo: Optional[str] = None,
                 sandboxes: Optional[Dict[str, sb.WorkspaceSpec]] = None,
                 drivers: Optional[Dict[str, Any]] = None,
                 redactor: Optional[sb.Redactor] = None,
                 keep_data: bool = True):
        self._repo = repo
        self._sandboxes = sandboxes          # 测试注入；缺省每次惰性 load_sandboxes
        self._drivers = drivers or {}        # 测试注入 {driver_name: driver}
        self._redactor = redactor
        self._keep_data = keep_data
        self._snapshots: Dict[str, Dict[str, Any]] = {}   # id → 快照
        self._released: set = set()                       # 幂等：已释放过的 id
        self.events: List[Dict[str, Any]] = []

    # ── FlowCallback 钩子 ───────────────────────────────────────────────
    def on_node_end(self, flow, node, result=None, error=None,
                    exception=None, **kwargs) -> None:
        ws = (result or {}).get("workspace") if isinstance(result, dict) else None
        if isinstance(ws, dict) and ws.get("id"):
            self._snapshots[str(ws["id"])] = ws

    def on_flow_suspend(self, flow, **kwargs) -> None:
        self._drain("suspend")

    def on_flow_end(self, flow, result=None, error=None, exception=None,
                    **kwargs) -> None:
        self._drain("end")

    # ── 释放逻辑 ────────────────────────────────────────────────────────
    def drain(self, snapshots: Optional[List[Dict[str, Any]]] = None,
              phase: str = "end",
              keep_data: Optional[bool] = None) -> List[Dict[str, Any]]:
        """释放给定快照（缺省=本进程累积的那批），返回逐条结果。

        宿主可从**持久化上下文**收集快照后调用（``collect_workspace_snapshots``）：
        分布式步进下 agent 节点跑在**早先的 step**，而每一步都会新建 handlers 列表
        ——进程内累积到 flow 结束那一步往往是空的，只有上下文里的
        ``$NODE.*.workspace`` 跨 step/跨进程权威（2026-10-08 实测：plaita#22 跑完
        24 分钟后实例仍 running，就是只靠进程内快照的后果）。

        ``keep_data``：None=用实例缺省；False=kill（成功路径不留现场）；
        True=pause（失败/取消路径保现场可恢复）。**幂等**：同一 id 只释放一次
        （终态钩子与 ``on_flow_end`` 可能各调一次，重复释放会再 kill 已死实例）。
        """
        targets = ([self._snapshots[k] for k in list(self._snapshots)]
                   if snapshots is None else list(snapshots))
        self._snapshots.clear()
        results: List[Dict[str, Any]] = []
        for snap in targets:
            sid = str((snap or {}).get("id") or "")
            if sid and sid in self._released:
                continue
            try:
                outcome = self._release_one(snap, keep_data=keep_data)
            except Exception as exc:  # best-effort：单点失败不拖累其它 workspace
                outcome = f"error: {exc}"
            if sid:
                self._released.add(sid)
            self.events.append({"phase": phase, "id": sid, "outcome": outcome})
            logger.info("sandbox lifecycle phase=%s id=%s -> %s", phase, sid, outcome)
            results.append({"id": sid, "outcome": outcome})
        return results

    def _drain(self, phase: str) -> None:
        self.drain(phase=phase)

    def _release_one(self, snap: Dict[str, Any],
                     keep_data: Optional[bool] = None) -> str:
        exec_id, _, ws_key = str(snap["id"]).partition(":")
        specs = self._sandboxes
        if specs is None:
            specs = sb.load_sandboxes(repo=self._repo)
        spec = specs.get(ws_key) or specs.get(str(snap.get("ws_key", "")))
        if spec is None:
            return self._release_by_handle(snap, keep_data=keep_data)
        driver = self._drivers.get(spec.driver) or sb.get_driver(spec.driver)
        if driver is None:
            return f"no-driver:{spec.driver}"
        handle = driver.ensure(spec, exec_id, ws_key)  # 幂等 attach / 按名重建
        return sb.suspend_release(
            driver, handle, spec, redactor=self._redactor,
            keep_data=self._keep_data if keep_data is None else keep_data)

    def _release_by_handle(self, snap: Dict[str, Any],
                           keep_data: Optional[bool] = None) -> str:
        """回退路径：直接用快照里的 driver + id 释放（不做 spec 反查）。

        为什么需要：**remote-API 型 driver（AGS）的反查不成立**——这类部署全流程
        常共用一个 spec 名（registry 键如 "ags"），而 ws_key 是流内键（如 "main"），
        `specs.get(ws_key)` 必然落空；且它们没有宿主侧 reaper 兜底（reaper 扫宿主
        目录，远端实例不在其中）。没有这条回退，装了回收器也会**从不回收**。
        仅做 release（不 wip_push——那需要 spec 的 provision 信息）；AGS 上 release
        默认即 kill，实例销毁后数据一并回收。
        """
        name = str(snap.get("driver") or "")
        sid = str(snap.get("id") or "")
        if not name or not sid:
            return "no-spec(快照缺 driver/id，留给 AGS 侧 timeout 兜底)"
        driver = self._drivers.get(name) or sb.get_driver(name)
        if driver is None:
            return f"no-driver:{name}"
        try:
            handle = sb.WorkspaceHandle(
                driver=name, id=sid, path=str(snap.get("path") or ""),
                ws_key=str(snap.get("ws_key") or ""),
                execution_id=str(sid).partition(":")[0])
            driver.release(
                handle, keep_data=self._keep_data if keep_data is None else keep_data)
            return f"released-by-handle({name})"
        except Exception as exc:  # noqa: BLE001 — 尽力而为，AGS timeout 兜底
            return f"handle-release-error: {exc}"

    # ── 观测 ────────────────────────────────────────────────────────────
    @property
    def pending(self) -> List[str]:
        """尚未释放的 handle id（同一进程内挂起后未再执行节点时为空）。"""
        return list(self._snapshots)
