"""spawn 节点的子进程 env 隔离 + 输出截断（#5）。

验收对齐：
1. gate / capture 不再 ``os.environ.copy()``——宿主凭据（``PLAITA_CREDENTIALS_KEY``
   等）不出现在子进程 env 里，白名单里的宿主变量仍在；
2. capture 输出超阈值时头尾保留并标注 ``…[省略 N 字符]…``；
3. agent 直跑的宿主 env 同样不整份继承（``recursive_stream_turn`` 的 stub 子进程
   看不到宿主凭据，但 agents.json 的 env 仍显式可达）。
"""
from __future__ import annotations

import json
import re
import sys

import pytest

from fake_exec import FakeExecution
from plaita_nodes._subprocess_env import build_subprocess_env
from plaita_nodes.capture import CaptureNode
from plaita_nodes.gate import GateNode

OMIT_RE = re.compile(r"…\[省略 (\d+) 字符\]…")
SECRET_KEYS = ("PLAITA_CREDENTIALS_KEY", "DATABASE_URL", "ANTHROPIC_AUTH_TOKEN")
ENV_DUMP = "import json, os; print(json.dumps(dict(os.environ)))"
STDOUT_CAP = 4000
STDERR_CAP = 2000


def _leak_host_secrets(monkeypatch) -> None:
    for key in SECRET_KEYS:
        monkeypatch.setenv(key, f"host-secret-{key}")


def _dump_env_cmd() -> list[str]:
    return [sys.executable, "-c", ENV_DUMP]


def _emit_cmd(tmp_path, stream: str, body: str) -> list[str]:
    script = tmp_path / f"emit_{stream}.py"
    script.write_text(f"import sys\nsys.{stream}.write({body!r})\n", encoding="utf-8")
    return [sys.executable, str(script)]


