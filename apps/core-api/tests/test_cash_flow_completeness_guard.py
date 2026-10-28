"""完成度不能把被财务口径闸门拒绝的原值当作有效解释。"""
import pytest
from investment_steward_core import report_quality as quality


def block(currency="CNY", period="2026H1", unit="百万"):
    return quality.cash_flow_quality(
        operating_cash_flow=100, net_income=80, depreciation_amortization=20, capex=60,
        require_currency=True, allowed_sources={"S4"},
        metric_metadata={key: {"period": "2026H1" if key == "operating_cash_flow" else period,
                              "unit": "百万" if key == "operating_cash_flow" else unit,
                              "currency": "CNY" if key == "operating_cash_flow" else currency, "sources": ["S4"]}
                         for key in ("operating_cash_flow", "net_income", "depreciation_amortization", "capex")},
    )


def item(cash):
    return next(row for row in quality.research_completeness({"cash_flow_quality": cash})["items"]
                if row["key"] == "cash_flow_and_capex")


@pytest.mark.parametrize("cash", [block(currency="USD"), block(currency=""), block(period="2025FY"), block(unit="千元")])
def test_rejected_metrics_cannot_mark_research_complete(cash):
    result = item(cash)
    assert result["state"] == "partial", "字段原值保留，但不可比数据不构成已完成研究"
    assert "折旧摊销" in result["gaps"] and "资本开支" in result["gaps"]
    assert "折旧摊销" not in result["covered"]


def test_comparable_group_can_still_complete_research():
    assert item(block())["state"] == "done"


def test_each_period_must_pass_independently():
    assert item({"currency_policy": "required", "periods": [block(), block(currency="USD")]})["state"] == "partial"
    assert item({"currency_policy": "required", "periods": [block(), block()]})["state"] == "done"


def test_frontend_delivery_fixtures_match_current_backend_rules():
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "apps/web-shell/src/routes/workbench/__fixtures__/cash-flow-delivery.json"
    for fixture in json.loads(path.read_text(encoding="utf-8")):
        report = fixture["report"]
        assert report["cash_flow_quality"] == quality.cash_flow_quality(
            **fixture["input"], require_currency=True, allowed_sources={"S4"})
        assert report["research_completeness"] == quality.research_completeness(report)
