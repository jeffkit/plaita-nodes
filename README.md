# plaita-nodes

plaita 的**通用节点集**（infra 级）：把 plaita 声明式流程接到真实世界——Agent CLI、本地命令、微信人工确认、通知、文件。

> 背景：[ADR-2026-08-27 编排双轨收敛](../docs/ADR-2026-08-27-orchestration-converge-on-plaita.md)
> ——编排内核收敛到 plaita，Agent 执行层统一走 agentproc；本仓是这层决议的节点承载。

## 节点一览

| type | 节点 | 说明 |
|------|------|------|
| `agentrun` | Agent 运行 | **Agent 原子**：多步工具循环（模型可调工具自主多轮）。经 [agentproc](../agentproc) 调用 Agent CLI（recursive / claude）；配置复用 flowcast 的 `agents.json` / `providers.json` |
| `llm` | LLM 补全 | **LLM 原子**：单次 chat/completions 生成文本（OpenAI 兼容端点）。与 agentrun 的边界见下 |
| `capture` | 命令执行 | 跑本地命令捕获输出；失败不抛错（`exit_code` 返回，流程自行分支） |
| `hitl` | 人工确认 | 直连 hitl-server（iLink 微信通道）：发消息 → 轮询回复 |
| `notify` | 通知 | terminal 后端（stdout） |
| `writefile` | 写文件 | UTF-8 写文件，支持 JSON 序列化 |

节点经 pyproject 的 `[project.entry-points."plaita.nodes"]` 自动注册；`plaita_nodes.register_all()` 可手动注册。

## 原子节点设计原则

抽象一条硬标准——**原子性**（一个节点只做一件不可再分的事）、**通用性**
（不绑定业务语义与特定凭证）、**普适性**（覆盖一类外部交互）：

- **Agent ≠ LLM**：`agentrun` 是多步工具循环的 Agent 原子（重）；`llm` 是单次
  补全的 LLM 原子（轻）。流程里"摘要/改写/抽取/分类"用 `llm`，"多步编码/
  工具任务"用 `agentrun`。
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
