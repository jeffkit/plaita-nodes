"""pn#13：github_comment 去重不得把 marker 拼进 jq，查询失败不得静默发布。

- 含引号的 marker 仍能按字面去重（旧实现拼出非法 jq，非零 rc 被当「无重复」）；
- gh 查询失败 / gh 缺失抛具名 ``GithubCommentError``，评论零外发。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fake_exec import FakeExecution
from plaita_nodes.github_comment import GithubCommentError, GithubCommentNode


class FakeGh:
    """假 gh：support ``--json comments``（新实现）与 ``--jq``（旧实现负控）。"""

    def __init__(self, tmp_path: Path, comments=None, view_fail: bool = False):
        self.state = tmp_path / "gh_state.json"
        self.state.write_text(json.dumps({
            "comments": comments or [],
            "view_fail": view_fail,
        }))
        self.calls_path = tmp_path / "gh_calls.log"
        self.posted = tmp_path / "posted.md"
        self.bin = tmp_path / "fakebin"
        self.bin.mkdir(exist_ok=True)
        script = self.bin / "gh"
        script.write_text(f"""#!/usr/bin/env python3
import json, subprocess, sys
args = sys.argv[1:]
state = json.load(open({str(self.state)!r}))
with open({str(self.calls_path)!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:2] == ["issue", "view"]:
    if state.get("view_fail"):
        sys.stderr.write("view boom\\n")
        sys.exit(1)
    doc = json.dumps({{"comments": state.get("comments", [])}})
    if "--jq" in args:
        prog = args[args.index("--jq") + 1]
        r = subprocess.run(["jq", prog], input=doc, text=True, capture_output=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)
    sys.stdout.write(doc)
    sys.exit(0)
if args[:2] == ["issue", "comment"]:
    body_file = args[args.index("--body-file") + 1]
    with open({str(self.posted)!r}, "w") as f:
        f.write(open(body_file).read())
    sys.stdout.write("https://example/comment\\n")
    sys.exit(0)
sys.exit(2)
""")
        script.chmod(0o755)

    @property
    def calls(self) -> list[list[str]]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def commented(self) -> bool:
        return any(call[:2] == ["issue", "comment"] for call in self.calls)


def _setup(tmp_path: Path, monkeypatch, comments=None, view_fail: bool = False) -> FakeGh:
    gh = FakeGh(tmp_path, comments=comments, view_fail=view_fail)
    monkeypatch.setenv("PATH", f"{gh.bin}:{os.environ['PATH']}")
    return gh


def _node(tmp_path: Path, **fields) -> GithubCommentNode:
    art = tmp_path / "art"
    return GithubCommentNode(id="c", repo="o/r", issue_number=7, text="hello",
                             artifact_dir=str(art), **fields)


def test_quoted_marker_is_still_deduplicated(tmp_path, monkeypatch):
    marker = '<!-- "quoted" marker -->'
    gh = _setup(tmp_path, monkeypatch, comments=[{"body": f"previous\n{marker}"}])

    out = _node(tmp_path, dedup_marker=marker).execute(FakeExecution())

    assert out["posted"] is False and "跳过" in out["note"]
    assert not gh.commented(), "含引号 marker 没命中已有评论，重复发布了"


def test_dedup_query_failure_raises_named_error(tmp_path, monkeypatch):
    gh = _setup(tmp_path, monkeypatch, view_fail=True)

    with pytest.raises(GithubCommentError, match="去重查询失败"):
        _node(tmp_path, dedup_marker="<!-- issue-pipeline -->").execute(FakeExecution())

    assert not gh.commented(), "去重查询失败后仍发布了评论"


def test_missing_gh_raises_named_error(tmp_path, monkeypatch):
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    with pytest.raises(GithubCommentError, match="gh CLI"):
        _node(tmp_path, dedup_marker="<!-- issue-pipeline -->").execute(FakeExecution())


def test_no_marker_still_posts_once(tmp_path, monkeypatch):
    gh = _setup(tmp_path, monkeypatch, comments=[{"body": "unrelated comment"}])

    out = _node(tmp_path, dedup_marker="<!-- issue-pipeline -->").execute(FakeExecution())

    assert out["posted"] is True
    assert gh.commented()
