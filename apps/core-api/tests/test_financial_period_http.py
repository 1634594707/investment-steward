"""Financial period rules must survive generation, persistence and historical detail."""
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from conftest import run_stock_report
from test_t03_scan_history_http import scan_http  # noqa: F401
from test_v31_repair_and_comparison import _good_payload, _mock_sources
from investment_steward_core import jev_client, lhb_feed, report_quality
from investment_steward_core.api import app as app_module
from investment_steward_core.domain import ModelProfileStatus
from investment_steward_core.model_client import ModelReply


def test_legacy_cash_flow_is_rechecked_without_rewriting_the_historical_record(scan_http):
    http, headers, database = scan_http
    source = Path(__file__).resolve().parents[3] / "docs/assets/history-audit-2026-10-25/original-report.json"
    original_bytes = source.read_bytes()
    original = json.loads(original_bytes)
    database.insert_ai_research_report(original)
    before = database.export_payload_tables()["ai_research_reports"]
    response = http.get(f"/ai-research/reports/{original['report_id']}", headers=headers)
    assert response.status_code == 200
    view = response.json()["item"]
    cash = view["cash_flow_quality"]
    assert cash["verdict"] == "不足以判断盈利质量", "Historical negative FCF must not retain an unsupported quality verdict"
    assert cash["coverage_ratio"] is None and cash["free_cash_flow"] is None
    assert cash["free_cash_flow_reported"] == -2.63
    assert cash["operating_cash_flow"] == 10.08 and cash["capex"] == 6.06
    assert "盈利质量偏弱" in cash["downgrade_note"]
    completeness = view["research_completeness"]
    assert completeness == report_quality.research_completeness(view)
    cash_item = next(row for row in completeness["items"] if row["key"] == "cash_flow_and_capex")
    assert "自由现金流" not in cash_item["covered"] and "自由现金流" in cash_item["gaps"]
    assert completeness["done_count"] == 1, "Unreviewed historical factors must not remain completed"
    assert view["decision_card"]["research_completeness"] == completeness
    for warning in original["quality_warnings"]:
        assert f"生成时告警（历史留痕，不代表当前核验）：{warning}" in view["quality_warnings"]
    assert all(warning in view["quality_warnings"] for warning in completeness["warnings"])
    assert view["report"] == original["report"] and view["generated_at"] == original["generated_at"]
    assert http.get(f"/ai-research/reports/{original['report_id']}", headers=headers).json()["item"] == view
    assert database.export_payload_tables()["ai_research_reports"] == before
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("capex_period,unit,expected", [
    ("2026H1", "亿元", -2.63), ("TTM-2026H1", "亿元", None), ("2026H1", "", None),
])
@pytest.mark.parametrize("collab", [False, True])
@pytest.mark.parametrize("review_enabled", [False, True])
def test_report_preserves_financial_period_rules_and_unverified_analysis(scan_http, monkeypatch, capex_period, unit, expected, collab, review_enabled):
    http, headers, database = scan_http
    _mock_sources(monkeypatch)
    profile = SimpleNamespace(profile_id=uuid4(), model="period-test", status=ModelProfileStatus.ACTIVE)
    monkeypatch.setattr(database, "list_model_profiles", lambda: [profile])
    monkeypatch.setattr(lhb_feed, "fetch_billboard", lambda *args, **kwargs: [])
    monkeypatch.setattr(lhb_feed, "fetch_seats", lambda *args, **kwargs: [])
    reviews = []

    def build_judge(*args, **kwargs):
        if not review_enabled or kwargs.get("purpose") != "jev:earnings-analysis":
            return None

        def judge(state, questions):
            assert len(questions) == 3
            assert "缺产销量" in state and "缺产品价格及价差" in state and "缺成本费用拆分" in state
            reviews.append(state)
            return {"model": "test-factor-review", "answers": {
                key: jev_client.JevAnswer(question_id=key, type="choice", choice="insufficient")
                for key in questions
            }}

        return judge

    monkeypatch.setattr(app_module.jev_client, "build_judge", build_judge)
    payload = _good_payload()
    payload["cash_flow_quality"] = {
        "periods": [
            {"operating_cash_flow": 10.08, "net_income": 7.93,
             "metric_metadata": {key: {"period": "TTM-2026H1", "unit": "亿元", "sources": ["S4"]}
                                 for key in ("operating_cash_flow", "net_income")}},
            {"operating_cash_flow": 3.43, "net_income": 3.73, "capex": 6.06, "free_cash_flow": 99,
             "metric_metadata": {
                 "operating_cash_flow": {"period": "2026H1", "unit": "亿元", "sources": ["S4"]},
                 "net_income": {"period": "2026H1", "unit": "亿元", "sources": ["S4"]},
                 "capex": {"period": capex_period, "unit": unit, "sources": ["S4", "S99"]},
             }},
        ],
    }
    payload["earnings_growth"] = [
        {"factor": factor, "effect": gap, "basis": "所引财务数据不足以解释该因素的作用",
         "sources": ["S4"], "analysis_state": "analyzed"}
        for factor, gap in (("volume", "缺产销量，无法验证"), ("price", "缺产品价格及价差，无法验证"),
                            ("cost_efficiency", "缺成本费用拆分，无法验证"))
    ]
    monkeypatch.setattr(app_module.model_client, "call_active_model", lambda *args, **kwargs:
                        ModelReply(content=json.dumps(payload), model="period-test", provider="test", latency_ms=0))
    if collab:
        monkeypatch.setattr(app_module.collab_engine.model_client, "call_active_model", lambda *args, **kwargs:
                            ModelReply(content=json.dumps({**payload, "observations": [], "risks": []}), model="period-test", provider="test", latency_ms=0))
        created = http.post("/evidence/collab-run", json={"symbol": "600001"}, headers=headers).json()
        assert created["ok"] is True, created
        for stage in app_module.collab_engine.STAGES:
            response = http.post("/evidence/collab-next", json={"run_id": created["run"]["run_id"]}, headers=headers).json()
            assert response["executed_stage"] == stage and response["executed_status"] == "done", response
        result = database.get_ai_research_report(response["collab_report_id"])
        result["persisted"] = response["persisted"]
    else:
        result = run_stock_report(http, headers, symbol="600001", with_counter_check=False, with_auto_repair=False)
    assert result["ok"] is True and result["persisted"] is True, result
    block = result["cash_flow_quality"]
    assert block["verdict"] == "分期列示" and block["coverage_ratio"] is None
    ttm, half_year = block["periods"]
    assert ttm["coverage_ratio"] == 1.27 and ttm["free_cash_flow"] is None
    assert half_year["coverage_ratio"] == 0.92
    assert half_year["free_cash_flow"] == expected
    assert half_year["free_cash_flow_reported"] == 99
    assert half_year["metric_metadata"]["capex"]["sources"] == ["S4"]
    assert half_year["verdict"] == "不足以判断盈利质量", "Funding balance alone must not certify earnings quality"
    state = "insufficient" if review_enabled else "unverified"
    assert all(row["analysis_state"] == state for row in result["earnings_growth"])
    assert bool(reviews) is review_enabled, "Both generation routes must consume factor review, not merely discard self-ratings"
    if review_enabled:
        assert all(row["analysis_model"] == "test-factor-review" for row in result["earnings_growth"])
        assert all(row["analysis_note"] == "已检查，证据不足以支持该因素分析。" for row in result["earnings_growth"])
    assert all(row["contribution_pct"] is None for row in result["earnings_growth"])
    earnings_item = next(row for row in result["research_completeness"]["items"]
                         if row["key"] == "earnings_decomposition")
    assert earnings_item["state"] != "done" and earnings_item["covered"] == [], (
        "Model-authored completion cannot count as reviewed factor analysis"
    )
    saved = database.get_ai_research_report(result["report_id"])
    detail = http.get(f"/ai-research/reports/{result['report_id']}", headers=headers)
    assert detail.status_code == 200
    for key in ("cash_flow_quality", "earnings_growth", "research_completeness"):
        assert saved[key] == result[key] == detail.json()["item"][key]
        if collab:
            revised = next(item for item in response["run"]["stages"] if item["stage"] == "revise")
            assert revised["result"][key] == saved[key], "Immediate collaborative view must match persisted financial rules"
