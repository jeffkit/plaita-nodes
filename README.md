# plaita-nodes

plaita 的**通用节点集**（infra 级）：把 plaita 声明式流程接到真实世界——Agent CLI、本地命令、微信人工确认、通知、文件。

> 背景：[ADR-2026-08-27 编排双轨收敛](../docs/ADR-2026-08-27-orchestration-converge-on-plaita.md)
> ——编排内核收敛到 plaita，Agent 执行层统一走 agentproc；本仓是这层决议的节点承载。

## 节点一览

| type | 节点 | 说明 |
|------|------|------|
| `agentrun` | Agent 运行 | **Agent 原子**：多步工具循环（模型可调工具自主多轮）。经 [agentproc](../agentproc) 调用 Agent CLI（recursive / claude）；配置复用 flowcast 的 `agents.json` / `providers.json` |
| `llm` | LLM 补全 | **LLM 原子**：单次 chat/completions 生成文本（OpenAI 兼容端点）。与 agentrun 的边界见下 |
| `decision` | 结构化决策 | **决策原子**：封闭决策空间 → 类型化选择 + 置信度。单条（`input`）或批量（`items`，provider 单次调用逐项判定，适合快照剪枝/批量预筛）。provider 可插拔：`llm` / `jev`（官方 Jev 与自托管 [OpenJev](https://github.com/razorback16/openjev) 同说的 `/v1/systemone` 线协议）/ `jevlike`（本地打分器，`model`=checkpoint 路径，懒加载 torch）/ 自定义注册；低于阈值可标记/走默认项/抛错升级 HITL |
| `capture` | 命令执行 | 跑本地命令捕获输出；失败不抛错（`exit_code` 返回，流程自行分支） |
| `hitl` | 人工确认 | 直连 hitl-server（iLink 微信通道）：发消息 → 轮询回复 |
| `notify` | 通知 | terminal 后端（stdout） |
| `writefile` | 写文件 | UTF-8 写文件，支持 JSON 序列化 |
| `github_comment` | GitHub 评论 | 公开出害口收敛点：正文消毒（本机路径/密钥/未执行的 `$()` 命令替换打码）+ `dedup_marker` 去重（断点续跑不重发）+ `footer` 尾注 + artifact 留档；dry-run 写草稿不连网 |
| `git_publish` | Git 发布 | 幂等 commit/push：有改动一律先 commit，远端头==本地头才跳过（重投不丢改动）；`merge_mode=main` 时 ff 合并 `origin/<branch>`（失败 abort 如实回报）；提交消息 `commit_message` > `plan_file` 的 `COMMIT_MESSAGE:` 行 > `fix: issue #N` |
| `parse_json` | JSON 解析 | LLM 结构化输出解析：逐行倒序找严格 JSON → rfind 切片兜底（正文带花括号不误杀）；`choices` verdict 白名单、`default` fail-safe 兜底（`parse_ok`/`parse_error` 明细）、`join_fields` 列表拼接 |

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

## agents.json / providers.json 兼容性

配置搜索顺序与 flowcast 一致：`~/.flowx → ~/.flowcast → <repo>/.flowcast`（深合并）。

与 flowcast 的两处行为差异（有意为之）：

1. agents.json 里的 `env` 字段 flowcast 白名单会**静默丢弃**，本仓按配置透传
   （如 glm-52 的 `RECURSIVE_MAX_TOKENS`）。
2. recursive 直路径 flowcast 默认无超时，本仓 `timeout_secs` 默认 1800。

## 设计边界

- **安全**：任何日志不打 apiKey / ANTHROPIC_AUTH_TOKEN。
- **dry-run**：所有有副作用的节点尊重 `globalContext.dry_run`——agentrun/capture/hitl 返回 fake 结果，writefile 照常写（草稿便于检查）。
- **断点续跑**：hitl 为阻塞版（Normal 模式）；崩溃级恢复走 plaita Distributed + EventNode 模式（见 ADR phase 2）。
- 新增执行器：在 `agentproc` executor 层扩展 + `config.EXECUTOR_ALIASES` 加映射，本仓节点无需改动。

## 开发

```bash
pip install -e ".[dev]"
pytest
```

## 变更摘要

- **0.6.1**（2026-09-30）：修复与契约收口——`gate.max_retries` 假重试修真（Popen 移入循环，每轮重新执行命令）；移除 `sql_query` 遗留调试输出（params 泄漏风险）；`webhooks`×4 / `generic_webhook` / `api_request` / `email_send` / `sql_query` 补齐 dry-run 契约（先于凭据解析，不连网）；`__all__` 漂移修复并补齐新节点导出，新增 entry-points↔`_ALL_NODES`↔`__all__` 一致性守卫测试；补 rate_limit / report / gate 重试测试。
- **0.6.0**（2026-09-29）：新增 `github_comment` / `git_publish` / `parse_json` 三节点——出害口消毒+去重、幂等发布、LLM 输出健壮解析（issue-pipeline #43 解析策略沉淀）。修复 `__version__` 漂移（0.4.0 → 与 pyproject 同步）。
