"""文件名白名单（plaita#201）——rate_limit key / report token 的命名空间约束。

**为什么是白名单而不是黑名单**：``replace("..", "")`` 族的修法对
``....//``、``%2e%2e%2f``、全角 ``．．``、``..\\``、``a/../../b`` 都能被绕过
（``....//`` 去掉 ``..`` 后仍是 ``..``；URL 编码在落盘前不一定会被解码，但一旦
解码就是穿越；Unicode 同形字符在只看 ASCII 的判据里直接隐身）。本模块只承认
**单个文件名分量**：

    ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$

* 首字符必须是 ASCII 字母/数字 ⇒ ``.``、``..``、``.hidden`` 一律拒绝；
* 其余只允许 ASCII 字母/数字与 ``._-`` ⇒ ``/`` ``\\`` ``%`` ``:`` 空白、NUL、
  Unicode 同形字符、URL 编码串都进不来；
* 任何不合规输入 **直接抛** :class:`UnsafePathName`（``ValueError`` 子类），
  fail-closed：不静默改名、不落盘。

第二道防线在 :func:`resolve_child`：拼接后 ``resolve()`` 并校验
``is_relative_to(base.resolve())``。白名单将来若被放宽，越界仍然是错误而不是
一次越权写盘。

错误消息**不回显原始 key/token**（它可能来自表达式/上游数据）：只给
``kind``、字符长度与 SHA-256 指纹前缀，足以关联日志又不足以还原原文。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Union

#: 允许的单个文件名分量（完整匹配，非搜索）。
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")

#: 进错误消息的白名单说明（与 :data:`_NAME_RE` 同源，改一处必须改两处——
#: ``tests/test_flow_control_key_safety.py`` 用非法输入反向钉住这条消息）。
_PATTERN_TEXT = "[A-Za-z0-9][A-Za-z0-9._-]{0,199}"


class UnsafePathName(ValueError):
    """key/token 不是合法的单个文件名分量（fail-closed，未发生任何落盘）。"""


def _fingerprint(raw: str) -> str:
    """原文的短指纹（关联日志用；不可逆，且不含原文）。"""
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def safe_component(value: object, *, kind: str) -> str:
    """把 ``value`` 收敛成合法文件名分量；不合法即抛 :class:`UnsafePathName`。

    合法值**逐字返回**（不做大小写/Unicode 归一化、不改名），所以 ``xhs-publish``
    这类既有 key/token 的文件名与修复前完全一致。
    """
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = str(value)
        except Exception:  # noqa: BLE001 — 具名异常即 fail-closed 的可观测面
            raise UnsafePathName(
                f"{kind} 无法字符串化：fail-closed：不静默改名、不落盘。"
            ) from None
    if _NAME_RE.fullmatch(raw) is None:
        raise UnsafePathName(
            f"{kind} 非法（长度 {len(raw)}，指纹 {_fingerprint(raw)}）："
            f"只允许单个文件名分量 {_PATTERN_TEXT}；"
            "拒绝 . / .. / 路径分隔符 / URL 编码 / Unicode 同形字符。"
            "fail-closed：不静默改名、不落盘。"
        )
    return raw


def resolve_child(
    base: Union[str, Path],
    name: str,
    *,
    kind: str,
    suffix: str = "",
) -> Path:
    """``base / f"{name}{suffix}"``，并校验 resolve 后仍在 ``base`` 内。

    ``name`` 必须已通过 :func:`safe_component`（本函数不重复校验，调用方负责
    语义，例如默认值回落）。越界抛 :class:`UnsafePathName`——与白名单同一错误
    类型，调用方只需捕获一处。
    """
    base_path = Path(str(base))
    candidate = base_path / f"{name}{suffix}"
    try:
        base_resolved = base_path.resolve(strict=False)
        candidate_resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        # symlink 环等：路径无法安全判定 ⇒ fail-closed。
        raise UnsafePathName(
            f"{kind} 路径无法解析（指纹 {_fingerprint(name)}）："
            "拒绝在无法校验命名空间时落盘。"
        ) from None
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError:
        raise UnsafePathName(
            f"{kind} 越出存储目录（指纹 {_fingerprint(name)}）："
            "fail-closed：不落盘。"
        ) from None
    return candidate
