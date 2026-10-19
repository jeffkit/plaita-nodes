# RED-PROOF — plaita-nodes P2 批：webhook 脱敏 / 取消消费 / 确定性重试 / 计时 fail-closed

日期：2026-10-19 · 执行者：修复落地席 B3（第三十八轮）· 仓库 `plaita-nodes` ·
基线 `origin/main 1509f18` · 台账来源 `duo-a2-ledger.md` 第七/八轮
（A2R7-04/05、A2R8-01/02/03）

## §0 结论速览

| 条目 | 修法 | 负控（回退生产改动） | 正控 |
|---|---|---|---|
| A2R8-01 IM webhook 异常带 token | `_post_json` 捕获 `requests.RequestException` → `_scrub_exception_text`；相对 URL（`with url: /path?token`）按段打码；非 200 文案过 `_scrub_url_text` | 2 failed / 3 passed：`token_in_exception=True`、`token_in_flow_error=True` | token 不出现，host 保留 |
| A2R8-02 capture/gate 不消费取消 | 新增 `_cancel.communicate_cancellable`：0.25s 步进观察 `cancel_event`/`cancel_requested`，取消/超时都 killpg 整组 + 有界回收；gate 重试环入口再查一次 | 4 failed：子进程存活/ sentinel 落盘 / hitl 继续轮询 | 取消后 1s 内子进程退出、sentinel 不落盘、gate 只跑一轮 |
| A2R8-03 hitl 轮询不消费取消 | 循环条件 + 可唤醒等待（`cancel_event.wait`），单次 GET 受剩余 deadline 约束；返回 `status="cancel"` | 同上（hitl 未在 1s 内停止） | `poll_interval=5s` 下取消后 1s 内停止，轮询计数不再增长 |
| A2R7-04 确定性错误被重试 | 新增 `_retry`：`DeterministicError`（ValueError mixin，保留 RuntimeError 身份）+ HTTP 状态码分类表（4xx 除 408/425/429 不重试；5xx/限流重试） | 4 failed / 1 passed：`retry_times=2` 下 LLM 401 / API 401 请求计数 3（应 1） | 全部计数 = 1；503 仍 3 次 |
| A2R7-05 计时参数三态 | 新增 `_timeouts`：gate/capture `timeout_secs`、api/generic `timeout_ms` 字面量构造期具名拒绝；gate DSL 表达式求值后、spawn 前拒绝 | 17 failed / 2 passed：0/负值/布尔全部构造成功 | 0/-1/NaN/inf/bool 全部 `ValidationError`；表达式 0 在 Popen 前 `ValueError` |

- 定向正控：**40 passed**（`tests/test_a2r8_webhook_redaction.py`、
  `tests/test_a2r8_cancel.py`、`tests/test_a2r7_retry_classification.py`、
  `tests/test_a2r7_timeout_matrix.py`、`tests/test_log_redaction.py`）
- 全量：**284 passed / 2 skipped**（基线 249 passed / 2 skipped，+35 新用例）
- 探针复核（修复后）：r7 retry matrix 六项全部 `executions=1`/`requests=1`；
  r8 cancel 探针 `alive_after_flow_return+0.9s=False`、
  `late_side_effect_after_flow_timeout=False`；r8 notify 探针
  `token_in_exception=False`、`token_in_flow_error=False`

## §1 A2R8-01 · IM webhook 传输异常带出机器人 token

`notify_backends._post_json` 原先直接放行 `requests.post`。`ConnectionError`
/`Timeout` 的文本带完整 URL path/query：

- Feishu `/open-apis/bot/v2/hook/<token>`、Slack `/services/T/B/<token>` 在 path；
- DingTalk `access_token` 在 query；
- requests 的 `Max retries exceeded with url: /...` 是**相对 URL**，原
  `_scrub_url_text` 只匹配绝对 URL，token 会漏过。

修法：

