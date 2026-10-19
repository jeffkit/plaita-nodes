"""pn#16：流程级 globalContext.dry_run 的零副作用契约。

git_publish / notify 的节点字段 dry-run 已有用例；这里钉死流程级 dry-run：
即使节点 ``dry_run=False``，``globalContext.dry_run=True`` 也必须零 git 子进程、
零通知 backend 调用。负控（只判节点字段）下两条都会红。
"""
from __future__ import annotations

import subprocess

import pytest

from fake_exec import FakeExecution
from plaita_nodes.git_publish import GitPublishNode
from plaita_nodes.notify import NotifyNode
from plaita_nodes.notify_backends import NOTIFY_BACKENDS, register_notify_backend


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """worktree repo + bare origin（离线真实 git），已切到发布分支。"""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "clone", str(origin), str(wt)],
                   check=True, capture_output=True)
    _git("config", "user.email", "t@t", cwd=wt)
    _git("config", "user.name", "t", cwd=wt)
    (wt / "a.txt").write_text("v1")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "init", cwd=wt)
    _git("push", "-u", "origin", "main", cwd=wt)
    _git("checkout", "-b", "p/issue-1", cwd=wt)
    return wt, origin


def test_git_publish_global_dry_run_zero_git_calls(git_repo, monkeypatch):
    wt, origin = git_repo
    (wt / "b.txt").write_text("uncommitted change")
    head_before = _git("rev-parse", "HEAD", cwd=wt)

    calls = []
    real_run = subprocess.run

    def spy(argv, *args, **kwargs):
        calls.append(list(argv) if isinstance(argv, (list, tuple)) else argv)
        return real_run(argv, *args, **kwargs)

    # 同时覆盖 git_publish 自身与 _git_env 的 filter 枚举（同一 subprocess 模块）。
    monkeypatch.setattr("plaita_nodes.git_publish.subprocess.run", spy)

    out = GitPublishNode(
        id="g", worktree_dir=str(wt), branch_name="p/issue-1",
        issue_number=1, dry_run=False,
    ).execute(FakeExecution({"dry_run": True}))

    assert calls == [], f"dry-run 期间发生 git 子进程调用: {calls}"
    assert out["dry_run"] is True and out["pushed"] is True
    assert _git("rev-parse", "HEAD", cwd=wt) == head_before
    assert (wt / "b.txt").exists(), "dry-run 不应清理工作区"
    assert not (origin / "refs" / "heads" / "p" / "issue-1").exists(), \
        "dry-run 不应在远端创建分支"


class _CountingExecution(FakeExecution):
    def __init__(self, global_vars):
        super().__init__(global_vars)
        self.evaluated = []

    def evaluate(self, value):
        self.evaluated.append(value)
        return value


def test_notify_global_dry_run_zero_backend_calls(monkeypatch):
    calls = []

    def _fake(execution, node, text):
        calls.append(text)
        return {"ok": True, "channel": node.channel}

    register_notify_backend("dry_probe", _fake)
    try:
        ex = _CountingExecution({"dry_run": True})
        out = NotifyNode(id="n", message="SHOULD_NOT_SEND", channel="dry_probe",
                         dry_run=False).execute(ex)
    finally:
        NOTIFY_BACKENDS.pop("dry_probe", None)

    assert calls == [], f"dry-run 期间通知 backend 被调用: {calls}"
    assert out == {"ok": True, "channel": "dry_probe", "dry_run": True}
    assert ex.evaluated == [], "dry-run 闸应早于 message 求值"


def test_notify_terminal_global_dry_run_is_silent(capsys):
    out = NotifyNode(id="n", message="SHOULD_NOT_PRINT", dry_run=False).execute(
        FakeExecution({"dry_run": True}))
    printed = capsys.readouterr().out
    assert printed == "", f"dry-run 期间终端收到通知: {printed!r}"
    assert out == {"ok": True, "channel": "terminal", "dry_run": True}
