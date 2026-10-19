"""capture / gate 子进程的租户凭据闸门（pn#18，同构 plaita A42-06 / #256）。

capture / gate 在 worker 宿主上以**同一 OS 用户**执行流程作者给出的命令，
子进程可读 worker 的文件系统；所有租户的凭据文件又共用同一把 Fernet 密钥。
因此无论是否把租户显式传进子进程，它都**不是**租户凭据隔离边界：

- 不传租户：命令里的 ``plaita.credentials`` 回退 default 租户，按 cwd 读到
  default 凭据文件；
- 传租户：命令内 ``set_current_tenant("default")`` 改写路由，或直接按路径
  读取 default 凭据文件并解密，仍能拿到 default 密钥。

结论与主仓 ``f03a37e``（A42-06）相同：非 default 租户在 ``Popen`` 之前
fail-closed（具名 :class:`TenantContextPropagationError`，消息含
``PLAITA_SUBPROCESS_TENANT_ISOLATION_UNAVAILABLE``），命令零执行机会；
default 租户语义不变。租户级命令执行请放进不挂载宿主凭据的容器
（docker / 独立沙箱）。
"""
from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger(__name__)

TENANT_ISOLATION_MARKER = "PLAITA_SUBPROCESS_TENANT_ISOLATION_UNAVAILABLE"


class TenantContextPropagationError(RuntimeError):
    """非 default 租户无法在宿主子进程中安全执行（pn#18 / A42-06）。

    子进程与 worker 同用户、文件系统不受限，且所有租户共用同一把解密密钥：
    传不传租户上下文都无法阻止命令读取 default 凭据文件。因此在 spawn 前
    拒绝（命令零执行机会）；需要租户级命令时使用容器化执行器。
    """


def non_default_tenant() -> Optional[str]:
    """当前租户不是 default 时返回租户 id，否则 None（含 plaita 无租户层）。"""
    try:
        from plaita.tenant_context import DEFAULT_TENANT_ID, current_tenant
    except ImportError:  # plaita 0.5.x 无租户上下文 = 单租户部署
        return None
    tenant = current_tenant() or DEFAULT_TENANT_ID
    if tenant == DEFAULT_TENANT_ID:
        return None
    return tenant


def refuse_non_default_tenant(node_type: str) -> None:
    """非 default 租户在 spawn 前拒绝（pn#18）。

    拒绝点先打 WARNING（tenant / node_type / marker，不含路径），满足
    「fail-closed 必须可观测」。
    """
    tenant = non_default_tenant()
    if tenant is None:
        return
    _log.warning(
        "%s subprocess refused non-default tenant=%s (%s); command not executed",
        node_type, tenant, TENANT_ISOLATION_MARKER,
    )
    raise TenantContextPropagationError(
        f"{TENANT_ISOLATION_MARKER}: {node_type} 子进程无法为租户 {tenant!r} "
        "隔离凭据：它与 worker 同用户、文件系统不受限，且所有租户共用同一把"
        "解密密钥，命令可以直接读取 default 租户的凭据文件。拒绝执行；"
        "租户级命令请放进不挂载宿主凭据的容器（docker）或独立沙箱执行，"
        "default 租户语义不变。"
    )


__all__ = [
    "TENANT_ISOLATION_MARKER",
    "TenantContextPropagationError",
    "non_default_tenant",
    "refuse_non_default_tenant",
]
