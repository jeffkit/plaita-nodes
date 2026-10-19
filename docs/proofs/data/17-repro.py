"""pn#17 探针：git hook / gh 子进程能否看到宿主凭据 env。

- git_publish：工作树内安装恶意 ``.git/hooks/pre-commit``，把子进程 env
  原样写到文件；``git commit`` 触发钩子（git 标准行为）。
- github_comment：PATH 注入假 ``gh``，它同样把 env 写到文件。

修复前：两份 env 都含 ``PLAITA_CREDENTIALS_KEY=host-secret-...``；
修复后：凭据键与值均不存在，PATH / 非敏感 ``GIT_*`` 仍在。

用法::

    plaita/.venv/bin/python docs/proofs/data/17-repro.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from plaita_nodes.git_publish import GitPublishNode  # noqa: E402
from plaita_nodes.github_comment import GithubCommentNode  # noqa: E402

SECRETS = {
    "PLAITA_CREDENTIALS_KEY": "host-secret-plaita-key",
    "DATABASE_URL": "postgres://user:pass@host/db",
    "ANTHROPIC_AUTH_TOKEN": "host-secret-anthropic",
    "GH_TOKEN": "host-secret-gh-token",
    "GITHUB_TOKEN": "host-secret-github-token",
}


class _Exec:
    express_prefix = "$"

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _write_probe(path: Path, probe_file: Path) -> None:
    path.write_text(f"#!/bin/sh\nenv > {str(probe_file)!r}\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _report(label: str, env_text: str) -> dict:
    leaked = {
        key: (f"{key}=" in env_text or value in env_text)
        for key, value in SECRETS.items()
    }
    return {
        "label": label,
        "secrets_visible": leaked,
        "leaked": any(leaked.values()),
        "path_visible": "PATH=" in env_text,
        "git_author_visible": "GIT_AUTHOR_NAME=" in env_text,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pn17-") as tmp:
        root = Path(tmp)
        wt = root / "wt"
        subprocess.run(["git", "init", "-b", "main", str(wt)], check=True,
                       capture_output=True)
        _git("config", "user.email", "t@t", cwd=wt)
        _git("config", "user.name", "t", cwd=wt)
        (wt / "a.txt").write_text("v1", encoding="utf-8")
        _git("add", "-A", cwd=wt)
        _git("commit", "-m", "init", cwd=wt)

        hook_probe = root / "hook-env.txt"
        _write_probe(wt / ".git" / "hooks" / "pre-commit", hook_probe)

        for key, value in SECRETS.items():
            os.environ[key] = value
        os.environ["GIT_AUTHOR_NAME"] = "Probe Author"
        (wt / "b.txt").write_text("change", encoding="utf-8")

        node = GitPublishNode(id="g", worktree_dir=str(wt), branch_name="main",
                              commit_message="probe: env", merge_mode="none")
        publish = node.execute(_Exec())

        gh_bin = root / "fakebin"
        gh_bin.mkdir()
        gh_probe = root / "gh-env.txt"
        _write_probe(gh_bin / "gh", gh_probe)
        os.environ["PATH"] = f"{gh_bin}:{os.environ['PATH']}"
        art = root / "art"
        comment = GithubCommentNode(id="c", repo="o/r", issue_number=7, text="hello",
                                    artifact_dir=str(art)).execute(_Exec())

        hook_report = _report("git pre-commit", hook_probe.read_text(errors="replace"))
        gh_report = _report("gh child", gh_probe.read_text(errors="replace"))
        result = {
            "git_publish": {
                "publish_ok": publish.get("note", ""),
                "hook_ran": hook_probe.exists(),
                **hook_report,
            },
            "github_comment": {
                "posted": comment.get("posted"),
                "gh_ran": gh_probe.exists(),
                **gh_report,
            },
        }

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["git_publish"]["leaked"] or result["github_comment"]["leaked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
