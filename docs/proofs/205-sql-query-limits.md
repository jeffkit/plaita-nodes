# RED-PROOF — #205 sql_query 结果行数/字节上限（fail-closed）

日期：2026-10-18 · 执行者：修复落地席（jeffkit）· 仓库 `plaita-nodes` ·
基线 `48d087e` · 单 commit
工作区：`/tmp/wt205-nodes`（worktree）

## §0 结论速览

| 验收项 | 结果 |
|---|---|
| 100k 行默认查询 | 修复前 **100000 行 / 25.84MB peak / 无上限字段**；修复后 **`SqlResultLimitError`，1001 行处 fail-closed / 0.29MB peak** |
| 默认值 | `max_rows=1000`、`max_bytes=4 MiB`（按行 JSON 编码累计；写入模块/类 docstring） |
| 小于上限 | 与现状逐字一致（3 行用例 exact dict） |
| 精确边界 | 5 行 + `max_rows=5` 正常返回 5 行 |
| 超限标记 | 具名 `SqlResultLimitError`（含 max_rows/max_bytes/observed），不返回部分结果 |
| 写 / dry-run | 写 `{"rowcount": n}`、dry `{"rows": [], "rowcount": 0, "dry_run": True}` 不变 |
| 负控 | 恢复全量物化 ⇒ 新测试 **3 failed / 4 passed**（行/字节/100k 红；正常/边界/写/dry 绿） |
| 测试 | 定向 **7 passed**；connectors **21 passed**；plaita-nodes 全量 before **11F/150P** → after **11F/157P**，失败集合**逐条相同**（环境面） |
| 主仓 landing gate | `plaita` origin/main `1ab7e1b` CI success（主仓未改；代码在兄弟仓） |

## §1 缺陷、规格与载体

`plaita-nodes/src/plaita_nodes/database.py` 的 `SqlQueryNode.execute` 原样：

```python
result = conn.execute(text(sql), bind or {})
if result.returns_rows:
    rows = [dict(r._mapping) for r in result]
    return {"rows": rows, "rowcount": len(rows)}
```

无行数/字节上限——10 万行 SELECT 会全量物化进节点输出，进一步进
checkpoint/执行状态；worker 可 OOM，分布式重投形成「重跑→再 OOM」循环。

规格（#205 comment `6238...`）：
- 新增 `max_rows`（默认 1000）/`max_bytes`（默认 4 MiB）；
- `fetchmany`/分批 + `stream_results`，达到上限即停止；
- 10 万行：≤ max_rows、**具名标记**、内存有界；小于上限结果逐字一致；
- 写操作与 dry-run 不变；负控移除上限必红；不改 SQL/params/凭据。

**载体**：`FROM-OKGUITAR/patch-w39-plaita-nodes.patch` 本机不存在，gist/分支
也未检索到；按提报席最后一条「已修」评论（fail-closed 交付）实现，只改
`database.py` 结果面 + 本单测试/证明。

**一处与 issue 正文的偏差（沿用提报席最终裁决）**：issue 建议返回
`truncated=true` 的部分结果；最终交付选择 **fail-closed 抛
`SqlResultLimitError`，不返回部分结果**——静默截断会让下游在不完整数据上
继续计算。具名异常即「截断标记」，且不静默丢。默认值已写进模块/类
docstring 与证明。

## §2 实现

`SqlQueryNode` 新增字段：

```python
max_rows: int = 1000
max_bytes: int = 4 * 1024 * 1024
```

`execute` 的 SELECT 分支：

- `conn.execute(text(sql).execution_options(stream_results=True), ...)`；
- `result.fetchmany(256)` 分批；
- 每行 `dict(r._mapping)` 后按 `json.dumps(..., default=str)` 的 UTF-8 字节
  累计；
- **先判上限再入列**：`len(rows)+1 > max_rows` 或
  `used_bytes+size > max_bytes` ⇒ 抛 `SqlResultLimitError`
  （`max_rows/max_bytes/observed_rows/observed_bytes`）；
- `rowcount` 仍为**返回行数**（不改成总行数，保持兼容）；
- 写操作（`returns_rows` 假）与 dry-run 路径不变；不改 SQL/params/凭据。

## §3 修复前 / 修复后

探针：`docs/proofs/data/205-repro.py`（SQLite 100k 行）。修复前在干净
worktree `48d087e` 跑（`PYTHONPATH=/tmp/wt205-before/src`），修复后在本树跑；
机读结果 `docs/proofs/data/205-before-after.json`。

