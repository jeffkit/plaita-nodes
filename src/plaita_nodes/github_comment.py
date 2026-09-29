"""GithubCommentNode —— GitHub issue/PR 评论外发（出害口统一收敛点）。

把「公开评论出害」的三类控制收敛到一个节点，替代手写 code 节点九连拷：

- **消毒（redact）**：本机路径 / 密钥赋值 / 未执行的 ``$(...)`` 命令替换 / artifact
  目录名，一律打码后才出网——评论正文属不可信输入，绝不能原文发出；
- **去重（dedup_marker）**：发出前查 issue 已有评论是否含标记（如
  ``<!-- issue-pipeline -->``），断点续跑不重复发；
- **尾注（footer）**：可选的 ``---\n*<footer>*`` 尾行，落地事实（分支/推送/
  合并）等由流程侧拼接传入——节点只管追加，不绑定业务文案。

副作用说明：dry-run 下**仍然写 artifact 草稿**（同 writefile 约定，便于人工
检查），但跳过去重查询与真实发布。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, ClassVar, List, Optional, Tuple

from plaita import Node

# 消毒规则（issue-pipeline 生产验证过的清单；\1 反向引用保留命令原文）
_REDACT_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"/Users/\S+"), "[REDACTED-PATH]"),
    (re.compile(r"/home/\S+"), "[REDACTED-PATH]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"), "[REDACTED-SECRET]"),
    (re.compile(r"`\$\(([^`]+)\)`"), r"（命令 `\1` 未在发布时执行，以仓库实际状态为准）"),
]


def redact_text(text: str, extra_secrets: Optional[List[str]] = None) -> str:
    """按固定规则消毒评论文本。extra_secrets 逐字面替换（如 artifact 目录路径）。"""
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    for secret in extra_secrets or []:
        if secret:
            text = text.replace(secret, "[ARTIFACT-DIR]")
    return text


class GithubCommentNode(Node):
    """发一条 GitHub 评论（消毒 + 可选去重 + 可选核验尾行）。

    JSON 字段：
    - ``repo``: repo_full（支持表达式）
    - ``issue_number``: issue/PR 号（支持表达式）
    - ``text``: 评论正文（支持表达式）
    - ``redact``: 消毒开关，默认 True
    - ``artifact_dir``: 提供时正文写入 <artifact_dir>/reply.md 留档，
      且正文中的该目录路径被替换为 [ARTIFACT-DIR]
    - ``dedup_marker``: 提供时先查已有评论，含标记则跳过发布（posted=False）
    - ``footer``: 提供时正文末追加空行 + 分隔线 + ``*<footer>*`` 尾注
    - ``dry_run``: 为 true（或流程 globalContext.dry_run）时写草稿但不连 GitHub

    输出：``{"posted", "note"}``（posted=False 时 note 带 stderr 尾部或跳过原因）。
    """

    node_type: ClassVar[str] = "github_comment"
    node_name: ClassVar[str] = "GitHub 评论"

    repo: Any = None
    issue_number: Any = None
    text: Any = None
    redact: bool = True
    artifact_dir: Optional[Any] = None
    dedup_marker: Optional[str] = None
    footer: Optional[Any] = None
    dry_run: bool = False
    timeout_secs: int = 60

    def execute(self, execution: Any) -> dict:
        if self.repo is None or self.issue_number is None or self.text is None:
            raise ValueError("github_comment 节点缺少 repo/issue_number/text 字段")
        repo = str(execution.evaluate(self.repo))
        number = int(execution.evaluate(self.issue_number))
        body = str(execution.evaluate(self.text) or "")

        artifact_dir: Optional[str] = None
        if self.artifact_dir is not None:
            artifact_dir = str(execution.evaluate(self.artifact_dir) or "") or None

        dry = self.dry_run or bool(execution.get_global_variable("dry_run", False))
        if dry:
            body = self._finalize(body, artifact_dir)
            self._write_artifact(body, artifact_dir)
            return {"posted": True, "dry_run": True, "note": "dry-run：写草稿但跳过 GitHub 调用"}

        if self.dedup_marker and self._already_commented(repo, number, self.dedup_marker):
            return {"posted": False, "note": "已有含去重标记的评论（断点续跑），跳过"}

        body = self._finalize(body, artifact_dir)
        if self.footer is not None:
            tail = str(execution.evaluate(self.footer) or "")
            if tail:
                body = body + f"\n\n---\n*{tail}*"
        body_path = self._write_artifact(body, artifact_dir)

        cmd = ["gh", "issue", "comment", str(number), "-R", repo, "--body-file", body_path]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_secs)
        except FileNotFoundError as exc:
            raise RuntimeError(f"github_comment：gh 不可用（{exc}）；请确认 PATH 含 gh CLI") from None
        if r.returncode != 0:
            return {"posted": False, "note": (r.stderr or "")[-200:]}
        return {"posted": True, "note": ""}

    # -- 内部 ---------------------------------------------------------------

    def _finalize(self, body: str, artifact_dir: Optional[str]) -> str:
        if self.redact:
            body = redact_text(body, extra_secrets=[artifact_dir] if artifact_dir else None)
        return body

    def _already_commented(self, repo: str, number: int, marker: str) -> bool:
        chk = subprocess.run(
            ["gh", "issue", "view", str(number), "-R", repo, "--json", "comments",
             "--jq", f'.comments | map(select(.body | contains("{marker}"))) | length'],
            capture_output=True, text=True, timeout=self.timeout_secs,
        )
        return chk.returncode == 0 and chk.stdout.strip() not in ("", "0")

    def _write_artifact(self, body: str, artifact_dir: Optional[str]) -> str:
        """正文落盘：artifact_dir 优先（留档），否则临时文件（gh --body-file 需要路径）。"""
        import tempfile
        if artifact_dir:
            path = Path(artifact_dir) / "reply.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            return str(path)
        fd, tmp = tempfile.mkstemp(suffix=".md")
        with open(fd, "w", encoding="utf-8") as f:
            f.write(body)
        return tmp
