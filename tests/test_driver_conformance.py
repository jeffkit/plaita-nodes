"""driver conformance 实跑套件（设计 §10 的 driver 均质子集；P2 落地件）。

参数化跑 docker / krunvm（环境不可用自动 skip）：

- 契约 1：ensure 幂等 attach（二次 ensure 不重建，现场状态保留）；
- 契约 2：handle 可重派生（新 driver 实例按 (execution_id, ws_key) 重连）；
- 契约 3：三段击杀——沙箱内墙钟到点即杀；宿主客户端被 SIGKILL 后无孤儿；
- 契约 6（lite）：``git()`` 数据面可用（docker 在卷内执行 / krunvm 在宿主数据目录）；
- 契约 8：未 pin 的 image → ensure 必须拒绝。

契约 4（脱敏）为 driver 无关机制（test_sandbox::TestRedactor）；5（wire 等价）
属 e2b 分叉循环（P3）；7（dry_run 零调用）在 test_agent_run_workspace；
9（release→重建 == 最后 push ref）在两份 E2E 的 git 数据面用例。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import plaita_nodes.sandbox as sb
from plaita_nodes.sandbox import (
    SandboxPinError,
    WorkspaceSpec,
    resource_name,
)

EXEC_ID = "conf-exec-0001"
WS_KEY = "main"
DRIVER_IMAGE = {
    "docker": "alpine/git:latest",
    "krunvm": "alpine:latest",
    # ags 用的是**沙箱模板名**（不是 OCI 镜像）：AGS 侧 ensure 需要 template/image，
    # 取自本机注册表 `~/.plaita/sandboxes.json` 的 ags spec（生产实测值）。
    "ags": os.environ.get("PLAITA_AGS_TEMPLATE", "ags-agent-stack"),
}


def _cli_ok(argv: list) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _driver_available(name: str) -> bool:
    if name == "docker":
        return _cli_ok(["docker", "info", "--format", "{{.ServerVersion}}"])
    if name == "krunvm":
        return _cli_ok(["krunvm", "list"])
    if name == "ags":
        # ⚠ **必须显式 opt-in**：AGS 是**按量计费**的云沙箱，且 conformance 会真实
        # 创建/销毁实例。默认不跑，避免"随手 pytest 就花钱"。
        # 开法：PLAITA_CONFORMANCE_AGS=1（并确保 E2B_API_KEY / E2B_DOMAIN 已配）。
        if os.environ.get("PLAITA_CONFORMANCE_AGS", "") != "1":
            return False
        if not os.environ.get("E2B_API_KEY"):
            return False
        try:
            # tests/ 不是包（无 __init__.py），故用绝对 import；
            # pytest 会把 rootdir/tests 加入 sys.path（见 conftest.py 形态）。
            from _ags_probe import ags_reachable
        except Exception:  # noqa: BLE001 —— 探针不可用时保守视为不可用
            return False
        return ags_reachable()
    return False


def _pull(image: str) -> bool:
    if "@" in image:
        return True
    # AGS 的 DRIVER_IMAGE 是**沙箱模板名**（不是 OCI 镜像）⇒ 没有 `docker pull`
    # 这一步，模板由 AGS 侧管理，直接放行。
    # （2026-10-13 实测：此前 `cmd=None` 会传给 subprocess ⇒
    #   `TypeError: 'NoneType' object is not iterable`，6 个用例全 ERROR。）
    if image == DRIVER_IMAGE.get("ags"):
        return True
    cmd = ["docker", "pull", image] if image.endswith(":latest") or ":" in image else None
    if cmd is None:
        return True
    try:
        return subprocess.run(cmd, capture_output=True, timeout=600).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# AGS 明确**不放进默认列表**（按量计费）；只有显式 opt-in 时 `_driver_available("ags")`
# 才会返回 True，从而被下面的推导式纳入。
ALL_DRIVERS = [d for d in ("docker", "krunvm", "ags") if _driver_available(d)]

pytestmark = pytest.mark.skipif(not ALL_DRIVERS,
                                reason="无可用沙箱 CLI（docker/krunvm）"
                                       "（ags 需 PLAITA_CONFORMANCE_AGS=1 显式开启）")


def _cleanup_quiet(argv: list) -> None:
    """清理命令缺失（未安装该 driver CLI）时静默跳过，不炸 teardown。"""
    try:
        subprocess.run(argv, capture_output=True)
    except OSError:
        pass


def _ags_cleanup() -> None:
    """清理 AGS 侧可能残留的沙箱（**按量计费 ⇒ 必须清**）。

    为什么单独写：`_cleanup_resources` 原先只清 docker/krunvm 的**本地**资源
    （`docker rm` / `krunvm delete`），**AGS 是远端云沙箱，没有本地残留可清** ——
    若不做这一步，开 `PLAITA_CONFORMANCE_AGS=1` 跑一次就会**留下真实计费实例**。

    做法：走 driver 自己的 `ensure/release` 语义 —— 用与该用例相同的
    `WorkspaceSpec`（同 `name`/`template`）拿到同一 handle，再 `release`。
    handle 由 `(execution_id, ws_key)` **确定性派生**（`sandbox.handle_id`），
    故无需保存状态即可重建。
    """
    if "ags" not in ALL_DRIVERS:
        return
    try:
        drv = sb.get_driver("ags")
        spec = _spec("ags")
        handle = drv.ensure(spec, EXEC_ID, WS_KEY)
        drv.release(handle, keep_data=False)
    except Exception as exc:  # noqa: BLE001 —— 清理失败必须**响亮**但不掩蔽测试结论
        print(f"[conformance] ⚠ AGS 清理失败，请人工核查是否残留计费实例：{exc!r}")


@pytest.fixture(autouse=True)
def _cleanup_resources():
    yield
    vm = resource_name(EXEC_ID, WS_KEY, "vm").lower()
    _cleanup_quiet(["docker", "rm", "-f", "--force",
                    resource_name(EXEC_ID, WS_KEY, "ctr")])
    _cleanup_quiet(["docker", "volume", "rm", "--force",
                    resource_name(EXEC_ID, WS_KEY, "vol")])
    _cleanup_quiet(["krunvm", "delete", vm])
    _ags_cleanup()


@pytest.fixture(params=ALL_DRIVERS, ids=ALL_DRIVERS)
def driver(request, tmp_path, monkeypatch):
    """每个 driver 一套隔离 HOME（注册表/租约/数据根）+ 已拉取镜像。"""
    name = request.param
    if not _pull(DRIVER_IMAGE[name]):
        pytest.skip(f"镜像拉取失败：{DRIVER_IMAGE[name]}")
    monkeypatch.setattr(sb.Path, "home", lambda: tmp_path)
    (tmp_path / ".plaita").mkdir(parents=True, exist_ok=True)
    return sb.get_driver(name)


def _spec(name: str, **kw):
    base = {"name": WS_KEY, "driver": name, "image": DRIVER_IMAGE[name],
            "resources": {"timeout": 6}}
    base.update(kw)
    return WorkspaceSpec(**base)


def _unpinned_spec(name: str):
    # docker：镜像带 tag；krunvm：allow_unpinned 需为 False 才会触发拒绝
    return _spec(name, image=DRIVER_IMAGE[name])


class TestConformance:
    def test_c8_unpinned_rejected(self, driver):
        if driver.name == "krunvm":
            pytest.skip("krunvm E2E 用 allow_unpinned 注册表；pin 拒绝由 docker 档覆盖同一实现")
        if driver.name == "ags":
            # AGS 的 DRIVER_IMAGE 是**沙箱模板名**（不是 OCI 引用）⇒ 没有 digest 可 pin，
            # 该 driver 不实现 `SandboxPinError`。这是**能力差异**，不是缺陷：
            # 「未 pin 拒绝」这条约束对本地镜像型 driver 才有意义。
            # ⚠ 记入 docs：AGS 的供应链保证依赖**模板名在 AGS 侧的管理**，本仓不校验。
            pytest.skip("ags 用沙箱模板名而非 OCI 引用 ⇒ 无 digest 可 pin（能力差异，非缺陷）")
        with pytest.raises(SandboxPinError):
            driver.ensure(_unpinned_spec(driver.name), EXEC_ID, WS_KEY)

    def test_c1_ensure_idempotent_attach(self, driver, tmp_path):
        spec = _spec(driver.name, allow_unpinned=True)
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)
        # 现场标记：docker 写进卷 / krunvm 写进数据目录（**宿主侧**）。
        if driver.name == "ags":
            # AGS 是**远端云沙箱**：没有宿主侧 datadir 可写标记。
            # 仍可验证「二次 ensure = attach 同实例」这条**核心**不变量。
            # ⚠ `data["fresh"]` 会由 True 变 False（首建 → 复用），**这正是 attach 的证据**，
            # 所以只能断言 id / container / path 一致，不能整体比较 data。
            handle2 = driver.ensure(spec, EXEC_ID, WS_KEY)
            assert handle2.id == handle.id
            assert handle2.data["container"] == handle.data["container"]
            assert handle2.data["path"] == handle.data["path"]
            assert handle2.data["fresh"] is False, "二次 ensure 应复用而非重建"
            return
        marker_host = Path(handle.data.get("datadir",
                                           "/nonexistent")) / ".conf-marker"
        marker_host.write_text("v1", encoding="utf-8")

        handle2 = driver.ensure(spec, EXEC_ID, WS_KEY)   # 二次 = attach
        assert handle2.id == handle.id
        assert handle2.data["datadir"] == handle.data["datadir"]
        assert (Path(handle2.data["datadir"]) / ".conf-marker").read_text() == "v1"  # 未被重建

    def test_c2_handle_rederivable(self, driver, tmp_path, monkeypatch):
        """上下文丢失后：新 driver 实例按 (execution_id, ws_key) 重连同一资源。"""
        spec = _spec(driver.name, allow_unpinned=True)
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)
        fresh = type(driver)()
        rebuilt = fresh.ensure(spec, EXEC_ID, WS_KEY)
        assert rebuilt.id == handle.id
        assert rebuilt.data.get("volume") == handle.data.get("volume")
        assert rebuilt.data.get("datadir") == handle.data.get("datadir")

    def test_c3_wallclock_kills_sleeper(self, driver, tmp_path):
        """契约 3a：沙箱内 `timeout` 墙钟到点即杀（宿主客户端活着也杀）。"""
        spec = _spec(driver.name, allow_unpinned=True,
                     resources={"timeout": 3})
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)
        envfile = tmp_path / "empty.env"
        envfile.write_text("", encoding="utf-8")
        argv = driver.wrap_argv(handle, ["sleep", "60"], 3, envfile)
        start = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, timeout=60)
        elapsed = time.monotonic() - start
        assert proc.returncode != 0                        # 被 timeout 终结
        assert elapsed < 30                                # 60s 的 sleep 没跑满

    def test_c3_host_kill_no_orphan(self, driver, tmp_path):
        """契约 3b：宿主客户端被 SIGKILL 后，沙箱内进程在墙钟内被终结。"""
        if driver.name != "docker":
            pytest.skip("krunvm 的 VMM 在调用进程内（客户端死=VM 死），按构造无孤儿")
        spec = _spec(driver.name, allow_unpinned=True,
                     resources={"timeout": 6})
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)
        envfile = tmp_path / "empty.env"
        envfile.write_text("", encoding="utf-8")
        argv = driver.wrap_argv(handle, ["sleep", "60"], 6, envfile)
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1.0)
        os.killpg(proc.pid, 9)                             # 宿主客户端被 SIGKILL
        proc.wait(timeout=30)
        ctr = handle.data["container"]
        deadline = time.monotonic() + 45                   # 墙钟 6s + 轮询余量
        while time.monotonic() < deadline:
            listed = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name={ctr}",
                 "--format", "{{.Names}}"], capture_output=True, text=True,
                timeout=60).stdout
            if ctr not in listed:
                break
            time.sleep(1)
        else:
            pytest.fail("宿主客户端死后容器未在墙钟内消失（孤儿）")

    def test_c6_git_dataplane_smoke(self, driver):
        spec = _spec(driver.name, allow_unpinned=True)
        handle = driver.ensure(spec, EXEC_ID, WS_KEY)
        init = driver.git(handle, ["init", "-q"])
        assert init.returncode == 0 or "already exists" in (init.stderr or "")
        log = driver.git(handle, ["log", "--oneline"])
        assert log.returncode != 0                         # 空仓无提交，但 git 在跑