1. `_post_json` 捕获 `requests.RequestException` →
   `RuntimeError(_scrub_exception_text(exc)) from None`；
2. `_redact` 新增 `_RELATIVE_URL_RUN_RE`：只匹配 `with url: /...` 形态，
   path 逐段、query/fragment 整条打码（不误伤普通路径文本）；
3. 非 200 的自建文案同样过 `_scrub_url_text`（防响应体回显 URL）；
4. `api.py` / `llm.py` 的传输异常走同一清洗纪律。

修复后探针（`docs/proofs/data/38-probe-r8-notify.txt`）：

```
exception_type = RuntimeError
token_in_exception = False
flow_exception_type = NodeExecutionError
token_in_flow_error = False
```

## §2 A2R8-02/03 · capture/gate/hitl 消费 cancel_event

引擎在节点预算超时时置位 `execution.cancel_event` 并遗弃节点线程
（`plaita/core/runner.py::_run_sync_node`）；`cancel()` 另置位粘滞的
`cancel_requested`。三个节点原先都不看这两个标志。

修法（`src/plaita_nodes/_cancel.py`）：

- `cancellation_requested`：两个 Event 任一置位即取消（缺失属性视为未取消，
  兼容旧执行桩）；
- `communicate_cancellable`：0.25s 步进 `communicate`；取消命中 → killpg
  整组 + 有界回收 → `NodeCancelledError`；自身超时 → 同样 killpg →
  `timed_out=True`（保持 exit 124 语义）；
- `wait_cancellable`：`cancel_event.wait(seconds)`，取消即唤醒。

节点接入：

- `capture`：等待改走 `communicate_cancellable`（stdin 仅首调用传）；
- `gate`：每轮 `Popen` 后同样等待；重试环入口再查取消，取消后不再进入下一轮；
- `hitl`：轮询循环入口查取消、`sleep` 换可唤醒等待；单次 GET
  `timeout=min(15, 剩余 deadline)`；取消返回 `status="cancel"`。

修复后探针（`docs/proofs/data/38-probe-r8-cancel.txt`，flow 节点
`timeout=PT0.3S`、节点自身 `timeout_secs=30`）：

```
D) capture: flow 返回于 0.31s ... cancel_event_set=True
   child_pid=56621 alive_after_flow_return+0.9s=False
   late_side_effect_after_flow_timeout=False
D) gate:    flow 返回于 0.31s ... cancel_event_set=True
   child_pid=56684 alive_after_flow_return+0.9s=False
   late_side_effect_after_flow_timeout=False
```

（探针末行「但 capture/gate 未消费」是 r8 采集时的静态结论，本轮证据看
`alive=False` / `late_side_effect=False`。）

## §3 A2R7-04 · 确定性错误不被 retryTimes 放大

引擎只把 `NodeException(1001)` 与非输出校验的 `ValueError` 当确定性错误
（`plaita/core/runner.py::_is_retryable_error`）。r7 探针在 `retry_times=2`
下记录到 CaptureConfigError / SqlResultLimitError / AgentRun 租户拒绝 /
执行器未接入 / API 401 均执行 3 次。

修法（`src/plaita_nodes/_retry.py`）：

- `DeterministicError(ValueError)` mixin：与既有 `RuntimeError` 错误类
  多重继承，保留原异常身份（`pytest.raises(XxxError)` 不变）；
- `is_retryable_http_status` 分类表：5xx 与 408/425/429 可重试，其余 4xx
  确定性；
- 接入：`CaptureConfigError`、`SqlResultLimitError`、`AgentConfigError`、
  `AgentRunConfigError`（多租户拒绝/执行器未接入/沙箱注册缺失）、
  `LlmDeterministicError`（端点配置、401/403/404 等）、api/notify 的 4xx、
  `report` 非法 action（ValueError）。

Agent 自身非零退出 / `is_error` 仍抛 `AgentRunError`（可重试）；LLM
429/5xx/网络异常仍抛 `LlmError`（可重试）。修复后 r7 探针
（`docs/proofs/data/38-probe-retry-matrix.txt`）：

