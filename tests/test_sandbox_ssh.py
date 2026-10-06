"""ssh 沙箱 driver 单测（假 run_fn 断言远端命令串的转义与组装）。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from plaita_nodes.sandbox import (
    SandboxConfigError,
    WorkspaceSpec,
    handle_id,
    resource_name,
)
from plaita_nodes.sandbox_ssh import SshDriver, wrap_argv_from_env


def _driver(recorder: list):
    def run(argv, timeout, cwd=None):
        recorder.append((argv, cwd))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return SshDriver(run_fn=run)


def _spec(**kw):
    base = {"name": "main", "driver": "ssh", "host": "vm.example",
            "user": "agent", "port": 2222, "identity": "/keys/id_ed25519",
            "known_hosts": "/keys/known_hosts", "remote_root": "/srv/plaita-ws"}
    base.update(kw)
    return WorkspaceSpec(**base)


class TestSshDriver:
    def test_ensure_requires_host(self):
        driver = _driver([])
        with pytest.raises(SandboxConfigError, match="需要 host"):
            driver.ensure(WorkspaceSpec(name="m", driver="ssh"), "e1", "main")

    def test_ensure_mkdir_sidecar_and_handle(self):
        recorder: list = []
        driver = _driver(recorder)
        handle = driver.ensure(_spec(), "e1", "main")
        wsdir = f"/srv/plaita-ws/{resource_name('e1', 'main', 'ws')}"
        assert handle.id == handle_id("e1", "main")
        assert handle.path == wsdir
        mkdir_cmd = recorder[0][0][-1]
        assert mkdir_cmd == f"mkdir -p {wsdir}"
        # 连接选项：私钥 + 端口 + 严格 host key + 批处理
        opts = recorder[0][0][:recorder[0][0].index("agent@vm.example")]
        assert "-i" in opts and "/keys/id_ed25519" in opts
        assert "-p" in opts and "2222" in opts
        assert "StrictHostKeyChecking=yes" in opts
        assert "BatchMode=yes" in opts

    def test_ensure_provision_clone_quoted(self):
        recorder: list = []

        def run(argv, timeout, cwd=None):
            recorder.append((argv, cwd))
            rc = 1 if "rev-parse" in argv[-1] else 0   # 未 provision → 触发 clone
            return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

        driver = SshDriver(run_fn=run)
        spec = _spec(provision={"git": {"repo": "https://x/y.git",
                                        "branch": "feat/x"}})
        driver.ensure(spec, "e1", "main")
        clone_cmd = next(argv[-1] for argv, _ in recorder if "git clone" in argv[-1])
        assert "--branch feat/x --single-branch" in clone_cmd
        assert clone_cmd.startswith("cd /srv/plaita-ws/")

    def test_wrap_quotes_argv_boundaries(self):
        recorder: list = []
        driver = _driver(recorder)
        handle = driver.ensure(_spec(), "e1", "main")
        argv = driver.wrap_argv(handle, ["echo", "hello world", "&&", "rm -rf /"],
                                30, Path("/tmp/ignored.env"))
        assert argv[0] == "ssh"
        assert argv[-1].startswith("cd /srv/plaita-ws/")
        assert "timeout 30" in argv[-1]
        # 空格与元字符被 shlex 转义，参数边界不丢
        assert "'hello world'" in argv[-1]
        assert "'&&'" in argv[-1]
        assert "'rm -rf /'" in argv[-1]

    def test_wrap_from_env_missing_knobs(self):
        with pytest.raises(SandboxConfigError, match="旋钮缺失"):
            wrap_argv_from_env({"PLAITA_SANDBOX_TIMEOUT": "30"}, ["x"])

    def test_enforce_pkills_by_wsdir(self):
        recorder: list = []
        driver = _driver(recorder)
        handle = driver.ensure(_spec(), "e1", "main")
        recorder.clear()
        driver.enforce(handle)
        remote_cmd = recorder[0][0][-1]
        # [/] 括号技巧：正则命中目标 cmdline，但不匹配调用壳里的字面模式
        # （shlex.quote 会给含 [ 的模式加单引号，远端 shell 解出的仍是 [/]srv 正则）
        assert "pkill -f '[/]srv/plaita-ws/" in remote_cmd

    def test_git_quotes_args(self):
        recorder: list = []
        driver = _driver(recorder)
        handle = driver.ensure(_spec(), "e1", "main")
        recorder.clear()
        driver.git(handle, ["commit", "-m", "two words"])
        remote_cmd = recorder[0][0][-1]
        assert remote_cmd.startswith("cd /srv/plaita-ws/")
        assert "git commit -m 'two words'" in remote_cmd

    def test_release_keep_data_semantics(self):
        recorder: list = []
        driver = _driver(recorder)
        handle = driver.ensure(_spec(), "e1", "main")
        recorder.clear()
        driver.release(handle, keep_data=True)
        assert recorder == []                       # 同盘语义：保留即不动
        driver.release(handle, keep_data=False)
        assert recorder[0][0][-1].startswith("rm -rf /srv/plaita-ws/")


# ── reaper 枚举聚合：ssh v1 恒空 ────────────────────────────────────────

def test_reaper_enumerate_includes_ssh_as_empty():
    from plaita_nodes.sandbox_reaper import enumerate_resources
    assert enumerate_resources(["ssh"]) == []
