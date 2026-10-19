# RED-PROOF — pn#21 git_publish 恶意 worktree 边界前移

日期：2026-10-19 · 执行者：修复落地席 B3（第四十一轮）· 仓库 `plaita-nodes` ·
基线 `origin/main 6f45d6d` · 单 commit（closes [#21](https://github.com/jeffkit/plaita-nodes/issues/21)）

## §0 结论速览

| 验收项 | 结果 |
|---|---|
| 根因 | pn#17 的 env 白名单只按变量名过滤，**不是**恶意 worktree hook 的凭据边界：hook 可读同 UID 的 `/proc/<pid>/environ`、用 `SSH_AUTH_SOCK` 跑 `ssh-add -l`、在 `GIT_SSH_COMMAND`/`*_PROXY` 值里夹带 token 绕过键名过滤 |
| 修法 ①（主） | 边界前移：`git_publish` 每条 git 命令统一带 `-c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.ext.allow=never`，并枚举所有 scope 的 `filter.<driver>.{clean,smudge,process}` 覆盖为空——不可信工作树的 pre-commit/pre-push/clean filter/fsmonitor 零执行 |
| 修法 ② | `SSH_AUTH_SOCK` 加入拒绝集，不再进入任何 git/gh 子进程；`extra` 注入也会被最终过滤 |
| 修法 ③（次级） | `build_git_env` 对 `GIT_SSH_COMMAND`/`GIT_PROXY_COMMAND`/`*_PROXY` 的值做凭据扫描（URL 口令、已知 token、Bearer/Basic、`token=` 赋值、代理认证参数、代理 userinfo），命中抛 `GitEnvCredentialError`，报错不回显值 |
| 负控 | 撤掉生产改动后 **12 failed / 6 passed**：pre-commit/pre-push/clean filter（含带点 driver 名）/fsmonitor 探针文件全部出现；hook env 里可见 `SSH_AUTH_SOCK` 与 `GIT_SSH_COMMAND`；真实 ssh-agent 探针列出 `SHA256:...` 身份；`SSH_AUTH_SOCK` 仍被放行、值级类不存在（证据 `docs/proofs/data/41-nc-pn21-hook-boundary.txt`） |
| 正控 | 恢复后定向 **18 passed**；`test_repo_nodes` 合并 **47 passed**；全量 **319 passed / 2 skipped**；commit/push/comment/main 合并全部照常 |
| 兼容性 | 正常工作树 hook 不再执行（本单语义）；`required=true` 的 filter（如 LFS）fail-closed 而不是静默提交未过滤内容；ssh push 改显式 key（`GIT_SSH_COMMAND` 带 `-i`）或 credential helper；带 userinfo 的代理变量具名拒绝 |

## §1 威胁模型（issue 原文三旁路）

1. **同 UID `/proc`**：git 从 worker fork 出来，hook 的祖先进程就是 worker；
   `/proc/<worker_pid>/environ` 对同 UID 可读，键名白名单完全绕开。
2. **SSH agent 身份**：`SSH_AUTH_SOCK` 被有意放行且所有本地 git 调用共用同一份
   env；恶意 `pre-commit` 实测 `ssh-add -l` 能列出用户身份，可对任意可达主机认证。
3. **值盲过滤**：`GIT_SSH_COMMAND` / `SUBPROCESS_ENV_EXTRA` 里的
   `HTTPS_PROXY=http://user:pw@...` 或 `Authorization: Bearer ...` 只改值不改名，
   `_SECRET_NAME_RE` 不命中。

结论：env 白名单只是卫生措施；**只要攻击者代码能在发布过程中执行**，同 UID
下就没有凭据边界。根治必须让不可信工作树的 hook/filter 零执行。

## §2 修法

`src/plaita_nodes/_git_env.py`：

- 新增 `git_hardened_argv(repo, env, timeout)`：返回
  `git -c core.hooksPath=/dev/null -c core.fsmonitor=false
  -c protocol.ext.allow=never [-c filter.<driver>.<op>= ...]`；
  driver 列表经 `git config --get-regexp --null --name-only` 枚举**所有 scope**，
  按 NUL 解析（driver 名可含点/空格，不能按空白截断）；
- `_DENY_EXACT` 增加 `SSH_AUTH_SOCK`，`_EXTRA_ALLOW` 移除它；`extra` 注入同样
  被最终过滤；
- 新增 `GitEnvCredentialError` 与值级扫描 `_reject_embedded_credentials()`：
  只扫 `GIT_SSH_COMMAND` / `GIT_PROXY_COMMAND` / `PROXY` / `*_PROXY` 的值，
  命中即拒绝（fail-closed），错误信息只含变量名与命中类别。

`src/plaita_nodes/git_publish.py`：

- 主 `sh()`、`_merge_to_main()` 的 fetch/merge/push 全部改为
  `[*git_hardened_argv(...), subcommand, ...]`；`_create_pr` 的 `gh` 仍走同一份
  白名单 env（gh 不需要 `SSH_AUTH_SOCK`）。

## §3 负控（stash 生产改动，最终测试集）

```text
$ git stash push -- src/plaita_nodes/_git_env.py src/plaita_nodes/git_publish.py
$ pytest tests/test_issue21_git_hook_boundary.py \
         tests/test_issue17_subprocess_env_whitelist.py -q --tb=short
12 failed, 6 passed
```

关键探针输出：

```text
hook=pre-commit
SSH_AUTH_SOCK=/tmp/pn21-fake-agent.sock
GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=no
...
hook=agent
SSH_AUTH_SOCK=/tmp/pn21-agent-8hu2u8z4/agent.sock
256 SHA256:+U3WVQR9hgFBZh94jkjeAKh7vrQmJTUqRNY4r7+mtiU kong@MacBookPro (ED25519)
```

精确红：pre-commit、pre-push、clean filter、带点 driver 名 filter、fsmonitor、
#17 hook 边界、ssh-agent 身份共 7 条零执行用例；值级 3 条
（`GitEnvCredentialError` 不存在 / `SSH_AUTH_SOCK` 仍放行）；`_git_env` 2 条；
#17 既有契约 1 条。完整输出见 `docs/proofs/data/41-nc-pn21-hook-boundary.txt`。

## §4 正控与回归

```text
$ pytest tests/test_issue21_git_hook_boundary.py \
         tests/test_issue17_subprocess_env_whitelist.py tests/test_repo_nodes.py -q
47 passed
$ pytest -q
319 passed, 2 skipped
```

正控覆盖：none 模式本地 commit、branch 模式真实 push 到本地 bare origin、
main 模式 ff 合并、pr 模式、正常 `gh issue comment`（artifact + 发布）、
`GIT_AUTHOR_NAME` 经 env 仍生效。

## §5 兼容性与残留

- **正常工作树 hook 不再执行**：这是本单的边界语义；依赖 `commit-msg` 等 hook
  做校验的流程需把校验移到节点/CI。
- **required filter fail-closed**：LFS 等 `filter.<driver>.required=true` 的仓库
  在 `git add` 会失败（`clean filter 'x' failed`），不会把未过滤内容静默入库；
  需要 LFS 的发布应在可信侧完成或移除该依赖。
- **ssh push**：`SSH_AUTH_SOCK` 不再传递，需显式 key
  （`GIT_SSH_COMMAND="ssh -i ..."`）或 credential helper；`GIT_SSH_COMMAND` 值
  内嵌凭据会被具名拒绝。
- **代理**：无 userinfo 的代理值可继续经 `SUBPROCESS_ENV_EXTRA` 传入；带
  userinfo/token 的值抛 `GitEnvCredentialError`。
- **残留信任面**：`SUBPROCESS_ENV_EXTRA` 本身是部署方写入口（agent 不可写），
  本轮未把它改成固定键 allowlist；loader 级变量（`LD_PRELOAD` 等）的收口属于
  平台层（plaita#30）范围。并发进程在「枚举 filter → 执行 git」窗口内改写
  `.git/config` 的竞态不在本单模型内；彻底隔离仍需独立用户/容器（与 #18 结论
  一致）。
