"""pn#18 回归：capture / gate 非 default 租户在 spawn 前 fail-closed。

缺陷（与 plaita 主仓 A42-06 / #256 同族）：capture / gate 在 worker 宿主上以
同一 OS 用户执行命令，子进程不带租户上下文时 ``plaita.credentials`` 回退
default，按 cwd 解出 default 凭据；即使显式传租户，命令内
``set_current_tenant("default")`` 或直接读文件也绕得开——同一文件系统 +
所有租户共用一把解密密钥，子进程不是凭据隔离边界。

契约：
- 非 default 租户：``Popen`` 之前抛 :class:`TenantContextPropagationError`
  （消息含 ``PLAITA_SUBPROCESS_TENANT_ISOLATION_UNAVAILABLE`` 与 docker
  指引），命令零执行机会；
- default 租户：既有语义不变（命令照跑，cwd 下的 default 凭据可读）。

负控（回退生产改动）时，本文件 ``test_tenant_a_*_refused_before_command``
会精确红：命令被执行且打印 ``DEFAULT-ONLY-SECRET``。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from fake_exec import FakeExecution
from plaita.tenant_context import reset_current_tenant, set_current_tenant
from plaita_nodes._tenant_guard import (
    TENANT_ISOLATION_MARKER,
    TenantContextPropagationError,
)
from plaita_nodes.capture import CaptureNode
from plaita_nodes.gate import GateNode

DEFAULT_SECRET = "DEFAULT-ONLY-SECRET"


def _write_store(path: Path, key: bytes, entries: dict) -> None:
    store = {
        name: {
            "type": "api",
            "data": Fernet(key).encrypt(json.dumps(data).encode()).decode(),
        }
        for name, data in entries.items()
    }
    path.write_text(json.dumps(store), encoding="utf-8")


@pytest.fixture()
def deployment_dir(tmp_path, monkeypatch):
    """worker 部署目录：cwd 同时有 default 凭据与密钥（真实布局）。

    子进程 env 白名单不含 ``PLAITA_CREDENTIALS_FILE`` /
    ``PLAITA_CREDENTIALS_KEY_FILE``，但命令以 cwd 为工作目录，按默认文件名
    仍能解出 default 凭据——这正是 pn#18 的泄露面。
    """
    key = Fernet.generate_key()
    (tmp_path / ".plaita-credentials.key").write_bytes(key)
    _write_store(tmp_path / ".plaita-credentials.json", key, {
        "default-only": {"token": DEFAULT_SECRET},
    })
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "PLAITA_CREDENTIALS_FILE", str(tmp_path / ".plaita-credentials.json"))
    monkeypatch.setenv(
        "PLAITA_CREDENTIALS_KEY_FILE", str(tmp_path / ".plaita-credentials.key"))
    return tmp_path


def _probe_command(marker: Path) -> list[str]:
    """读 default 凭据；执行前先落 marker，证明命令确实跑过。"""
    code = (
        "import json\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('COMMAND-RAN', encoding='utf-8')\n"
        "from plaita.credentials import get_credential\n"
        f"print(json.dumps({{'token': get_credential('default-only')['token']}}))\n"
    )
    return [sys.executable, "-c", code]


def _node(kind: str, command: list[str], cwd: Path):
    if kind == "capture":
        return CaptureNode(id="c", command=command, cwd=str(cwd))
    return GateNode(id="g", command=command, gate_name="tenant", cwd=str(cwd))


@pytest.fixture()
def tenant_a():
    token = set_current_tenant("tenant-a")
    yield "tenant-a"
    reset_current_tenant(token)


class TestTenantARefusedBeforeSpawn:
    @pytest.mark.parametrize("kind", ["capture", "gate"])
    def test_tenant_a_node_refused_before_command(
            self, kind, deployment_dir, tenant_a):
        marker = deployment_dir / f"{kind}-ran.marker"
        node = _node(kind, _probe_command(marker), deployment_dir)

        with pytest.raises(TenantContextPropagationError) as exc:
            node.execute(FakeExecution())

        message = str(exc.value)
        assert TENANT_ISOLATION_MARKER in message
        assert "tenant-a" in message
        assert "docker" in message
        assert DEFAULT_SECRET not in message
        assert not marker.exists(), "非 default 租户的命令不得执行"

    @pytest.mark.parametrize("kind", ["capture", "gate"])
    def test_tenant_a_never_calls_popen(self, kind, deployment_dir, tenant_a,
                                        monkeypatch):
        import plaita_nodes.capture as capture_mod
        import plaita_nodes.gate as gate_mod

        module = capture_mod if kind == "capture" else gate_mod
        with monkeypatch.context() as m:
            m.setattr(
                module.subprocess, "Popen",
                lambda *a, **k: pytest.fail("Popen must not be called"),
            )
            with pytest.raises(TenantContextPropagationError):
                _node(kind, [sys.executable, "-c", "print('x')"],
                      deployment_dir).execute(FakeExecution())


class TestDefaultTenantUnchanged:
    @pytest.mark.parametrize("kind", ["capture", "gate"])
    def test_default_tenant_still_runs_and_reads_default_credentials(
            self, kind, deployment_dir):
        """兼容正控：default 租户命令照跑，cwd 下 default 凭据语义不变。

        同一探针在非 default 租户下被拒（上一组用例）；default 下必须
        仍能跑通——证明闸门没有把单租户路径一并关死。
        """
        marker = deployment_dir / f"default-{kind}.marker"
        out = _node(kind, _probe_command(marker), deployment_dir).execute(
            FakeExecution())

        assert marker.exists()
        assert json.loads(out["stdout"])["token"] == DEFAULT_SECRET
        if kind == "capture":
            assert out["ok"] is True
        else:
            assert out["passed"] is True

    def test_dry_run_not_refused(self, deployment_dir, tenant_a):
        """dry-run 不 spawn、不读凭据，保持既有 fake 返回（同 agent 直跑）。"""
        node = CaptureNode(id="c", command=_probe_command(deployment_dir / "m"),
                           cwd=str(deployment_dir), dry_run=True)
        out = node.execute(FakeExecution())
        assert out["dry_run"] is True
        assert not (deployment_dir / "m").exists()
