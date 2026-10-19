# pn#20：`recursive_stream_turn` 流式路径租户闸

## 缺口

`AgentRunNode` 的宿主直跑路径（`repo`/默认 cwd）在 Popen 前对非 default 租户
具名拒绝；流式辅助路径 `recursive_stream_turn` 没有同一道闸，直接
`subprocess.Popen`。console 的 `/api/flows/ai-generate/stream` 正是这条路径，
且 argv 固定 `--permission-mode auto`——同一宿主语义两套闸，漏掉的是更危险的
流式面。

## 修法

`recursive_stream_turn` 生成器入口、任何副作用（preflight / 凭据解析 /
Popen）之前复用 `_tenant_guard.refuse_non_default_tenant`：

- 非 default 租户 → `TenantContextPropagationError`，消息含
  `PLAITA_SUBPROCESS_TENANT_ISOLATION_UNAVAILABLE` 与租户 id；
- default 租户 / 无租户层（单租户部署）→ 语义不变。

该闸独立于非流式路径的 `PLAITA_ALLOW_HOST_AGENT_RUN=1` 部署放行：多租户下
流式宿主直跑始终 fail-closed（单租户部署上下文恒为 default，无行为变化）。

## 负控

仅删除生产代码中的 `refuse_non_default_tenant("recursive_stream_turn")` 一行，
保留测试：

```text
$ .venv/bin/python -m pytest tests/test_issue20_stream_tenant_guard.py -q
2 failed, 1 passed
FAILED test_non_default_tenant_refused_before_popen
FAILED test_guard_applies_even_if_host_run_opt_out_set
# 精确红：执行到 src/plaita_nodes/agent_run.py:748 的 subprocess.Popen
# （AssertionError: Popen 不得被调用）
```

证据：`docs/proofs/data/20-nc-pn-stream-tenant-guard.txt`。

## 正控与回归

```text
$ .venv/bin/python -m pytest tests/test_issue20_stream_tenant_guard.py \
    tests/test_agent_run_stream.py tests/test_agent_host_run_guard.py -q
16 passed

$ .venv/bin/python -m pytest -q
307 passed, 2 skipped
```

## 跨仓

console 侧平台身份闸 + 工作区隔离见 plaita 仓 wt-b3 的
`docs/proofs/20-ai-generate-stream-platform-gate.md`。
