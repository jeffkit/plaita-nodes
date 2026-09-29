"""GitPublishNode —— 幂等的 commit/push（可选 ff 合并 main）。

替代 issue-pipeline 的 deliver+merge 两个手写 code 节点，并把 README 已知缺口
#6 的语义修对：**deliver 曾见分支已在远端就直接返回 pushed=True，跳过
add/commit/push——重复投递时工作区里新产生的改动被静默丢弃**。本节点的
幂等语义：

1. 工作区有未提交改动（``status --porcelain`` 非空）→ **一律先 commit**
   （消息优先级：``commit_message`` > ``plan_file`` 中的 ``COMMIT_MESSAGE:`` 行
   > ``fix: issue #<issue_number>`` 兜底）；
2. commit 后远端分支头 == 本地 HEAD → 跳过 push（真重复投递，改动无损）；
3. 远端无分支 → ``push -u origin <branch>``；有分支且本地领先 → 普通 push。

main 模式（``merge_mode="main"``）：fetch 后 ``merge --ff-only origin/<branch>``
（**合并的是分支，不是 origin/main**——合并错对象曾让 main 不含修复却谎报
成功），失败 ``merge --abort`` 并如实返回 merged=False。

none 模式（``merge_mode="none"``，issue-pipeline v0.3）：只做幂等 commit，
**不 push 不合并**——改动保留在本地分支（只读/不出害仓位用）。

pr 模式（``merge_mode="pr"``）：推分支后 ``gh pr create --base <base_branch>``
（argusai 家族等 PR-合并制仓用）；PR 已存在时如实记 note，不算失败。
``base_branch`` 参数（默认 main）同时决定 main 模式推送的目标分支
（不再硬编码 main——argusai 家族的集成分支是 develop）。

副作用说明：dry-run 下不做任何 git 操作，返回假成功 + ``dry_run`` 标记。
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, ClassVar, Optional

from plaita import Node


class GitPublishNode(Node):
    """commit + push（幂等），可选 ff 合并 main。

    JSON 字段：
    - ``worktree_dir``: 仓库/工作树路径（支持表达式）
    - ``branch_name``: 发布分支名（支持表达式）
    - ``commit_message``: 显式提交消息（优先级最高）
    - ``plan_file``: 计划文件路径——从中提取 ``^COMMIT_MESSAGE: (.+)$``
    - ``issue_number``: 提供时兜底消息为 ``fix: issue #N``
    - ``merge_mode``: ``branch``（默认，仅推分支）| ``main``（ff 合并并推 base 分支）
      | ``none``（只本地 commit，不 push）| ``pr``（推分支并开 PR）
    - ``main_clone``: main 模式必填——main 所在 clone 路径
    - ``base_branch``: 集成/目标分支名（默认 ``main``）——main 模式推送目标与
      pr 模式的 ``--base``；经 DSL 传入是表达式串，节点内先求值
    - ``dry_run``: 为 true（或流程 globalContext.dry_run）时不做任何 git 操作

    输出：``{"pushed": bool, "merged": bool|None, "note": str, "push_note": str}``
    （note 对齐旧 merge 节点语义，branch 模式 = "branch 模式：仅推分支，不合并 main"）。
    """

    node_type: ClassVar[str] = "git_publish"
    node_name: ClassVar[str] = "Git 发布"

    worktree_dir: Any = None
    branch_name: Any = None
    commit_message: Optional[Any] = None
    plan_file: Optional[Any] = None
    issue_number: Optional[Any] = None
    merge_mode: str = "branch"
    main_clone: Optional[Any] = None
    base_branch: Optional[Any] = None
    dry_run: bool = False
    timeout_secs: int = 300

    def execute(self, execution: Any) -> dict:
        if self.worktree_dir is None or self.branch_name is None:
            raise ValueError("git_publish 节点缺少 worktree_dir/branch_name 字段")
        wt = str(execution.evaluate(self.worktree_dir))
        branch = str(execution.evaluate(self.branch_name))
        # merge_mode 经 DSL 传入时是表达式串（如 "$INPUT.push_mode"），必须求值
        merge_mode = str(execution.evaluate(self.merge_mode) or "branch")
        if merge_mode not in ("branch", "main", "none", "pr"):
            raise ValueError(f"git_publish merge_mode 非法: {merge_mode!r}")
        base_branch = str(execution.evaluate(self.base_branch) or "main") if self.base_branch else "main"

        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            return {"pushed": True, "merged": None, "push_note": "", "note": "dry-run：跳过 git 操作",
                    "dry_run": True}
        if merge_mode == "main" and self.main_clone is None:
            raise ValueError("git_publish merge_mode=main 需要 main_clone 字段")

        def sh(args, cwd=None, t=None):
            return subprocess.run(args, cwd=cwd or wt, capture_output=True, text=True,
                                  timeout=t or self.timeout_secs)

        dirty = sh(["git", "status", "--porcelain"]).stdout.strip() != ""
        committed_now = False
        msg = self._resolve_message(execution) if (dirty or merge_mode == "pr") else None
        if dirty:
            sh(["git", "add", "-A"])
            rc = sh(["git", "commit", "-m", msg])
            if rc.returncode != 0:
                return {"pushed": False, "merged": None, "push_note": "",
                        "note": f"commit 失败: {(rc.stderr or '')[-300:]}",
                        "push_note": (rc.stderr or "")[-300:]}
            committed_now = True

        if merge_mode == "none":
            return {"pushed": False, "merged": None, "push_note": "",
                    "note": f"none 模式：改动已 commit 到本地分支 {branch}，未推送"}

        remote_head = self._remote_head(sh, branch)
        head = sh(["git", "rev-parse", "HEAD"]).stdout.strip()
        if remote_head and remote_head == head and not committed_now:
            pushed, push_note = True, "远端已同步，跳过重复 push"
        else:
            args = ["git", "push", "-u", "origin", branch] if not remote_head else ["git", "push", "origin", branch]
            rp = sh(args)
            pushed = rp.returncode == 0
            push_note = "" if pushed else (rp.stderr or "")[-300:]

        if merge_mode == "pr":
            if not pushed:
                return {"pushed": False, "merged": False, "push_note": push_note,
                        "note": f"分支推送失败，未开 PR：{push_note}"}
            return {**self._create_pr(wt, branch, base_branch, msg), "pushed": True,
                    "push_note": push_note}

        if merge_mode != "main":
            return {"pushed": pushed, "merged": None, "push_note": push_note,
                    "note": "branch 模式：仅推分支，不合并 main"}

        if not pushed:
            return {"pushed": False, "merged": False, "push_note": push_note,
                    "note": f"分支推送失败，未合并 {base_branch}：{push_note}"}
        return {**self._merge_to_main(str(execution.evaluate(self.main_clone)), branch, base_branch), "pushed": True,
                "push_note": push_note}

    # -- 内部 ---------------------------------------------------------------

    def _resolve_message(self, execution: Any) -> str:
        if self.commit_message is not None:
            msg = str(execution.evaluate(self.commit_message) or "").strip()
            if msg:
                return msg
        if self.plan_file is not None:
            plan_path = str(execution.evaluate(self.plan_file) or "")
            try:
                plan = open(plan_path, encoding="utf-8").read()
                m = re.search(r"^COMMIT_MESSAGE: (.+)$", plan, re.M)
                if m:
                    return m.group(1).strip()
            except OSError:
                pass
        if self.issue_number is not None:
            return f"fix: issue #{int(execution.evaluate(self.issue_number))}"
        raise ValueError("git_publish：无 commit_message/plan_file/issue_number，无法确定提交消息")

    def _remote_head(self, sh, branch: str) -> Optional[str]:
        lr = sh(["git", "ls-remote", "--heads", "origin", branch], t=60)
        out = lr.stdout.strip()
        return out.split("\t")[0] if out else None

    def _merge_to_main(self, main_clone: str, branch: str, base_branch: str = "main") -> dict:
        def sh(args, t=None):
            return subprocess.run(args, cwd=main_clone, capture_output=True, text=True,
                                  timeout=t or self.timeout_secs)

        sh(["git", "fetch", "origin"])
        r1 = sh(["git", "merge", "--ff-only", f"origin/{branch}"])
        if r1.returncode != 0:
            sh(["git", "merge", "--abort"])
            return {"merged": False,
                    "note": f"ff 合并失败（{base_branch} 已前进或分支未推送），分支在远端，请人工合并"}
        r2 = sh(["git", "push", "origin", f"HEAD:{base_branch}"])
        if r2.returncode == 0:
            return {"merged": True, "note": f"已 ff 合并推送 {base_branch}"}
        return {"merged": False, "note": f"合并成功但推送 {base_branch} 失败"}

    def _create_pr(self, wt: str, branch: str, base_branch: str, title: str) -> dict:
        """推分支后开 PR（pr 模式）。PR 已存在不算失败——如实记 note。"""
        def sh(args, t=None):
            return subprocess.run(args, cwd=wt, capture_output=True, text=True,
                                  timeout=t or self.timeout_secs)

        title = (title or f"merge {branch}").splitlines()[0][:120]
        body = f"Automated by issue-pipeline (branch {branch})."
        rc = sh(["gh", "pr", "create", "--head", branch, "--base", base_branch,
                 "--title", title, "--body", body], t=120)
        out = (rc.stdout or "") + (rc.stderr or "")
        if rc.returncode == 0:
            url = (rc.stdout or "").strip().splitlines()[-1] if rc.stdout.strip() else ""
            return {"merged": False, "pr_url": url,
                    "note": f"已推送分支并创建 PR（base={base_branch}）{url}"}
        if "already exists" in out.lower():
            return {"merged": False, "pr_url": "",
                    "note": f"分支已推送；PR 已存在（base={base_branch}），详见仓库 PR 列表"}
        return {"merged": False, "pr_url": "",
                "note": f"分支已推送，但 gh pr create 失败：{out[-200:]}"}
