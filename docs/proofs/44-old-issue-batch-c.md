# RED-PROOF — pn 旧单批 C（#6/#9/#12/#14/#8）+ #15 特判

日期：2026-10-19 · 执行者：修复落地席 B3（第四十四轮）
仓：`plaita-nodes`（基线 `origin/main 7e5ef75`）+ `plaita`（基线 `origin/main 37434a0`）

> 本轮按 **issue 标题** 取规格（正文与标题存在编号错位：如 #12 正文写的是 #9、
> #9 正文写的是 #11；#8 正文是 #7）。#14 的代码在 `plaita` 仓（`plaita/server/factory.py`），
> 不在 `plaita-nodes`；#9 的「run_token 写入点」在 `plaita` 引擎。
> 所有「已覆盖」结论都以当前 main 的真实代码与测试运行为准，不按 issue 正文推断。

| Issue | commit | 内容 |
|---|---|---|
| [#8](https://github.com/jeffkit/plaita-nodes/issues/8) | `e93f7b9` | decision 批量结果按模型返回 index 贴标签 |
| [#6](https://github.com/jeffkit/plaita-nodes/issues/6) | `2690a11` | `resolve_provider` 的 keyEnv 回退走宿主 environ + fail-fast |
| [#12](https://github.com/jeffkit/plaita-nodes/issues/12) | `81264eb` | email `body_is_html` 的 text/plain 备件去标签 |
| [#9](https://github.com/jeffkit/plaita-nodes/issues/9) | `a43f710`（plaita）+ `50421f9`（nodes 回归） | 根 run 注入 `run_token`，跨 run 唯一、map 共享 |
| [#14](https://github.com/jeffkit/plaita-nodes/issues/14) | 无（已由 plaita#88 覆盖，merge `fe1590e`） | `_parse_redis_url` 三形态 + 具名拒绝 |
| [#15](https://github.com/jeffkit/plaita-nodes/issues/15) | 无（分支 + plaita 控制面已覆盖） | sync_in fail-closed + 有界重放证据 |

## §0 结论速览

| 验收项 | 结果 |
|---|---|
| #8 根因 | `_provider_llm` 与 `_execute_batch` 都 `enumerate(entries)` 按数组位置写 `index`，模型返回的 `index` 被完全忽略 |
| #8 修法 | `_indexed_batch()` 统一校验条数/对象/index 整数/0..N-1/不重复/不缺失，按模型 index 归位；重复、越界、非整数、缺失均抛具名 `DecisionError` |
| #6 根因 | `(environ or {}).get(keyEnv)`：`environ=None` 时退化成空 dict，旧 flowcast 的 keyEnv 恒解析为空串且无告警 |
| #6 修法 | `environ=None` 时读 `os.environ`（与 `interpolate` 同源）；keyEnv 变量缺失/为空 → 具名 `AgentConfigError`（含 provider/变量名，不含值） |
| #12 根因 | `msg.set_content(text)` 把 HTML 原文塞进 text/plain 件，再 `add_alternative(html)`——不支持 HTML 的客户端/网关看到标签源码 |
| #12 修法 | `body_is_html=True` 时 text/plain 件 = `_html_to_text()`（去标签、实体解码、块级换行、跳过 script/style/head）；HTML 仍为 alternative 主件 |
| #9 根因 | report 的 token 回退链末跳是字面量 `"default"`，`run_token` 全仓无写入点 → 同 flow 的多次 run 写同一个 `default.jsonl` |
| #9 修法 | `plaita` 的 `ExecutionContext.setup_flow` 仅在**根上下文**注入 `globalContext.run_token = 当前 execution_id`；子流程不复制、经 parent 回落到同一 run；显式 `flow.global_context.run_token` 优先 |
| #14 结论 | 已由 plaita#88 修复并合入 main（`fe1590e` merge）：`rediss://` 保留端点并 `ssl=True`、`user:pass@` 正确取用户名/口令、省略端口默认 6379，畸形形态抛 `RedisUrlError` 而非回落 localhost |
| #15 结论 | AGS 沙箱线**未并入 main**（main 无 `sandbox_*`/`sync_in`）；分支的语义是 **fail-closed**（非自愈），分支测试通过；控制面有界性测试已在 plaita main（`170daa0`，上限钉死 12），原「重放 181 次」症状由条件 2 的测试覆盖 |
| 负控 | #8 **7 failed**；#6 **4 failed / 2 passed**；#12 **4 failed**；#9 plaita 单测 **2 failed / 1 passed** + nodes 端到端 **1 failed（read 到 6 条）** |
| 正控 | nodes 定向 50 passed / 2 skipped + email 6 passed；nodes 全量 **351 passed / 2 skipped**；plaita context 相关 **201 passed / 1418 subtests**；#14 定向 **6 passed / 38 subtests**；#15 分支 sync_in **2 passed**、plaita 有界性 **4 passed** |

## §1 pn#8 decision 批量 index

`src/plaita_nodes/decision.py`：

- 新增 `_indexed_batch(entries, count)`：先判条数与元素类型，再要求每条携带
  `index`（`bool` 不算整数、字符串不隐式转换），`index` 必须落在
  `0..count-1`、不得重复、不得缺失；返回按 index 升序的 `(index, entry)`。
- `_provider_llm` 与 `DecisionNode._execute_batch` 共用该函数。后者是自定义
  provider 的最后一层守卫——即使 provider 自己乱序，也不会把 choice/置信度
  贴到错误条目上。
- `on_low_confidence="error"` 的报错文案改用模型 index（原来用数组位置）。

负控（只回退 `src/plaita_nodes/decision.py`，保留新测试）：

```text
$ pytest tests/test_decision.py -q -k "model_index or bad_index or custom_provider_batch"
FAILED test_llm_provider_batch_uses_model_index_not_array_position
FAILED test_llm_provider_batch_rejects_bad_index[0-index 重复]
FAILED test_llm_provider_batch_rejects_bad_index[7-index 越界]
FAILED test_llm_provider_batch_rejects_bad_index[1-index 非整数]
FAILED test_llm_provider_batch_rejects_bad_index[True-index 非整数]
FAILED test_llm_provider_batch_rejects_bad_index[None-缺少 index]
FAILED test_custom_provider_batch_index_mapping_and_bounds
7 failed, 25 deselected
```

乱序用例的精确红是 `[(0, 'ham'), (1, 'spam')] != [(0, 'spam'), (1, 'ham')]`——
即旧实现按数组位置贴标签。证据 `docs/proofs/data/44-nc-pn8-index.txt`。

## §2 pn#6 keyEnv 回退

`src/plaita_nodes/config.py::resolve_provider`：

- `api_key` 为空且声明了 `keyEnv` 时：
  - `environ is None` → `os.environ`（生产默认；与 `interpolate` 一致）；
  - 变量不存在 → `AgentConfigError("provider 'x' 声明的 keyEnv 'V' 未设置…")`；
  - 变量存在但为空 → `AgentConfigError("…的 keyEnv 'V' 为空…")`。
- `apiKey` 显式值仍优先于 `keyEnv`；未声明 keyEnv 的旧行为不变。

负控（只回退 `src/plaita_nodes/config.py`）：

```text
$ pytest tests/test_config.py -q -k "key_env"
FAILED test_resolve_provider_key_env_reads_host_environ
FAILED test_resolve_provider_key_env_missing_fails_fast
FAILED test_resolve_provider_key_env_empty_fails_fast
FAILED test_resolve_agent_key_env_translates_to_recursive_env
4 failed, 2 passed
```

`test_resolve_agent_key_env_translates_to_recursive_env` 的精确红是
`assert '' == 'sk-keyenv'`，即生产路径的 `RECURSIVE_API_KEY` 为空串。
两个仍绿的是「显式 environ 优先」「apiKey 优先」的守卫（旧实现本就满足）。
证据 `docs/proofs/data/44-nc-pn6-keyenv.txt`。

## §3 pn#12 email MIME 备件

`src/plaita_nodes/notify_backends.py`：

- 新增 `_HtmlToText(HTMLParser)`：块级标签（p/div/br/li/tr/td/h1-6/…）换行，
  `script`/`style`/`head`/`title` 内容跳过，实体解码，逐行折叠空白。
- 新增 `_build_email_message()`：`body_is_html=True` 时先写去标签的 plain
  备件，再 `add_alternative(text, subtype="html")`；纯标记无文本时给可读占位。
  非 HTML 正文仍只有 text/plain 件。
- `_backend_email_send` 改为调用该构造函数（SMTP 逻辑不变）。

负控（只回退 `src/plaita_nodes/notify_backends.py`）：

```text
$ pytest tests/test_connectors.py -q -k "email_html or email_plain_body or email_send_html"
FAILED test_email_html_plain_fallback_strips_tags
FAILED test_email_plain_body_has_no_html_part
FAILED test_email_html_only_markup_gets_readable_placeholder
FAILED test_email_send_html_body_mime_parts
4 failed, 27 deselected
```

节点级用例的精确红是 text/plain 件内容等于 `<p>完成 <b>3</b> 项</p>`（原始
HTML）。证据 `docs/proofs/data/44-nc-pn12-email.txt`。

## §4 pn#9 report run_token 写入点（跨两仓）

现状（修复前）：

- `ReportNode._token`：`token or get_global_variable("run_token", "default") or "default"`；
- `plaita` 的 `ExecutionContext.setup_flow` 只写 `flow_id`，全仓无 `run_token` 写入点；
- 后果：同 flow 两次 run 都写 `<base_dir>/default.jsonl`，第二次 read 读回前一 run
  的条目（实测 6 条）。

修法（plaita `a43f710`）：

```python
global_context.update({"flow_id": flow.flow_id})
if self.parent is None:
    global_context.setdefault("run_token", self.execution_id)
```

- 只有根上下文注入；`map`/`while`/child 子流程不复制该键，
  `get_global_variable` 经 parent 回落到同一个根 token；
- `flow.global_context` 显式给出的 `run_token` 优先；
- 旧 checkpoint 无该键时仍回退 `default`（resume 兼容）。

nodes 侧回归（`50421f9`，`tests/test_issue9_report_run_token.py`）：真实引擎跑
同一 flow 两次，map 子流程（3 个 item）各写一条，主流程 read 断言每次只看到
本 run 的 3 条；磁盘上是两个不同 token 文件，且不存在 `default.jsonl`。

负控 A（plaita 单测，只回退 `plaita/core/context.py`）：

```text
FAILED test_setup_flow_child_inherits_parent_run_token
FAILED test_setup_flow_root_injects_per_run_token
2 failed, 1 passed
```

精确红：根的 `run_token` 为 `None`（`None != '<uuid>'`）。显式值优先用例仍绿。
负控 B（nodes 端到端，用未修复的 plaita）：第二次 run 的 read 读到 6 条
（`[a,b,c,a,b,c]`），精确复现跨 run 污染。证据
`docs/proofs/data/44-nc-pn9-run-token.txt`。

## §5 pn#14 `_parse_redis_url`（已由 plaita#88 覆盖）

定位：`_parse_redis_url` 在 `plaita/server/factory.py`，`plaita-nodes` 全仓无此
符号。plaita#88 已关闭并合入 main（`fe1590e merge: 并入 fix/88`），当前
`origin/main` 的实现：

| 输入 | 结果 |
|---|---|
| `rediss://redis.internal:6380/0` | `RedisTarget(host='redis.internal', port=6380, db=0, scheme='rediss')`（`ssl=True` 路径） |
| `redis://user:pass@host:6379/0` | `username='user', password='pass', host='host'` |
| `redis://host` | `host='host', port=6379, db=0` |
| `redis://host:notaport/0` | `RedisUrlError: Redis URL 非法（Port could not be cast…）` |
| `http://host` | `RedisUrlError: 不支持的 Redis URL` |
| `redis://` | `RedisUrlError: 缺少主机名` |

定向运行 plaita 的 6 个对应用例（含 `test_issue_reproduction_targets`、
`test_omitted_port_and_db_default`、`test_username_and_password_userinfo`、
`test_rediss_keeps_endpoint_and_marks_ssl`、
`test_every_refused_shape_raises_named_error`、
`test_never_returns_localhost_for_malformed`）：

```text
6 passed, 95 deselected, 38 subtests passed
```

结论：本单是 plaita#88 的重复提报，无需在 `plaita-nodes` 再写一份解析器；
按「已覆盖」关闭，证据 `docs/proofs/data/44-pn14-redis-url.txt`。

## §6 pn#15 sync_in 自愈 / 沙箱基线漂移（特判）

核查结果：

| 项 | 事实 |
|---|---|
| main 是否投产 AGS 沙箱线 | **否**：`git ls-tree origin/main | grep -i sandbox` 为空；`git grep sync_in origin/main` 为空 |
| `origin/feat/ags-driver` | 未合并；已 rebase 到 main（merge-base `48d087e`，领先 32 提交）；含 rebase 后的 `sync_in` 系列（`d1b4d44`/`1b9cbd0` 等，即原 `30957fd`/`204c608` 的后继） |
| 分支的 sync_in 语义 | **fail-closed 校验**（宿主与沙箱已提交状态不等价即拒绝），不是「自愈」；`test_sync_in_fail_closed_on_host_drift` / `test_sync_in_ok_when_equivalent` **2 passed** |
| 条件 2（漂移→反复重放的有界性） | 已由 plaita main `170daa0` 覆盖：`TestSyncInDriftIsBounded` 的 4 个用例 **4 passed**，含 `test_sync_in_replay_is_bounded_then_exhausted` 与把上限钉死为 12 的绝对断言 |
| 条件 3（mergeable branch + reference） | 分支已 rebase 到 main、可合并；issue 评论已记录 |
| 条件 1（self-heal 或 bounded fail-closed） | 走「bounded fail-closed」分支：沙箱侧确定性失败 + 控制面 12 次终态化 |

结论：issue 的两条验收路径（自愈 / 有界 fail-closed）已由「分支 fail-closed +
plaita 控制面有界性」覆盖，原「self-heal」表述已被 fail-closed 设计取代；
本轮**不改已过时语义**。AGS 线本身仍在 `feat/ags-driver` 上未并入 main，属
另一条落地线，不在本单的 sync_in 语义范围内。证据
`docs/proofs/data/44-pn15-sync-in.txt`。

## §7 回归与门禁

```text
# nodes 定向
tests/test_config.py tests/test_decision.py tests/test_issue9_report_run_token.py
tests/test_flow_control.py                              50 passed, 2 skipped
tests/test_connectors.py -k email                        6 passed

# nodes 全量（PYTHONPATH 指向 plaita wt-b3 @ a43f710）
pytest -q                                                351 passed, 2 skipped

# plaita 相关
tests/unit/test_context.py + clean_c1 + issue69 mutation
+ coverage_sweep + issue73_deterministic_bounded          201 passed, 1418 subtests
```

plaita 全量 unit 结果与两仓 CI run 链接见台账第四十四轮行（推送后补）。
