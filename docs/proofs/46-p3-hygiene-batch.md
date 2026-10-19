# RED-PROOF — P3 卫生批四项（看门狗线程 / 写锁失败收尸 / 置信度 / tmp 残留）

日期：2026-10-19 · 执行者：修复落地席 B3（第四十六轮）
仓：`plaita-nodes`（基线 `origin/main 4785eb2`）
规格：`duo-a2-ledger.md` 第八轮 A2R8-06/07/08、第十二轮 A2R12-03；
残留清扫采用第十一轮 A2R11-08 的通用形态（nodes 侧唯一实例：
`hitl_poller` sessions 原子写）。A2 探针：
`~/.issue-keeper/duty/a2-r8-scripts/probe_decision_gate.py`、
`~/.issue-keeper/duty/a2-r12-scripts/p7_popen_try_window.py`。

## §0 结论速览

| 项 | 结论 |
|---|---|
| ① A2R11-06/A2R8-06 看门狗线程 | `_enforce_deadline` 无条件 `sleep(remaining)`：3 个正常完成的 60s turn 后残留 **3 条**存活线程，短流高频生成线性累积。改为 `threading.Event.wait(remaining)` + 正常完成/提前 close 时 `set()` 并 `join(1s)`，线程立即回收。 |
| ② A2R12-03 写锁失败 | `Popen` 成功、`write_run_lock` 抛非 `OSError` 时 `finally` 尚未建立：agent 存活且无锁（探针 `ORPHAN-NO-LOCK`）。写锁失败路径先 `_kill_process_group`（含 5s 有界回收）再按 pid 归属清锁，最后原样抛出。 |
| ③ A2R8-08 置信度 | `min(1.0, nan)` 在 Python 得 **1.0**，`confidence: NaN` 冒充最高置信且 `low_confidence=False`；`inf` 原样输出。统一 `_confidence()`：布尔/非数值/非有限/越界均具名 `DecisionError`，llm/jev/custom/批量共用；`min_confidence` 在 `validate()` 构造期拒绝。 |
| ④ A2R11-08 tmp 残留 | 旧实现 replace 前把 tmp chmod 成目标权限（常见 0644），SIGKILL 后留下 0644 全内容 `.sessions.json.*.tmp`；无清扫。临时文件保持 0600 到替换，目标继承 0600；写回前 + session 结束 + 空启动时清扫过龄残留（300s 年龄门槛，避免误删并发写者）。 |
| 负控 | 只保留新测试、回退生产文件：**19 failed / 1 passed**（1 passed 是「新 tmp 不误删」安全用例），精确红见 §2。 |
| 正控 | 定向 **81 passed / 2 skipped**；全量 **374 passed / 2 skipped**；时序敏感用例连跑 3 次稳定。 |

## §1 修法

`src/plaita_nodes/agent_run.py`：

1. `_stream_turn_body`：新增 `finished = threading.Event()`；看门狗从
   `time.sleep(remaining)` 改为 `finished.wait(remaining)`，正常完成或
   消费者提前 close 时在 `finally` 里 `finished.set()` + `watchdog.join(1s)`。
   超时击杀语义不变（deadline 到仍未结束 → killpg）；`stdout` EOF 后的
   `proc.wait(deadline 剩余)` 兜底保留。
2. `recursive_stream_turn`：`write_run_lock` 包进 `try/except BaseException`；
   失败路径 `_kill_process_group(proc)`（killpg + wait(5)）→
   `_clear_run_lock_owned(lock_key, proc.pid)`（只清归属本 proc 的锁，清锁
   异常不掩盖原始异常）→ `raise`。`Popen` 之后的任何失败都不再留下无锁孤儿。

`src/plaita_nodes/decision.py`：

3. 新增 `_confidence(value, *, label="confidence")`：拒绝 `bool`、非数值、
   `NaN/±inf`、`[0,1]` 越界，错误信息带条目上下文（`第 N 项 confidence`）。
   `_provider_llm._one`、`_provider_jev._answer`、`DecisionNode._checked`
   全部改走该函数，单条/批量/自定义 provider 同一口径。
4. `DecisionNode.validate()`：`min_confidence` 复跑同一校验，构造期
   `ValueError`（`decision 节点配置非法: min_confidence 非有限值/越界`）。

`src/plaita_nodes/hitl_poller.py`：

5. 新增 `_sweep_stale_tmp(path, max_age_secs=300)`：清扫同目录
   `.<name>.*.tmp` 中 mtime 超过门槛的残留；并发写者毫秒级临时文件不受影响。
6. `_write_sessions_atomic`：`mkstemp` 后 `fchmod(0600)`，删除 replace 前
   「继承目标权限」的 chmod；写回前先清扫残留。替换后目标为 0600。
7. `poll_round` 在本轮有终态发布（session 结束）时清扫一次；`main()` 在
   「无可轮询 session」退出前清扫一次。

## §2 负控（只回退生产文件，保留新测试）

