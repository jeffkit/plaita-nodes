"""git_publish / github_comment 子进程的安全边界（pn#17 + pn#21）。

pn#17：所有 git/gh 调用显式传 ``env=``，按白名单重建——宿主凭据
（``PLAITA_CREDENTIALS_KEY`` 等）不随子进程走。

pn#21：白名单只是卫生措施，**不是恶意 worktree 的凭据边界**。agent 可写的
工作树里，hook / clean filter / fsmonitor 仍会在 ``git status/add/commit``
时执行；同 UID 进程可读 ``/proc/<pid>/environ``；``SSH_AUTH_SOCK`` 等于
agent 身份。因此本模块提供两层：

1. 边界前移 ``git_hardened_argv``：每条 git 命令都带
   ``-c core.hooksPath=/dev/null``（hooks 零执行）、
   ``-c core.fsmonitor=false``（fsmonitor 是等价 hook）、
   ``-c protocol.ext.allow=never``（``ext::`` 传输可执行任意命令），并把仓库
   配置里所有 ``filter.<driver>.{clean,smudge,process}`` 覆盖为空（filter
   零执行；``required=true`` 的 driver 会失败而非静默放行原始内容）。
2. 值级兜底：``build_git_env`` 不再放行 ``SSH_AUTH_SOCK``；``GIT_SSH_COMMAND``
   / ``GIT_PROXY_COMMAND`` / ``*_PROXY`` 的值命中内嵌凭据形态时抛
   ``GitEnvCredentialError``（报错只带变量名与命中类别，不回显值）。

允许的最小集：
- 基础：PATH / HOME / TMPDIR / LANG / LC_* / TZ / PYTHONIOENCODING；
- git 身份与 ssh 传输：``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` /
  ``GIT_SSH_COMMAND``（无内嵌凭据时）/ ``GIT_CONFIG_GLOBAL`` 等 ``GIT_*``
  （除下方拒绝项）；
- gh 配置定位：``GH_CONFIG_DIR`` / ``GH_HOST`` / ``GH_REPO`` 等非凭据项 +
  ``XDG_CONFIG_HOME``。

拒绝项：
- 凭据类名字（``*TOKEN*`` / ``*SECRET*`` / ``*PASSWORD*`` /
  ``*CREDENTIAL*`` / ``*PRIVATE_KEY*`` / ``*API_KEY*``）——包括
  ``GH_TOKEN`` / ``GITHUB_TOKEN``；token 档部署请改用 ``gh auth login``
  落盘的配置或 credential helper。
- ``SSH_AUTH_SOCK``（pn#21）：agent socket 是身份凭据。ssh push 改用显式
  key（``GIT_SSH_COMMAND`` 带 ``-i``）或 credential helper。
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
import subprocess
from typing import Mapping, Optional

from ._subprocess_env import build_subprocess_env

__all__ = ["GitEnvCredentialError", "build_git_env", "git_hardened_argv"]

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
    "SSH_AUTH_SOCK",  # pn#21：agent 身份不进 git 子进程
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
    "XDG_CONFIG_HOME",  # gh 默认配置目录
    "TZ",  # 提交时间戳的本地时区
})

# ---------------------------------------------------------------------------
# 值级凭据扫描（pn#21 次级兜底）
# ---------------------------------------------------------------------------

# 只有这些变量的「值」可能承载凭据；普通 env 值不扫，避免误伤。
_SCAN_KEY_RE = re.compile(r"^(?:GIT_SSH_COMMAND|GIT_PROXY_COMMAND|PROXY|.*_PROXY)$", re.I)
_PROXY_KEY_RE = re.compile(r"^(?:PROXY|.*_PROXY)$", re.I)

_VALUE_PATTERNS = (
    ("URL 内嵌口令",
     re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("已知 token 形态",
     re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|glpat-[A-Za-z0-9_-]{20,}|xox[abposr]-[A-Za-z0-9-]{10,}"
                r"|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
                r"|npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,})")),
    ("Authorization 头",
     re.compile(r"(?i)\b(?:authorization\s*:\s*)?(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("内嵌 secret 赋值",
     re.compile(r"(?i)\b(?:token|secret|password|passwd|api[_-]?key|access[_-]?key)"
                r"\s*[=:]\s*\S")),
    ("代理/认证命令行参数",
     re.compile(r"(?i)(?:--proxy-user|--proxy-password|sshpass\b[^|;]*\s-p)\s+\S+")),
)
# 代理 URL 的任意 userinfo 都视为凭据面（token 常直接作 userinfo）。
_PROXY_USERINFO_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+@")


class GitEnvCredentialError(RuntimeError):
    """git 子进程 env 的值疑似内嵌凭据（pn#21）；拒绝执行而不是静默剔除。"""


