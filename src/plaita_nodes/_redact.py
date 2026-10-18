"""日志安全渲染 —— 照 plaita#151 纪律在本仓的同类最小实现。

plaita 主仓的唯一渲染路径是 ``plaita/core/url_redaction._scrub_url_text`` /
``_scrub_exception_text``。本仓（AGENTS.md）只允许依赖 plaita 的公开窄接口，
不能 import 私有模块，因此按同一口径保留一份最小实现：

* :func:`_scrub_url_text` —— 任意文本里的绝对 URL 只留
  ``scheme://host[:port]``，path 的每一段与整条 query/fragment 一律 ``***``；
  非 http(s) scheme / 畸形 URL / 无法编码的串退化为安全常量；
* :func:`_scrub_exception_text` —— ``str(exc)`` **与整条 traceback** 一起渲染
  再清洗。调用方以纯文本参数记录、不再传 ``exc_info``：``Formatter`` 会在
  logger 调用之后用 ``exc_info`` 重新渲染 traceback（末行就是 ``str(exc)``），
  在调用点传干净参数盖不住那一步（plaita#161 的结论）。

两份都是 **total**：任意输入都返回 ``str``，清洗失败退化为安全常量，
绝不上抛、绝不退回原文。

**边界**：本模块只服务日志/异常文本；访问控制不许用它（黑名单式清洗不足以
约束路径），控制面用 ``_safe_path.safe_component`` 的白名单。
"""
from __future__ import annotations

import logging
import re
import traceback
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

#: 清洗失败 / 无法安全渲染时的常量——绝不退回原始串。
UNPRINTABLE = "<unprintable>"

#: 掩码占位（与 plaita url_redaction 的 ``PLACEHOLDER`` 同一语义）。
PLACEHOLDER = "***"

_SAFE_SCHEMES = frozenset({"http", "https"})

#: 绝对 URL。token 边界只认 ASCII 空白（与 plaita#159 r4 同一判据）：
#: ``\s`` 对 str 是 Unicode 感知的，NBSP / 全角空格会把 URL 截断在凭据之前。
_ABSOLUTE_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^ \t\r\n\f\v]+")

#: ``password=…`` / ``token: …`` 这类裸凭据运行（无 URL 形状时的兜底）。
#: 只认**凭据键名**，``code=1002`` / ``port=8080`` 等诊断文本原样保留。
_CREDENTIAL_RUN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|client[_-]?secret|private[_-]?key)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s&,;]+)"
)


def _render_url(token: str) -> str:
    """绝对 URL → ``scheme://host[:port]`` + 形状化 path/query。

    userinfo（``user:pw@``）整段丢弃；path 的每个非空 segment 与整条 query /
    fragment 换成占位。非 http(s) scheme（``postgresql://user:pw@host/db``）与
    畸形串无从判断端点边界 ⇒ 退化为 :data:`UNPRINTABLE`。
    """
    try:
        parts = urlsplit(token)
    except Exception:  # noqa: BLE001 — 畸形串退化为常量，绝不上抛
        # 刻意不回显异常文本：它可能带着刚被清洗掉的原文片段。
        _log.debug("URL 无法解析，退化为安全常量")
        return UNPRINTABLE
    if parts.scheme not in _SAFE_SCHEMES or not parts.netloc:
        return UNPRINTABLE
    host = parts.netloc.rpartition("@")[2]
    if not host:
        return UNPRINTABLE
    rendered = f"{parts.scheme}://{host}" + "/".join(
        PLACEHOLDER if segment else segment for segment in parts.path.split("/")
    )
    if parts.query:
        rendered += f"?{PLACEHOLDER}"
    if parts.fragment:
        rendered += f"#{PLACEHOLDER}"
    try:
        rendered.encode("utf-8")
    except UnicodeEncodeError:
        # 孤立代理字符：任何 utf-8 handler 都会炸 ⇒ 退化常量，绝不原样放出。
        _log.debug("URL 渲染结果无法编码为 UTF-8，退化为安全常量")
        return UNPRINTABLE
    return rendered


def _scrub_url_text(text: object) -> str:
    """清洗任意文本里的 URL / 凭据分量（日志与异常消息的唯一入口）。

    **total**：非字符串先 ``str()``；清洗过程任何异常退化为
    :data:`UNPRINTABLE`；结果必须 UTF-8 可编码。
    """
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:  # noqa: BLE001 — fail-closed，绝不上抛
            _log.debug("待清洗文本无法字符串化，退化为安全常量")
            return UNPRINTABLE
    try:
        scrubbed = _ABSOLUTE_URL_RE.sub(lambda m: _render_url(m.group(0)), text)
        scrubbed = _CREDENTIAL_RUN_RE.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}", scrubbed
        )
    except Exception:  # noqa: BLE001 — 清洗失败不得变成新的异常面
        # 同上：不回显异常文本，只记录「退化」这一事实。
        _log.debug("文本清洗失败，退化为安全常量")
        return UNPRINTABLE
    try:
        scrubbed.encode("utf-8")
    except UnicodeEncodeError:
        _log.debug("清洗结果无法编码为 UTF-8，退化为安全常量")
        return UNPRINTABLE
    return scrubbed


def _scrub_exception_text(exc: object) -> str:
    """异常对象 → 进日志的单份文本（``str(exc)`` + 整条 traceback，统一清洗）。

    调用方以纯文本参数记录（不再传 ``exc_info``），诊断价值（栈帧、异常链）
    保留，URL / 凭据分量按 :func:`_scrub_url_text` 抹掉。
    """
    try:
        text = "".join(
            traceback.format_exception(
                type(exc), exc, getattr(exc, "__traceback__", None)
            )
        )
    except Exception:  # noqa: BLE001 — 渲染失败退回 str()，绝不因此丢日志
        # 刻意不回显异常文本（可能带原文片段），只记「退化」这一事实。
        _log.debug("异常 traceback 渲染失败，退回异常文本")
        text = exc
    return _scrub_url_text(text)
