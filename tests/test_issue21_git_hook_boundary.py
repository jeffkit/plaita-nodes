"""pn#21 回归：恶意 worktree 的 hooks/filters 零执行 + 凭据边界前移。

pn#17 的 env 白名单只管「变量名」，不是恶意 worktree hook 的凭据边界：
同 UID 可读 worker 的 ``/proc/<pid>/environ``；``SSH_AUTH_SOCK`` 被有意放行，
hook 可 ``ssh-add -l`` 列出 agent 身份；``GIT_SSH_COMMAND`` / ``*_PROXY`` 的
**值**内嵌 token，键名过滤天然不防。

修法两层：
1. 边界前移：``git_publish`` 的每条 git 命令都带
   ``-c core.hooksPath=/dev/null -c core.fsmonitor=false
   -c filter.<driver>.{clean,smudge,process}=``——agent 可写工作树里的
   hook/filter 对发布过程零执行；
2. 值级兜底：``build_git_env`` 不再放行 ``SSH_AUTH_SOCK``，并对
   ``GIT_SSH_COMMAND`` / ``*_PROXY`` 的值做凭据扫描，命中即具名拒绝。

负控（回退生产改动）时，``*_zero_execution`` 与 ``hook_cannot_reach_agent``
精确红在「探针文件出现」；``TestValueLevelCredentialScan`` 红在
``GitEnvCredentialError`` 不存在/未抛出；``ssh_auth_sock_dropped`` 红在
socket 仍被放行。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from plaita_nodes.git_publish import GitPublishNode


class _FakeExec:
    express_prefix = "$"

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _git_env_mod():
    """延迟取模块：负控时新符号不存在，失败点精确落在用例内而非 collection。"""
    from plaita_nodes import _git_env

    return _git_env


@pytest.fixture(autouse=True)
def _hermetic_git_env(monkeypatch):
    """宿主可能自带代理/ssh 配置；用例按需显式设置，避免环境相关红绿。"""
    for key in (
        "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND", "SSH_AUTH_SOCK",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True)
    _git("config", "user.email", "pn21@t", cwd=wt)
    _git("config", "user.name", "pn21", cwd=wt)
    (wt / "a.txt").write_text("v1", encoding="utf-8")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "init", cwd=wt)
    return wt


@pytest.fixture
def publish_repo(tmp_path: Path):
    """worktree repo + bare origin，已推 main 并切到 p/issue-21。"""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "clone", str(origin), str(wt)], check=True, capture_output=True)
    _git("config", "user.email", "pn21@t", cwd=wt)
    _git("config", "user.name", "pn21", cwd=wt)
    (wt / "a.txt").write_text("v1", encoding="utf-8")
    _git("add", "-A", cwd=wt)
    _git("commit", "-m", "init", cwd=wt)
    _git("push", "-u", "origin", "main", cwd=wt)
    _git("checkout", "-b", "p/issue-21", cwd=wt)
    return wt, origin


def _install_probe_hook(hook_path: Path, probe: Path, label: str) -> None:
    """恶意 hook：记录三条旁路探针（env / 同 UID environ / ssh-agent 身份）。"""
    script = """#!/bin/sh
{
  echo "hook=%(label)s"
  env | grep -E '^(PLAITA_CREDENTIALS_KEY|SSH_AUTH_SOCK|GIT_SSH_COMMAND|HTTPS_PROXY)=' || true
  if [ -d /proc ]; then
    for f in /proc/[0-9]*/environ; do tr '\\0' '\\n' < "$f" 2>/dev/null; done \\
      | grep -E '^(PLAITA_[C]REDENTIALS_KEY|SSH_AUTH_[S]OCK)=' && echo 'probe=/proc:LEAK' || true
  fi
  if [ -n "$SSH_AUTH_SOCK" ] && command -v ssh-add >/dev/null 2>&1; then
    ssh-add -l 2>&1 || true
  fi
} > %(probe)s
exit 0
""" % {"label": label, "probe": shlex.quote(str(probe))}
    hook_path.write_text(script, encoding="utf-8")
    hook_path.chmod(0o755)


def _leak_host_secrets(monkeypatch) -> None:
    monkeypatch.setenv("PLAITA_CREDENTIALS_KEY", "host-secret-pn21")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/pn21-fake-agent.sock")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o StrictHostKeyChecking=no")


def _fail_if_probe(probe: Path, what: str) -> None:
    if probe.exists():
        pytest.fail(
            f"{what}被执行（pn#21 应为零执行）；探针输出：\n"
            + probe.read_text(encoding="utf-8", errors="replace")
        )


# ---------------------------------------------------------------------------
# 硬化参数：机制显式可见
# ---------------------------------------------------------------------------

class TestHardenedGitArgv:
    def test_hooks_fsmonitor_ext_and_configured_filters_all_disabled(self, local_repo):
        mod = _git_env_mod()
        _git("config", "filter.evil.clean", "tee /tmp/pn21-filter", cwd=local_repo)

        argv = mod.git_hardened_argv(str(local_repo), env=mod.build_git_env())

        assert argv[0] == "git"
        assert "core.hooksPath=/dev/null" in argv
        assert "core.fsmonitor=false" in argv
        assert "protocol.ext.allow=never" in argv
        for op in ("clean", "smudge", "process"):
            assert f"filter.evil.{op}=" in argv


# ---------------------------------------------------------------------------
# 负控：恶意 hook / filter / fsmonitor 零执行
# ---------------------------------------------------------------------------

class TestMaliciousHooksZeroExecution:
    def test_pre_commit_zero_execution(self, local_repo, tmp_path, monkeypatch):
        probe = tmp_path / "pre-commit-probe.txt"
        _install_probe_hook(local_repo / ".git" / "hooks" / "pre-commit", probe, "pre-commit")
        _leak_host_secrets(monkeypatch)
        (local_repo / "b.txt").write_text("payload", encoding="utf-8")

        out = GitPublishNode(
            id="g", worktree_dir=str(local_repo), branch_name="main",
            commit_message="pn#21 hook probe", merge_mode="none",
        ).execute(_FakeExec())

        assert "commit 失败" not in out["note"]
        assert "未推送" in out["note"]  # 正控：none 模式 commit 照常完成
        _fail_if_probe(probe, "恶意 pre-commit ")
        assert "pn#21 hook probe" in _git("log", "-1", "--pretty=%s", cwd=local_repo).stdout

    def test_pre_push_zero_execution(self, publish_repo, tmp_path, monkeypatch):
        wt, _origin = publish_repo
        probe = tmp_path / "pre-push-probe.txt"
        _install_probe_hook(wt / ".git" / "hooks" / "pre-push", probe, "pre-push")
        _leak_host_secrets(monkeypatch)
        (wt / "b.txt").write_text("payload", encoding="utf-8")

        out = GitPublishNode(
            id="g", worktree_dir=str(wt), branch_name="p/issue-21",
            commit_message="pn#21 push probe", merge_mode="branch",
        ).execute(_FakeExec())

        assert out["pushed"] is True  # 正控：正常 push 不受影响
        _fail_if_probe(probe, "恶意 pre-push ")
        assert "p/issue-21" in _git("ls-remote", "--heads", "origin", cwd=wt).stdout

    def test_clean_filter_zero_execution(self, local_repo, tmp_path):
        marker = tmp_path / "clean-filter-ran"
        _git("config", "filter.evil.clean", f"tee {shlex.quote(str(marker))}", cwd=local_repo)
        (local_repo / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        (local_repo / "b.txt").write_text("payload", encoding="utf-8")

        out = GitPublishNode(
            id="g", worktree_dir=str(local_repo), branch_name="main",
            commit_message="pn#21 filter probe", merge_mode="none",
        ).execute(_FakeExec())

        # 非 required filter：空覆盖 = 原样入库（不执行、不阻塞）
        assert "commit 失败" not in out["note"]
        assert not marker.exists(), "worktree 的 clean filter 在发布时被执行"
        assert _git("show", "HEAD:b.txt", cwd=local_repo).stdout == "payload"

    def test_clean_filter_with_dotted_driver_name_disabled(self, local_repo, tmp_path):
        """driver 名可含点（filter.evil.v2）；枚举不能按空白截断漏掉它。"""
        marker = tmp_path / "dotted-filter-ran"
        _git("config", "filter.evil.v2.clean", f"tee {shlex.quote(str(marker))}",
             cwd=local_repo)
        (local_repo / ".gitattributes").write_text("* filter=evil.v2\n", encoding="utf-8")
        (local_repo / "b.txt").write_text("payload", encoding="utf-8")

        out = GitPublishNode(
            id="g", worktree_dir=str(local_repo), branch_name="main",
            commit_message="pn#21 dotted filter probe", merge_mode="none",
        ).execute(_FakeExec())

        assert "commit 失败" not in out["note"]
        assert not marker.exists(), "带点 driver 名的 clean filter 在发布时被执行"

    def test_fsmonitor_hook_zero_execution(self, local_repo, tmp_path):
        marker = tmp_path / "fsmonitor-ran"
        script = tmp_path / "fsmonitor.sh"
        script.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
        script.chmod(0o755)
        _git("config", "core.fsmonitor", str(script), cwd=local_repo)
        (local_repo / "b.txt").write_text("payload", encoding="utf-8")

        out = GitPublishNode(
            id="g", worktree_dir=str(local_repo), branch_name="main",
            commit_message="pn#21 fsmonitor probe", merge_mode="none",
        ).execute(_FakeExec())

        assert "commit 失败" not in out["note"]
        assert not marker.exists(), "core.fsmonitor（等价 hook）在发布时被执行"


@pytest.fixture
def ssh_agent(tmp_path):
    """真实 ssh-agent + 一把临时 key：用于证明 hook 拿不到身份。"""
    if shutil.which("ssh-agent") is None or shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-agent/ssh-keygen 不可用")
    sock_dir = Path(tempfile.mkdtemp(prefix="pn21-agent-", dir="/tmp"))
    sock = sock_dir / "agent.sock"
    agent = subprocess.Popen(["ssh-agent", "-D", "-a", str(sock)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            if sock.exists():
                break
            time.sleep(0.01)
        else:
            pytest.skip("ssh-agent 未就绪")
        key = sock_dir / "id_ed25519"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                       check=True)
        subprocess.run(["ssh-add", str(key)], check=True, capture_output=True,
                       env={**os.environ, "SSH_AUTH_SOCK": str(sock)})
        yield sock
    finally:
        agent.terminate()
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_hook_cannot_reach_ssh_agent(local_repo, tmp_path, monkeypatch, ssh_agent):
    """SSH_AUTH_SOCK 不进 git 子进程：hook 即使执行也列不出 agent 身份。"""
    probe = tmp_path / "agent-probe.txt"
    _install_probe_hook(local_repo / ".git" / "hooks" / "pre-commit", probe, "agent")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(ssh_agent))
    monkeypatch.setenv("PLAITA_CREDENTIALS_KEY", "host-secret-pn21")
    (local_repo / "b.txt").write_text("payload", encoding="utf-8")

    out = GitPublishNode(
        id="g", worktree_dir=str(local_repo), branch_name="main",
        commit_message="pn#21 agent probe", merge_mode="none",
    ).execute(_FakeExec())

    assert "commit 失败" not in out["note"]
    _fail_if_probe(probe, "恶意 hook ")


# ---------------------------------------------------------------------------
# 值级兜底：*_PROXY / GIT_SSH_COMMAND 内嵌凭据 → 具名拒绝
# ---------------------------------------------------------------------------

def _deploy_env(monkeypatch, key: str, value: str) -> None:
    from plaita_nodes import _subprocess_env

    monkeypatch.setitem(_subprocess_env.SUBPROCESS_ENV_EXTRA, key, value)


class TestValueLevelCredentialScan:
    def test_git_ssh_command_with_token_rejected(self, monkeypatch):
        mod = _git_env_mod()
        token = "ghp_" + "A" * 30
        monkeypatch.setenv(
            "GIT_SSH_COMMAND",
            f"ssh -o ProxyCommand='curl -H \"Authorization: Bearer {token}\"'",
        )

        with pytest.raises(mod.GitEnvCredentialError) as excinfo:
            mod.build_git_env()

        message = str(excinfo.value)
        assert "GIT_SSH_COMMAND" in message
        assert token not in message  # 报错不回显凭据值

    def test_proxy_userinfo_rejected(self, monkeypatch):
        mod = _git_env_mod()
        _deploy_env(monkeypatch, "HTTPS_PROXY", "http://user:pass@proxy.corp:8080")

        with pytest.raises(mod.GitEnvCredentialError, match="HTTPS_PROXY"):
            mod.build_git_env()

    def test_proxy_without_credentials_allowed(self, monkeypatch):
        mod = _git_env_mod()
        _deploy_env(monkeypatch, "HTTPS_PROXY", "http://proxy.corp:8080")

        assert mod.build_git_env()["HTTPS_PROXY"] == "http://proxy.corp:8080"

    def test_ssh_command_without_credentials_allowed(self, monkeypatch):
        mod = _git_env_mod()
        monkeypatch.setenv("GIT_SSH_COMMAND",
                           "ssh -i /home/me/.ssh/id_ed25519 -o StrictHostKeyChecking=yes")

        assert mod.build_git_env()["GIT_SSH_COMMAND"].startswith("ssh -i ")

    def test_ssh_auth_sock_dropped(self, monkeypatch):
        mod = _git_env_mod()
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")

        assert "SSH_AUTH_SOCK" not in mod.build_git_env()
        assert "SSH_AUTH_SOCK" not in mod.build_git_env(
            {"SSH_AUTH_SOCK": "/tmp/other-agent.sock"})
