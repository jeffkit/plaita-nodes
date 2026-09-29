"""GithubCommentNode / ParseJsonNode / GitPublishNode 单测。

- github_comment：消毒/去重/核验尾行/artifact，gh 经 PATH 注入的 fake 脚本桩；
- parse_json：健壮解析回归（含 issue-pipeline #43 原文形态）+ fail-safe；
- git_publish：本地 bare remote 全离线跑真实 git，覆盖幂等语义（缺口 #6）。
"""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from plaita_nodes.github_comment import GithubCommentNode, redact_text
from plaita_nodes.git_publish import GitPublishNode
from plaita_nodes.parse_json import ParseJsonNode, extract_json_candidates


# ---------------------------------------------------------------------------
# github_comment
# ---------------------------------------------------------------------------

class FakeGh:
    """写一个假 gh 到 PATH：/comment 记录 body、/view 按 verdict 返回已有评论数。"""

    def __init__(self, tmp_path: Path, existing_marker_count: int = 0, fail: bool = False):
        self.calls: list[list[str]] = []
        self.bin = tmp_path / "fakebin"
        self.bin.mkdir(exist_ok=True)
        state = tmp_path / "gh_state.json"
        state.write_text(json.dumps({"existing": existing_marker_count, "fail": fail}))
        self.state = state
        script = self.bin / "gh"
        body = textwrap.dedent(f"""
            #!/usr/bin/env python3
            import json, sys
            args = sys.argv[1:]
            st = json.load(open({str(state)!r}))
            with open({str(state)!r}.replace('.json', '_calls.log'), 'a') as f:
                f.write(json.dumps(args) + '\\n')
            if st.get("fail"):
                sys.stderr.write("gh boom")
                sys.exit(1)
            if args[:2] == ["issue", "view"]:
                print(json.dumps(st["existing"]))
            elif args[:2] == ["issue", "comment"]:
                body_file = args[args.index("--body-file") + 1]
                with open({str(tmp_path / 'posted.md')!r}, 'w') as f:
                    f.write(open(body_file).read())
            sys.exit(0)
        """).lstrip("\n")  # shebang 必须在首行——带前导空行 exec 会走离真 gh
        script.write_text(body)
        script.chmod(0o755)

    def env(self, monkeypatch):
        monkeypatch.setenv("PATH", f"{self.bin}:{os.environ['PATH']}")


def _run_comment(tmp_path, monkeypatch, gh, **fields):
    gh.env(monkeypatch)
    art = tmp_path / "art"
    art.mkdir(exist_ok=True)
    node = GithubCommentNode(**{"id": "c1", "repo": "o/r", "issue_number": 7,
                                "text": "hello", "artifact_dir": str(art), **fields})
    return node.execute(_FakeExec()), art


class _FakeExec:
    express_prefix = "$"

    def evaluate(self, v):
        return v

    def get_global_variable(self, key, default=None):
        return default


def test_comment_posts_and_writes_artifact(tmp_path, monkeypatch):
    gh = FakeGh(tmp_path)
    out, art = _run_comment(tmp_path, monkeypatch, gh)
    assert out == {"posted": True, "note": ""}
    assert (art / "reply.md").read_text(encoding="utf-8") == "hello"
    assert (tmp_path / "posted.md").read_text(encoding="utf-8") == "hello"


def test_comment_redacts_paths_secrets_and_cmd_substitution(tmp_path, monkeypatch):
    gh = FakeGh(tmp_path)
    body = "see /Users/kong/secret.txt and token=abc123 and `$(git rev-parse HEAD)`"
    _, art = _run_comment(tmp_path, monkeypatch, gh, text=body)
    posted = (art / "reply.md").read_text(encoding="utf-8")
    assert "/Users/" not in posted
    assert "kong" not in posted
    assert "abc123" not in posted
    assert "[REDACTED-PATH]" in posted and "[REDACTED-SECRET]" in posted
    assert "未在发布时执行" in posted


def test_comment_redacts_artifact_dir(tmp_path, monkeypatch):
    gh = FakeGh(tmp_path)
    art = tmp_path / "pipeline" / "recursive-17"
    art.mkdir(parents=True)
    gh.env(monkeypatch)
    node = GithubCommentNode(id="c", repo="o/r", issue_number=1,
                             text=f"art dir is {art}", artifact_dir=str(art))
    node.execute(_FakeExec())
    assert str(art) not in (art / "reply.md").read_text(encoding="utf-8")
    assert "[ARTIFACT-DIR]" in (art / "reply.md").read_text(encoding="utf-8")