class TestBuildSubprocessEnv:
    def test_host_secrets_dropped_allowlist_kept(self, monkeypatch):
        _leak_host_secrets(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        env = build_subprocess_env({"EXTRA_MARKER": "yes", "NUMERIC": 7})
        assert not set(SECRET_KEYS) & set(env)
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["EXTRA_MARKER"] == "yes"
        assert env["NUMERIC"] == "7"  # 子进程 env 只能是字符串

    def test_module_extra_is_deployment_escape_hatch(self, monkeypatch):
        """部署启动脚本经 SUBPROCESS_ENV_EXTRA 放行额外宿主变量（如代理设置）。"""
        from plaita_nodes._subprocess_env import SUBPROCESS_ENV_EXTRA

        monkeypatch.setitem(SUBPROCESS_ENV_EXTRA, "DEPLOY_OP", "1")
        assert build_subprocess_env()["DEPLOY_OP"] == "1"

    def test_prefers_plaita_public_layer_when_present(self):
        """公共层在场时必须用它（本仓兜底只在 plaita#30 未合并的 checkout 上生效）。"""
        import plaita_nodes._subprocess_env as shim

        try:
            import plaita.subprocess_env as public
        except ImportError:
            pytest.skip("plaita checkout 早于 #30：走本仓兜底实现")
        assert shim.build_subprocess_env is public.build_subprocess_env
        assert shim.clip_output is public.clip_output


class TestGateChildEnv:
    def test_host_secrets_do_not_reach_child(self, monkeypatch, tmp_path):
        _leak_host_secrets(monkeypatch)
        node = GateNode(id="g", command=_dump_env_cmd(), gate_name="env", cwd=str(tmp_path))
        out = node.execute(FakeExecution())
        assert out["passed"] is True
        child_env = json.loads(out["stdout"])
        assert not set(SECRET_KEYS) & set(child_env)
        assert child_env["PATH"]  # 白名单变量仍在：命令照常跑得起来


class TestCaptureChildEnv:
    def test_host_secrets_do_not_reach_child(self, monkeypatch, tmp_path):
        _leak_host_secrets(monkeypatch)
        node = CaptureNode(id="c", command=_dump_env_cmd(), cwd=str(tmp_path))
        out = node.execute(FakeExecution())
        assert out["ok"] is True
        child_env = json.loads(out["stdout"])
        assert not set(SECRET_KEYS) & set(child_env)
        assert child_env["PATH"]

    def test_explicit_env_field_still_reaches_child(self, monkeypatch, tmp_path):
        _leak_host_secrets(monkeypatch)
        node = CaptureNode(id="c", command=_dump_env_cmd(), cwd=str(tmp_path),
                           env={"MY_MARKER": "42"})
        out = node.execute(FakeExecution())
        child_env = json.loads(out["stdout"])
        assert child_env["MY_MARKER"] == "42"
        assert not set(SECRET_KEYS) & set(child_env)


class TestCaptureOutputClip:
    def test_stdout_head_tail_kept_and_omission_marked(self, tmp_path):
        body = "H" * 6000 + "TAIL-DIAGNOSTIC"
        node = CaptureNode(id="c", command=_emit_cmd(tmp_path, "stdout", body), cwd=str(tmp_path))
        out = node.execute(FakeExecution())
        assert out["stdout"] != body
        match = OMIT_RE.search(out["stdout"])
        assert match and int(match.group(1)) == len(body) - STDOUT_CAP
        assert out["stdout"].startswith(body[:STDOUT_CAP // 4])
        assert out["stdout"].endswith(body[-(STDOUT_CAP - STDOUT_CAP // 4):])
        assert "TAIL-DIAGNOSTIC" in out["stdout"]  # 尾部诊断可读

    def test_stderr_head_tail_kept_and_omission_marked(self, tmp_path):
        body = "E" * 3000 + "traceback-tail"
        node = CaptureNode(id="c", command=_emit_cmd(tmp_path, "stderr", body), cwd=str(tmp_path))
        out = node.execute(FakeExecution())
        match = OMIT_RE.search(out["stderr"])
        assert match and int(match.group(1)) == len(body) - STDERR_CAP
        assert "traceback-tail" in out["stderr"]

    @pytest.mark.parametrize("stream", ["stdout", "stderr"])
    def test_small_output_unchanged(self, tmp_path, stream):
        node = CaptureNode(id="c", command=_emit_cmd(tmp_path, stream, "short\n"), cwd=str(tmp_path))
        out = node.execute(FakeExecution())
        assert out[stream] == "short\n"
        assert not OMIT_RE.search(out[stream])


class TestAgentRunDirectPathChildEnv:
    def test_direct_run_does_not_inherit_host_env(self, agent_config_repo,
                                                  register_test_echo_executor, monkeypatch):
        """repo 直跑经 agentproc 组 env（infra 白名单 + profile env + extra）。"""
        from plaita_nodes.agent_run import AgentRunNode

        monkeypatch.setenv("PLAITA_CREDENTIALS_KEY", "host-secret")
        monkeypatch.setenv("ECHO_MARKER", "host-only")
        node = AgentRunNode(id="a", agent="echo", prompt="hi", repo=str(agent_config_repo))
        out = node.execute(FakeExecution())
        assert "msg=hi" in out["text"]
        assert "host-only" not in out["text"]


class TestAgentStreamTurnChildEnv:
    def test_stream_turn_child_env_whitelisted(self, agent_config_repo, tmp_path, monkeypatch):
        from test_agent_run_stream import _drain_generator, _register_stub_agent
        from plaita_nodes.agent_run import recursive_stream_turn

        stub = agent_config_repo / "stub-env.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "/usr/bin/env\n"
            "printf '%s\\n' '{\"result\": \"ok\", \"is_error\": false}'\n"
        )
        stub.chmod(0o755)
        _register_stub_agent(agent_config_repo, stub, "rec-env")
        _leak_host_secrets(monkeypatch)

        events = _drain_generator(recursive_stream_turn(
            "hi", workspace=str(tmp_path), profile="rec-env", timeout_secs=30))
        assert events[-1]["type"] == "done" and events[-1]["ok"] is True
        lines = [e["text"] for e in events if e["type"] == "line"]
        child_keys = {line.split("=", 1)[0] for line in lines if "=" in line}
        assert not set(SECRET_KEYS) & child_keys
        assert "RECURSIVE_BIN" in child_keys  # agents.json 的 env 是显式 extra
