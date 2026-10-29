"""Support retests must participate in the shared scan without future level data."""
from datetime import UTC, datetime, timedelta

import pytest
from investment_steward_core import support_resistance, tactics, tactics_score


def _bars():
    start = datetime(2026, 8, 1, tzinfo=UTC)
    return [{"timestamp": (start + timedelta(days=i)).isoformat(),
             "open": 10.1, "close": 10.2, "low": 10.0, "high": 10.4,
             "volume": 100000} for i in range(40)]


def test_registered_support_detector_reaches_scoring_using_only_prior_bars(monkeypatch):
    bars = _bars()
    seen = []
    detect_levels = support_resistance.detect_levels

    def observe_levels(history):
        seen.append(history)
        return detect_levels(history)

    monkeypatch.setattr(support_resistance, "detect_levels", observe_levels)
    snap = tactics.snapshot(bars, tactic_ids={"support_hold"})
    assert snap["degraded_tactics"] == [], "A valid support scan must not silently lose this detector"
    assert seen == [bars[:-3]], "The three bars being evaluated cannot establish their own support"
    assert [s["date"] for s in snap["signals"]] == [bar["timestamp"][:10] for bar in bars[-3:]]
    assert all(s["tactic_id"] == "support_hold" for s in snap["signals"])
    scored = tactics_score.score_for_bars(snap, bars, recent_bars=3)
    assert "support_hold" in scored["hit_tactics"], "Detected support retests must reach the shared ranking input"


@pytest.mark.parametrize("low,close,expected", [
    (9.95, 10.2, True), (10.049, 10.2, True),
    (9.94, 10.2, False), (10.06, 10.2, False), (10.0, 10.0, False),
])
def test_support_scan_preserves_price_band_and_bullish_candle_rules(low, close, expected):
    bars = _bars()
    for bar in bars[-3:]:
        bar.update(low=low, close=close)
    snap = tactics.snapshot(bars, tactic_ids={"support_hold"})
    assert snap["degraded_tactics"] == []
    assert bool(snap["signals"]) is expected, "Only a bullish retest within the existing 0.5% band qualifies"


def test_support_scan_does_not_invent_levels_from_short_history():
    snap = tactics.snapshot(_bars()[:24], tactic_ids={"support_hold"})
    assert snap["degraded_tactics"] == []
    assert snap["signals"] == []


def test_full_scan_keeps_all_detectors_available_for_valid_candles():
    assert tactics.snapshot(_bars())["degraded_tactics"] == []
