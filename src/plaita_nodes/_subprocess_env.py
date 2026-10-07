"""gate / capture / agent 直跑的子进程 env 白名单 + 输出截断接入层。

公共层在 plaita 仓（jeffkit/plaita#30 起，``plaita/subprocess_env.py``）：spawn 节点
一律经本模块取 ``build_subprocess_env`` / ``clip_output``，不再 ``os.environ.copy()``
——worker 进程 env 里的平台凭据（``PLAITA_CREDENTIALS_KEY``、DB/Redis 连接串、
provider token）不随子进程走；子进程确实需要的变量由调用点 ``extra`` 显式声明。

依赖缺席兜底：兄弟仓 plaita 的 checkout 可能早于 #30（import 期找不到
``plaita.subprocess_env``，每个 spawn 节点都会炸）。此时用一份同构实现顶上，
语义与公共层一致；公共层可用时**一律走公共层**（白名单与截断的唯一定义处）。
两档都提供 ``SUBPROCESS_ENV_EXTRA``（部署启动脚本往里加宿主变量，如代理设置）。
"""
from __future__ import annotations

import os
from typing import Dict, Mapping, Optional

try:
    from plaita.subprocess_env import (
        SUBPROCESS_ENV_EXTRA, build_subprocess_env, clip_output,
    )
except ImportError:  # plaita checkout 早于 #30——本仓兜底，白名单语义一致
    _HOST_ALLOWLIST = frozenset({
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "PYTHONIOENCODING",
    })
    SUBPROCESS_ENV_EXTRA: Dict[str, str] = {}

    def build_subprocess_env(extra: Optional[Mapping[str, object]] = None) -> dict:
        """按白名单重建子进程环境，再叠加 ``SUBPROCESS_ENV_EXTRA`` 与调用点
        ``extra``（后者覆盖前者）。"""
        child_env = {key: os.environ[key] for key in sorted(_HOST_ALLOWLIST)
                     if key in os.environ}
        child_env.update(SUBPROCESS_ENV_EXTRA)
        if extra:
            child_env.update({str(k): str(v) for k, v in extra.items()})
        return child_env

    def clip_output(text: str, cap: int) -> str:
        """超阈值时头 1/4 + 尾 3/4 保留并标注省略量（诊断信息在尾部）。"""
        if cap <= 0 or len(text) <= cap:
            return text
        head_len = cap // 4
        return f"{text[:head_len]}…[省略 {len(text) - cap} 字符]…{text[-(cap - head_len):]}"


__all__ = ["SUBPROCESS_ENV_EXTRA", "build_subprocess_env", "clip_output"]
