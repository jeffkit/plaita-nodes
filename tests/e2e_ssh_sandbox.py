"""ssh 沙箱 driver E2E（实验档）：用 Docker 里的 sshd 作为「远端 VM」靶机。

真实链路：AgentRunNode(workspace=…) → agentproc runner → `ssh <opts> target
"cd <wsdir> && exec timeout <T> …"` → 远端执行 → stdout 经 ssh 回传。

跳过条件：docker 不可用 / PLAITA_SKIP_SSH_E2E=1 / sshd 镜像构建失败。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

import plaita_nodes.sandbox as sb
from plaita_nodes.agent_run import AgentRunError, AgentRunNode
from plaita_nodes.sandbox import handle_id

from fake_exec import FakeExecution

EXEC_ID = FakeExecution.execution_id
WS_KEY = "main"
SSHD_IMAGE = "plaita-e2e-sshd:latest"

_DOCKERFILE = """FROM alpine/git:latest
RUN apk add --no-cache openssh-server \
 && ssh-keygen -A \
 && mkdir -p /root/.ssh /var/run/sshd \
 && sed -i 's/^root:!:/root:*:/' /etc/shadow \
 && grep -q '^PermitRootLogin' /etc/ssh/sshd_config \
   || echo 'PermitRootLogin prohibit-password' >> /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
"""

_ENTRYPOINT = """#!/bin/sh
# 挂载文件属主是宿主用户，sshd StrictModes 会拒——拷进 root 家目录并修权限
mkdir -p /root/.ssh
if [ -f /tmp/pub/authorized_keys ]; then
  cp /tmp/pub/authorized_keys /root/.ssh/authorized_keys
  chmod 700 /root/.ssh
  chmod 600 /root/.ssh/authorized_keys
