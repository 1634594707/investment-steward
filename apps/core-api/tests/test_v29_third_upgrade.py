"""v29 三次升级（研报质量与数据可视化）P0 测试。

覆盖四块：
A. 模式字数预算（report_quality.report_mode_budget / validate_report mode 参数）——全部软告警；
B. 单季财务拆分（financial_evidence.derive_quarterly_series）——累计差分、断档不补零、yoy 同口径；
C. 端点图表数据包（/evidence/stock-research-report 返回 source_meta / price_chart / quarterly / valuation_chart）；
D. 协同运行视图透传 evidence_meta、信号状态带触发日。
"""

from __future__ import annotations

import json

import pytest

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import financial_evidence, report_quality, tactics_score
from investment_steward_core.financial_evidence import FinancialStatementRow


def _fin_row(statement_type: str, period: str, published: str, items: dict[str, str]) -> FinancialStatementRow:
    return FinancialStatementRow(
        symbol="603328", statement_type=statement_type, period_end=period,
        published_raw=published, items=items, identity=f"test-{statement_type}-{period}",
    )


# ---------------------------------------------------------------------------
# A. 模式字数预算（§二）：三档软告警，未知模式报错不静默回落
# ---------------------------------------------------------------------------


def test_mode_budget_table_and_unknown_mode_rejected():
    name, label, lo, hi = report_quality.report_mode_budget("quick")
    assert (name, label, lo, hi) == ("quick", "快速版", 1500, 2500)
    assert report_quality.report_mode_budget("DEEP")[0] == "deep"      # 大小写归一
    assert report_quality.report_mode_budget(None)[0] == "standard"    # 缺省 standard
    name, _, lo, hi = report_quality.report_mode_budget("standard")
    assert (lo, hi) == (2000, 3500)
    deep_name, _, deep_lo, deep_hi = report_quality.report_mode_budget("deep")
    assert (deep_name, deep_lo, deep_hi) == ("deep", 3500, 6000)
    with pytest.raises(ValueError):
        report_quality.report_mode_budget("ultra")                     # 未知模式 422，不静默回落


def test_validate_report_reports_mode_budget_and_warns_by_mode():
    """mode 进入校验结果；同一段正文在深研档触发「低于预算」告警，在快速档则不触发。"""
    payload = {
        "title": "甲股：缓涨",
        "report": "### 技术面\n" + "指标口径一致的缓涨。" * 200 + " [S1]\n### 消息面\n中标 [S2]\n### 基本面\n稳健 [S4]\n### 综合判断\n中性",
        "citations": ["S1", "S2", "S4"],
        "confidence": "medium",
        "limitations": ["a", "b", "c"],
    }
    quick = report_quality.validate_report(dict(payload), allowed={"S1", "S2", "S4"}, mode="quick")
    deep = report_quality.validate_report(dict(payload), allowed={"S1", "S2", "S4"}, mode="deep")
    assert quick["report_mode"] == "quick"
    assert deep["report_mode"] == "deep"
    assert deep["report_mode_budget"] == {"min": 3500, "max": 6000}
    # 同一段正文（约 1000+ 字）：深研档提示低于预算，快速档无该告警
    deep_low = [w for w in deep["warnings"] if "低于" in w]
    quick_low = [w for w in quick["warnings"] if "低于" in w]
    assert deep_low and not quick_low


# ---------------------------------------------------------------------------
# B. 单季财务拆分（§三）：累计差分、断档不补零、yoy 同口径
# ---------------------------------------------------------------------------


_INCOME_ROWS = [
    _fin_row("income", "2026-06-30", "2026-08-20", {"revenue": "200"}),
    _fin_row("income", "2026-03-31", "2026-04-25", {"revenue": "90"}),
    _fin_row("income", "2025-12-31", "2026-03-28", {"revenue": "350"}),
    _fin_row("income", "2025-09-30", "2025-10-24", {"revenue": "240"}),
    _fin_row("income", "2025-06-30", "2025-08-19", {"revenue": "150"}),
    _fin_row("income", "2025-03-31", "2025-04-22", {"revenue": "70"}),
]


def test_quarterly_series_differs_cumulative_and_keeps_q1_raw():
    series = financial_evidence.derive_quarterly_series(_INCOME_ROWS, statement_type="income", item_key="revenue")
    by_label = {item["label"]: item for item in series}
    assert by_label["2025Q1"]["value"] == 70 and not by_label["2025Q1"]["is_derived"]   # Q1 即单季
    assert by_label["2026Q2"]["value"] == 110 and by_label["2026Q2"]["is_derived"]      # H1 200 − Q1 90
    assert by_label["2025Q4"]["value"] == 110 and by_label["2025Q4"]["is_derived"]      # 350 − 240
    assert by_label["2026Q2"]["cumulative"] == 200                                       # 累计原值并列保留
    # 单季同比只与去年同季单季比（服务端 round 4 位）；缺去年同季为 None（不用累计同比冒充）
    assert by_label["2026Q1"]["yoy"] == pytest.approx(0.2857, abs=1e-4)
    assert by_label["2025Q4"]["yoy"] is None                                            # 缺 2024Q4


def test_quarterly_series_keeps_gap_as_none_never_fills_zero():
    """★ 断档后紧邻报告期无法差分：保留 None + 说明，绝不把累计值冒充单季、也不补零。"""
    broken = [row for row in _INCOME_ROWS if row.period_end != "2026-03-31"]
    series = financial_evidence.derive_quarterly_series(broken, statement_type="income", item_key="revenue")
    by_label = {item["label"]: item for item in series}
    assert by_label["2026Q2"]["value"] is None
    assert "无法拆" in by_label["2026Q2"]["note"]
    assert by_label["2026Q2"]["cumulative"] == 200          # 累计原值仍在，可追溯
    assert "2026Q1" not in by_label                          # 被剔除的报告期不出现


