# RED-PROOF — A2R12-01/02 stream 遗言锁生命周期竞态

日期：2026-10-19 · 执行者：修复落地席 B3（第四十五轮）
仓：`plaita-nodes`（基线 `origin/main 2bdddd7`）
规格：`duo-a2-ledger.md` 第十二轮 A2R12-01/02（P2×2），探针
`~/.issue-keeper/duty/a2-r12-scripts/{p2_lock_races.py,p3_stale_unlink.py}`

## §0 结论速览

| 项 | 结论 |
|---|---|
| A2R12-01 根因 | `preflight → Popen → write_run_lock` 是 check-then-act，无互斥；同进程所有 stream 默认共享 `host-<pid>` 键。A/B 都能在对方落锁前越过 preflight，后写者覆盖先写者；先完成者在 `finally` 里 `clear_run_lock` 无条件 unlink，删掉仍在跑的兄弟锁 → 活进程无锁（pn#19 经共享键复发）。探针观测 `None→a→b→None→c→None`。 |
| A2R12-01 修法 | 同一遗言锁键上的 preflight / Popen / write / clear 进同一临界区：进程内可重入 `RLock`（console 多线程）+ 跨进程 guard 文件 `flock`（POSIX）。清锁改为**按锁记录 pid 归属**清理：只有 `record["pid"] == proc.pid` 才 unlink；归属不符/记录不可读一律保留给下一次 preflight。 |
| A2R12-02 根因 | `cleanup_stale_run` 的「读记录 → `_pid_alive`/`ps` 核实 → `_unlink`」无原子性；两个并发 preflight 读到同一 stale 快照时，慢者会用旧快照删掉快者随后重建的新锁。窗口含一次 `ps` 子进程，非纯理论。 |
| A2R12-02 修法 | `preflight_workspace` 把 `cleanup_stale_run` 整体放进同一 `_run_lock_guard`；所有同键写锁（stream 的 Popen→write）也持该 guard。读-判-删相对所有 pn 写者成为原子序列，慢清理者不可能在「读」与「unlink」之间插入一次新锁写入。 |
| 为什么不把键改成每 run 唯一 | A2 报告已注明：per-run 唯一键会让下一次 preflight 看不见同 workspace 其它 live agent（B2R11-02/「保锁不可见」盲区）。本轮保留共享键，用 serialization + ownership 修，而不是隐藏锁。 |
| 负控 | 只回退 `src/plaita_nodes/agent_run.py`（保留新测试）：**3 failed / 0 passed**，三条精确红见 §2。 |
| 正控 | 定向 24 passed（新增 3 + 既有 stream/lock/perrun/#20 21）；全量 **354 passed / 2 skipped**。 |
| 回归 | pn#19 终止语义不变：仍先 `_terminate_stream_process` 确认终态，确认后才清锁；无法确认终态仍保留锁。r40 租户闸仍在生成器第一行、任何副作用之前。 |

## §1 修法

`src/plaita_nodes/agent_run.py`：

1. 新增 `_run_lock_guard(lock_key)`（可重入）：
   - 进程内：按 guard 路径取 `threading.RLock`（console 多线程共享
     `workspace#host-<pid>` 键正是本单的复现面）；
   - 跨进程：`~/.agentproc/run-locks/<sha>.json.guard` 上 `flock(LOCK_EX)`；
   - 可重入：`recursive_stream_turn` 外层持锁跨
     `preflight_workspace → Popen → write_run_lock`，而
     `preflight_workspace` 自身也要为 keeper reaper / 非流式路径独立持锁。
   - 锁目录不可写时降级为进程内互斥（warning），不把遗言锁从
     best-effort 变成硬依赖。
2. `preflight_workspace`：`cleanup_stale_run` 在 guard 内执行（A2R12-02）。
3. `recursive_stream_turn`：preflight 与 Popen、`write_run_lock` 合并进同一
   guard 临界区（A2R12-01 的 check-then-act）。