```
[capture-config-error] executions=1
[sql-valueerror]       executions=1
[sql-result-limit]     executions=1
[agentrun-tenant-denied] executions=1
[agentrun-bad-executor]  executions=1
[api-401] requests=1
```

边界：A2R6-04 的 FoT `TOOL(action=)` 未注册工具 `KeyError` 位于 plaita 主仓
`plaita/node/tools.py`，不在本仓依赖面；本仓的 action 分发（rate_limit /
report）统一抛 `ValueError`，自然落入确定性分类。

## §4 A2R7-05 · 计时参数构造期 fail-closed

| 节点 | 历史 0 语义 | 现在 |
|---|---|---|
| gate `timeout_secs` | `or 600` → 静默 600s | 字面量构造期拒绝；DSL 表达式求值后、spawn 前拒绝 |
| capture `timeout_secs` | `communicate(timeout=0)` 立即 kill | 构造期拒绝 |
| api/generic `timeout_ms` | 拖到 requests 连接前才 `ValueError` | 构造期拒绝（含 generic_webhook） |

`_timeouts.positive_seconds/positive_millis`：拒绝 0、负值、NaN/inf、布尔；
错误文案带字段名（对齐主仓 r34/r39 A1 的 `_positive_timeout_seconds` 风格）。
gate 的 `$INPUT.x` 保留到 execute 求值，求值结果同样过校验。证据
`docs/proofs/data/38-probe-timeout-matrix.txt`：

```
[gate timeout_secs=0] ValidationError: ... gate timeout_secs must be a positive number ...
[capture timeout_secs=0] ValidationError: ... capture timeout_secs ...
[api timeout_ms=0] ValidationError: ... api_request timeout_ms ...
[gate expr=$INPUT.t->0] ValueError: gate timeout_secs must be a positive number ...
```

## §5 负控证据

| 文件 | 回退方式 | 结果 |
|---|---|---|
| `data/38-nc-a2r8-01.txt` | `_post_json` 恢复裸 `requests.post` | **2 failed / 3 passed**；原始 `ConnectionError` 文本含 `FEISHU_HOOK_SECRET_9f3a` |
| `data/38-nc-a2r8-0203.txt` | `_cancel` 的取消检查/可唤醒等待/可取消 communicate 全部退化 | **4 failed**；子进程存活、sentinel 落盘、hitl 未停止 |
| `data/38-nc-a2r7-04.txt` | `DeterministicError` 基类改回 `RuntimeError` | **4 failed / 1 passed**；LLM/API 401 在 `retry_times=2` 下各请求 3 次 |
| `data/38-nc-a2r7-05.txt` | `positive_seconds/positive_millis` 恢复宽松返回 | **17 failed / 2 passed**；0/负值/布尔构造全部成功 |

负控后源码已按备份逐字节恢复（`diff -r` 无差异），定向 39 passed。

## §6 验收命令

```bash
cd /Users/kong/projects/infra4agent/plaita-nodes
python3 -m pytest -q \
  tests/test_a2r8_webhook_redaction.py tests/test_a2r8_cancel.py \
  tests/test_a2r7_retry_classification.py tests/test_a2r7_timeout_matrix.py \
  tests/test_log_redaction.py
# -> 40 passed

python3 -m pytest -q
# -> 284 passed / 2 skipped
```

## §7 未纳入本轮（台账既有项）

- A2R7-06（200 + 业务错误码仍当成功）、A2R7-07（SQL 无 DB 级超时/取消）、
  A2R7-09/12/15、A2R8-04/05/06/07/08/10 等仍按台账待排期；本批只做用户指定
  的 A2R8-01/02/03、A2R7-04/05。
- `report` 的非法 action 从「静默按 write」改为 `ValueError`（与 rate_limit
  对齐），属 A2R7-04 分类落地的附带一致性修正。
