# RED-PROOF — P3 余量（看门狗早关 / 写锁 best-effort / jevlike 置信度）

日期：2026-10-20 · 执行者：修复落地席 B3（第五十七轮）
仓：`plaita-nodes`（基线 `origin/main 5d2bf7f`）
前置：r46（`663dbd0`）已修 A2R8-06/07/08 的主路径；本轮收三条**残留路径**。

## §0 结论速览

| 项 | 余量 | 结论 |
|---|---|---|
| ① 看门狗线程滞留（A2R8-06/A2R11-06） | r46 只钉了「正常完成」；`gen.close()`（SSE 断连）路径没有回归 | 生产代码无需再改（Event + 短 join 已覆盖 GeneratorExit）；补 `test_early_close_reaps_watchdog_thread` 钉住早关路径，防未来把 `finished.set()` 挪出 finally。 |
| ② 写入失败无收尸（A2R8-07/A2R12-03） | `agentproc.run_lock.write_run_lock` 对 OSError **返回 None**（best-effort），不是抛异常。r46 只捕获了异常路径 ⇒ 磁盘满/权限失败时 agent 仍**无锁继续跑**。 | `recursive_stream_turn` 显式把 `None` 升级为 `AgentRunError`：killpg 收尸 + 按 pid 清锁 + 抛出。新增 `test_best_effort_lock_failure_also_reaps_agent`。 |
| ③ decision 置信度 NaN（A2R8-08） | r46 的 `_confidence` 覆盖 llm/jev/节点 `_checked`；`_provider_jevlike._one` 仍裸 `float(p[best])`（provider 注册表可被直接调用）。 | jevlike `_one` 改用 `_confidence`，并校验输出维度与决策空间一致。新增 5 条坏值参数化 + 1 条正控。 |
| 负控 | 只回退 `agent_run.py`/`decision.py`、保留新测试 | **6 failed / 2 passed** 精确红（lock-None 1 + jevlike 5；2 passed = 早关看门狗验证 + jevlike 正控）。 |
| 正控 | 恢复生产文件 | 定向 **54 passed / 2 skipped**；全量 **391 passed / 2 skipped**。 |

## §1 修法

`src/plaita_nodes/agent_run.py`（`recursive_stream_turn`）：

```python
lock_path = _run_lock.write_run_lock(lock_key, proc.pid, argv)
if lock_path is None:
    # A2R8-07 余量：OSError 被 write_run_lock 吞成 None；不能当成功继续
    raise AgentRunError("流式 agent 遗言锁写入失败（write_run_lock 返回 None：…）")
```

仍由既有 `except BaseException` 接住：`_kill_process_group(proc)`（SIGKILL
组 + 5s 有界回收）→ `_clear_run_lock_owned` → 原样抛出。成功路径与
r46 的异常路径行为不变。

`src/plaita_nodes/decision.py`（`_provider_jevlike._one`）：

```python
if len(p) != len(choices):
    raise DecisionError("jevlike 输出维度 … 与决策空间 … 不一致")
return choices[best]["value"], _confidence(p[best])
```

`_confidence` 拒绝布尔/非数值/NaN/±inf/越界（r46 同一实现）。

## §2 负控（只回退生产文件，保留新测试）

```text
### PRE-FIX (仅回退两个生产文件，保留新测试)
FAILED TestLockWriteFailureCleanup::test_best_effort_lock_failure_also_reaps_agent
FAILED test_jevlike_provider_rejects_non_finite_confidence[probs0..4]
  Failed: DID NOT RAISE DecisionError
6 failed, 2 passed in 0.86s
```

唯一通过的是 `test_early_close_reaps_watchdog_thread`（r46 的 Event+join
已覆盖早关）与 jevlike 正控（合法 0.8 置信度不变）。原始输出：
`docs/proofs/data/57-nc-p3-residuals.txt`。

## §3 正控与回归

```text
### POST-FIX (定向)
54 passed, 2 skipped in 12.50s

### FULL SUITE
391 passed, 2 skipped in 55.22s
```

看门狗、写锁、decision 既有回归（r46 的 46 条）零变化。

## §4 边界

- 本仓 `recursive_stream_turn`（流式路径）已收口。非流式 `AgentRunNode`
  把 `run_lock_key` 交给 `agentproc.runner.run`，写锁发生在 agentproc 内部
  （`sdk/python/src/agentproc/runner.py` 两处 `write_run_lock`），同样未检查
  `None`；该仓不在本轮 plaita-nodes 范围内，留档供后续排期。
- `_drain_stderr` 排水线程仍为 daemon、不 join：子进程被 killpg 后 stderr
  EOF 即退出；若孙进程长期握住管道，线程会随进程一直存在（daemon，不阻退出）。
  与 A2R8-06 的「按 timeout_secs 线性累积」不同，本轮不改。
