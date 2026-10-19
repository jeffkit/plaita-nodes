"""pn#17 回归：git_publish / github_comment 子进程 env 收敛为显式白名单。

缺陷：三处 ``git`` 与两处 ``gh`` 调用都没有 ``env=``，宿主全量环境直通。
工作树里可写的 ``.git/hooks/pre-commit`` 在 ``git commit`` 时执行属 git
标准行为——提示注入让钩子打印 ``PLAITA_CREDENTIALS_KEY`` 即可外带平台凭据。

契约：
- 子进程 env 从白名单重建（PATH/HOME/GIT_* 最小集 + ssh-agent/gh 配置定位）；
- 凭据类变量（``*TOKEN*`` / ``*SECRET*`` / ``*PASSWORD*`` / ``*CREDENTIAL*``
  等）一律不传；
- 正常 publish/comment 照常工作（正控）。

负控（回退生产改动）时，``test_git_hook_does_not_see_host_credentials`` 与
``test_gh_child_does_not_see_host_credentials`` 精确红：钩子/gh 能看到
``PLAITA_CREDENTIALS_KEY=host-secret-...``。
"""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from plaita_nodes._git_env import build_git_env
from plaita_nodes.git_publish import GitPublishNode
from plaita_nodes.github_comment import GithubCommentNode

SECRET_KEYS = (
    "PLAITA_CREDENTIALS_KEY",
    "DATABASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


class _FakeExec:
    express_prefix = "$"

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


@pytest.fixture()
def host_secrets(monkeypatch):
    for key in SECRET_KEYS:
        monkeypatch.setenv(key, f"host-secret-{key}")
    return {key: f"host-secret-{key}" for key in SECRET_KEYS}


class TestBuildGitEnv:
    def test_credentials_dropped_git_and_ssh_kept(self, monkeypatch, host_secrets):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "hook-author")
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=no")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/gh-config")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        env = build_git_env()

        assert not set(SECRET_KEYS) & set(env)
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["GIT_AUTHOR_NAME"] == "hook-author"
        assert env["GIT_SSH_COMMAND"].startswith("ssh ")
        assert env["SSH_AUTH_SOCK"] == "/tmp/agent.sock"
        assert env["GH_CONFIG_DIR"] == "/tmp/gh-config"

    def test_credential_bearing_git_vars_dropped(self, monkeypatch, host_secrets):
        """可携带/吐出凭据、可重定向仓库目标的 git 变量不得进子进程。"""
        monkeypatch.setenv("GIT_ASKPASS", "/bin/echo")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Bearer host-secret")
        monkeypatch.setenv("GIT_DIR", "/somewhere/else")
        monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/else")
        monkeypatch.setenv("GIT_TRACE", "1")

        env = build_git_env()

        for key in ("GIT_ASKPASS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                    "GIT_CONFIG_VALUE_0", "GIT_DIR", "GIT_WORK_TREE", "GIT_TRACE"):
            assert key not in env
        assert "host-secret" not in json.dumps(env)

    def test_extra_is_also_filtered(self, monkeypatch):
        """部署方经 extra/SUBPROCESS_ENV_EXTRA 塞凭据也会被最终过滤。"""
        env = build_git_env({"GH_TOKEN": "leaked", "MY_FLAG": "1"})
        assert "GH_TOKEN" not in env
        assert env["MY_FLAG"] == "1"


# ---------------------------------------------------------------------------
# git_publish：恶意 pre-commit 探针
# ---------------------------------------------------------------------------

def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture()
def git_repo(tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True)
    _git("config", "user.email", "t@t", cwd=wt)
    _git("config", "user.name", "t", cwd=wt)
    (wt / "a.txt").write_text("v1", encoding="utf-8")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "init", cwd=wt)
    return wt


def _install_probe_hook(repo: Path, out_file: Path) -> None:
    """恶意 pre-commit：把子进程 env 原样写到 out_file 后正常退出。"""
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"env > {str(out_file)!r}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


