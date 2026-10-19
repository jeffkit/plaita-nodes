"""计时参数的 fail-closed 校验（A2R7-05，对齐 plaita r34/r39 A1 风格）。

历史上的三种「0 语义」互不一致：

* gate ``timeout_secs=0`` 被 ``or 600`` 吞成 10 分钟（预算写错却像正常）；
* capture ``timeout_secs=0`` 变成 ``communicate(timeout=0)`` 的立即 kill；
* api ``timeout_ms=0`` 拖到 requests 连接前才抛 ``ValueError``。

0/负值不表达任何合理的预算，统一在**构造期**（字面量）或**求值后立即**
（DSL 表达式）具名拒绝，绝不静默改道。错误文案与主仓
``_positive_timeout_seconds`` 同风格：带字段名，明确 0/负值被拒。
"""
from __future__ import annotations

import math
from typing import Any


def is_deferred_expression(value: Any) -> bool:
    """DSL 表达式串（含 ``$``）——构造期无法求值，留给 execute 求值后校验。"""
    return isinstance(value, str) and "$" in value


def _reject(name: str, value: Any, unit: str) -> ValueError:
    return ValueError(
        f"{name} must be a positive number of {unit} (got {value!r}); "
        "0 and negative values are rejected (fail-closed)"
    )


def positive_seconds(name: str, value: Any) -> float:
    """有限正数秒；0 / 负值 / 非有限值 / 布尔一律具名拒绝。"""
    if isinstance(value, bool) or value is None:
        raise _reject(name, value, "seconds")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise _reject(name, value, "seconds") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise _reject(name, value, "seconds")
    return seconds


def positive_millis(name: str, value: Any) -> float:
    """有限正数毫秒（返回 float，供 ``/1000`` 转 requests 的秒）。"""
    if isinstance(value, bool) or value is None:
        raise _reject(name, value, "milliseconds")
    try:
        millis = float(value)
    except (TypeError, ValueError) as exc:
        raise _reject(name, value, "milliseconds") from exc
    if not math.isfinite(millis) or millis <= 0:
        raise _reject(name, value, "milliseconds")
    return millis
