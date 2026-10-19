# RED-PROOF — pn#17 git_publish / github_comment 子进程 env 白名单

日期：2026-10-19 · 执行者：修复落地席 B3（第三十六轮）· 仓库 `plaita-nodes` ·
基线 `origin/main 3756d99` · 单 commit（closes [#17](https://github.com/jeffkit/plaita-nodes/issues/17)）

## §0 结论速览

| 验收项 | 结果 |
|---|---|
| 根因 | `git_publish.py` 三处 `subprocess.run` 与 `github_comment.py` 两处均无 `env=`，宿主全量环境直通。工作树内可写 `.git/hooks/pre-commit` / clean filter 在 `git add -A` / commit 时执行（git 标准行为），提示注入即可打印 `PLAITA_CREDENTIALS_KEY` 外带 |
| 修法 | 新增 `_git_env.build_git_env()`：基础白名单 + 宿主非敏感 `GIT_*` + `SSH_AUTH_SOCK` + gh 配置定位（`GH_CONFIG_DIR`/`GH_HOST`/...）；凭据类名字、可携带/吐出凭据的 git 变量（`GIT_ASKPASS`/`GIT_CONFIG_*`/`GIT_TRACE*`）、可重定向仓库的 `GIT_DIR`/`GIT_WORK_TREE`/... 一律剔除。五处调用全部显式传 `env=` |
| 负控 | 撤掉 `env=` 后，恶意 pre-commit 与假 `gh` 均看到全部 5 个宿主凭据（含 `PLAITA_CREDENTIALS_KEY`）；新测试 **2 failed / 4 passed**（hook 与 gh 两条探针精确红） |
| 正控 | 恢复后同一探针：凭据键/值均不可见，PATH 与 `GIT_AUTHOR_NAME` 仍在；git commit / gh 评论照常成功；新测试 **6 passed** |
| 相关套件 | `test_repo_nodes` / `test_subprocess_env_isolation` / `test_agent_host_run_guard` / `test_gate_output_tail` / `test_capture` 共 **58 passed** |
| 兼容性 | token 档部署不再经 `GH_TOKEN`/`GITHUB_TOKEN` 认证，改用 `gh auth login` 落盘配置或 credential helper；普通 SSH/HTTPS credential helper 路径不变 |

## §1 缺陷与威胁模型

`sh()` / `_merge_to_main()` / `_create_pr()` / `_already_commented()` 的
`subprocess.run` 都没有 `env=`。worker 进程环境里的平台变量（
`PLAITA_CREDENTIALS_KEY`、DB/Redis 连接串、provider token、`GH_TOKEN`）会
原样进入 git / gh 子进程。

`git add -A` → `git commit` 会执行工作树里可写的 `.git/hooks/pre-commit`；
`.git/config` 里的 clean/smudge filter 同样会在 add/checkout 时执行。这属于
git 标准行为，因此「agent 能改工作树」⇒「钩子能看到宿主 env」⇒ 一条提示注入
即可把平台凭据打到 stdout（节点输出会进 checkpoint / 事件流）或直接外带。

## §2 修法

`src/plaita_nodes/_git_env.py`：

- 基础层复用 `plaita.subprocess_env.build_subprocess_env()`（PATH/HOME/TMPDIR/
  LANG/...）；
- 放行宿主非敏感 `GIT_*`（作者/提交者身份、`GIT_SSH*`、`GIT_CONFIG_GLOBAL`
  等）、`SSH_AUTH_SOCK`、`TZ`、`XDG_CONFIG_HOME`；
- gh 只放行配置定位变量（`GH_CONFIG_DIR`/`GH_HOST`/`GH_REPO`/
  `GH_PROMPT_DISABLED`/`GH_NO_UPDATE_NOTIFIER`/`GH_FORCE_TTY`）；
- 拒绝：名字命中 `TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|API_KEY`
  的变量（含 `GH_TOKEN`/`GITHUB_TOKEN`）、`GIT_ASKPASS`、
  `GIT_CONFIG_COUNT`/`GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_KEY_*`/
  `GIT_CONFIG_VALUE_*`（可注入 `http.extraHeader`）、`GIT_TRACE*`/
  `GIT_CURL_VERBOSE`，以及 `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`/
  `GIT_OBJECT_DIRECTORY`/`GIT_ALTERNATE_OBJECT_DIRECTORIES`/`GIT_COMMON_DIR`/
  `GIT_NAMESPACE`/`GIT_PREFIX`（节点已显式指定 cwd / 仓库）；
- 最终整份再过滤一次，部署方经 `SUBPROCESS_ENV_EXTRA` / `extra` 塞进来的
  凭据变量也不会流进钩子。

接入点：`git_publish` 的 3 处 `subprocess.run`（主 `sh`、`_merge_to_main`、
`_create_pr`）与 `github_comment` 的 2 处（comment、dedup view）全部传
`env=`。

## §3 负控（撤掉 env=）

```text
$ python docs/proofs/data/17-repro.py
{
  "git_publish": {
    "publish_ok": "none 模式：改动已 commit 到本地分支 main，未推送",
    "hook_ran": true,
    "secrets_visible": {
      "PLAITA_CREDENTIALS_KEY": true,
      "DATABASE_URL": true,
      "ANTHROPIC_AUTH_TOKEN": true,
      "GH_TOKEN": true,
      "GITHUB_TOKEN": true
    },
    "leaked": true,
    "path_visible": true,
    "git_author_visible": true
  },
  "github_comment": {
    "posted": true,
    "gh_ran": true,
    "secrets_visible": { "...": "全部 true" },
    "leaked": true,
    ...
  }
}
$ python -m pytest tests/test_issue17_subprocess_env_whitelist.py -q
2 failed, 4 passed
#   test_git_hook_does_not_see_host_credentials
#   test_gh_child_does_not_see_host_credentials
```

## §4 正控（恢复 env=）

```text
$ python docs/proofs/data/17-repro.py
{
  "git_publish": {
    "publish_ok": "none 模式：改动已 commit 到本地分支 main，未推送",
    "hook_ran": true,
    "secrets_visible": {
      "PLAITA_CREDENTIALS_KEY": false,
      "DATABASE_URL": false,
      "ANTHROPIC_AUTH_TOKEN": false,
      "GH_TOKEN": false,
      "GITHUB_TOKEN": false
    },
    "leaked": false,
    "path_visible": true,
    "git_author_visible": true
  },
  "github_comment": {
    "posted": true,
    "gh_ran": true,
    "secrets_visible": { "...": "全部 false" },
    "leaked": false,
    "path_visible": true
  }
}
$ python -m pytest tests/test_issue17_subprocess_env_whitelist.py -q
6 passed
```

`GIT_AUTHOR_NAME` 正控：钩子内可见且提交作者确实是 env 指定值，证明
「非敏感 `GIT_*` 最小集」没有被一刀切掉。

## §5 相关套件

```text
$ python -m pytest tests/test_repo_nodes.py tests/test_subprocess_env_isolation.py \
    tests/test_agent_host_run_guard.py tests/test_gate_output_tail.py tests/test_capture.py -q
58 passed
```

全量 pytest 结果见交付台账。
