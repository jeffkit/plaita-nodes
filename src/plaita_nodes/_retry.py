"""节点错误的重试分类表 —— 确定性错误不被 ``retryTimes`` 放大（A2R7-04）。

plaita 引擎的判据（``plaita/core/runner.py::_is_retryable_error``）：

* ``NodeException(code=1001)`` → 确定性，不重试；
* 非 ``NodeException`` 的 ``ValueError`` → 确定性，不重试
  （节点输出校验钩子打标的异常除外，plaita#219）；
* 其余（``RuntimeError`` / ``KeyError`` / …）→ 按 ``retryTimes`` 重试。

节点把配置/参数/数据形状错误抛成 ``RuntimeError`` 时，一次失败会被放大成
``1 + retry_times`` 次副作用：API 重复请求、SQL 重复执行、webhook 重复发送。
本模块只提供两个窄工具：

* :class:`DeterministicError` —— ``ValueError`` mixin。与既有 ``RuntimeError``
  错误类多重继承，**保留原异常身份**（``pytest.raises(XxxError)`` 与
  ``isinstance(exc, RuntimeError)`` 不受影响），同时让引擎按 ``ValueError``
  分类为不重试；
* :func:`is_retryable_http_status` —— HTTP 状态码分类表：4xx（408/425/429
  除外）是请求本身的确定性错误；5xx 与 408/425/429 是瞬态，退避重试。

边界：``KeyError`` 不作为本仓节点的 action 分发错误——``rate_limit`` /
``report`` 的非法 action 统一抛 ``ValueError``（确定性分类）。FoT
``TOOL(action=)`` 的未注册工具 ``KeyError`` 在 plaita 主仓
``plaita/node/tools.py``（A2R6-04），不在本仓依赖面内。
"""
from __future__ import annotations

#: 可重试的 4xx：请求超时 / 过早 / 限流是瞬态，退避后可能成功。
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429})


class DeterministicError(ValueError):
    """确定性错误标记：引擎不再按 ``retryTimes`` 重试。

    用法（保留原异常身份）::

        class CaptureConfigError(DeterministicError, RuntimeError):
            ...
    """


class DeterministicRuntimeError(DeterministicError, RuntimeError):
    """无需专属类型的确定性错误（如 HTTP 4xx）——同时是 ``RuntimeError``。

    既有调用方按 ``RuntimeError`` 捕获的行为不变；引擎按 ``ValueError``
    分支判为不重试。
    """


def is_retryable_http_status(status: int) -> bool:
    """HTTP 状态码是否值得重试：5xx 与 408/425/429 为瞬态，其余 4xx 确定性。"""
    return status >= 500 or status in RETRYABLE_HTTP_STATUS
