"""沙箱孤儿回收守护（status-aware reaper，设计 §6.3）。

谓词（v1 曾用 ``end_time IS NULL``，**已纠正**——suspended 执行没有 end_time，
旧谓词会把等人审批的合法执行当孤儿回收）：

- ``status=running`` 且 ``last_update_time`` 超过 ``--stale-minutes`` → 僵尸，
  回收：断计算面（enforce）→ dirty-check → wip push 留档 → release（数据层保留）；
- ``status=suspended`` → **永不回收**（v1；天级可配置属后续）；
- ``status=completed/error``（终态孤儿：终态落盘后回调前崩溃）→ 回收计算层，
  数据层默认保留（``--purge-data`` 显式清除）；
- ExecutionStorage 里查无记录 → 同终态孤儿处理。

用法::

    python -m plaita_nodes.sandbox_reaper --once --dry-run          # 巡检不动作
    python -m plaita_nodes.sandbox_reaper --redis-url redis://... --interval 300
    python -m plaita_nodes.sandbox_reaper --once                    # 无存储：全部按无主处理

依赖：redis 存储需 ``pip install plaita[redis]``（lazy import）。
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import sandbox as sb

__all__ = ["decide", "reap_one", "run_cycle", "enumerate_resources", "main"]

logger = logging.getLogger("plaita_nodes.sandbox_reaper")

_TERMINAL = {"completed", "error"}


def _parse_ts(value: Optional[str]) -> Optional[_dt.datetime]:
    if not value:
        return None
    try:
        ts = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts


def decide(state: Optional[Any], *, stale_minutes: float,
           now: Optional[_dt.datetime] = None) -> str:
    """单条资源的处置决策（纯函数，决策表测试专用）。

    → ``skip-suspended`` / ``skip-fresh`` / ``reap-zombie`` / ``reap-terminal`` /
      ``reap-unknown``
    """
    if state is None:
        return "reap-unknown"
    status = str(getattr(state, "status", "") or "")
    if status == "suspended":
        return "skip-suspended"                       # 等人审批的合法挂起，永不回收
    if status == "running":
        ts = _parse_ts(getattr(state, "last_update_time", None))
        now = now or _dt.datetime.now(_dt.timezone.utc)
        stale = ts is None or (now - ts) >= _dt.timedelta(minutes=stale_minutes)
        return "reap-zombie" if stale else "skip-fresh"
    if status in _TERMINAL:
        return "reap-terminal"
    return "skip-fresh"


def reap_one(driver_name: str, execution_id: str, ws_key: str, *,
             spec_lookup: Callable[[str], Optional[sb.WorkspaceSpec]],
             keep_data: bool = True) -> str:
    """回收单个 workspace：ensure（attach/重建）→ 断计算面 → wip 留档 → release。

    僵尸的写者已死，dirty-check + wip push 是安全的留档动作（顺序有意为先
    enforce 后检查——先停可能的残留写入，再读工作区）。
    """
    spec = spec_lookup(ws_key)
    if spec is None:
        return "no-spec-skip(注册表已无此 workspace，留待运维)"
    driver = sb.get_driver(driver_name)
    if driver is None:
        return f"no-driver:{driver_name}"
    handle = driver.ensure(spec, execution_id, ws_key)
    driver.enforce(handle)                            # 先断计算面
    outcome = sb.wip_push_if_dirty(driver, handle, spec)
    driver.release(handle, keep_data=keep_data)
    return f"reaped:{outcome}"


def run_cycle(storage: Optional[Any], resources: Iterable[Dict[str, str]], *,
              stale_minutes: float = 30.0, now: Optional[_dt.datetime] = None,
              keep_data: bool = True, spec_lookup: Optional[Callable] = None,
              dry_run: bool = False) -> List[Dict[str, Any]]:
    """一轮巡检：枚举资源 → 决策 → 动作。``storage=None`` 视为全部无主。"""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    states: Dict[str, Any] = {}
    if storage is not None:
        for state in storage.list_executions(limit=1000):
            states[str(getattr(state, "execution_id", ""))] = state

    results: List[Dict[str, Any]] = []
    for res in resources:
        driver_name = str(res.get("driver", ""))
        exec_id = str(res.get("execution_id", ""))
        ws_key = str(res.get("ws_key", ""))
        decision = decide(states.get(exec_id), stale_minutes=stale_minutes, now=now)
        entry: Dict[str, Any] = {"driver": driver_name, "execution_id": exec_id,
                                 "ws_key": ws_key, "decision": decision,
                                 "outcome": ""}
        if decision.startswith("reap") and not dry_run:
            lookup = spec_lookup or (lambda key: sb.load_sandboxes().get(key))
            try:
                entry["outcome"] = reap_one(driver_name, exec_id, ws_key,
                                            spec_lookup=lookup,
                                            keep_data=keep_data)
            except Exception as exc:  # 单点失败不中断巡检
                entry["outcome"] = f"error: {exc}"
        results.append(entry)
        logger.info("%s %s:%s -> %s %s", driver_name, exec_id, ws_key,
                    decision, entry["outcome"])
    return results


def enumerate_resources(drivers: Sequence[str], *,
                        data_root: Optional[str] = None) -> List[Dict[str, str]]:
    """聚合各 driver 的本机资源枚举（docker labels / krunvm sidecar）。"""
    out: List[Dict[str, str]] = []
    if "docker" in drivers:
        from .sandbox_docker import enumerate_workspaces as _enum_docker
        out.extend(_enum_docker())
    if "krunvm" in drivers:
        from .sandbox_krunvm import enumerate_workspaces as _enum_krun
        out.extend(_enum_krun(data_root=data_root))
    if "ssh" in drivers:
        from .sandbox_ssh import enumerate_workspaces as _enum_ssh
        out.extend(_enum_ssh())  # v1 恒空：远端巡检需连接面注入（见 sandbox_ssh）
    return out
