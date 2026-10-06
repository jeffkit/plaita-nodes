"""真实 agent 沙箱 E2E（argusai 全系统闭环的前置；P3 里程碑）。

与其它沙箱 E2E 的区别：**被沙箱化的是真实 Agent CLI（recursive 0.8.x，
真实 provider 凭据，真实一次 LLM 工具循环）**——agent 在容器里读懂种子仓库、
编辑文件，宿主侧断言工作区真实变更。这正是「沙箱化 coding agent」的端到端
生产形态。

前置（一次性，见 scripts 注释）：
    ~/plaita-real-sandbox/recursive        # recursive-aarch64-unknown-linux-musl（SHA256 已校验）
    docker build -t plaita-real-sandbox:latest ~/plaita-real-sandbox
    ~/.plaita/providers.json + agents.json 含 recursive 型 agent（本机已有 glm53-flash）

跳过条件：PLAITA_SKIP_REAL_E2E=1 / 镜像缺失 / 凭据未配置。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from uuid import uuid4
from pathlib import Path

import pytest

import plaita_nodes.sandbox as sb
from plaita_nodes.agent_run import AgentRunNode
from plaita_nodes.sandbox import handle_id

from fake_exec import FakeExecution

EXEC_ID = FakeExecution.execution_id

WS_KEY = "main"
IMAGE = "plaita-real-sandbox:latest"
AGENT = "glm53-flash"          # 真实 ~/.plaita/agents.json 里的 recursive 型 agent


def _docker(args: list, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout)


def _skip_unless_real():
    if os.environ.get("PLAITA_SKIP_REAL_E2E") == "1":
        pytest.skip("PLAITA_SKIP_REAL_E2E=1")
    images = _docker(["images", "--format", "{{.Repository}}:{{.Tag}}"])
    if IMAGE not in images.stdout:
        pytest.skip("缺少沙箱镜像：构建 recursive linux-musl 到 ~/plaita-real-sandbox "
                    "后 `docker build -t plaita-real-sandbox:latest ~/plaita-real-sandbox`")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def real_workspace(tmp_path):
    """git daemon 提供种子仓库（容器经 host.docker.internal clone）。"""
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "app.py").write_text(
        'def greeting():\n    return "hi"\n', encoding="utf-8")
    (seed / "test_app.py").write_text(
        'from src.app import greeting\n\ndef test_greeting():\n    assert greeting() == "hello"\n',
        encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@t",
           "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@t",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for args in (["git", "init", "-q", "-b", "main", str(seed)],
                 ["git", "-C", str(seed), "add", "."],
                 ["git", "-C", str(seed), "commit", "-qm", "seed"]):
        subprocess.run(args, check=True, capture_output=True, env=env)

    gitd = tmp_path / "gitd"
    gitd.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(gitd / "seed.git")],
                   check=True, capture_output=True)
    port = _free_port()
    daemon = subprocess.Popen(
        ["git", "daemon", f"--base-path={gitd}", "--export-all",
         f"--enable=receive-pack", f"--port={port}", "--reuseaddr"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        url = f"git://host.docker.internal:{port}/seed.git"
        while time.monotonic() < deadline:
            probe = _docker(["run", "--rm", "--entrypoint", "git", IMAGE,
                             "ls-remote", url], timeout=120)
            if probe.returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.skip("容器不可达宿主 git daemon")
        yield url
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)


@pytest.fixture(autouse=True)
def real_registry_and_cleanup(real_workspace, monkeypatch):
    """向真实 infra 注册表（~/.plaita/sandboxes.json）临时注入 main 条目。

    备份/还原严格 try-finally——agent 凭据与 agents.json 走真实 ~/.plaita，
    不做任何 Path.home 补丁（patch pathlib.Path 会全局泄漏到 config 层，
    本次开发实测踩过）。用后清理 volume/容器。
    """
    registry_path = Path.home() / ".plaita" / "sandboxes.json"
    backup = registry_path.read_bytes() if registry_path.exists() else None

    def install():
        merged: dict = json.loads(backup.decode("utf-8")) if backup else {}
        raw = merged.get("sandboxes", merged)
        raw[WS_KEY] = {
            "driver": "docker",
            "image": IMAGE,
            "allow_unpinned": True,   # 本地构建镜像；生产注册表要求 digest pin
            "provision": {"git": {"repo": real_workspace, "branch": "main"}},
            "resources": {"timeout": 900, "cpus": "2", "memory": "2g"},
        }
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps({"sandboxes": raw}), encoding="utf-8")

    data_root = Path.home() / ".plaita-real-data"
    monkeypatch.setenv("PLAITA_DOCKER_DATA_ROOT", str(data_root))
    install()
    try:
        yield
    finally:
        if backup is None:
            registry_path.unlink(missing_ok=True)
        else:
            registry_path.write_bytes(backup)
        # 容器以 root 写 bind mount，宿主 rmtree 删不动 → 容器内清理
        data_root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "run", "--rm", "-v", f"{data_root}:/data",
                        "--entrypoint", "sh", IMAGE,
                        "-c", "rm -rf /data/.[!.]* /data/*"], capture_output=True)
        subprocess.run(["docker", "rm", "-f", "--force",
                        resource_name(EXEC_ID, WS_KEY, "ctr")], capture_output=True)
        datadir = (Path(os.environ["PLAITA_DOCKER_DATA_ROOT"])
                   / resource_name(EXEC_ID, WS_KEY, "data"))
        subprocess.run(["docker", "run", "--rm", "-v", f"{datadir}:/data",
                        "--entrypoint", "sh", IMAGE,
                        "-c", "rm -rf /data/.[!.]* /data/*"], capture_output=True)


def resource_name(execution_id: str, ws_key: str, kind: str) -> str:
    from plaita_nodes.sandbox import resource_name as _rn
    return _rn(execution_id, ws_key, kind)


class TestRealAgentInSandbox:
    def test_recursive_edits_real_repo_inside_sandbox(self, real_registry_and_cleanup):
        """真 recursive CLI + 真 LLM 工具循环：容器内读懂仓库并完成编辑任务。"""
        prompt = ("仓库的 src/app.py 里 greeting() 返回 'hi'。"
                  "请把返回值改成 'hello'（只改这一处，不要做其它改动），"
                  "改完用 `git diff` 确认即可，不要 commit。")
        node = AgentRunNode(id="real", agent=AGENT, workspace=WS_KEY,
                            prompt=prompt, timeout_secs=900)
        out = node.execute(FakeExecution())

        # 断言 1：真实 LLM 循环完成（非 dry、有文本、有 token 用量）
        assert out["dry_run"] is False
        assert out["text"].strip(), f"agent 空回复：{out}"
        assert out["cli"] == "recursive"
        assert out["usage"], f"recursive 未上报 usage：{out}"

        # 断言 2：容器内工作区真实变更（数据层 = volume）
        datadir = (Path.home() / ".plaita-real-data"
                   / resource_name(EXEC_ID, WS_KEY, "data"))
        app = datadir / "src" / "app.py"
        assert app.exists(), f"数据目录缺少 src/app.py：{sorted(p.name for p in datadir.rglob('*'))[:10]}"
        assert "hello" in app.read_text(encoding="utf-8"), (
            f"agent 未在沙箱内完成编辑。app.py={app.read_text(encoding='utf-8')[:200]!r} "
            f"agent 回复摘要={out['text'][:200]!r}")

        # 断言 3：观测快照（checkpoint 形态）齐备
        assert out["workspace"]["driver"] == "docker"
        assert out["workspace"]["id"] == handle_id(EXEC_ID, WS_KEY)


@pytest.fixture(autouse=True)
def _vol_trace_debug():
    """临时诊断：记录 driver 的每条 docker 命令时间线（P3 调试期暂存）。"""
    import time as _t
    log = open("/tmp/vol-debug.log", "a")
    driver = sb.get_driver("docker")
    orig = driver._run

    def traced(argv, timeout, cwd=None):
        log.write(f"{_t.strftime('%H:%M:%S')} {' '.join(map(str, argv))}\n")
        log.flush()
        if cwd is not None:
            return orig(argv, timeout, cwd=cwd)
        return orig(argv, timeout)

    driver._run = traced
    yield
    driver._run = orig
    log.close()
