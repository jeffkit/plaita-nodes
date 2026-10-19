# RED-PROOF — pn#19 流式消费方断连：先终止 agent，再释放遗言锁

日期：2026-10-19 · 执行者：修复落地席 B3（第三十七轮）· 仓库 `plaita-nodes` ·
基线 `origin/main 4e30339` · 单 commit（closes [#19](https://github.com/jeffkit/plaita-nodes/issues/19)）

## §0 结论速览

| 验收项 | 结果 |
|---|---|
| 根因 | `recursive_stream_turn`（`src/plaita_nodes/agent_run.py:690`）在 `finally` 里只调 `clear_run_lock`，不杀 agent。消费方提前关闭生成器（SSE 断连 / `gen.close()` / 路由层停止迭代）→ `GeneratorExit` 在 `yield` 处展开 `finally` → 清锁后 agent 进程组仍在写 workspace |
| 影响 | 下一次任何 run 的 `preflight_workspace` 看不到锁 → `clean` 放行 → 同 workspace 无锁并发写；锁已删 = 不可见，静默丢改动。窗口 = 剩余 `timeout_secs`（默认 1800s），反复断连可累积多个孤儿 |
| 修法 | 收尾顺序改为「**先终止并确认 agent 终态、再清锁**」：新增 `_terminate_stream_process`——`proc.poll()` 仍存活则 `_kill_process_group`（SIGKILL 整组）+ 有界 `wait`；确认终态才 `clear_run_lock`。若 5s 内无法确认终态，**保留遗言锁**并记 error，让下一次 preflight 走 kill-before-start（绝不「清锁 + 进程仍活」） |
| 负控 | 还原为 pre-fix 实现（仅还原 `agent_run.py`）：新测试 **3 failed / 5 passed**；精确红为 `断连后 agent 仍存活(pid=…) 且遗言锁已删除`。独立探针 pre-fix `alive=true / lock=false / preflight=clean / violation=true`（exit 1） |
| 正控 | 恢复修复后同一探针 `alive=false / lock=false / preflight=clean`（exit 0）；新测试 4 条全绿 |
| 相关套件 | `test_agent_run*` 四文件 **32 passed** |
| 全量 | `pytest` **249 passed / 2 skipped**（基线 245 passed / 2 skipped，+4 新用例） |
| 正常完成路径 | 零变化：子进程已由 `_stream_turn_body` wait；收尾不击杀、锁照旧清理（专项用例断言 `_kill_process_group` 零调用） |

## §1 缺陷链路

```python
proc = subprocess.Popen(argv, ..., start_new_session=True)
_run_lock.write_run_lock(lock_key, proc.pid, argv)
try:
    yield from _stream_turn_body(proc, timeout_secs)
finally:
    _run_lock.clear_run_lock(lock_key)   # ← 只清锁，没杀进程
```

- `recursive_stream_turn` 是 console 的流式生成后端；宿主断连时生成器被
  `close()`，`GeneratorExit` 在 `yield` 处抛出，`finally` 执行；
- `_stream_turn_body` 的看门狗线程仍存活，只会在 `timeout_secs` 到期时
  killpg——那段窗口内 agent 继续写 workspace，而锁文件已删除；
- 下一次 `preflight_workspace(workspace, run_key)` 读不到锁 → `clean` →
  新 agent 与孤儿并发写同一 workspace（`has_changes` / WIP 快照 / 发布可能
  基于互相覆盖的中间态）。

对照：`AgentRunNode` 直跑路径把 `run_lock_key` 交给 agentproc runner，锁的
生命周期覆盖子进程 wait；只有本流式辅助路径自己管锁，漏了「杀进程」这一半。

## §2 修法

`src/plaita_nodes/agent_run.py`：

- `recursive_stream_turn` 的 `finally`（`:734-745`）：
  ```python
  if _terminate_stream_process(proc):
      _run_lock.clear_run_lock(lock_key)
  else:
      _log.error("流式 agent pid=%s 未能确认终止，保留遗言锁 ...")
  ```
- 新增 `_terminate_stream_process(proc) -> bool`（`:748-762`）：
  - `proc.poll() is None` → `_kill_process_group(proc)`（已有 SIGKILL 整组 +
    5s 回收；`start_new_session` 使 agent 拉起的子树同组）；
  - 再 `proc.wait(timeout=5)`；`TimeoutExpired` → 返回 `False`，调用方保留锁；
  - 正常完成时子进程已退出 → 不击杀、立即返回 `True`，锁释放语义不变。
- 语义兜底：**清锁的前提是 agent 已终态**；无法确认终态时锁仍在，下一次
  `preflight` 会看到存活 pid（spawner=本进程）并抛 `RunLockBusy` 拒绝开工
  （fail-closed），而不是放行并发写。

## §3 负控复现（pre-fix 形态）

仅还原 `src/plaita_nodes/agent_run.py`（测试保留），跑
`tests/test_agent_run_stream.py`：

```text
FAILED test_close_kills_agent_before_lock_release
FAILED test_disconnect_never_leaves_unlocked_live_agent
FAILED test_unkillable_agent_keeps_lock_and_preflight_refuses
E   AssertionError: 断连后 agent 仍存活(pid=9696) 且遗言锁已删除：下一次 preflight 将放行并发写
E   assert not (True and not False)
3 failed, 5 passed in 4.60s
```

独立探针 `docs/proofs/data/19-repro.py`（真实子进程 + 真实遗言锁，同 issue
的 `probe_stream_abandon.py` 形态）：

| 版本 | `agent_alive_after_close` | `lock_exists_after_close` | `preflight` | 退出码 |
|---|---|---|---|---|
| pre-fix | `true` | `false` | `{"action": "clean"}` | 1（violation） |
| post-fix | `false` | `false` | `{"action": "clean"}` | 0 |

pre-fix 输出原文见 `docs/proofs/data/19-negative-control.txt`（pytest 负控）。

## §4 测试

新增 `TestConsumerCloseTerminatesAgent`（4 条）：

1. `test_close_kills_agent_before_lock_release`：运行中有锁；`gen.close()`
   返回后 agent pid 已不存在、锁已删除；
2. `test_disconnect_never_leaves_unlocked_live_agent`：负控不变式——断连后
   不得出现「agent 存活 + 锁不存在」；修后 agent 死、preflight 返回 clean；
3. `test_unkillable_agent_keeps_lock_and_preflight_refuses`：模拟收尾无法
   确认终态（`_terminate_stream_process` → `False`）→ 锁保留、agent 存活，
   `preflight_workspace` 抛 `AgentRunError`（不杀不放行）；身份探测桩化
   （Linux `ps` 会按 80 列截断长命令行，桩脚本 basename 可能落在截断外，
   与生产短命令名不同）；
4. `test_normal_completion_releases_lock_without_kill`：正常完成不调用
   `_kill_process_group`，锁按原语义释放。

结果：`tests/test_agent_run_stream.py` **8 passed**；`test_agent_run*` 四文件
**32 passed**；全量 **249 passed / 2 skipped**。

## §5 落地

- 单 commit：`fix(recursive_stream): 消费方断连终止 agent/保锁（closes #19）`
- 推送：`origin/main`（`4e30339..<commit>`）
- 证据：`docs/proofs/data/19-repro.py`、`docs/proofs/data/19-negative-control.txt`
