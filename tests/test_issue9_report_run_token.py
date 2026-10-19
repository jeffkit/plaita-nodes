"""#9：report 缺省 token 的 run 身份——跨 run 隔离 + map 子流程共享。

跑真实 plaita 引擎：根 run 在 ``setup_flow`` 注入 ``run_token``，map 子流程
经 parent 回落读同一个值。两个 run 必须落到不同文件，且同一 run 内 map 的
多条写入由主流程的 read 聚合。
"""
from __future__ import annotations

from pathlib import Path

from plaita.core.executor import FlowExecution
from plaita.core.flow import Flow

import plaita_nodes

plaita_nodes.register_all()


def _flow(base_dir: Path) -> dict:
    return {
        "flow_id": "report-run-token",
        "inputType": {"dataType": "object"},
        "nodes": [
            {"id": "s", "type": "start", "next": "m"},
            {
                "id": "m", "type": "map",
                "collection": ["a", "b", "c"],
                "child_flow": {
                    "flow_id": "map-body",
                    "nodes": [
                        {"id": "cs", "type": "start", "next": "w"},
                        {"id": "w", "type": "report", "action": "write",
                         "base_dir": str(base_dir),
                         "entry": {"item": "$INPUT.item"},
                         "next": "ce"},
                        {"id": "ce", "type": "end", "resultType": "success",
                         "output": "$NODE.w"},
                    ],
                },
                "next": "r",
            },
            {"id": "r", "type": "report", "action": "read",
             "base_dir": str(base_dir), "next": "e"},
            {"id": "e", "type": "end", "resultType": "success", "output": "$NODE.r"},
        ],
    }


def test_report_default_token_is_per_run_and_shared_with_map(tmp_path):
    flow = Flow.model_validate(_flow(tmp_path))
    expected = {"entries": [{"item": "a"}, {"item": "b"}, {"item": "c"}]}

    first = FlowExecution().run_compatible(flow, False)
    second = FlowExecution().run_compatible(flow, False)

    assert first == expected
    assert second == expected

    files = sorted(tmp_path.glob("*.jsonl"))
    assert len(files) == 2, [p.name for p in files]
    assert len({p.stem for p in files}) == 2
    assert not (tmp_path / "default.jsonl").exists()
    assert [len(p.read_text(encoding="utf-8").splitlines()) for p in files] == [3, 3]
