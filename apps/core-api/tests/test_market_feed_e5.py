"""E5 · 行情供应商优先级契约测试。

覆盖 `market_feed.py` 的 `_SOURCE_ORDER=("tencent","eastmoney")` 不变式：
1. 腾讯为主源：腾讯成功时优先返回腾讯且不回退东财；
2. 东财回退：腾讯失败时自动尝试东财；
3. 双源失败：统一抛 FeedError 并进入失败冷却（调用方降级 is_demo）。

全部通过 monkeypatch 模块函数完成，不发起任何真实网络请求。
"""

from __future__ import annotations

import pytest
from investment_steward_core.market_feed import FeedError, fetch_cn_kline

_FIXTURE_ROW = {
    "timestamp": "2026-09-04T00:00:00+00:00",
    "open": 4.6,
    "high": 4.7,
    "low": 4.5,
    "close": 4.62,
    "volume": 1000.0,
}


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    import investment_steward_core.market_feed as feed

    monkeypatch.setattr(feed, "_cache", {})
    monkeypatch.setattr(feed, "_fail_cache", {})
    yield


def test_tencent_is_primary_source_when_available(monkeypatch):
    """腾讯成功时返回腾讯标识，且东财不被调用。"""
    import investment_steward_core.market_feed as feed

    calls: list[str] = []

    def _fake_tx(symbol: str, limit: int, period: str = "day"):
        calls.append("tencent")
        return [_FIXTURE_ROW]

    def _fail_em(symbol: str, limit: int, period: str = "day"):  # pragma: no cover - 主源成功时不应触发
        calls.append("eastmoney")
        raise AssertionError("腾讯主源可用时不应回退东财")

    monkeypatch.setattr(feed, "_fetch_tx_kline", _fake_tx)
    monkeypatch.setattr(feed, "_fetch_em_kline", _fail_em)

    rows, source = fetch_cn_kline("510300", 5)
    assert source == "tencent"
    assert rows == [_FIXTURE_ROW]
    assert calls == ["tencent"]


def test_falls_back_to_eastmoney_when_tencent_fails(monkeypatch):
    """腾讯失败时自动回退东财，返回东财标识。"""
    import investment_steward_core.market_feed as feed

    calls: list[str] = []

    def _fail_tx(symbol: str, limit: int, period: str = "day"):
        calls.append("tencent")
        raise FeedError("腾讯不可达")

    def _ok_em(symbol: str, limit: int, period: str = "day"):
        calls.append("eastmoney")
        return [_FIXTURE_ROW]

    monkeypatch.setattr(feed, "_fetch_tx_kline", _fail_tx)
    monkeypatch.setattr(feed, "_fetch_em_kline", _ok_em)

    rows, source = fetch_cn_kline("510300", 5)
    assert source == "eastmoney"
    assert rows == [_FIXTURE_ROW]
    assert calls == ["tencent", "eastmoney"]


def test_both_sources_fail_raises_and_enters_failure_cooldown(monkeypatch):
    """两源都失败时抛 FeedError 并写入失败冷却，冷却期内不再发起请求。"""
    import investment_steward_core.market_feed as feed

    calls: list[str] = []

    def _fail_tx(symbol: str, limit: int, period: str = "day"):
        calls.append("tencent")
        raise FeedError("腾讯不可达")

    def _fail_em(symbol: str, limit: int, period: str = "day"):
        calls.append("eastmoney")
        raise FeedError("东财不可达")

    monkeypatch.setattr(feed, "_fetch_tx_kline", _fail_tx)
    monkeypatch.setattr(feed, "_fetch_em_kline", _fail_em)

    with pytest.raises(FeedError):
        fetch_cn_kline("510300", 5)
    assert calls == ["tencent", "eastmoney"]

    # 冷却期内再次取数直接短路，不重试任何供应商。
    calls.clear()
    with pytest.raises(FeedError):
        fetch_cn_kline("510300", 5)
    assert calls == []


def test_invalid_period_rejected_without_network(monkeypatch):
    """非法周期在发起任何供应商请求前即被拒绝。"""
    import investment_steward_core.market_feed as feed

    def _unexpected(symbol, limit, period="day"):  # pragma: no cover - 非法周期不应触发请求
        raise AssertionError("非法周期不应发起任何供应商请求")

    monkeypatch.setattr(feed, "_fetch_tx_kline", _unexpected)
    monkeypatch.setattr(feed, "_fetch_em_kline", _unexpected)

    with pytest.raises(FeedError):
        fetch_cn_kline("510300", 5, period="quarter")


def test_period_forwarded_to_primary_and_fallback_sources(monkeypatch):
    """week 请求必须以 week 调用主源；主源失败时回退源同样收到 week（不得悄悄按日线拉取）。"""
    import investment_steward_core.market_feed as feed

    tx_calls: list[tuple[str, int, str]] = []
    em_calls: list[tuple[str, int, str]] = []

    def _fail_tx(symbol: str, limit: int, period: str = "day"):
        tx_calls.append((symbol, limit, period))
        raise FeedError("腾讯不可达")

    def _ok_em(symbol: str, limit: int, period: str = "day"):
        em_calls.append((symbol, limit, period))
        return [_FIXTURE_ROW]

    monkeypatch.setattr(feed, "_fetch_tx_kline", _fail_tx)
    monkeypatch.setattr(feed, "_fetch_em_kline", _ok_em)

    _, source = fetch_cn_kline("510300", 5, period="week")
    assert source == "eastmoney"
    assert tx_calls == [("510300", 5, "week")]
    assert em_calls == [("510300", 5, "week")]


def test_cache_entries_isolated_by_period(monkeypatch):
    """day 与 week 各自成缓存条目：请求 week 不得命中 day 的缓存行。"""
    import investment_steward_core.market_feed as feed

    day_row = dict(_FIXTURE_ROW, timestamp="2026-09-01T00:00:00+00:00")
    week_row = dict(_FIXTURE_ROW, timestamp="2026-09-05T00:00:00+00:00")
    calls: list[str] = []

    def _fake_tx(symbol: str, limit: int, period: str = "day"):
        calls.append(period)
        return [day_row] if period == "day" else [week_row]

    monkeypatch.setattr(feed, "_fetch_tx_kline", _fake_tx)

    rows_day, _ = fetch_cn_kline("510300", 5, period="day")
    rows_week, _ = fetch_cn_kline("510300", 5, period="week")
    assert rows_day == [day_row]
    assert rows_week == [week_row]
    assert calls == ["day", "week"]

    # 缓存命中路径：周期各自命中各自条目，不再触发供应商。
    calls.clear()
    cached_day, _ = fetch_cn_kline("510300", 5, period="day")
    cached_week, _ = fetch_cn_kline("510300", 5, period="week")
    assert cached_day == [day_row]
    assert cached_week == [week_row]
    assert calls == []