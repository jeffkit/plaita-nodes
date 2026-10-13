"""AGS 可达性探针（conformance opt-in 用，**不创建沙箱**）。

用途：`test_driver_conformance.py` 在把 `ags` 纳入 driver 参数化之前，
先确认 AGS 端点**真的可达**——否则会在"凭据错/网络不通"的情况下
跑出一堆看不出原因的失败。

## 为什么不直接用 e2b SDK 做探针

2026-10-13 实测（见大仓 `docs/AGS_SWEEP_TRANSPORT_ISSUE.md`）：
本机 **python 的全部 HTTP 客户端（httpx / requests / urllib）对 AGS 主机确定性失败**
（`SSL: UNEXPECTED_EOF_WHILE_READING`），**只有 `curl` 稳定可用**：
```
curl                                   -> 401（链路通）
python 裸 ssl 握手                      -> 成功，证书链有效
httpx / requests / urllib              -> 确定性失败
```
e2b SDK 走 httpx ⇒ 用它做探针会**把传输问题误报成"AGS 不可用"**。
故这里**优先用 curl 子进程**，与 issue-keeper 的 `ags-orphan-sweep`
兜底路径保持同一策略（那边也在 httpx 失败后退到 curl）。
"""

from __future__ import annotations

import json
import os
import subprocess

DEFAULT_DOMAIN = "ap-guangzhou.tencentags.com"
PROBE_PATH = "/v2/sandboxes"          # 只读列表：不创建任何资源


def _clean_env() -> dict:
    """剔除代理：实测带 proxy 时连 curl 也失败（401 → 000）。"""
    return {k: v for k, v in os.environ.items()
            if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}


def _probe_with_curl(domain: str, api_key: str, timeout: float = 20.0) -> bool:
    cmd = [
        "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
        "-H", f"X-API-KEY: {api_key}",
        "--max-time", str(int(timeout)),
        f"https://api.{domain}{PROBE_PATH}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, env=_clean_env(),
                              timeout=timeout + 10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    code = proc.stdout.decode("utf-8", "replace").strip()
    # 200 = 通且有权限；401/403 = **链路通**（说明网络/TLS 没问题，只是凭据/权限）
    # 两者都算"端点可达"——凭据问题由 conformance 自身报错，不该在探针里吞掉。
    return code in ("200", "401", "403")


def _probe_with_urllib(domain: str, api_key: str, timeout: float = 20.0) -> bool:
    """curl 不可用时的兜底（虽然实测本机 urllib 对 AGS 会失败，但在别的机器上可用）。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"https://api.{domain}{PROBE_PATH}", headers={"X-API-KEY": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)
    except Exception:  # noqa: BLE001
        return False


def ags_reachable() -> bool:
    """AGS 端点是否可达（**只读探测，不创建资源**）。

    判定口径：能收到 HTTP 响应即算可达（200/401/403 都算）——
    因为本探针的目的是排除"网络/TLS 层不通"，**不是**校验凭据正确性
    （凭据错误应由 conformance 的 ensure 步骤以明确错误暴露）。
    """
    api_key = os.environ.get("E2B_API_KEY", "")
    if not api_key:
        return False
    domain = os.environ.get("E2B_DOMAIN", "").strip() or DEFAULT_DOMAIN
    if _probe_with_curl(domain, api_key):
        return True
    return _probe_with_urllib(domain, api_key)


if __name__ == "__main__":  # pragma: no cover - 手工排查入口
    print(json.dumps({"ags_reachable": ags_reachable()}, ensure_ascii=False))
