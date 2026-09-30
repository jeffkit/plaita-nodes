"""RateLimitNode / ReportNode 单测（此前零覆盖）。"""
from __future__ import annotations

from fake_exec import FakeExecution

from plaita_nodes.rate_limit import RateLimitNode
from plaita_nodes.report import ReportNode, read_entries


# ---- rate_limit ----

def test_rate_limit_check_record_clear(tmp_path):
    ex = FakeExecution()
    check = RateLimitNode(id="c", action="check", key="xhs", daily=2, weekly=5,
                          store_dir=str(tmp_path))
    record = RateLimitNode(id="r", action="record", key="xhs",
                           store_dir=str(tmp_path))

    assert check.execute(ex)["allowed"] is True
    record.execute(ex)
    after_one = check.execute(ex)
    assert after_one["today_count"] == 1 and after_one["allowed"] is True

    record.execute(ex)
    after_two = check.execute(ex)
    assert after_two["allowed"] is False and after_two["today_count"] == 2

    RateLimitNode(id="x", action="clear", key="xhs",
                  store_dir=str(tmp_path)).execute(ex)
    assert check.execute(ex)["allowed"] is True


# ---- report ----

def test_report_write_read_roundtrip(tmp_path):
    ex = FakeExecution()
    w = ReportNode(id="w", base_dir=str(tmp_path), token="run-1",
                   entry={"item": "a"})
    assert w.execute(ex) == {"written": True, "entry": {"item": "a"}}
    w2 = ReportNode(id="w2", base_dir=str(tmp_path), token="run-1", entry="plain")
    w2.execute(ex)  # 非 dict entry 包一层 {"value": ...}

    entries = ReportNode(id="r", action="read", base_dir=str(tmp_path),
                         token="run-1").execute(ex)["entries"]
    assert entries == [{"item": "a"}, {"value": "plain"}]
    assert read_entries(str(tmp_path), "run-1") == entries


def test_report_token_falls_back_to_run_token_global(tmp_path):
    ex = FakeExecution({"run_token": "rt-42"})
    ReportNode(id="w", base_dir=str(tmp_path), entry={"k": 1}).execute(ex)
    # token 未填 → 落到 run_token 全局变量指定的文件（map 子流程聚合场景）
    assert read_entries(str(tmp_path), "rt-42") == [{"k": 1}]