def test_comment_dedup_marker_skips(tmp_path, monkeypatch):
    gh = FakeGh(tmp_path, existing_marker_count=2)
    out, art = _run_comment(tmp_path, monkeypatch, gh, dedup_marker="<!-- issue-pipeline -->")
    assert out["posted"] is False
    assert "跳过" in out["note"]
    # 跳过路径不写 artifact（正文未出网）
    assert not (art / "reply.md").exists()


def test_comment_dedup_passes_when_no_marker(tmp_path, monkeypatch):
    gh = FakeGh(tmp_path, existing_marker_count=0)
    out, art = _run_comment(tmp_path, monkeypatch, gh, dedup_marker="<!-- issue-pipeline -->")
    assert out["posted"] is True
    assert (art / "reply.md").exists()


def test_comment_gh_failure_returns_posted_false(tmp_path, monkeypatch):
    gh = FakeGh(tmp_path, fail=True)
    out, _ = _run_comment(tmp_path, monkeypatch, gh)
    assert out["posted"] is False
    assert "boom" in out["note"]


def test_comment_verify_tail_appended_after_redaction(tmp_path, monkeypatch):
    gh = FakeGh(tmp_path)
    node = GithubCommentNode(id="c", repo="o/r", issue_number=1, text="done /Users/x",
                             footer="管线核验：分支 p/issue-1 · 推送=True")
    gh.env(monkeypatch)
    out = node.execute(_FakeExec())
    assert out["posted"] is True
    import tempfile
    # 无 artifact_dir 时走临时文件，从 gh 调用日志拿 body-file 路径验证
    calls = (tmp_path / "gh_state_calls.log").read_text()
    args = json.loads(calls.splitlines()[-1])
    body_file = args[args.index("--body-file") + 1]
    body = open(body_file).read()
    assert "*管线核验：分支 p/issue-1 · 推送=True*" in body
    assert "/Users/" not in body.split("---")[0]


def test_redact_text_pure_function():
    assert redact_text("a /home/u/x b") == "a [REDACTED-PATH] b"
    assert "REDACTED" in redact_text("API_KEY: xyz")


# ---------------------------------------------------------------------------
# parse_json
# ---------------------------------------------------------------------------

def test_extract_candidates_strict_line_first():
    raw = '说明 type={}, model={}\n{"verdict": "fix", "notes": "n"}'
    cands = extract_json_candidates(raw)
    assert cands[0] == '{"verdict": "fix", "notes": "n"}'  # 整行严格 JSON 候选在前


def test_parse_json_ok_with_choices_and_join(tmp_path):
    node = ParseJsonNode(id="p", text='前言\n{"verdict":"actionable","acceptance":["a","b"]}',
                         choices=["actionable", "blocked", "invalid"], join_fields=["acceptance"])
    out = node.execute(_FakeExec())
    assert out["parse_ok"] is True
    assert out["verdict"] == "actionable"
    assert out["acceptance_str"] == "a; b"


def test_parse_json_issue43_braces_in_body_still_parses():
    """#43 原文形态：正文带花括号，朴素 first-{ 切片必死；健壮解析必须活。"""
    body = 'preset resolves to: type={}, model={}\n{"verdict":"fix","notes":"cargo fmt 失败"}'
    node = ParseJsonNode(id="p", text=body, choices=["approve", "fix", "abort"])
    out = node.execute(_FakeExec())
    assert out["parse_ok"] is True and out["verdict"] == "fix"


def test_parse_json_unparseable_returns_default_with_detail():
    default = {"verdict": "abort", "notes": "review 输出无法解析，fail-safe 叫停"}
    node = ParseJsonNode(id="p", text="完全不是 JSON", default=default)
    out = node.execute(_FakeExec())
    assert out["parse_ok"] is False
    assert out["verdict"] == "abort"
    assert "解析失败" in out["parse_error"]
    assert "解析失败" in out["notes"]  # 明细追加进 notes 供模板引用


def test_parse_json_invalid_verdict_failsafe():
    node = ParseJsonNode(id="p", text='{"verdict":"maybe"}',
                         choices=["approve", "fix", "abort"],
                         default={"verdict": "abort", "notes": "非法 verdict"})
    out = node.execute(_FakeExec())
    assert out["parse_ok"] is False and out["verdict"] == "abort"
    assert "非法 verdict" in out["parse_error"]


def test_parse_json_without_default_raises():
    node = ParseJsonNode(id="p", text="not json")
    with pytest.raises(ValueError):
        node.execute(_FakeExec())


