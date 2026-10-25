"""R01: model-supplied cash flow must satisfy the same period rules as derived values."""

import hashlib
import json
from pathlib import Path

import pytest

from investment_steward_core.report_quality import cash_flow_quality


def test_original_user_report_never_reuses_mixed_period_quality_as_verified_analysis():
    """Actual archived input must lose its old verdict without rewriting the archive."""
    path = Path(__file__).resolve().parents[3] / "docs/assets/history-audit-2026-10-25/original-report.json"
    original = path.read_bytes()
    report = json.loads(original)
    cash = report["cash_flow_quality"]
    assert report["report_id"] == "bea494cc-5e3f-44e4-9f8d-5b8156c85a30"
    assert cash["operating_cash_flow"] == 10.08 and cash["capex"] == 6.06
    assert cash["free_cash_flow"] == -2.63 and cash["verdict"] == "盈利质量偏弱"
    assert "metric_metadata" not in cash
    current = cash_flow_quality(
        operating_cash_flow=cash["operating_cash_flow"], net_income=cash["net_income"],
        depreciation_amortization=cash["depreciation_amortization"],
        working_capital_change=cash["working_capital_change"], capex=cash["capex"],
        free_cash_flow=cash["free_cash_flow"], claimed_quality=cash["verdict"],
        operating_cash_flow_period=cash["operating_cash_flow_period"], capex_period=cash["capex_period"],
    )
    assert current["operating_cash_flow_period"] == "TTM-2026H1"
    assert current["capex_period"] == "2026H1"
    assert current["free_cash_flow"] is None and current["coverage_ratio"] is None
    assert current["free_cash_flow_reported"] == -2.63
    assert current["free_cash_flow_derived"] is False
    assert current["verdict"] == "不足以判断盈利质量"
    assert "统计期不一致或未声明" in current["note"]
    assert current["metric_metadata"] == {}, "Absent units and citations must not be invented from narrative text"
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(original).digest()


def test_metric_provenance_preserves_declared_units_and_only_real_source_ids():
    block = cash_flow_quality(
        metric_metadata={
            "operating_cash_flow": {"period": "2026H1", "unit": "亿元", "sources": ["S4", "S99"]},
            "capex": {"period": "2026H1", "unit": "万元", "sources": ["S4"]},
        },
        allowed_sources={"S4"},
    )
    assert block["metric_metadata"]["operating_cash_flow"] == {
        "period": "2026H1", "unit": "亿元", "sources": ["S4"],
    }
    assert block["metric_metadata"]["capex"]["unit"] == "万元"
    assert "net_income" not in block["metric_metadata"]


@pytest.mark.parametrize("reported", [-2.63, 4.02])
@pytest.mark.parametrize("period", ["2026H1", ""])
def test_reported_cash_flow_cannot_bypass_period_validation(reported, period):
    block = cash_flow_quality(
        operating_cash_flow=10.08,
        net_income=7.93,
        capex=6.06,
        operating_cash_flow_period="TTM-2026H1",
        capex_period=period,
        free_cash_flow=reported,
    )
    assert block["free_cash_flow"] is None
    assert block["free_cash_flow_reported"] == reported
    assert block["free_cash_flow_derived"] is False
    assert block["verdict"] == "不足以判断盈利质量"
    assert "统计期不一致或未声明" in block["note"]


def test_reported_cash_flow_is_recomputed_from_same_period_inputs():
    block = cash_flow_quality(
        operating_cash_flow=3.43,
        net_income=3.73,
        capex=6.06,
        operating_cash_flow_period="2026H1",
        capex_period="2026H1",
        free_cash_flow=99,
        metric_metadata={key: {"period": "2026H1", "unit": "亿元"}
                         for key in ("operating_cash_flow", "capex")},
    )
    assert block["free_cash_flow"] == -2.63
    assert block["free_cash_flow_reported"] == 99
    assert block["free_cash_flow_derived"] is True
    assert "模型提供的自由现金流 99.0" in block["note"]


def test_missing_units_never_authorize_subtraction_of_legacy_amounts():
    block = cash_flow_quality(
        operating_cash_flow=3.43, capex=6.06, free_cash_flow=-2.63,
        operating_cash_flow_period="2026H1", capex_period="2026H1",
    )
    assert block["free_cash_flow"] is None
    assert block["free_cash_flow_reported"] == -2.63
    assert "金额单位缺失或不一致" in block["note"]


def test_recomputed_fcf_inherits_input_provenance_instead_of_model_labels():
    block = cash_flow_quality(
        operating_cash_flow=3.43, capex=6.06, free_cash_flow=99,
        metric_metadata={
            "operating_cash_flow": {"period": "2026H1", "unit": "亿元", "sources": ["S4"]},
            "capex": {"period": "2026H1", "unit": "亿元", "sources": ["S2", "S4"]},
            "free_cash_flow": {"period": "TTM", "unit": "万元", "sources": ["S1"]},
        }, allowed_sources={"S1", "S2", "S4"},
    )
    assert block["free_cash_flow"] == -2.63
    assert block["metric_metadata"]["free_cash_flow"] == {
        "period": "2026H1", "unit": "亿元", "sources": ["S4", "S2"],
    }


def test_reported_cash_flow_without_inputs_cannot_support_a_verdict():
    block = cash_flow_quality(
        operating_cash_flow=3.43,
        net_income=3.73,
        free_cash_flow=-2.63,
        operating_cash_flow_period="2026H1",
        capex_period="2026H1",
    )
    assert block["free_cash_flow"] is None
    assert block["free_cash_flow_reported"] == -2.63
    assert "缺少经营现金流或资本开支" in block["note"]