def _credential_hits(key: str, value: str) -> list[str]:
    hits = [label for label, pattern in _VALUE_PATTERNS if pattern.search(value)]
    if _PROXY_KEY_RE.match(key) and _PROXY_USERINFO_RE.search(value):
        hits.append("代理 URL userinfo")
    return hits


def _reject_embedded_credentials(env: Mapping[str, object]) -> None:
    for key, raw in env.items():
        if not _SCAN_KEY_RE.match(key):
            continue
        value = raw if isinstance(raw, str) else str(raw)
        hits = _credential_hits(key, value)
        if hits:
            # 只报变量名与类别，绝不回显值（报错会进日志/issue）。
            raise GitEnvCredentialError(
                f"拒绝把 {key} 传入 git 子进程：值疑似内嵌凭据"
                f"（{'、'.join(hits)}）；请改用 credential helper / 预置远程 URL / "
                "无凭据代理（pn#21）"
            )


# ---------------------------------------------------------------------------
# 仓库可执行面硬化（pn#21 边界前移）
# ---------------------------------------------------------------------------

_HARDENED_CONFIG = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("protocol.ext.allow", "never"),
)
_FILTER_KEY_QUERY = r"^filter\..*\.(clean|smudge|process)$"
_FILTER_KEY_RE = re.compile(r"^filter\.(.+)\.(clean|smudge|process)$", re.S)
_FILTER_OPS = ("clean", "smudge", "process")


def _base_argv() -> list[str]:
    argv = ["git"]
    for key, value in _HARDENED_CONFIG:
        argv.extend(["-c", f"{key}={value}"])
    return argv


def _filter_drivers(repo: str, env: Mapping[str, str], timeout: float) -> set[str]:
    """枚举所有 scope（local/global/system/worktree）里配置过的 filter driver。"""
    try:
        # --null --name-only：driver 名可含空格/点，按空白切分会解析不全。
        result = subprocess.run(
            [*_base_argv(), "config", "--get-regexp", "--null", "--name-only",
             _FILTER_KEY_QUERY],
            cwd=repo, capture_output=True, text=True, timeout=timeout,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        # git 不可用/仓库损坏时后续命令自会失败；这里不因枚举失败而改变语义。
        return set()
    drivers: set[str] = set()
    for key in result.stdout.split("\0"):
        match = _FILTER_KEY_RE.match(key)
        if match:
            drivers.add(match.group(1))
    return drivers


def git_hardened_argv(repo: str, env: Optional[Mapping[str, object]] = None,
                      timeout: float = 60) -> list[str]:
    """返回带安全 ``-c`` 覆盖的 git 命令前缀（不含子命令）。

    ``repo`` 是即将执行 git 的工作树/克隆路径；调用方拼成
    ``[*git_hardened_argv(repo, env), "status", ...]``。每次调用都会重新枚举
    filter driver——本地配置属于 agent 可写面，不能跨操作缓存。
    """
    if env is None:
        env = build_git_env()
    child_env = {str(k): str(v) for k, v in env.items()}
    argv = _base_argv()
    for driver in sorted(_filter_drivers(repo, child_env, timeout)):
        for op in _FILTER_OPS:
            argv.extend(["-c", f"filter.{driver}.{op}="])
    return argv


# ---------------------------------------------------------------------------
# env 白名单
# ---------------------------------------------------------------------------

def _denied(key: str) -> bool:
    upper = key.upper()
    if upper in _DENY_EXACT:
        return True
    if upper.startswith(_DENY_PREFIXES):
        return True
    return bool(_SECRET_NAME_RE.search(upper))


def build_git_env(extra: Optional[Mapping[str, object]] = None) -> dict:
    """按 git/gh 最小集重建子进程环境，返回可直接传给 ``Popen(env=...)`` 的 dict。

    合并顺序（后者覆盖前者）：基础白名单 → 宿主非敏感 ``GIT_*`` / gh 配置 →
    调用点 ``extra``。最终整份再过滤一次拒绝项——部署方经
    ``SUBPROCESS_ENV_EXTRA`` 塞进来的凭据变量也不会流进 git 钩子；随后对
    ``GIT_SSH_COMMAND`` / ``*_PROXY`` 的值做凭据扫描，命中即
    ``GitEnvCredentialError``（fail-closed，见 pn#21）。
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
    filtered = {key: value for key, value in env.items() if not _denied(key)}
    _reject_embedded_credentials(filtered)
    return filtered