| 观测 | 修复前 | 修复后 |
|---|---|---|
| `rows_returned` | `100000` | —（异常） |
| `rowcount` | `100000` | — |
| 结果 | 整表返回 | **`SqlResultLimitError`** |
| `observed_rows` | — | **`1001`**（第 1001 行 fail-closed） |
| `peak_mb`（tracemalloc，仅 execute） | **25.84** | **0.29** |
| `has_max_rows` / `has_max_bytes` | `false/false` | `true/true` |
| `elapsed_s` | 1.226 | 0.034 |

（提报席另测 94.6MB / 0.5MB，差异来自 SQLite 行宽与 tracemalloc 口径；
方向与判据一致：全量物化 → 有界 fail-closed。）

## §4 验收

### §4.1 定向

```
PYTHONPATH=/tmp/wt205-nodes/src pytest tests/test_issue205_sql_query_limits.py -q
⇒ 7 passed
PYTHONPATH=/tmp/wt205-nodes/src pytest tests/test_connectors.py \
    tests/test_issue205_sql_query_limits.py -q
⇒ 21 passed
```

覆盖：小于上限逐字一致、精确边界、行上限、字节上限、写路径、dry-run、
100k fail-closed + peak < 20MB。

### §4.2 plaita-nodes 全量（基线对照）

```
before (48d087e): 11 failed, 150 passed, 2 skipped
after  (f6cf5ef): 11 failed, 157 passed, 2 skipped
```

失败集合**逐条相同**：`test_agent_run_lock*` / `test_registry::test_all_nodes_matches_entry_points` /
`test_subprocess_env_isolation`——均为 worktree/editable 环境面（非 sql_query），
before/after 同红；after 只多 7 个 #205 测试通过。

### §4.3 主仓 landing gate

`plaita`（主仓）未改代码；`landing_gate.py ci` 最近完成 run
`1ab7e1b` conclusion=success，允许落地。代码修复在兄弟仓 `plaita-nodes`，
其 `test.yml` 在推送后运行；本单以全量本地对照 + 定向 21 passed 作为验收证据。

## §5 负控（变异纪律）

在 `f6cf5ef` 的临时 worktree 恢复全量物化：

```diff
-                rows: list[Dict[str, Any]] = []
-                ... fetchmany/limits ...
+                rows = [dict(r._mapping) for r in result]
                 return {"rows": rows, "rowcount": len(rows)}
```

结果：

```
tests/test_issue205_sql_query_limits.py ⇒ 3 failed, 4 passed
```

- **红**：`test_row_limit_fails_closed`、`test_byte_limit_fails_closed`、
  `test_100k_default_fails_closed_with_bounded_peak`（无异常、整表返回）；
- **绿**：小于上限、精确边界、写、dry-run（不依赖上限）。

变异 diff 与红名单：`docs/proofs/data/205-negative-control.diff` /
`205-negative-control.txt`；SHA256 见 `205-before-after.json`。

## §6 关系与边界

- 只动 `plaita-nodes/src/plaita_nodes/database.py` 的 sql_query 结果面；
  不改 SQL/params 表达式求值、凭据解析、节点名/既有输出字段含义。
- `truncated` 字段未新增：按提报席最终 fail-closed 裁决，用
  `SqlResultLimitError` 作具名标记；若后续要部分结果语义，另开规格。
- 默认 1000 行对存量大查询是**破坏性收紧**；错误消息给出调大
  `max_rows`/`max_bytes` 或加 WHERE/LIMIT 的指引。

## §7 复跑

```bash
cd /tmp/wt205-nodes
PYTHONPATH=/tmp/wt205-nodes/src <venv>/python docs/proofs/data/205-repro.py
PYTHONPATH=/tmp/wt205-nodes/src <venv>/python -m pytest \
    tests/test_issue205_sql_query_limits.py -q
PYTHONPATH=/tmp/wt205-nodes/src <venv>/python -m pytest tests -q
cd /Users/kong/projects/infra4agent/plaita && <venv>/python scripts/landing_gate.py ci
```

## §8 变更文件

- `src/plaita_nodes/database.py`：`SqlResultLimitError`、默认上限字段、
  `fetchmany` + `stream_results` fail-closed 读取；
- `tests/test_issue205_sql_query_limits.py`：7 条上限/边界/写/dry/100k 测试；
- `docs/proofs/data/205-repro.py` / `205-before-after.json` /
  `205-negative-control.{diff,txt}`：机读旁证。
