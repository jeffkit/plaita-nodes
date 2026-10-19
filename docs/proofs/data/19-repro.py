"""pn#19 探针：消费方提前关闭 recursive_stream_turn 是否留下「无锁存活的 agent」。

桩 agent = ``sh -c 'echo $$ > pidfile; echo started; sleep 30'``；拿到首行后
``gen.close()``（等价于 SSE 断连）再观测：

- ``agent_alive_after_close``：close 后 agent 进程组是否仍存活；
- ``lock_exists_after_close``：遗言锁是否已被删除；
- ``preflight``：同 workspace/run_key 的下一次开工探测结果。

修复前（fail-open）：``alive=True`` 且 ``lock=False`` → preflight 返回
``clean`` 放行 → 同 workspace 无锁并发写（窗口 = 剩余 timeout_secs，最长
1800s）。修复后：close 路径先 SIGKILL 整组并确认终态，再清锁（``alive=False``）；
若无法确认终态则保留锁，preflight 报错拒绝开工。

用法::

    plaita-nodes/.venv/bin/python docs/proofs/data/19-repro.py

退出码 0 = 不变式成立；1 = 复现「进程存活 + 锁已删」。
"""
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from agentproc.run_lock import _pid_alive, lock_path_for  # noqa: E402

from plaita_nodes.agent_run import (  # noqa: E402
    _run_lock_key,
    preflight_workspace,
    recursive_stream_turn,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pn19-") as tmp:
        root = Path(tmp)
        home = root / "home"
        workspace = root / "workspace"
        pidfile = root / "agent.pid"
        stub = root / "stub-agent.sh"
        for path in (home, workspace, home / ".flowcast"):
            path.mkdir(parents=True, exist_ok=True)

        stub.write_text(
            "#!/bin/sh\n"
            f"echo $$ > '{pidfile}'\n"
            "echo started\n"
            "sleep 30\n"
        )
        stub.chmod(0o755)
        (home / ".flowcast" / "agents.json").write_text(json.dumps({
            "agents": {
                "probe": {
                    "executor": "recursive",
                    "env": {"RECURSIVE_BIN": str(stub)},
                },
            },
        }), encoding="utf-8")

        os.environ["HOME"] = str(home)
        os.environ["AGENTPROC_RUN_LOCK_DIR"] = str(root / "run-locks")
        run_key = "pn19-probe"
        lock_path = lock_path_for(
            _run_lock_key(os.path.abspath(str(workspace)), run_key))

        gen = recursive_stream_turn(
            "hi", workspace=str(workspace), profile="probe",
            timeout_secs=60, run_key=run_key)
        try:
            first = next(gen)
            agent_pid = int(pidfile.read_text().strip())
        finally:
            gen.close()
        time.sleep(0.2)  # 让孤儿存活的观测窗口与 issue 探针一致

        alive = _pid_alive(agent_pid)
        lock_exists = lock_path.exists()
        result = {
            "first_event": first,
            "agent_pid": agent_pid,
            "agent_alive_after_close": alive,
            "lock_exists_after_close": lock_exists,
            "lock_path": str(lock_path),
        }
        try:
            result["preflight"] = preflight_workspace(
                str(workspace), run_key=run_key)
        except Exception as exc:  # noqa: BLE001 — 探针记录拒绝形态
            result["preflight_error"] = f"{type(exc).__name__}: {exc}"
        result["violation"] = bool(alive and not lock_exists)

        if alive:  # 探针自清理（含旧实现的孤儿进程组）
            try:
                os.killpg(agent_pid, signal.SIGKILL)
            except OSError:
                pass
        lock_path.unlink(missing_ok=True)

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["violation"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
