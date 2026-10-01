# plaita-nodes

plaita 的**通用节点集**（infra 级）：把 plaita 声明式流程接到真实世界——Agent CLI、
本地命令、微信人工确认、通知、文件，以及 REST API / SQL / 邮件 / IM webhook 等
外部系统，外加 gate / rate_limit / report 等流程控制原子。

> 背景：[ADR-2026-08-27 编排双轨收敛](../docs/ADR-2026-08-27-orchestration-converge-on-plaita.md)
> ——编排内核收敛到 plaita，Agent 执行层统一走 agentproc；本仓是这层决议的节点承载。

## 节点一览

| type | 节点 | 说明 |
|------|------|------|
| `agentrun` | Agent 运行 | **Agent 原子**：多步工具循环（模型可调工具自主多轮）。经 [agentproc](../agentproc) 调用 Agent CLI（recursive / claude）；配置复用 flowcast 的 `agents.json` / `providers.json` |
| `llm` | LLM 补全 | **LLM 原子**：单次 chat/completions 生成文本（OpenAI 兼容端点）。与 agentrun 的边界见下 |
| `decision` | 结构化决策 | **决策原子**：封闭决策空间 → 类型化选择 + 置信度。单条（`input`）或批量（`items`，provider 单次调用逐项判定，适合快照剪枝/批量预筛）。provider 可插拔：`llm` / `jev`（官方 Jev 与自托管 [OpenJev](https://github.com/razorback16/openjev) 同说的 `/v1/systemone` 线协议）/ `jevlike`（本地打分器，`model`=checkpoint 路径，懒加载 torch）/ 自定义注册；低于阈值可标记/走默认项/抛错升级 HITL |
| `capture` | 命令执行 | 跑本地命令捕获输出；失败不抛错（`exit_code` 返回，流程自行分支） |
| `gate` | 质量门 | 验证命令语义化为 `passed` 布尔（对标 flowcast runGate）；`max_retries` 失败自动**重跑命令**；超时 kill 进程组返回 `exit_code=124` |
| `rate_limit` | 频率限制 | 文件计数器按 key 日/周限次（`check`/`record`/`clear`）；`acquire` 原子判定+占坑（flock 互斥），check/record 两步间不会 crash/并发双发 |
| `report` | 结果通道 | run 级 jsonl 追加/读取（`<repo>/.flowcast/plaita-reports/<token>.jsonl`）：map 子流程与主流程聚合的旁路——绕开内核 map end 递归限制与 if 作用域隔离（workaround，内核侧追踪 [plaita#16](https://github.com/jeffkit/plaita/issues/16)） |
| `hitl` | 人工确认 | 直连 hitl-server（iLink 微信通道）：发消息 → 进程内轮询回复（**阻塞版**，Normal 模式） |
| `hitl_await` | 人工确认(挂起) | **挂起版**（Distributed 模式专用）：发消息即返回 `pending` 并快照挂起，外部 poller 轮询回复经 EventBus 唤醒——等微信回复期间进程可崩溃/重启（ADR phase 2） |
| `notify` | 通知 | backend 注册表分发（`channel`）：`terminal`（stdout）默认，或指向任何已注册通知 backend（`feishu_webhook` 等）；**新渠道走 `register_notify_backend`，不再新增节点** |
| `writefile` | 写文件 | UTF-8 写文件，支持 JSON 序列化 |
| `github_comment` | GitHub 评论 | 公开出害口收敛点：正文消毒（本机路径/密钥/未执行的 `$()` 命令替换打码）+ `dedup_marker` 去重（断点续跑不重发）+ `footer` 尾注 + artifact 留档；dry-run 写草稿不连网 |
| `git_publish` | Git 发布 | 幂等 commit/push：有改动一律先 commit，远端头==本地头才跳过（重投不丢改动）；`merge_mode=main` 时 ff 合并 `origin/<branch>`（失败 abort 如实回报）；提交消息 `commit_message` > `plan_file` 的 `COMMIT_MESSAGE:` 行 > `fix: issue #N` |
| `parse_json` | JSON 解析 | LLM 结构化输出解析：逐行倒序找严格 JSON → rfind 切片兜底（正文带花括号不误杀）；`choices` verdict 白名单、`default` fail-safe 兜底（`parse_ok`/`parse_error` 明细）、`join_fields` 列表拼接 |
| `api_request` | API 请求 | REST 连接器：凭据提供 `base_url` + 静态鉴权头，节点描述 method/path/query/body（path 支持表达式）——覆盖「静态 Header 鉴权」的开放 API，无需逐 SaaS 写节点 |
| `generic_webhook` | Webhook 调用 | 任意 JSON payload POST 到凭据指定 URL |
| `sql_query` | SQL 查询 | SQLAlchemy 任意库（凭据给 `url` 或 host/port/user/password/database 全集）；`:param` 绑定防注入；可选依赖 `pip install plaita-nodes[sql]` |
| `email_send` | 邮件发送 | SMTP（stdlib，无额外依赖）；凭据给 host/port/username/password/use_ssl/use_tls |
| `feishu_webhook` | 飞书通知 | IM 群机器人 webhook，凭据给 `{"url": ...}` |
| `wecom_webhook` | 企微通知 | IM 群机器人 webhook，凭据同上 |
| `slack_webhook` | Slack 通知 | IM 群机器人 webhook，凭据同上 |
| `dingtalk_webhook` | 钉钉通知 | IM 群机器人 webhook，凭据可带 `secret` 自动加签（timestamp+HMAC-SHA256） |

连接器族（`api_request` 起的 8 行）经 `credential` 字段按名引用，plaita.credentials
解密读取（编排台「凭据」页创建）。hitl/hitl_await 共享发送协议层
（`_hitl_client.py`）：发消息/图片降级细节一处维护。

节点经 pyproject 的 `[project.entry-points."plaita.nodes"]` 自动注册；`plaita_nodes.register_all()` 可手动注册。

## 原子节点设计原则

抽象一条硬标准——**原子性**（一个节点只做一件不可再分的事）、**通用性**
（不绑定业务语义与特定凭证）、**普适性**（覆盖一类外部交互）：

- **Agent ≠ LLM**：`agentrun` 是多步工具循环的 Agent 原子（重）；`llm` 是单次
  补全的 LLM 原子（轻）。流程里"摘要/改写/抽取/分类"用 `llm`，"多步编码/
  工具任务"用 `agentrun`。
- **决策 ≠ 生成**：`decision` 是单步、封闭决策空间的判断原子（路由/分类/
  打分），输出类型化决策 + 置信度，choice 必落在决策空间内；开放文本生成
  归 `llm`。理念对标 System One 决策模型（如 TypeSafe Jev）——流程里
  "该走哪个分支"用 `decision`，"写一段话"用 `llm`。
- 纯文本变换（判决提取、frontmatter 解析等）**不做节点**——注册为表达式
  `F.*` 函数（`ExpressionRegistry.register`），在 assignment 里一行使用。
- 业务领域的状态机（如内容池销账）属于业务仓，不放本仓。
- **通知类渠道接入走 backend，不加节点**：协议适配注册进 `notify_backends.NOTIFY_BACKENDS`
  （先例 `DECISION_PROVIDERS`），节点层只留薄壳；webhook×4 / email_send 已是薄壳委托
  （[plaita-nodes#1](https://github.com/jeffkit/plaita-nodes/issues/1)）。

## 快速上手

```bash
# monorepo 内可编辑安装（plaita / agentproc 均为兄弟仓）
pip install -e ../plaita[http] -e ../agentproc/sdk/python -e .
```

```python
from plaita import Flow

flow = Flow.from_string(open("flow.json").read())
result = flow.run(track="default", platforms=["twitter"], topic="", repo="/path/to/repo")
```

JSON 用法示例（agentrun + 模板表达式）：

```json
{
  "type": "agentrun", "id": "brief",
  "agent": "glm-52",
  "prompt": "{% $F.concat($INPUT.item.brief_prompt) %}",
  "next": "write"
}
```

## 业务仓接入要点

1. **安装即注册 ≠ 可用**：节点经 `[project.entry-points."plaita.nodes"]` 懒发现，须在构建 Flow（尤其 `flow_from_source`）**之前**显式触发——业务入口统一：

   ```python
   import plaita_nodes                    # 顺带注册 agentproc recursive-direct executor
   from plaita.node import get_default_registry
   get_default_registry()                 # ★ 触发 entry_points 懒发现，漏了节点全"未注册"
   ```

2. **console / flow_worker 侧**：console 拉起的 worker 不认识业务 venv 里的节点，须注入 `PLAITA_NODE_PATH` / `PLAITA_NODE_MODULES`（节点包）与 `PLAITA_PYTHON`（业务解释器）——否则「发布成功、调度必败」。机制见大仓 ADR-2026-08-27。
3. **轻逻辑**：纯变换优先 `F.*` 表达式或 plaita 内置 CODE 节点（`register_code_node(default_backend="subprocess")`，code 须自带 `def run(input) -> dict`）；不够用再写业务粘接节点（放业务仓，不放本仓）。
4. **节点 I/O 参考**：当前以各节点源码 docstring 为准（`src/plaita_nodes/<node>.py`）；`@flow` 源码里占位符 = `node_type` 大写（如 `AGENTRUN(...)`）。
5. **写 flow 的完整规范**（作者硬约束 / 项目结构 / 发布链路）：`../plaita/plaita-ai/plaita_ai/skills/flow-coder/references/authoring-spec.md`。

## agents.json / providers.json 兼容性

配置搜索顺序与 flowcast 一致：`~/.flowx → ~/.flowcast → <repo>/.flowcast`（深合并）。

与 flowcast 的两处行为差异（有意为之）：

1. agents.json 里的 `env` 字段 flowcast 白名单会**静默丢弃**，本仓按配置透传
   （如 glm-52 的 `RECURSIVE_MAX_TOKENS`）。
2. recursive 直路径 flowcast 默认无超时，本仓 `timeout_secs` 默认 1800。

## 设计边界

- **安全**：任何日志不打 apiKey / ANTHROPIC_AUTH_TOKEN。
- **dry-run**：所有有副作用的节点尊重 `globalContext.dry_run`——agentrun/capture/gate/hitl/hitl_await 与全部连接器（webhook×4 / generic_webhook / api_request / sql_query / email_send）dry 下**不解析凭据、不连网**，返回带 `dry_run` 标记的 fake 结果；writefile 照常写（草稿便于检查）。
- 新增执行器：在 `agentproc` executor 层扩展 + `config.EXECUTOR_ALIASES` 加映射，本仓节点无需改动。

## 断点续跑（Distributed 模式）

- `hitl`（**阻塞版**，Normal 模式）：execute 内轮询到底——进程挂了，等待即丢。
- `hitl_await`（**挂起版**，Distributed 模式）：发消息即返回 `pending` 并快照挂起——**等微信回复期间进程可崩溃/重启**（ADR-2026-08-27 phase 2）。
- `hitl_poller`（**恢复桥**，独立进程）：轮询 hitl-server 的待确认 session，回复到达即向 EventBus 发布 `hitl_reply` 事件唤醒挂起节点：

  ```bash
  python -m plaita_nodes.hitl_poller \
    --hitl-url http://127.0.0.1:8081 \
    --sessions sessions.json \
    --bus redis://127.0.0.1:6379/0 \
    [--interval 5]
  ```

  `sessions.json` 为 `{execution_id: {"session_id": ..., "node_id": ...}}` 映射，
  由调用方挂起后落盘；事件发布（`replied`/`timeout`）后对应条目自动移除。
  `--bus memory` 仅限调试——事件只发布在 poller 进程内，没有消费者；生产走 redis。

## 开发

```bash
pip install -e ".[dev]"
pytest
```

## 变更摘要

- **0.8.0**（2026-09-30）：通知出口收敛——新增 `notify_backends.NOTIFY_BACKENDS` 注册表（先例 `DECISION_PROVIDERS`），协议适配（payload 组装/钉钉加签/SMTP）收进 backend；`webhook`×4 / `email_send` 改薄壳委托（DSL 兼容、输出形状不变，既有连接器测试全绿）；`notify.channel` 可指向任何已注册 backend（新渠道只加 backend 不加节点，[plaita-nodes#1](https://github.com/jeffkit/plaita-nodes/issues/1)）；`notify` 补 dry-run。
- **0.7.0**（2026-09-30）：修复、契约收口与结构收敛。
  - *修复*：`gate.max_retries` 假重试（Popen 在循环外导致从未真正重跑，现每轮重新执行命令）；移除 `sql_query` 遗留调试输出（params 泄漏风险）；`hitl_poller` 缺 `os` import（默认 `--hitl-url` 路径 NameError）。
  - *dry-run 契约*：`webhooks`×4 / `generic_webhook` / `api_request` / `email_send` / `sql_query` 补齐——dry 下不解析凭据、不连网，返回带 `dry_run` 标记的 fake 结果。
  - *结构*：新增 `_hitl_client.py` 共享发送协议层，hitl / hitl_await 去重 ~30 行（HitlError 移至该层，`.hitl` 保持再导出）。
  - *功能*：`rate_limit` 新增 `acquire` 原子 check+record（flock 互斥），check/record 两步间不再 crash/并发双发。
  - *工程*：新增 Tests CI（3.10–3.12 矩阵，plaita/agentproc 兄弟仓 checkout 后 editable 安装）；`jevlike` extras（torch）显式声明；README「断点续跑」小节沉淀 `hitl_poller` 运维用法 + 冒烟测试。
  - *守卫*：`__all__` 漂移修复（`report_append`→`append_entry`）并补齐新节点导出；新增 pyproject entry-points ↔ `_ALL_NODES` ↔ `__all__` 一致性测试；补 rate_limit / report / gate 重试测试；README 节点表补全 22 节点。
  - *追踪*：`report` 节点的内核 workaround 状态立案 [plaita#16](https://github.com/jeffkit/plaita/issues/16)。
- **0.6.0**（2026-09-29）：新增 `github_comment` / `git_publish` / `parse_json` 三节点——出害口消毒+去重、幂等发布、LLM 输出健壮解析（issue-pipeline #43 解析策略沉淀）。修复 `__version__` 漂移（0.4.0 → 与 pyproject 同步）。
