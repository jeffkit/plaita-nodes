# RED-PROOF — pn#18 capture/gate 子进程非 default 租户 fail-closed

日期：2026-10-19 · 执行者：修复落地席 B3（第三十六轮）· 仓库 `plaita-nodes` ·
基线 `origin/main 3756d99` · 单 commit（closes [#18](https://github.com/jeffkit/plaita-nodes/issues/18)）

## §0 结论速览

| 验收项 | 结果 |
|---|---|
| 根因 | capture/gate 在 worker 宿主以同一 OS 用户执行命令；子进程无租户上下文时 `plaita.credentials` 回退 default，按 cwd 解出 default 凭据。即使显式传租户，命令内 `set_current_tenant("default")` 或直接读文件仍绕得开（同用户 + 宿主 FS + 租户共用一把 Fernet 密钥） |
| 修法 | 与 plaita 主仓 A42-06（`f03a37e`）同构：非 default 租户在 `Popen` 之前抛 `TenantContextPropagationError`（marker `PLAITA_SUBPROCESS_TENANT_ISOLATION_UNAVAILABLE` + docker 指引），命令零执行；default 语义不变 |
| 负控 | 临时撤掉两处闸门 → 探针 capture/gate 均返回 `DEFAULT-ONLY-SECRET`、marker 显示命令已执行；新测试 **4 failed / 3 passed**（两条拒绝 + 两条 Popen 前置断言精确红） |
| 正控 | 恢复闸门后同探针返回具名拒绝、`command_ran=false`；新测试 **7 passed**；default 租户命令照跑并读到 default 凭据 |
| 相关套件 | `test_repo_nodes` / `test_subprocess_env_isolation` / `test_agent_host_run_guard` / `test_gate_output_tail` / `test_capture` 共 **58 passed** |
| 互引 | 本仓 commit 引用 plaita 主仓 `f03a37e`（A42-06）；同名契约 marker |

## §1 缺陷与威胁模型

`CaptureNode` / `GateNode` 的 `Popen` 虽然已按 `plaita.subprocess_env`
白名单重建 env（宿主 `PLAITA_CREDENTIALS_KEY` 不进子进程），但**子进程本身
不是租户凭据隔离边界**：

- 子进程与 worker 同 OS 用户，文件系统不受限；
- 所有租户的凭据文件共用同一把 Fernet 密钥（`PLAITA_CREDENTIALS_KEY` 或
  `.plaita-credentials.key`）；
- 无租户上下文时 `credentials_file()` 回退 default 文件名——命令只要以
  worker cwd 执行，就能读到 default 文件并解密；
- 把租户显式传进子进程也不够：命令内 `set_current_tenant("default")`
  改写路由，或按绝对/相对路径裸读 default 文件，仍拿到 default 凭据。

这与 plaita 主仓 A42-06（#256 续）的结论完全一致：同一 OS 用户 + 同一文件
系统 + 共享密钥下，子进程内隔离不可行。

## §2 修法（与 A42-06 同构）

新增 `src/plaita_nodes/_tenant_guard.py`：

- `non_default_tenant()`：读 `plaita.tenant_context.current_tenant()`，
  default / 无租户层（plaita 0.5.x）返回 `None`；
- `refuse_non_default_tenant(node_type)`：非 default 在 spawn 前抛
  `TenantContextPropagationError`，消息含
  `PLAITA_SUBPROCESS_TENANT_ISOLATION_UNAVAILABLE`、tenant id、docker /
  独立沙箱指引；拒绝点先打 WARNING（不含路径），满足 fail-closed 可观测。

`capture.py` / `gate.py` 在 dry-run 早返回之后、构建 env / `Popen` 之前调用
闸门。dry-run 不 spawn、不读凭据，语义不变（与 agent 直跑 dry-run 一致）。

**不做 opt-out**：与 A42-06 保持一致，避免把同一泄露面重新打开。租户级命令
执行必须放进不挂载宿主凭据的容器 / 独立沙箱；default 租户（单租户部署）
行为零变化。

## §3 负控（撤闸）

```text
$ python docs/proofs/data/18-repro.py     # 临时把两处 refuse 调用改为 pass
{
  "capture": {
    "stdout": "{\"token\": \"DEFAULT-ONLY-SECRET\"}",
    "command_ran": true
  },
  "gate": {
    "stdout": "{\"token\": \"DEFAULT-ONLY-SECRET\"}",
    "command_ran": true
  }
}
$ python -m pytest tests/test_issue18_subprocess_tenant_isolation.py -q
4 failed, 3 passed
# 失败集合：
#   test_tenant_a_node_refused_before_command[capture]  DID NOT RAISE
#   test_tenant_a_node_refused_before_command[gate]     DID NOT RAISE
#   test_tenant_a_never_calls_popen[capture]            Popen must not be called
#   test_tenant_a_never_calls_popen[gate]               Popen must not be called
```

## §4 正控（恢复闸门）

```text
$ python docs/proofs/data/18-repro.py
capture subprocess refused non-default tenant=tenant-a (...); command not executed
gate subprocess refused non-default tenant=tenant-a (...); command not executed
{
  "capture": {"error_type": "TenantContextPropagationError",
              "error": "PLAITA_SUBPROCESS_TENANT_ISOLATION_UNAVAILABLE: capture ... docker ...",
              "command_ran": false},
  "gate":    {"error_type": "TenantContextPropagationError",
              "error": "PLAITA_SUBPROCESS_TENANT_ISOLATION_UNAVAILABLE: gate ... docker ...",
              "command_ran": false}
}
$ python -m pytest tests/test_issue18_subprocess_tenant_isolation.py -q
7 passed
```

default 租户正控（同文件 `TestDefaultTenantUnchanged`）：探针照常执行，
stdout 为 `{"token": "DEFAULT-ONLY-SECRET"}`，capture `ok=True` /
gate `passed=True`——闸门只关非 default 路径。

## §5 相关套件

```text
$ python -m pytest tests/test_repo_nodes.py tests/test_subprocess_env_isolation.py \
    tests/test_agent_host_run_guard.py tests/test_gate_output_tail.py tests/test_capture.py -q
58 passed
```

全量 pytest 结果见交付台账。
