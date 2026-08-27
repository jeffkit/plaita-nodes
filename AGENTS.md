# AGENTS.md — plaita-nodes

> plaita 通用节点集：AgentRun（经 agentproc）/ Capture / Hitl / Notify / WriteFile。
> 大仓 ADR-2026-08-27（编排收敛 plaita + agentproc）的节点承载层。

## 项目概述

为 plaita 流程提供"接到真实世界"的通用节点：Agent CLI 调用（复用 flowcast 的
agents/providers 配置，执行走 agentproc Python SDK）、本地命令、微信 HITL、通知、写文件。
业务粘接节点（读 persona/pool 等）**不放本仓**，放业务仓（如 mediaflow/plaita_flows）。

**技术栈：** Python 3.10+ · plaita（兄弟仓 editable）· agentproc（兄弟仓 editable）· requests
**主仓库：** `git@github.com:jeffkit/plaita-nodes.git`

## 架构地图

依赖方向：`config.py`（flowcast 兼容配置层）← `agent_run.py`（agentproc executor 注册 + 节点）← 其余节点独立。
对 plaita 只依赖 `plaita.Node` 基类与 `NodeExecutionContext` 窄接口；对 agentproc 只依赖 `runner.run` + `EXECUTORS` 注册表。

关键路径：
- `src/plaita_nodes/config.py` — agents/providers 加载（flowcast 搜索顺序 + 深合并 + `${VAR}` 插值 + provider→env 翻译）
- `src/plaita_nodes/agent_run.py` — AgentRunNode + `recursive-direct` executor（语义 = flowcast runRecursiveDirect）
- `pyproject.toml` — `[project.entry-points."plaita.nodes"]` 注册表

## 开发约定

**分支：** main（dev/test/prod 同支）。
**禁止事项：**
- 业务逻辑入仓（业务节点放业务仓）
- 日志打印 apiKey / ANTHROPIC_AUTH_TOKEN
- 依赖 plaita/agentproc 的内部私有 API（只走公开窄接口）

**dry-run 契约：** 有副作用的节点一律尊重 `globalContext.dry_run`；writefile 例外（照常写，便于检查草稿）。

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