# ---------------------------------------------------------------------------
# git_publish（本地 bare remote，全离线真实 git）
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """worktree repo + bare origin，含一个初始提交。"""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "clone", str(origin), str(wt)], check=True, capture_output=True)
    _git("config", "user.email", "t@t", cwd=wt)
    _git("config", "user.name", "t", cwd=wt)
    (wt / "a.txt").write_text("v1")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "init", cwd=wt)
    _git("push", "-u", "origin", "main", cwd=wt)
    _git("checkout", "-b", "p/issue-1", cwd=wt)
    return wt, origin


def test_publish_branch_mode_first_push(git_repo, _fake_exec):
    wt, _origin = git_repo
    (wt / "b.txt").write_text("change")
    node = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1",
                          commit_message="feat: x ( #1)", issue_number=1)
    out = node.execute(_fake_exec)
    assert out["pushed"] is True and out["merged"] is None
    assert "branch 模式" in out["note"]
    heads = _git("ls-remote", "--heads", "origin", cwd=wt)
    assert "p/issue-1" in heads


def test_publish_idempotent_rerun_with_new_changes_not_lost(git_repo, _fake_exec):
    """缺口 #6 回归：远端已有分支时，新改动必须被 commit+push，不得静默丢弃。"""
    wt, _origin = git_repo
    (wt / "b.txt").write_text("change")
    node = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1",
                          issue_number=1, commit_message="feat: first")
    assert node.execute(_fake_exec)["pushed"] is True
    # 第二次投递：新改动落在已有远端分支的工作区
    (wt / "c.txt").write_text("second")
    out = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1",
                         issue_number=1, commit_message="feat: second").execute(_fake_exec)
    assert out["pushed"] is True
    log = _git("log", "--oneline", "origin/p/issue-1", cwd=wt)
    assert "feat: second" in log  # 新改动进了提交，没被丢


def test_publish_true_duplicate_skips_push(git_repo, _fake_exec):
    wt, _origin = git_repo
    (wt / "b.txt").write_text("change")
    node = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1",
                          issue_number=1, commit_message="feat: x")
    node.execute(_fake_exec)
    out = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1",
                         issue_number=1, commit_message="feat: x").execute(_fake_exec)
    assert out["pushed"] is True
    assert "跳过重复 push" in out["push_note"]


def test_publish_main_mode_ff_merges_branch(git_repo, tmp_path, _fake_exec):
    wt, origin = git_repo
    main_clone = tmp_path / "main"
    subprocess.run(["git", "clone", str(origin), str(main_clone)], check=True, capture_output=True)
    _git("config", "user.email", "t@t", cwd=main_clone)
    _git("config", "user.name", "t", cwd=main_clone)
    (wt / "b.txt").write_text("change")
    plan = tmp_path / "02-plan.md"
    plan.write_text("内容\nCOMMIT_MESSAGE: fix: the thing (#1)\n", encoding="utf-8")
    node = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1",
                          plan_file=str(plan), merge_mode="main", main_clone=str(main_clone))
    out = node.execute(_fake_exec)
    assert out["pushed"] is True and out["merged"] is True
    assert out["note"] == "已 ff 合并推送 main"
    # main 上确实包含分支改动，且 commit message 来自 plan 文件
    _git("fetch", "origin", cwd=main_clone)
    log = _git("log", "--oneline", "origin/main", cwd=main_clone)
    assert "fix: the thing (#1)" in log
    assert (main_clone / "b.txt").exists() or "b.txt" in log


def test_publish_message_priority_commit_message_over_plan(git_repo, tmp_path, _fake_exec):
    wt, _origin = git_repo
    (wt / "b.txt").write_text("x")
    plan = tmp_path / "p.md"
    plan.write_text("COMMIT_MESSAGE: from-plan\n")
    node = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1",
                          commit_message="from-explicit", plan_file=str(plan), issue_number=1)
    node.execute(_fake_exec)
    assert "from-explicit" in _git("log", "--oneline", "-1", cwd=wt)


def test_publish_fallback_message_uses_issue_number(git_repo, _fake_exec):
    wt, _origin = git_repo
    (wt / "b.txt").write_text("x")
    node = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1", issue_number=9)
    node.execute(_fake_exec)
    assert "fix: issue #9" in _git("log", "--oneline", "-1", cwd=wt)


def test_publish_dry_run_no_git_side_effect(git_repo, _fake_exec):
    wt, origin = git_repo
    node = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="p/issue-1",
                          issue_number=1, dry_run=True)
    out = node.execute(_fake_exec)
    assert out["pushed"] is True and out.get("dry_run") is True
    assert "p/issue-1" not in _git("ls-remote", "--heads", "origin", cwd=wt)


@pytest.fixture
def _fake_exec():
    return _FakeExec()
