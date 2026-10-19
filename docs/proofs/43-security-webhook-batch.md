# RED-PROOF — pn#11 / pn#16 / pn#13 security/webhook 批

日期：2026-10-19 · 执行者：修复落地席 B3（第四十三轮）· 仓库 `plaita-nodes` ·
基线 `origin/main 5b91eb1` · 三个单 commit：

| Issue | commit | 内容 |
|---|---|---|
| [#11](https://github.com/jeffkit/plaita-nodes/issues/11) | `e14b943` | `api_request` 重定向默认不跟随 + 跨源剥离凭据头 |
| [#16](https://github.com/jeffkit/plaita-nodes/issues/16) | `64d3dd6` | `git_publish`/`notify` 流程级 dry-run 零外发回归钉死 |
| [#13](https://github.com/jeffkit/plaita-nodes/issues/13) | `f58a7b4` | `github_comment` dedup marker 字面匹配 + 查询失败具名报错 |

## §0 结论速览

| 验收项 | 结果 |
|---|---|
| #11 根因 | `requests.request(...)` 默认 `allow_redirects=True`；requests 只自动剥 `Authorization`，凭据里的 `X-Api-Key` 等自定义头在 302 跨源跳转时原样转发 |
| #11 修法 | 新增 `allow_redirects: bool = False`；显式开启后手动跟随（最多 10 跳），同源保留凭据头，scheme/host/port 任一不同即清空；方法/请求体改写对齐 requests 语义 |
| #16 根因 | 当前实现正确，但测试只覆盖节点字段 `dry_run=True`，未钉流程级 `globalContext.dry_run`——回归成 `dry = self.dry_run` 时全绿 |
| #16 修法 | 仅补回归用例：节点 `dry_run=False` + `globalContext.dry_run=True` 时，git 子进程（含 `_git_env` 的 filter 枚举）零调用、远端无分支、工作区不变；notify backend 零调用、terminal 零输出、message 零求值 |
| #13 根因 | dedup 把 marker f-string 内插进 `--jq` 程序（含引号即语法错误），且 `rc != 0` 被当作「无重复」继续发布；gh 缺失抛裸 `FileNotFoundError` |
| #13 修法 | 改为 `gh issue view --json comments` 后在 Python 侧做字面子串匹配（marker 不进入程序文本）；非零 rc / 非法 JSON / 缺 `comments` 数组 / gh 缺失一律抛具名 `GithubCommentError`，评论零外发 |
| 负控 | #11 **2 failed / 1 passed**；#16 git **1 failed**（7 条 git 调用含 `add/commit/push`）、notify **2 failed / 1 passed**；#13 **3 failed / 1 passed** |
| 正控 | 定向 55/37/51 passed；全量 **333 passed / 2 skipped** |

## §1 pn#11 api_request 重定向

`src/plaita_nodes/api.py`：

- `ApiRequestNode` 新增 `allow_redirects: bool = False`，docstring 写明默认
  3xx 原样返回；
- 新增 `_perform_request()`：每跳以 `allow_redirects=False` 发送；显式开启时
  用 `urljoin(resp.url, Location)` 手动跟随，`_origin()` 归一化默认端口后比较
  scheme/host/port——跨源立即清空凭据头且后续 hop 不回填；同源继续透传；
- 方法/请求体改写与 requests 一致：303 全改 GET、302 非 HEAD 改 GET、301 仅
  POST 改 GET；301/302/303 丢弃请求体并移除 `Content-Length`/`Content-Type`/
  `Transfer-Encoding`；307/308 保留；
- 超过 10 跳抛具名 `RuntimeError`，不无限循环。

负控（`git checkout -- src/plaita_nodes/api.py`，最终测试集）：

```text
$ pytest tests/test_issue11_api_redirect.py -q
FF.                                                                      [100%]
FAILED test_redirect_not_followed_by_default            assert 200 == 302
FAILED test_cross_origin_redirect_strips_credential_headers
       AssertionError: 跨源重定向把 X-Api-Key 转发到了外部主机
2 failed, 1 passed
```

证据 `docs/proofs/data/43-nc-pn11-redirect.txt`。同源透传用例在旧实现下仍绿
（requests 本来就会跟），说明红的正是「默认不跟随」与「跨源剥离」两条新契约。

## §2 pn#16 流程级 dry-run

无生产改动（现有语义已正确）。新增 `tests/test_issue16_global_dry_run.py`：

1. `test_git_publish_global_dry_run_zero_git_calls`：真实 bare origin + worktree，
   节点 `dry_run=False`、`FakeExecution({"dry_run": True})`；spy
   `subprocess.run` 覆盖 `git_publish` 与 `_git_env` 的 filter 枚举，断言零调用、
   HEAD 不变、远端无 `p/issue-1`；
2. `test_notify_global_dry_run_zero_backend_calls`：注册探针 backend，断言零
   调用、返回 `dry_run=True`、message 零求值；
3. `test_notify_terminal_global_dry_run_is_silent`：terminal 零输出。

负控 A（只把 `dry = self.dry_run or ...` 改为 `dry = self.dry_run`）：

```text
E AssertionError: dry-run 期间发生 git 子进程调用: [
  ['git', ..., 'config', '--get-regexp', ...],
  ['git', ..., 'status', '--porcelain'],
  ['git', ..., 'add', '-A'],
  ['git', ..., 'commit', '-m', 'fix: issue #1'],
  ['git', ..., 'ls-remote', '--heads', 'origin', 'p/issue-1'],
  ['git', ..., 'rev-parse', 'HEAD'],
  ['git', ..., 'push', '-u', 'origin', 'p/issue-1']]
1 failed
```

负控 B（只判 `if self.dry_run:`）：

```text
E AssertionError: dry-run 期间通知 backend 被调用: ['SHOULD_NOT_SEND']
E AssertionError: dry-run 期间终端收到通知: '[notify] SHOULD_NOT_PRINT\n'
2 failed, 1 passed
```

证据 `docs/proofs/data/43-nc-pn16-git-dryrun.txt`、`43-nc-pn16-notify-dryrun.txt`。

## §3 pn#13 github_comment dedup

`src/plaita_nodes/github_comment.py`：

- 新增 `GithubCommentError(RuntimeError)`；
- `_already_commented()` 不再拼 jq：调用
  `gh issue view <n> -R <repo> --json comments`，`json.loads` 后对
  `comments[].body` 做 `marker in body` 字面匹配——引号/反斜杠不再改变语义；
- 非零 rc → `GithubCommentError("去重查询失败（gh rc=...）")`；输出非 JSON /
  缺 `comments` 数组同样具名报错；
- `FileNotFoundError` → `GithubCommentError("去重查询需要 gh CLI ...")`；发布
  路径的 gh 缺失也统一为该异常类（仍是 `RuntimeError` 子类，兼容旧调用方）；
- `tests/test_repo_nodes.py` 的 FakeGh 改为返回 `{"comments": [...]}`，与新
  查询契约一致。

负控（回退 `_already_commented` 旧实现；保留异常类定义以便测试收集）：

```text
$ pytest tests/test_issue13_github_comment_dedup.py -q
FFF.                                                                     [100%]
FAILED test_quoted_marker_is_still_deduplicated
       assert (True is False)         # 含引号 marker 被当成无重复，二次发布
FAILED test_dedup_query_failure_raises_named_error
       Failed: DID NOT RAISE GithubCommentError   # rc=1 被当无重复
FAILED test_missing_gh_raises_named_error
       FileNotFoundError: [Errno 2] No such file or directory: 'gh'
3 failed, 1 passed
```

证据 `docs/proofs/data/43-nc-pn13-dedup.txt`。

## §4 正控与回归

```text
$ pytest tests/test_issue11_api_redirect.py tests/test_connectors.py \
         tests/test_a2r7_retry_classification.py tests/test_a2r7_timeout_matrix.py -q
55 passed

$ pytest tests/test_issue16_global_dry_run.py tests/test_repo_nodes.py \
         tests/test_notify_backends.py -q
37 passed

$ pytest tests/test_issue13_github_comment_dedup.py tests/test_repo_nodes.py \
         tests/test_issue17_subprocess_env_whitelist.py \
         tests/test_issue21_git_hook_boundary.py -q
51 passed

$ pytest -q
333 passed, 2 skipped
```

## §5 边界

- #11 只处理 `api_request` 的重定向面；`generic_webhook` 无鉴权头字段，未动。
  跨源 307/308 的请求体仍按 HTTP 语义保留；本单只剥离凭据头。
- #16 不改 dry-run 语义，只补测试；节点字段 dry-run 既有用例保持。
- #13 不改 `posted` 输出形状与 dry-run 草稿语义；发布失败（`gh issue comment`
  非零 rc）仍按原契约返回 `posted=False`，只有去重查询失败改为抛错。
- 未总开关化 `gh` 去重路径；`dedup_marker` 未提供时行为不变。