fi
exec /usr/sbin/sshd -D -e
"""


def _docker(args: list, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout)


def _skip_unless_docker():
    if os.environ.get("PLAITA_SKIP_SSH_E2E") == "1":
        pytest.skip("PLAITA_SKIP_SSH_E2E=1")
    if not _docker(["info", "--format", "{{.ServerVersion}}"], timeout=60).stdout.strip():
        pytest.skip("docker 不可用")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def sshd(tmp_path_factory):
    """构建 sshd 镜像 + 起容器 + 生成密钥对/known_hosts，产出连接参数。"""
    _skip_unless_docker()
    root = tmp_path_factory.mktemp("sshd")
    ctx = root / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
    (ctx / "entrypoint.sh").write_text(_ENTRYPOINT, encoding="utf-8")
    built = subprocess.run(["docker", "build", "-t", SSHD_IMAGE, str(ctx)],
                           capture_output=True, text=True, timeout=600)
    if built.returncode != 0:
        pytest.skip(f"sshd 镜像构建失败：{built.stderr.strip()[-200:]}")

    key = root / "id_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                   check=True, capture_output=True)
    # 公钥暂存必须在 colima/Docker Desktop 的挂载白名单目录（~ 下）——
    # /var/folders 等路径 -v 进容器会是空目录（build context 不受影响）
    pubdir = Path.home() / ".plaita-e2e-ssh"
    pubdir.mkdir(parents=True, exist_ok=True)
    (pubdir / "authorized_keys").write_text(
        key.with_suffix(".pub").read_text("utf-8").strip() + "\n", encoding="utf-8")

    port = _free_port()
    name = "plaita-e2e-sshd"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    run = _docker(["run", "-d", "--name", name,
                   "-p", f"127.0.0.1:{port}:22",
                   "-v", f"{pubdir}:/tmp/pub:ro",
                   SSHD_IMAGE], timeout=300)
    if run.returncode != 0:
        pytest.skip(f"sshd 容器启动失败：{run.stderr.strip()[-200:]}")

    known_hosts = root / "known_hosts"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        scan = subprocess.run(
            ["ssh-keyscan", "-p", str(port), "-T", "5", "127.0.0.1"],
            capture_output=True, text=True, timeout=30)
        if scan.stdout.strip():
            known_hosts.write_text(scan.stdout, encoding="utf-8")
            break
        time.sleep(0.5)
    else:
        _docker(["rm", "-f", name])
        pytest.skip("ssh-keyscan 未能取到 host key")

    # keyscan 拿到 host key ≠ sshd 能完成认证——加真实 SSH 探活门
    probe_deadline = time.monotonic() + 20
    while True:
        probe = subprocess.run(
            ["ssh", "-p", str(port), "-i", str(key),
             "-o", f"UserKnownHostsFile={known_hosts}",
             "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
             "-o", "ConnectTimeout=5", "root@127.0.0.1", "true"],
            capture_output=True, timeout=30)
        if probe.returncode == 0:
            break
        if time.monotonic() > probe_deadline:
            _docker(["rm", "-f", name])
            pytest.skip(f"sshd 就绪探测失败：{probe.stderr.strip()[:120]}")
        time.sleep(0.5)

    yield {"port": port, "key": str(key), "known_hosts": str(known_hosts),
           "name": name}

    _docker(["rm", "-f", name])


@pytest.fixture
def ssh_home(tmp_path, monkeypatch, sshd):
    monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
    (tmp_path / ".plaita").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".plaita" / "sandboxes.json").write_text(json.dumps({
        "sandboxes": {WS_KEY: {
            "driver": "ssh",
            "host": "127.0.0.1",
            "user": "root",
            "port": sshd["port"],
            "identity": sshd["key"],
            "known_hosts": sshd["known_hosts"],
            "remote_root": "/srv/work",
        }}
    }), encoding="utf-8")
    return tmp_path


class TestSshRoundtrip:
    def test_real_remote_roundtrip(self, ssh_home, sshd, agent_config_repo,
                                   register_test_echo_executor):
        node = AgentRunNode(id="e2e", agent="echo", prompt="ssh-hello",
                            workspace=WS_KEY, timeout_secs=30)
        out = node.execute(FakeExecution())
        assert out["dry_run"] is False
        assert "ssh-hello" in out["text"]              # 远端 stdout 经 ssh 回传
        assert out["workspace"]["driver"] == "ssh"
        assert out["workspace"]["id"] == handle_id(EXEC_ID, WS_KEY)

        # exit code 透传：远端 false → 非零
        vm_probe = subprocess.run(
            ["ssh", "-p", str(sshd["port"]), "-i", sshd["key"],
             "-o", f"UserKnownHostsFile={sshd['known_hosts']}",
             "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
             "root@127.0.0.1", "false"], capture_output=True, timeout=60)
        assert vm_probe.returncode != 0

    def test_wallclock_and_enforce(self, ssh_home, sshd, agent_config_repo,
                                   register_test_echo_executor):
        driver = sb.get_driver("ssh")
        spec = sb.load_sandboxes()[WS_KEY]
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)

        # 墙钟：远端 timeout 终结 60s 的 sleep
        envfile = Path(handle.data["wsdir"])  # 占位（ssh 忽略 envfile）
        argv = driver.wrap_argv(handle, ["sleep", "60"], 3, envfile)
        start = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, timeout=60)
        assert proc.returncode != 0
        assert time.monotonic() - start < 30

        # enforce：按 wsdir pkill 会话壳（cmdline 携带 wsdir）；孙进程由墙钟收尾
        slow = subprocess.Popen(
            driver.wrap_argv(handle, ["sleep", "30"], 300, envfile),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        driver.enforce(handle)
        # 会话壳已死：另起 ssh 查远端已无 wsdir 标记进程（排除 pkill 自身的转义形态）
        deadline = time.monotonic() + 20
        gone = False
        while time.monotonic() < deadline:
            check = subprocess.run(
                ["ssh", "-p", str(sshd["port"]), "-i", sshd["key"],
                 "-o", f"UserKnownHostsFile={sshd['known_hosts']}",
                 "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
                 "root@127.0.0.1",
                 f"pgrep -f '[/]{handle.data['wsdir'][1:]}' | wc -l"],
                capture_output=True, text=True, timeout=60)
            if check.stdout.strip() == "0":
                gone = True
                break
            time.sleep(0.5)
        assert gone, f"enforce 后远端仍有 wsdir 标记进程：{check.stdout!r} {check.stderr!r}"
        slow.wait(timeout=60)   # 孙进程由墙钟收尾，客户端随后退出（实验档口径）

    def test_git_dataplane_lite(self, ssh_home, sshd, agent_config_repo,
                                register_test_echo_executor):
        """数据面同盘：driver.git 在远端工作目录执行（init/commit/log）。"""
        node = AgentRunNode(id="e2e", agent="echo", prompt="hi", workspace=WS_KEY,
                            timeout_secs=30)
        node.execute(FakeExecution())                  # ensure + 远端 sidecar

        driver = sb.get_driver("ssh")
        spec = sb.load_sandboxes()[WS_KEY]
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)
        init = driver.git(handle, ["init", "-q"])
        assert init.returncode == 0 or "already exists" in (init.stderr or "")
        commit = driver.git(handle, ["-c", "user.name=e2e", "-c",
                                     "user.email=e2e@t", "commit",
                                     "--allow-empty", "-m", "ssh-seed"])
        assert commit.returncode == 0
        log = driver.git(handle, ["log", "--oneline"])
        assert "ssh-seed" in log.stdout

        # 远端 sidecar 确实写入（reaper 聚合的数据源）
        sidecar = subprocess.run(
            ["ssh", "-p", str(sshd["port"]), "-i", sshd["key"],
             "-o", f"UserKnownHostsFile={sshd['known_hosts']}",
             "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
             "root@127.0.0.1",
             f"cat {handle.data['wsdir']}/.plaita-sandbox.json"],
            capture_output=True, text=True, timeout=60)
        meta = json.loads(sidecar.stdout)
        assert meta["execution_id"] == EXEC_ID and meta["ws_key"] == WS_KEY

    def test_failure_path_enforces(self, ssh_home, sshd, agent_config_repo,
                                   register_test_echo_executor, monkeypatch):
        import plaita_nodes.sandbox_ssh as ss
        driver = sb.get_driver("ssh")
        spec = sb.load_sandboxes()[WS_KEY]
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)

        def failing_wrap(env, argv):
            return driver.wrap_argv(handle, ["false"], 30, Path("/tmp/e.env"))

        monkeypatch.setattr(sb, "wrap_agent_argv_from_env", failing_wrap)
        node = AgentRunNode(id="e2e", agent="echo", prompt="hi", workspace=WS_KEY,
                            timeout_secs=30)
        with pytest.raises(AgentRunError):
            node.execute(FakeExecution())
