"""git_publish / github_comment 子进程的显式 env 白名单（pn#17）。

历史实现三处 ``git`` 与两处 ``gh`` 调用均未传 ``env=``，宿主全量环境直通。
工作树内 ``.git/hooks/pre-commit`` / clean filter 在 ``git add -A`` / commit
时执行属 git 标准行为，提示注入即可让钩子把 ``PLAITA_CREDENTIALS_KEY`` 等
平台凭据打到 stdout / 外带。修法与 capture/gate 同一策略：**白名单重建**，
绝不 ``os.environ.copy()``。

允许的最小集：
- 基础：PATH / HOME / TMPDIR / LANG / LC_* / TZ / PYTHONIOENCODING；
- git 身份与 ssh 传输：``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` /
  ``GIT_SSH*`` / ``GIT_CONFIG_GLOBAL`` 等 ``GIT_*``（除下方拒绝项）+
  ``SSH_AUTH_SOCK``；
- gh 配置定位：``GH_CONFIG_DIR`` / ``GH_HOST`` / ``GH_REPO`` 等非凭据项 +
  ``XDG_CONFIG_HOME``。

拒绝项：
- 凭据类名字（``*TOKEN*`` / ``*SECRET*`` / ``*PASSWORD*`` /
  ``*CREDENTIAL*`` / ``*PRIVATE_KEY*`` / ``*API_KEY*``）——包括
  ``GH_TOKEN`` / ``GITHUB_TOKEN``；token 档部署请改用 ``gh auth login``
  落盘的配置或 credential helper。
- 能携带或吐出凭据的 git 变量：``GIT_ASKPASS``、``GIT_CONFIG_COUNT`` /
  ``GIT_CONFIG_PARAMETERS`` / ``GIT_CONFIG_KEY_*`` / ``GIT_CONFIG_VALUE_*``
  （可用于注入 ``http.extraHeader``）、``GIT_TRACE*`` / ``GIT_CURL_VERBOSE``。
- 会改写操作目标的变量：``GIT_DIR`` / ``GIT_WORK_TREE`` / ``GIT_INDEX_FILE`` /
  ``GIT_OBJECT_DIRECTORY`` / ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` /
  ``GIT_COMMON_DIR`` / ``GIT_NAMESPACE``（节点已显式指定 cwd / 仓库）。
"""
from __future__ import annotations

import os
import re
from typing import Mapping, Optional

from ._subprocess_env import build_subprocess_env

__all__ = ["build_git_env"]

# 名字命中即拒绝（大小写不敏感）。GIT_SSH_COMMAND 只含路径，不命中。
_SECRET_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|API_KEY)", re.I)

_DENY_EXACT = frozenset({
    "GIT_ASKPASS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CURL_VERBOSE",
})
_DENY_PREFIXES = (
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
    "GIT_TRACE",
)

# gh 的配置定位变量（非凭据）。GH_TOKEN / GH_ENTERPRISE_TOKEN 不在名单内。
_GH_ALLOWLIST = frozenset({
    "GH_CONFIG_DIR",
    "GH_HOST",
    "GH_REPO",
    "GH_PROMPT_DISABLED",
    "GH_NO_UPDATE_NOTIFIER",
    "GH_FORCE_TTY",
})

_EXTRA_ALLOW = frozenset({
    "SSH_AUTH_SOCK",  # ssh-agent：git push 走 ssh 时需要
    "XDG_CONFIG_HOME",  # gh 默认配置目录
    "TZ",  # 提交时间戳的本地时区
})


def _denied(key: str) -> bool:
    upper = key.upper()
    if upper in _DENY_EXACT:
        return True
    if upper.startswith(_DENY_PREFIXES):
        return True
    return bool(_SECRET_NAME_RE.search(upper))


def build_git_env(extra: Optional[Mapping[str, object]] = None) -> dict:
    """按 git/gh 最小集重建子进程环境，返回可直接传给 ``Popen(env=...)`` 的 dict。

    合并顺序（后者覆盖前者）：基础白名单 → 宿主非敏感 ``GIT_*`` / gh 配置 /
    ssh-agent → 调用点 ``extra``。最终整份再过滤一次拒绝项——部署方经
    ``SUBPROCESS_ENV_EXTRA`` 塞进来的凭据变量也不会流进 git 钩子。
    """
    env: dict = build_subprocess_env()

    for key, value in os.environ.items():
        if key.startswith("GIT_") and not _denied(key):
            env[key] = value
    for key in _GH_ALLOWLIST:
        if key in os.environ:
            env[key] = os.environ[key]
    for key in _EXTRA_ALLOW:
        if key in os.environ:
            env[key] = os.environ[key]

    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return {key: value for key, value in env.items() if not _denied(key)}