class TestGitPublishHookEnv:
    def test_git_hook_does_not_see_host_credentials(
            self, git_repo, tmp_path, host_secrets, monkeypatch):
        probe = tmp_path / "hook-env.txt"
        _install_probe_hook(git_repo, probe)
        monkeypatch.setenv("GIT_MERGE_AUTOEDIT", "no")  # 非敏感 GIT_* 应放行
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")  # 凭据面应剔除
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Bearer host-secret")
        (git_repo / "b.txt").write_text("change", encoding="utf-8")

        node = GitPublishNode(id="g", worktree_dir=str(git_repo), branch_name="main",
                              commit_message="test: hook probe", merge_mode="none")
        out = node.execute(_FakeExec())

        # 正控：正常 commit 成功（none 模式不 push）
        assert out["pushed"] is False and "未推送" in out["note"]
        assert "commit 失败" not in out["note"]
        assert probe.exists(), "pre-commit 应被 git 正常执行"

        child_env = probe.read_text(encoding="utf-8", errors="replace")
        assert "PATH=" in child_env
        assert "GIT_MERGE_AUTOEDIT=no" in child_env  # 非敏感 GIT_* 最小集放行
        for key, value in host_secrets.items():
            assert f"{key}=" not in child_env, f"{key} 泄漏进 git 钩子"
            assert value not in child_env
        assert "GIT_CONFIG_COUNT" not in child_env
        assert "GIT_CONFIG_KEY_0" not in child_env
        assert "Authorization: Bearer" not in child_env

    def test_git_hook_sees_git_author_env(self, git_repo, tmp_path, host_secrets,
                                          monkeypatch):
        """git 身份经 env 传入仍生效（GIT_* 最小集不是一刀切）。"""
        probe = tmp_path / "hook-env.txt"
        _install_probe_hook(git_repo, probe)
        monkeypatch.setenv("GIT_AUTHOR_NAME", "Hook Author")
        (git_repo / "b.txt").write_text("change", encoding="utf-8")

        out = GitPublishNode(
            id="g", worktree_dir=str(git_repo), branch_name="main",
            commit_message="test: author env", merge_mode="none",
        ).execute(_FakeExec())

        assert "commit 失败" not in out["note"]
        assert "GIT_AUTHOR_NAME=Hook Author" in probe.read_text(encoding="utf-8")
        assert _git("log", "-1", "--pretty=%an", cwd=git_repo) == "Hook Author"


# ---------------------------------------------------------------------------
# github_comment：gh 子进程探针
# ---------------------------------------------------------------------------

class _FakeGhEnvProbe:
    """假 gh：把 env 写到 probe 文件，行为与真 gh 的 comment 调用一致。"""

    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "fakebin"
        self.bin.mkdir()
        self.probe = tmp_path / "gh-env.txt"
        script = self.bin / "gh"
        script.write_text(textwrap.dedent(f"""
            #!/bin/sh
            env > {str(self.probe)!r}
            exit 0
        """).lstrip("\n"), encoding="utf-8")
        script.chmod(0o755)

    def install(self, monkeypatch):
        monkeypatch.setenv("PATH", f"{self.bin}:{os.environ['PATH']}")


class TestGithubCommentChildEnv:
    def test_gh_child_does_not_see_host_credentials(
            self, tmp_path, host_secrets, monkeypatch):
        gh = _FakeGhEnvProbe(tmp_path)
        gh.install(monkeypatch)
        art = tmp_path / "art"

        node = GithubCommentNode(id="c", repo="o/r", issue_number=7, text="hello",
                                 artifact_dir=str(art))
        out = node.execute(_FakeExec())

        # 正控：评论照常发出、artifact 落盘
        assert out == {"posted": True, "note": ""}
        assert (art / "reply.md").read_text(encoding="utf-8") == "hello"
        assert gh.probe.exists()

        child_env = gh.probe.read_text(encoding="utf-8", errors="replace")
        assert "PATH=" in child_env
        for key, value in host_secrets.items():
            assert f"{key}=" not in child_env, f"{key} 泄漏进 gh 子进程"
            assert value not in child_env
