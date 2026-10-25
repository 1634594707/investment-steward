"""Archive the selected response series; a qfq request alone proves no response basis."""
import pytest

from investment_steward_core import market_feed


@pytest.mark.parametrize("series,adjust", [("qfqday", "qfq"), ("day", "none")])
def test_tencent_selected_series_basis_survives_cache(monkeypatch, series, adjust):
    monkeypatch.setattr(market_feed, "_cache", {})
    monkeypatch.setattr(market_feed, "_fail_cache", {})
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return {"data": {"sh600519": {series: [["2026-09-04", "10", "11", "12", "9", "100"]]}}}

    monkeypatch.setattr(market_feed, "_http_get", get)
    rows, provider = market_feed.fetch_cn_kline("600519", 10)
    assert provider == "tencent"
    assert rows[0]["source_series"] == series
    assert rows[0]["requested_adjust"] == "qfq"
    assert market_feed.kline_adjustment(rows) == adjust
    assert market_feed.kline_adjustment_label(rows) == ("前复权" if adjust == "qfq" else "未复权")
    assert rows[0]["close"] == 11
    cached, _ = market_feed.fetch_cn_kline("600519", 10)
    assert cached == rows and len(calls) == 1


def test_eastmoney_preserves_request_without_claiming_response_confirmation(monkeypatch):
    monkeypatch.setattr(market_feed, "_http_get", lambda *args, **kwargs:
                        {"data": {"klines": ["2026-09-04,10,11,12,9,100"]}})
    rows = market_feed._fetch_em_kline("600519", 10)
    assert rows[0]["requested_adjust"] == "qfq"
    assert rows[0]["source_series"] == "data.klines"
    assert market_feed.kline_adjustment(rows) is None
    assert market_feed.kline_adjustment_label(rows) == "复权未确认"


def test_missing_or_mixed_basis_is_not_reconstructed():
    assert market_feed.kline_adjustment(None) is None
    assert market_feed.kline_adjustment([]) is None
    assert market_feed.kline_adjustment([{}]) is None
    assert market_feed.kline_adjustment([{"adjust": "qfq"}, {}]) is None
    assert market_feed.kline_adjustment([{"adjust": "qfq"}, {"adjust": "none"}]) is None
    assert market_feed.kline_adjustment_label([{}]) == "复权未确认"