```text
$ pytest -q [四条新增用例]
FFFFFFFFFFFFFFFFFF.F                                                     [100%]

FAILED TestWatchdogThreadLifecycle::test_normal_completion_does_not_leak_watchdog_threads
  AssertionError: 3 个正常完成的短 turn 后仍有 3 条 _enforce_deadline 线程存活
  assert 3 == 0

FAILED TestLockWriteFailureCleanup::test_write_lock_failure_kills_spawned_agent
  AssertionError: 写锁失败后已 spawn 的 agent 必须被终止并回收（否则是无锁孤儿）
  assert None is not None  # proc.poll()

FAILED test_llm_provider_rejects_non_finite_confidence[nan/inf/-inf]
FAILED test_custom_provider_rejects_non_finite_confidence[nan/inf/-inf]
  Failed: DID NOT RAISE <class DecisionError>
  # NaN 被夹成 1.0、inf 原样放行

FAILED test_confidence_out_of_range_rejected[-0.01/1.01]
FAILED test_confidence_bool_rejected
FAILED test_batch_rejects_non_finite_confidence
  Failed: DID NOT RAISE  # 越界/布尔/批量均未校验

FAILED test_validate_rejects_bad_min_confidence[nan/inf/-0.1/1.1]
  Failed: DID NOT RAISE <class ValueError>

FAILED test_atomic_write_keeps_target_private
  AssertionError: assert 420 == 384  # 0644 != 0600

FAILED test_atomic_write_sweeps_stale_tmp_siblings
  AssertionError: SIGKILL 残留的 sessions 临时文件应被清扫

FAILED test_terminal_session_sweeps_stale_tmp
  AssertionError: session 结束时应清扫残留临时文件

19 failed, 1 passed, 1 warning in 2.45s
```

唯一通过的是 `test_sweep_keeps_recent_tmp`：它断言「新 tmp 不被误删」，
pre-fix 本来就不清扫，故为绿；它防的是修复后的过度清扫。

证据原文：`docs/proofs/data/46-nc-p3-hygiene.txt`。

## §3 正控与回归

```text
# 新增用例（修复后）
$ pytest -q [四条新增用例]
20 passed in 2.11s

# 时序敏感部分连跑 3 次
$ pytest -q test_agent_run_stream.py::TestWatchdogThreadLifecycle \
    test_agent_run_stream.py::TestLockWriteFailureCleanup \
    test_hitl_poller_tmp_hygiene.py
6 passed in 0.69s / 0.51s / 0.53s

# 定向（agent stream/lock/perrun/#20 + decision + hitl_poller/#7/#10 + 新文件）
$ pytest -q tests/test_agent_run_stream.py tests/test_agent_run_lock.py \
    tests/test_agent_run_lock_races.py tests/test_agent_run_lock_perrun.py \
    tests/test_issue20_stream_tenant_guard.py tests/test_decision.py \
    tests/test_hitl_poller.py tests/test_issue7_hitl_sessions_lifecycle.py \
    tests/test_issue10_hitl_reply_correlation.py tests/test_hitl_poller_tmp_hygiene.py
81 passed, 2 skipped in 26.27s

# 全量
$ pytest -q
374 passed, 2 skipped in 64.63s
```

回归面：

- 看门狗：`test_hung_process_killed_at_deadline`、
  `test_output_after_deadline_reports_timeout` 仍绿——deadline 击杀与
  「有输出但拖过 deadline」不受影响；
- 锁：#19 四项（close 先杀后清锁 / 无法确认保锁 / 正常完成不杀）、
  A2R12-01/02 三项并发竞态、per-run 可见性、r40 租户闸全绿；
- decision：既有序号归位、阈值 default/error/passthrough、dry-run、
  jev/llm/自定义 provider 全绿；合法 0..1 置信度行为不变；
- hitl_poller：#7 每轮重读/增量删除、#10 correlation_id、原子替换
  无残留断言全绿。

## §4 改动文件

| 文件 | 内容 |
|---|---|
| `src/plaita_nodes/agent_run.py` | 看门狗 Event 唤醒 + 短 join；写锁失败 killpg/清锁 |
| `src/plaita_nodes/decision.py` | `_confidence` 校验（llm/jev/custom/批量共用）；`min_confidence` 构造期校验 |
| `src/plaita_nodes/hitl_poller.py` | `_sweep_stale_tmp`；tmp 保持 0600；session 结束/空启动清扫 |
| `tests/test_agent_run_stream.py` | 看门狗线程回收 + 写锁失败收尸两条 |
| `tests/test_decision.py` | NaN/inf/越界/布尔/批量/min_confidence 构造期九条参数化用例 |
| `tests/test_hitl_poller_tmp_hygiene.py` | 0600、残留清扫、session 结束清扫、新 tmp 不误删四条 |
| `docs/proofs/data/46-nc-p3-hygiene.txt` | 负控原始输出（19F/1P） |
