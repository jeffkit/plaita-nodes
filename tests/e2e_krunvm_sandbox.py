"""krunvm microVM 沙箱 E2E（实验档）。

真实链路：AgentRunNode(workspace=…) → agentproc runner → `krunvm start`（真
libkrun microVM）→ stdout 回传；git 数据面在宿主数据目录上（krunvm 驱动的
设计差异，见 sandbox_krunvm 模块 docstring）。

跳过条件：krunvm 未安装/未初始化 / PLAITA_SKIP_KRUNVM_E2E=1。
运行：pytest -q tests/e2e_krunvm_sandbox.py
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from uuid import uuid4
from pathlib import Path

import pytest

import plaita_nodes.sandbox as sb
from plaita_nodes.agent_run import AgentRunError, AgentRunNode
from plaita_nodes.sandbox import (
    SandboxError,
    handle_id,
    resource_name,
    suspend_release,
)

from fake_exec import FakeExecution

EXEC_ID = FakeExecution.execution_id

IMAGE = "alpine:latest"
WS_KEY = "main"


def _krunvm(args: list, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["krunvm", *args], capture_output=True, text=True,
                          timeout=timeout)


def _skip_unless_krunvm():
    if os.environ.get("PLAITA_SKIP_KRUNVM_E2E") == "1":
        pytest.skip("PLAITA_SKIP_KRUNVM_E2E=1")
    probe = _krunvm(["list"], timeout=60)
    if probe.returncode != 0:
        pytest.skip(f"krunvm 不可用/未初始化：{probe.stderr.strip()[:120]}")


def _vm_name() -> str:
    return resource_name(EXEC_ID, WS_KEY, "vm").lower()


@pytest.fixture
def krun_home(tmp_path, monkeypatch):
    """HOME 隔离（注册表 + 租约 + 驱动数据根都在 tmp）。"""
    monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
    (tmp_path / ".plaita").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".plaita" / "sandboxes.json").write_text(json.dumps({
        "sandboxes": {WS_KEY: {
            "driver": "krunvm",
            "image": IMAGE,
            "allow_unpinned": True,   # E2E 本地镜像；生产注册表要求 digest pin
            "resources": {"cpus": "2", "mem": "512"},
        }}
    }), encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _krun_cleanup():
    yield
    if os.environ.get("PLAITA_SKIP_KRUNVM_E2E") == "1":
        return
    vm = _vm_name()
    if subprocess.run(["which", "krunvm"], capture_output=True).returncode == 0:
        subprocess.run(["krunvm", "delete", vm], capture_output=True, timeout=120)
    data = Path.home() / ".plaita" / "sandbox-data"
    if data.exists():  # HOME 未隔离的兜底（正常路径由 driver 自己清理）
        shutil_ignore = None


class TestKrunvmRoundtrip:
    def test_real_microvm_roundtrip(self, krun_home, agent_config_repo,
                                    register_test_echo_executor):
        _skip_unless_krunvm()
        node = AgentRunNode(id="e2e", agent="echo", prompt="microvm-hello",
                            workspace=WS_KEY, timeout_secs=30)
        out = node.execute(FakeExecution())

        assert out["dry_run"] is False
        assert "microvm-hello" in out["text"]       # 真 microVM stdout 回传
        snap = out["workspace"]
        assert snap["driver"] == "krunvm"
        assert snap["id"] == handle_id(EXEC_ID, WS_KEY)
        assert snap["path"] == "/work"

        vm = _vm_name()
        listed = _krunvm(["list"], timeout=60).stdout
        assert vm in listed

        # 租约已释放：同 key 可再获取
        assert sb.default_lease_store().acquire(handle_id(EXEC_ID, WS_KEY), "post", 60)
        sb.default_lease_store().release(handle_id(EXEC_ID, WS_KEY), "post")

        # exit code 透传：guest 内 false → 非零退出码
        rc = subprocess.run(["krunvm", "start", vm, "--", "false"],
                            capture_output=True, timeout=120).returncode
        assert rc != 0

    def test_git_dataplane_and_wip(self, krun_home, tmp_path, agent_config_repo,
                                   register_test_echo_executor):
        """数据面（宿主侧 git）：provision clone → 脏工作区 → wip push → 重建。"""
        _skip_unless_krunvm()
        # 本地 bare 仓 + 种子提交（host git，file:// 协议）
        seed_bare = tmp_path / "seed.git"
        work = tmp_path / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)],
                       check=True, capture_output=True)
        (work / "seed.txt").write_text("seed-content\n", encoding="utf-8")
        env = {"GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@t",
               "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@t",
               "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        subprocess.run(["git", "-C", str(work), "add", "."], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(work), "commit", "-qm", "krun-seed"],
                       check=True, capture_output=True, env=env)
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(seed_bare)],
                       check=True, capture_output=True)

        (krun_home / ".plaita" / "sandboxes.json").write_text(json.dumps({
            "sandboxes": {WS_KEY: {
                "driver": "krunvm", "image": IMAGE, "allow_unpinned": True,
                "provision": {"git": {"repo": f"file://{seed_bare}", "branch": "main"}},
            }}
        }), encoding="utf-8")

        out = AgentRunNode(id="e2e", agent="echo", prompt="hi", workspace=WS_KEY,
                           timeout_secs=30).execute(FakeExecution())
        assert out["dry_run"] is False

        driver = sb.get_driver("krunvm")
        spec = sb.load_sandboxes()[WS_KEY]
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)
        datadir = Path(handle.data["datadir"])
        assert (datadir / "seed.txt").exists()      # provision clone 真实发生（宿主数据目录）

        logged = driver.git(handle, ["log", "--oneline"])
        assert "krun-seed" in logged.stdout

        # 模拟 VM 内 agent 经 bind mount 写脏工作区（宿主视角即数据目录落文件）
        (datadir / "dirty.txt").write_text("dirty-change\n", encoding="utf-8")

        outcome = suspend_release(driver, handle, spec)
        assert outcome == "wip-pushed"

        # wip 分支真被推上 bare 仓（数据面走宿主侧，与 VM 网络无关）
        remote = subprocess.run(["git", "ls-remote", str(seed_bare)],
                                capture_output=True, text=True, timeout=30)
        wip_ref = f"refs/heads/plaita/wip/{EXEC_ID}/{WS_KEY}"
        assert wip_ref in remote.stdout

        # 重建（数据层在）：working tree == wip 提交
        rebuilt = driver.ensure(spec, EXEC_ID, WS_KEY)
        status = driver.git(rebuilt, ["status", "--porcelain"])
        assert status.stdout.strip() == ""
        log = driver.git(rebuilt, ["log", "--oneline", "-1"])
        assert "wip snapshot" in log.stdout

    def test_guest_egress_probe(self, krun_home, agent_config_repo,
                                register_test_echo_executor):
        """信息性探测（驱动文档 egress 声明，2026-10-01 实测）：krunvm guest
        默认 **HTTPS/DNS 可出网**（ICMP 不通）——egress 白名单档对 krunvm 同样
        是 P2 必需项，而非「VM 天然无网络」的想当然。"""
        _skip_unless_krunvm()
        vm = _vm_name()
        AgentRunNode(id="e2e", agent="echo", prompt="hi", workspace=WS_KEY,
                     timeout_secs=30).execute(FakeExecution())   # 确保 VM 存在
        # 信息性探测（结论已记录文档：krunvm guest 可出网）——网络抖动容错重试
        probe = None
        for _ in range(3):
            probe = subprocess.run(
                ["krunvm", "start", vm, "--", "sh", "-c",
                 "wget -q -T 8 -O- https://github.com | head -c 20"],
                capture_output=True, text=True, timeout=120)
            if probe.returncode == 0 and probe.stdout.strip():
                break
            time.sleep(1)
        if probe is None or probe.returncode != 0:
            pytest.skip(f"guest 出网探测不可达（网络抖动/受限）："
                        f"{(probe.stderr if probe else '').strip()[:120]}")
        assert len(probe.stdout) > 0

    def test_failure_path_enforces(self, krun_home, agent_config_repo,
                                   register_test_echo_executor, monkeypatch):
        """执行失败 → driver.enforce 兜底。dispatch 替身把 argv 改写成 guest 内
        会失败的 `false`（真 microVM 仍执行它），exit code 经 krunvm 透传。"""
        _skip_unless_krunvm()
        import plaita_nodes.sandbox_krunvm as sk
        monkeypatch.setattr(sb, "wrap_agent_argv_from_env",
                            lambda env, argv: sk.wrap_argv_from_env(
                                {**env, "PLAITA_SANDBOX_NAME": _vm_name()}, ["false"]))
        try:
            node = AgentRunNode(id="e2e", agent="echo", prompt="hi", workspace=WS_KEY,
                                timeout_secs=30)
            with pytest.raises(AgentRunError):
                node.execute(FakeExecution())
            listed = _krunvm(["list"], timeout=60).stdout
            assert _vm_name() in listed  # VM 仍在（enforce 后按名存在与否不破坏数据层）
        finally:
            pass
