"""ags_exec —— AGS 远程执行的本地代理（agentproc runner 仍是唯一执行者）。

由 ``AgsDriver.wrap_argv`` / ``wrap_argv_from_env`` 生成调用：
agentproc runner 在宿主 spawn 本模块 → 本模块经 E2B 数据面把 argv 转发到
沙箱内执行 → 流式回传 stdout/stderr → 退出码透传（非零退出以 sys.exit 传出，
agentproc 侧与本地 agent 进程逐字节同形）。

用法（内部约定，不建议手工调用）：

    python -m plaita_nodes.ags_exec --instance <sandbox_id> \
        [--envfile <path>] [--cwd <dir>] [--timeout <secs>] -- <argv...>

- ``--envfile``：KEY=VALUE 行（0600 即焚），作为远端执行的 envs；
- stdin 非 tty 时把本地 stdin 全部读入并经数据面 stdin 通道转发（agentproc
  的 ``--stdin`` 管道形态）；
- 宿主环境需 ``E2B_DOMAIN`` / ``E2B_API_KEY``。
"""
from __future__ import annotations

import argparse
import sys


def _read_envfile(path: str) -> dict:
    envs = {}
    if not path:
        return envs
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                envs[key.strip()] = value
    except OSError:
        return {}
    return envs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="plaita-nodes.ags_exec", add_help=False)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--envfile", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--forward-stdin", action="store_true",
                        help="把本地 stdin 转发到远端（envd >= 支持 close_stdin 的版本才可用；"
                             "recursive-direct 等消息在 argv 的执行器无需开启）")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    remote_argv = [a for a in args.argv if a != "--"]
    if not remote_argv:
        print("ags_exec: 缺少远端命令", file=sys.stderr)
        return 2

    from .sandbox_ags import AgsClient, AgsError

    envs = _read_envfile(args.envfile)
    # stdin 排空纪律：agentproc wire 0.4 会把 turn 写进子进程 stdin，但
    # recursive-direct 等执行器的消息经 build_args 落在 argv——默认**读走丢弃**
    # （不转发，避免远端阻塞；envd 0.2.10 不支持 close_stdin，转发会炸）。
    # 需要 stdin 的执行器显式 --forward-stdin（并要求镜像 envd 版本支持）。
    stdin_data = None
    if not sys.stdin.isatty():
        try:
            data = sys.stdin.buffer.read()
            stdin_data = data if (data and args.forward_stdin) else None
        except Exception:  # noqa: BLE001 - stdin 不可读按无输入
            stdin_data = None

    client = AgsClient()

    def _out(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _err(text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    try:
        code, _out_text, _err_text = client.exec_argv(
            args.instance, remote_argv,
            envs=envs or None, cwd=args.cwd or None,
            timeout=args.timeout,
            on_stdout=_out, on_stderr=_err,
            stdin_data=stdin_data,
        )
    except AgsError as exc:
        print(f"ags_exec: {exc}", file=sys.stderr)
        return 3
    return int(code)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