def test_declared_different_units_prevent_subtraction_even_when_periods_match():
    block = cash_flow_quality(
        operating_cash_flow=3.43, net_income=3.73, capex=60600,
        operating_cash_flow_period="2026H1", capex_period="2026H1",
        metric_metadata={
            "operating_cash_flow": {"period": "2026H1", "unit": "亿元"},
            "capex": {"period": "2026H1", "unit": "万元"},
        },
    )
    assert block["free_cash_flow"] is None
    assert "金额单位缺失或不一致" in block["note"]


@pytest.mark.parametrize("profit_period,profit_unit", [("2026H1", "亿元"), ("TTM-2026H1", "万元"), ("", "亿元")])
def test_coverage_never_divides_incompatible_inputs(profit_period, profit_unit):
    block = cash_flow_quality(
        operating_cash_flow=10.08, net_income=3.73,
        metric_metadata={
            "operating_cash_flow": {"period": "TTM-2026H1", "unit": "亿元"},
            "net_income": {"period": profit_period, "unit": profit_unit},
        },
    )
    assert block["coverage_ratio"] is None
    assert "覆盖倍数未计算" in block["note"]


def test_half_year_and_ttm_inputs_are_normalized_as_separate_blocks():
    def metadata(period):
        return {key: {"period": period, "unit": "亿元", "sources": ["S4"]}
                for key in ("operating_cash_flow", "net_income", "capex")}

    block = cash_flow_quality(periods=[
        {"operating_cash_flow": 10.08, "net_income": 7.93, "metric_metadata": metadata("TTM-2026H1")},
        {"operating_cash_flow": 3.43, "net_income": 3.73, "capex": 6.06, "metric_metadata": metadata("2026H1")},
    ], allowed_sources={"S4"})
    ttm, half_year = block["periods"]
    assert ttm["coverage_ratio"] == 1.27
    assert ttm["free_cash_flow"] is None
    assert half_year["free_cash_flow"] == -2.63
    assert half_year["coverage_ratio"] == 0.92


def test_conflicting_ocf_period_blocks_coverage_even_when_metadata_matches_profit():
    block = cash_flow_quality(
        operating_cash_flow=10.08, net_income=7.93,
        operating_cash_flow_period="2026H1",
        metric_metadata={key: {"period": "TTM-2026H1", "unit": "亿元"}
                         for key in ("operating_cash_flow", "net_income")},
    )
    assert block["coverage_ratio"] is None
    assert block["strength"] == "insufficient"
    assert "期间声明冲突" in block["note"]


@pytest.mark.parametrize("rows", [
    [{"operating_cash_flow": 3.43, "net_income": 3.73,
      "metric_metadata": {key: {"period": "2026H1", "unit": "亿元"}
                          for key in ("operating_cash_flow", "net_income")}}],
    [{"operating_cash_flow": 3.43, "net_income": 3.73}],
    [{"operating_cash_flow": 3.43, "net_income": 3.73}, {}],
    [{}],
])
def test_period_only_reports_do_not_claim_all_cash_flow_inputs_are_absent(rows):
    block = cash_flow_quality(periods=rows)
    assert block["verdict"] == "分期列示"
    assert block["strength"] == "insufficient"
    assert block["coverage_ratio"] is None
    assert "不作跨期汇总判定" in block["note"]
    assert len(block["periods"]) == len(rows)


def test_top_level_values_and_verdict_survive_additional_periods():
    block = cash_flow_quality(operating_cash_flow=0, periods=[{"operating_cash_flow": 3.43}])
    assert block["operating_cash_flow"] == 0
    assert block["verdict"] == "无现金流口径"
    assert block["periods"][0]["operating_cash_flow"] == 3.43


@pytest.mark.parametrize("capex,expected", [(2, 1.43), (6.06, -2.63)])
def test_funding_surplus_or_shortfall_alone_does_not_establish_earnings_quality(capex, expected):
    block = cash_flow_quality(operating_cash_flow=3.43, net_income=3.73, capex=capex,
        metric_metadata={key: {"period": "2026H1", "unit": "亿元"}
                         for key in ("operating_cash_flow", "net_income", "capex")})
    assert block["free_cash_flow"] == expected
    assert block["verdict"] == "不足以判断盈利质量"
    assert "折旧摊销" in block["note"]
    assert ("缺口" if expected < 0 else "结余") in block["funding_balance_note"]
    assert "资本开支性质" in block["funding_balance_note"]


@pytest.mark.parametrize("period,unit", [("TTM-2026H1", "亿元"), ("2026H1", "万元"), ("", "")])
def test_incompatible_explanatory_amounts_do_not_upgrade_quality(period, unit):
    metadata = {key: {"period": "2026H1", "unit": "亿元"}
                for key in ("operating_cash_flow", "net_income", "capex")}
    metadata["depreciation_amortization"] = {"period": period, "unit": unit}
    block = cash_flow_quality(operating_cash_flow=10, net_income=5, capex=2,
                             depreciation_amortization=3, metric_metadata=metadata)
    assert block["depreciation_amortization"] == 3
    assert block["explanatory_present"] == []
    assert block["verdict"] == "不足以判断盈利质量"
    assert "解释项缺失或期间、单位不一致" in block["note"]
