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
    - ``merge_mode``: ``branch``（默认，仅推分支）| ``main``（ff 合并并推 main）
    - ``main_clone``: main 模式必填——main 所在 clone 路径
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
    dry_run: bool = False
    timeout_secs: int = 300

    def execute(self, execution: Any) -> dict:
        if self.worktree_dir is None or self.branch_name is None:
            raise ValueError("git_publish 节点缺少 worktree_dir/branch_name 字段")
        wt = str(execution.evaluate(self.worktree_dir))
        branch = str(execution.evaluate(self.branch_name))

        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            return {"pushed": True, "merged": None, "push_note": "", "note": "dry-run：跳过 git 操作",
                    "dry_run": True}
        if self.merge_mode == "main" and self.main_clone is None:
            raise ValueError("git_publish merge_mode=main 需要 main_clone 字段")

        def sh(args, cwd=None, t=None):
            return subprocess.run(args, cwd=cwd or wt, capture_output=True, text=True,
                                  timeout=t or self.timeout_secs)

        dirty = sh(["git", "status", "--porcelain"]).stdout.strip() != ""
        committed_now = False
        if dirty:
            msg = self._resolve_message(execution)
            sh(["git", "add", "-A"])
            rc = sh(["git", "commit", "-m", msg])
            if rc.returncode != 0:
                return {"pushed": False, "merged": None, "push_note": "",
                        "note": f"commit 失败: {(rc.stderr or '')[-300:]}",
                        "push_note": (rc.stderr or "")[-300:]}
            committed_now = True

        remote_head = self._remote_head(sh, branch)
        head = sh(["git", "rev-parse", "HEAD"]).stdout.strip()
        if remote_head and remote_head == head and not committed_now:
            pushed, push_note = True, "远端已同步，跳过重复 push"
        else:
            args = ["git", "push", "-u", "origin", branch] if not remote_head else ["git", "push", "origin", branch]
            rp = sh(args)
            pushed = rp.returncode == 0
            push_note = "" if pushed else (rp.stderr or "")[-300:]

        if self.merge_mode != "main":
            return {"pushed": pushed, "merged": None, "push_note": push_note,
                    "note": "branch 模式：仅推分支，不合并 main"}

        if not pushed:
            return {"pushed": False, "merged": False, "push_note": push_note,
                    "note": f"分支推送失败，未合并 main：{push_note}"}
        return {**self._merge_to_main(str(execution.evaluate(self.main_clone)), branch), "pushed": True,
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

    def _merge_to_main(self, main_clone: str, branch: str) -> dict:
        def sh(args, t=None):
            return subprocess.run(args, cwd=main_clone, capture_output=True, text=True,
                                  timeout=t or self.timeout_secs)

        sh(["git", "fetch", "origin"])
        r1 = sh(["git", "merge", "--ff-only", f"origin/{branch}"])
        if r1.returncode != 0:
            sh(["git", "merge", "--abort"])
            return {"merged": False,
                    "note": "ff 合并失败（main 已前进或分支未推送），分支在远端，请人工合并"}
        r2 = sh(["git", "push", "origin", "HEAD:main"])
        if r2.returncode == 0:
            return {"merged": True, "note": "已 ff 合并推送 main"}
        return {"merged": False, "note": "合并成功但推送 main 失败"}
