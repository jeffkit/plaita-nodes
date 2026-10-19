"""A2R7-04 回归：确定性错误不被 ``retryTimes`` 放大。

引擎判据（``plaita/core/runner.py::_is_retryable_error``）只把
``NodeException(1001)`` 与非输出校验的 ``ValueError`` 当确定性错误；节点若
把配置/参数错误抛成 ``RuntimeError``，一次失败会被重放成 ``1 + retry_times``
次副作用（API 重复请求、SQL 重复执行、agent 重复启动）。

本轮把下列错误标记为 ``DeterministicError``（ValueError mixin，保留原
RuntimeError 身份）：CaptureConfigError、SqlResultLimitError、
AgentConfigError、AgentRunConfigError、LlmDeterministicError（端点配置 /
401·403·404 等 4xx）、api/notify 的 4xx、report 非法 action。

负控（回退生产改动）：``retry_times=2`` 下 LLM 401 / API 401 的请求计数
从 1 变 3——见 ``docs/proofs/38-a2r7-retry-classification.md``。
"""
from __future__ import annotations

import pytest

from plaita.core.executor import FlowExecution
from plaita.core.flow import Flow
from plaita.core.runner import _backoff_sleep, _is_retryable_error

import plaita_nodes
import plaita_nodes.api as api_mod
import plaita_nodes.llm as llm_mod
from plaita_nodes.agent_run import AgentRunConfigError
from plaita_nodes.capture import CaptureConfigError, CaptureNode
from plaita_nodes.config import AgentConfigError
from plaita_nodes.database import SqlResultLimitError
from plaita_nodes.llm import LlmDeterministicError, LlmError

plaita_nodes.register_all()


class _Resp:
    def __init__(self, status: int, body: str = "{}"):
        self.status_code = status
        self.text = body
        self._body = body

    def json(self):
        return {"error": self.text}


def _run_with_retries(node_type: str, **fields):
    flow = Flow.model_validate({
        "nodes": [
            {"type": "start", "id": "start", "next": "n"},
            {"type": node_type, "id": "n", **fields, "next": "end",
             "errorHandler": {"strategy": "abort", "retryTimes": 2}},
            {"type": "end", "id": "end", "resultType": "success",
             "output": "$NODE.n"},
        ]
    })
    try:
        FlowExecution().run_compatible(flow, False)
    except Exception as exc:  # noqa: BLE001 — 本用例断言的是重试次数
        return exc
    raise AssertionError("预期节点失败")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _noop(_delay):
        return None

    monkeypatch.setattr("plaita.core.runner._backoff_sleep", _noop)


def test_llm_401_sent_once_despite_retry_times_2(monkeypatch):
    calls = {"n": 0}

    def post(*args, **kwargs):
        calls["n"] += 1
        return _Resp(401, '{"error":"unauthorized"}')

    monkeypatch.setattr(llm_mod.requests, "post", post)
    exc = _run_with_retries(
        "llm", prompt="x", api_base="http://127.0.0.1:9",
        api_key="k", model="m")
    assert calls["n"] == 1
    assert isinstance(exc.__cause__, LlmDeterministicError)
    assert _is_retryable_error(exc.__cause__) is False


def test_llm_503_still_retried(monkeypatch):
    calls = {"n": 0}

    def post(*args, **kwargs):
        calls["n"] += 1
        return _Resp(503, "busy")

    monkeypatch.setattr(llm_mod.requests, "post", post)
    exc = _run_with_retries(
        "llm", prompt="x", api_base="http://127.0.0.1:9",
        api_key="k", model="m")
    # 1 次首发 + 2 次重试；证明分类表没有把瞬态 5xx 一并禁掉
    assert calls["n"] == 3
    assert not isinstance(exc.__cause__, LlmDeterministicError)


def test_api_401_sent_once_despite_retry_times_2(monkeypatch):
    calls = {"n": 0}

    def request(*args, **kwargs):
        calls["n"] += 1
        return _Resp(401, '{"error":"bad token"}')

    monkeypatch.setattr(api_mod, "get_credential",
                        lambda name: {"base_url": "http://127.0.0.1:9"})
    monkeypatch.setattr(api_mod.requests, "request", request)
    exc = _run_with_retries("api_request", credential="c", path="x")
    assert calls["n"] == 1
    assert isinstance(exc.__cause__, ValueError)


def test_capture_config_error_executes_once(monkeypatch):
    calls = {"n": 0}
    original = CaptureNode.execute

    def counted(self, execution):
        calls["n"] += 1
        return original(self, execution)

    monkeypatch.setattr(CaptureNode, "execute", counted)
    exc = _run_with_retries("capture")  # 缺 command
    assert calls["n"] == 1
    assert isinstance(exc.__cause__, CaptureConfigError)


def test_deterministic_error_types_are_not_retryable():
    """分类表覆盖的异常类型：同时保留原 RuntimeError 身份。"""
    cases = [
        CaptureConfigError("missing command"),
        SqlResultLimitError(max_rows=1, max_bytes=1,
                            observed_rows=2, observed_bytes=2),
        AgentConfigError("bad agents.json"),
        AgentRunConfigError("executor not registered"),
        LlmDeterministicError("HTTP 401"),
    ]
    for exc in cases:
        assert isinstance(exc, RuntimeError), type(exc)
        assert isinstance(exc, ValueError), type(exc)
        assert _is_retryable_error(exc) is False, type(exc)
    # 瞬态错误保持可重试
    assert _is_retryable_error(LlmError("HTTP 503")) is True
    # 非法 action 是 ValueError（确定性）；不再出现 KeyError 被重试的路径
    from plaita_nodes.report import ReportNode

    with pytest.raises(ValueError):
        ReportNode(id="r", action="bogus", entry={}).execute(
            type("E", (), {"evaluate": lambda self, v: v,
                           "get_global_variable": lambda self, k, d=None: d})())
