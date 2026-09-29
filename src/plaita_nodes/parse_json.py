"""ParseJsonNode —— LLM 结构化输出解析（fail-safe 语义内建）。

把「agent 输出一行严格 JSON → 解析 + 校验 + fail-safe 兜底」收敛为一个节点，
替代每个 flow 手写解析 code 节点。解析策略是 issue-pipeline #43 事故换来的
健壮版（含回归测试）：

1. 逐行**从后往前**找「整行就是 JSON 对象」的候选（提示词要求输出一行 JSON，
   最后一行命中最可信）；
2. 退化到「最后一个 ``{`` 到最后一个 ``}``」切片——**不能用第一个 ``{``**：
   正文里一旦出现花括号（#43 的 ``type={}, model={}``），朴素切片会把正文
   和 JSON 粘成一段，必然解析失败 → fail-safe 叫停，把本该放行的输出误杀。

失败语义：``choices`` 校验不过或解析不出 → 返回 ``default``（缺省则大声抛错）
并附 ``parse_ok=False`` + ``parse_error`` 明细；``default.notes`` 为字符串时会
自动追加明细（下游提示词模板可直接引用 notes）。
"""
from __future__ import annotations

import json
from typing import Any, ClassVar, Dict, List, Optional

from plaita import Node


def extract_json_candidates(raw: str) -> List[str]:
    """按健壮度排序的 JSON 候选串（逐行倒序 → rfind 切片）。"""
    cands: List[str] = []
    for line in reversed((raw or "").splitlines()):
        t = line.strip().strip("`").strip()
        if t.startswith("{") and t.endswith("}"):
            cands.append(t)
    lo, lc = raw.rfind("{"), raw.rfind("}")
    if lo != -1 and lc > lo:
        cands.append(raw[lo:lc + 1])
    return cands


class ParseJsonNode(Node):
    """解析 LLM 输出中的 JSON 并做 verdict 白名单校验。

    JSON 字段：
    - ``text``: 原始输出（支持表达式）
    - ``choices``: 提供时 ``verdict`` 字段必须取值其中，否则按失败处理
    - ``default``: 失败兜底 dict；缺省 = 失败时大声抛错
    - ``join_fields``: 列表字段名清单——解析成功后把每个列表字段 f 序列化为
      ``f_str``（``'; '.join``），供提示词模板引用

    输出：解析成功 = dict 字段平铺 + ``parse_ok=True``；失败 = ``{**default,
    parse_ok: False, parse_error: <明细>}``（``notes`` 为字符串时追加明细）。
    """

    node_type: ClassVar[str] = "parse_json"
    node_name: ClassVar[str] = "JSON 解析"

    text: Any = None
    choices: Optional[List[str]] = None
    default: Optional[Dict[str, Any]] = None
    join_fields: Optional[List[str]] = None

    def execute(self, execution: Any) -> dict:
        if self.text is None:
            raise ValueError("parse_json 节点缺少 text 字段")
        raw = str(execution.evaluate(self.text) or "").strip()

        parsed: Optional[dict] = None
        parse_error = ""
        for cand in extract_json_candidates(raw):
            try:
                v = json.loads(cand)
            except Exception:
                continue
            if isinstance(v, dict):
                parsed = v
                break

        if parsed is None:
            return self._fail(f"解析失败: {raw[:150]}")
        if self.choices is not None and parsed.get("verdict") not in self.choices:
            return self._fail(f"输出非法 verdict: {parsed.get('verdict')!r}")

        out = dict(parsed)
        out["parse_ok"] = True
        out["parse_error"] = ""
        for field in self.join_fields or []:
            val = out.get(field)
            if isinstance(val, list):
                out[f"{field}_str"] = "; ".join(str(x) for x in val)
        return out

    def _fail(self, detail: str) -> dict:
        if self.default is None:
            raise ValueError(f"parse_json 解析失败且未提供 default 兜底：{detail}")
        out = dict(self.default)
        out["parse_ok"] = False
        out["parse_error"] = detail
        if isinstance(out.get("notes"), str) and out["notes"]:
            out["notes"] = f"{out['notes']}；{detail}"
        elif isinstance(out.get("notes"), str):
            out["notes"] = detail
        return out
