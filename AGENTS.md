# AGENTS.md — plaita-nodes

> plaita 通用节点集（22 节点）：Agent/LLM/决策原子（agentrun/llm/decision）·
> 流程控制（gate/rate_limit/report/hitl/hitl_await）·出害口（github_comment/git_publish/
> notify/writefile/parse_json）·连接器（api_request/generic_webhook/sql_query/email_send + IM webhook×4）。
> 大仓 ADR-2026-08-27（编排收敛 plaita + agentproc）的节点承载层。

## 项目概述

为 plaita 流程提供"接到真实世界"的通用节点：Agent CLI 调用（复用 flowcast 的
agents/providers 配置，执行走 agentproc Python SDK）、本地命令、微信 HITL、通知、
写文件，以及凭据化外部系统连接器（REST / SQL / 邮件 / IM webhook）与流程控制原子
（质量门 / 限频 / run 级结果通道）。业务粘接节点（读 persona/pool 等）**不放本仓**，
放业务仓（如 mediaflow/plaita_flows）。

**技术栈：** Python 3.10+ · plaita（兄弟仓 editable）· agentproc（兄弟仓 editable）· requests
**主仓库：** `git@github.com:jeffkit/plaita-nodes.git`

## 架构地图

依赖方向：`config.py`（flowcast 兼容配置层）← `agent_run.py`（agentproc executor 注册 + 节点）← 其余节点独立。
对 plaita 只依赖 `plaita.Node` 基类与 `NodeExecutionContext` 窄接口；对 agentproc 只依赖 `runner.run` + `EXECUTORS` 注册表。

关键路径：
- `src/plaita_nodes/config.py` — agents/providers 加载（flowcast 搜索顺序 + 深合并 + `${VAR}` 插值 + provider→env 翻译）
- `src/plaita_nodes/llm.py` — LlmNode + `resolve_llm_endpoint`（端点三级回退：字段 > provider bundle > LLM_* env，供 decision 复用）
- `src/plaita_nodes/decision.py` — DecisionNode + `DECISION_PROVIDERS` 注册表（`llm` / `jev` 过渡契约 / 自定义注册）
- `src/plaita_nodes/agent_run.py` — AgentRunNode + `recursive-direct` executor（语义 = flowcast runRecursiveDirect）
- `src/plaita_nodes/_hitl_client.py` — hitl/hitl_await 共享发送协议层（send/图片降级/解析，协议细节只改这里）
- `src/plaita_nodes/hitl_poller.py` — Distributed 模式恢复桥（`python -m plaita_nodes.hitl_poller`，轮询 session → EventBus 发布 hitl_reply）
- `src/plaita_nodes/notify_backends.py` — 通知 backend 注册表（NOTIFY_BACKENDS，先例 DECISION_PROVIDERS；新通知渠道只加 backend 不加节点，webhook×4/email_send 为薄壳委托）
- `src/plaita_nodes/{api,database,email,webhooks}.py` — 凭据化连接器（`credential` 字段 → plaita.credentials 解密读取）
- `pyproject.toml` — `[project.entry-points."plaita.nodes"]` 注册表

## 开发约定

**分支：** main（dev/test/prod 同支）。
**禁止事项：**
- 业务逻辑入仓（业务节点放业务仓）
- 日志打印 apiKey / ANTHROPIC_AUTH_TOKEN
- 依赖 plaita/agentproc 的内部私有 API（只走公开窄接口）

**dry-run 契约：** 有副作用的节点一律尊重 `globalContext.dry_run`（连接器 dry 下
不解析凭据、不连网）；writefile 例外（照常写，便于检查草稿）。

**注册表三处同步有守卫：** pyproject entry-points ↔ `_ALL_NODES` ↔ `__all__` 由
`tests/test_registry.py` 一致性测试看护，新增节点只需改 pyproject + `_ALL_NODES`，
漏改会被测试逮住。

## 常用命令

```bash
pip install -e ".[dev]"
pytest
```

## 深入阅读

| 文档 | 说明 |
|------|------|
| `README.md` | 节点表 + 配置兼容性说明 |
| `../docs/ADR-2026-08-27-orchestration-converge-on-plaita.md` | 决议背景 |
