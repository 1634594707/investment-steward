"""真实只读夹具的统计核验；统计不是来源语义或日历核验。"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "youzi"


@pytest.mark.parametrize("side,distinct,duplicate,conflict,reasons", [
    ("buy", 295, 51, 50, 31), ("sell", 295, 47, 45, 32),
])
def test_identity_collisions_use_actual_direction_amount(side, distinct, duplicate, conflict, reasons):
    name = "em_lhb_detailbuy_20260916.json" if side == "buy" else "em_lhb_detailsell_20260916.json"
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    rows = payload["result"]["data"]
    assert len(rows) == 365
    groups = defaultdict(list)
    for row in rows:
        # 禁止使用 BUY_AMT/SELL_AMT/AMOUNT 等不存在的字段，把 None 误计为相等。
        assert side.upper() in row
        key = (row["TRADE_DATE"], row["SECURITY_CODE"], row["OPERATEDEPT_CODE"], side)
        groups[key].append(row)
    assert len(groups) == distinct
    assert sum(len(group) > 1 for group in groups.values()) == duplicate
    assert sum(len({row[side.upper()] for row in group}) > 1 for group in groups.values()) == conflict
    assert sum(len({row["EXPLANATION"] for row in group}) > 1 for group in groups.values()) == reasons


def test_bucket_counts_across_both_reports():
    rows = []
    for name in ("em_lhb_detailbuy_20260916.json", "em_lhb_detailsell_20260916.json"):
        rows.extend(json.loads((FIXTURES / name).read_text(encoding="utf-8"))["result"]["data"])
    counts = Counter(row["OPERATEDEPT_NAME"] for row in rows)
    assert counts["机构专用"] == 146
    assert counts["沪股通专用"] == 20
    assert counts["深股通专用"] == 43
    assert len({row["TRADE_ID"] for row in rows}) == 73
