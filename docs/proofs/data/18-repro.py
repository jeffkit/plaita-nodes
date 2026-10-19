"""pn#18 探针：tenant-a 父进程下 capture / gate 子进程能否解出 default 凭据。

复现泄露面：子进程 env 白名单不含凭据路径，但命令以 cwd 为工作目录，
``plaita.credentials`` 在无租户上下文时按默认文件名回退 default 租户。

修复前：capture / gate 都打印 ``DEFAULT-ONLY-SECRET``（命令实际执行）；
修复后：spawn 前抛 ``TenantContextPropagationError``，命令零执行。

用法::

    plaita/.venv/bin/python docs/proofs/data/18-repro.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from cryptography.fernet import Fernet  # noqa: E402

from plaita.tenant_context import reset_current_tenant, set_current_tenant  # noqa: E402
from plaita_nodes.capture import CaptureNode  # noqa: E402
from plaita_nodes.gate import GateNode  # noqa: E402

DEFAULT_SECRET = "DEFAULT-ONLY-SECRET"


class _Exec:
    express_prefix = "$"

    def evaluate(self, value):
        return value

    def get_global_variable(self, key, default=None):
        return default


def _write_store(path: Path, key: bytes) -> None:
    entry = {"default-only": {
        "type": "api",
        "data": Fernet(key).encrypt(json.dumps({"token": DEFAULT_SECRET}).encode()).decode(),
    }}
    path.write_text(json.dumps(entry), encoding="utf-8")


def _probe(marker: Path) -> list[str]:
    code = (
        "import json\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('COMMAND-RAN', encoding='utf-8')\n"
        "from plaita.credentials import get_credential\n"
        "print(json.dumps({'token': get_credential('default-only')['token']}))\n"
    )
    return [sys.executable, "-c", code]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pn18-") as tmp:
        root = Path(tmp)
        key = Fernet.generate_key()
        (root / ".plaita-credentials.key").write_bytes(key)
        _write_store(root / ".plaita-credentials.json", key)
        os.chdir(root)
        os.environ["PLAITA_CREDENTIALS_FILE"] = str(root / ".plaita-credentials.json")
        os.environ["PLAITA_CREDENTIALS_KEY_FILE"] = str(root / ".plaita-credentials.key")

        result = {}
        token = set_current_tenant("tenant-a")
        try:
            for kind, node in (
                ("capture", CaptureNode(id="c", command=_probe(root / "capture.marker"),
                                        cwd=str(root))),
                ("gate", GateNode(id="g", command=_probe(root / "gate.marker"),
                                  gate_name="tenant", cwd=str(root))),
            ):
                marker = root / f"{kind}.marker"
                try:
                    out = node.execute(_Exec())
                except Exception as exc:  # noqa: BLE001 — 探针记录具名拒绝
                    result[kind] = {
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "command_ran": marker.exists(),
                    }
                else:
                    result[kind] = {
                        "stdout": out["stdout"].strip(),
                        "command_ran": marker.exists(),
                    }
        finally:
            reset_current_tenant(token)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    leaked = any(DEFAULT_SECRET in json.dumps(item) for item in result.values())
    return 1 if leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