def test_quarterly_point_statement_is_not_diffed():
    """资产负债表是时点数：单季值=披露原值，绝不差分。"""
    rows = [
        _fin_row("balance", "2026-06-30", "2026-08-20", {"total_equity": "500"}),
        _fin_row("balance", "2026-03-31", "2026-04-25", {"total_equity": "480"}),
    ]
    series = financial_evidence.derive_quarterly_series(rows, statement_type="balance", item_key="total_equity")
    by_label = {item["label"]: item for item in series}
    assert by_label["2026Q2"]["value"] == 500 and not by_label["2026Q2"]["is_derived"]


def test_describe_quarterly_mentions_gap():
    broken = [row for row in _INCOME_ROWS if row.period_end != "2026-03-31"]
    series = financial_evidence.derive_quarterly_series(broken, statement_type="income", item_key="revenue")
    text = financial_evidence.describe_quarterly(series, "revenue")
    assert "单季" in text and "不补零" in text
    assert "无法拆" in text or "缺失" in text


# ---------------------------------------------------------------------------
# C. 端点：图表数据包 + 来源元数据 + 模式回显
# ---------------------------------------------------------------------------


def test_stock_report_returns_chart_pack_and_source_meta(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report = json.dumps({
        "title": "甲股：缓涨",
        "executive_summary": "结论：技术面缓涨。",
        "report": "### 技术面\n缓涨 [S1]\n### 消息面\n中标 [S2]\n### 基本面\n稳健 [S4]\n### 综合判断\n中性",
        "citations": ["S1", "S2", "S4"],
        "confidence": "medium",
    }, ensure_ascii=False)

    def _fake(_base_url, _payload, _key, _timeout):
        return {"choices": [{"message": {"content": report}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001", "mode": "quick"}, headers=headers,
    ).json()
    assert body["ok"] is True, body
    assert body["report_mode"] == "quick"                      # 模式回显

    # 来源元数据：S1 用实际行情 provider；S2-S5 各自带提供方与取数时刻
    meta = body["source_meta"]
    assert meta["S1"]["provider"] == "test-provider"
    for sid in ("S2", "S3", "S4", "S5"):
        assert meta[sid]["provider"], sid
        assert "T" in meta[sid]["retrieved_at"], sid

    # 价格量能图：前复权口径注明 + MA 与蜡烛等长对齐
    price = body["price_chart"]
    assert price["adjust"] == "前复权"
    assert len(price["candles"]) > 0
    assert len(price["ma5"]) == len(price["candles"])
    assert price["candles"][-1]["date"] >= price["candles"][0]["date"]

    # 单季序列：mock 财务只有 2025H1 / 2025FY / 2026H1 三期（缺 2025Q1、2025Q3、2026Q1）→
    # 三个报告期都因紧邻报告期缺失而无法差分：单季值全为 None（断档保留，不补零），累计原值仍在。
    revenue = body["quarterly"]["revenue"]
    assert len(revenue) == 3
    by_label = {item["label"]: item for item in revenue}
    for label, cumulative in (("2025Q2", 900_000_000), ("2025Q4", 1_800_000_000), ("2026Q2", 1_000_000_000)):
        assert by_label[label]["value"] is None, label
        assert by_label[label]["cumulative"] == cumulative, label
        assert "无法拆" in by_label[label]["note"], label

    # 估值分位：快照取数结果直通（键与 mock 契约一致）；peer_count=0（mock 未给同业）也是合法取数结果
    valuation = body["valuation_chart"]
    assert valuation["percentiles"]["pe_ttm"] == 6.7
    assert isinstance(valuation["peer_count"], int) and valuation["peer_count"] >= 0


def test_stock_report_default_mode_is_standard(client, tmp_path, monkeypatch):
    """不传 mode → standard 回显（行为向后兼容）。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report = json.dumps({
        "title": "甲股", "report": "### 技术面\nx [S1]\n### 消息面\ny [S2]\n### 基本面\nz [S4]\n### 综合判断\nw",
        "citations": ["S1", "S2", "S4"], "confidence": "low",
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": report}}]})

    body = test_client.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()
    assert body["ok"] is True
    assert body["report_mode"] == "standard"


# ---------------------------------------------------------------------------
# D. 协同视图透传 evidence_meta；信号状态带触发日
# ---------------------------------------------------------------------------


def test_collab_public_view_exposes_evidence_meta():
    from investment_steward_core import collab as collab_engine

    run = {
        "run_id": "r1", "symbol": "600001", "question": "q", "model": "m",
        "stage_models": {}, "created_at": "2026-09-12T00:00:00",
        "next_index": 0, "stages": [], "source_keys": ["S1"], "source_errors": {},
        "evidence_meta": {"price_chart": {"adjust": "前复权"}, "source_meta": {"S1": {"provider": "p"}}},
    }
    view = collab_engine.public_view(run)
    assert view["evidence_meta"]["price_chart"]["adjust"] == "前复权"
    # 旧运行缺 evidence_meta → 空 dict，不抛错
    legacy = collab_engine.public_view({**run, "evidence_meta": None})
    assert legacy["evidence_meta"] == {}


def test_signal_state_carries_signal_date():
    from datetime import date, timedelta

    bars = [
        {"timestamp": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
         "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1_000_000}
        for i in range(30)
    ]
    state = tactics_score.signal_state(
        direction="bullish", signal_date="2026-01-25", trigger_price=10.0, bars=bars,
    )
    assert state["signal_date"] == "2026-01-25"