4. 新增 `_clear_run_lock_owned(lock_key, pid)`：在 guard 内读锁记录，
   `pid` 归属匹配才 unlink；兄弟 run 重建的新锁、不可读/损坏记录都保留
   （A2R12-01 的迟到删除）。
5. 未改 `write_run_lock` / `cleanup_stale_run` 的 agentproc 语义，也未改
   stream 的终止/超时/断连路径。

## §2 负控（只回退生产文件）

```text
$ cp src/plaita_nodes/agent_run.py /tmp/agent_run_fixed_r45.py
$ git checkout -- src/plaita_nodes/agent_run.py     # 回到 2bdddd7
$ pytest -q tests/test_agent_run_lock_races.py
FAILED TestConcurrentStreamTurnGate::test_concurrent_stream_turns_second_is_gated
  AssertionError: 第二个 stream 未被闸：{'ok': True}
  assert False = isinstance(None, AgentRunError)

FAILED TestClearRunLockOwnership::test_late_clear_keeps_sibling_lock
  AssertionError: A 的迟到收尾把 B 重建的锁删了（B 的 agent 仍存活）
  assert False = exists()  # .../<sha>.json

FAILED TestStaleCleanupRace::test_stale_cleanup_does_not_delete_rebuilt_lock
  AssertionError: 慢 preflight 的迟到 unlink 删除了快 run 重建的新锁（活进程无锁）
  assert False = exists()  # .../<sha>.json

3 failed in 4.26s
```

三条红分别对应规格里的三个负控观察：

1. A/B 并发越过 preflight 后，第二个**没有**被闸（两个 agent 都 ok）；
2. 先结束者的无条件清锁删掉了兄弟 run 重建的锁（agent 仍存活）；
3. 并发 preflight 下快者重建的新锁被慢者的迟到 unlink 删除。

证据：`docs/proofs/data/45-nc-a2r12-lock-races.txt`。

## §3 正控与回归

```text
# 定向（新增 3 + 既有 stream/lock/per-run/#20）
$ pytest -q tests/test_agent_run_lock_races.py tests/test_agent_run_stream.py \
    tests/test_agent_run_lock.py tests/test_agent_run_lock_perrun.py \
    tests/test_issue20_stream_tenant_guard.py
24 passed in 13.31s

# 新增用例连跑 3 次
3 passed in 8.04s / 8.04s / 8.06s

# 全量（PYTHONPATH 指向 plaita wt3 @ a43f710，含 r44 run_token 修复）
$ pytest -q
354 passed, 2 skipped in 65.52s
```

说明：本机 editable 的 plaita 主 checkout 停在 `37434a0`（早于 r44 的
`a43f710`），直接跑全量会红 `test_issue9_report_run_token`；这与本轮改动无关。
按 r44 的口径把 `PYTHONPATH` 指向 plaita wt3 后全量绿。

回归面：

- pn#19：`test_close_kills_agent_before_lock_release`、
  `test_disconnect_never_leaves_unlocked_live_agent`、
  `test_unkillable_agent_keeps_lock_and_preflight_refuses`、
  `test_normal_completion_releases_lock_without_kill` 全绿——终止确认后
  才清锁、无法确认则保锁的语义未变；
- pn#20：`test_issue20_stream_tenant_guard.py` 全绿——租户闸仍在生成器
  入口、preflight/Popen 之前；
- per-run 锁可见性：`test_agent_run_lock_perrun.py` 全绿——兄弟 run 的
  live agent 与锁不被误杀/误删。

## §4 改动文件

| 文件 | 内容 |
|---|---|
| `src/plaita_nodes/agent_run.py` | `_run_lock_guard` / `_guard_path` / `_guard_rlock`；`preflight_workspace` 持锁；stream 临界区；`_clear_run_lock_owned` |
| `tests/test_agent_run_lock_races.py` | 三条竞态回归（并发闸 / 清锁归属 / stale 迟到 unlink） |
| `docs/proofs/data/45-nc-a2r12-lock-races.txt` | 负控原始输出 |
