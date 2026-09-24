"""v21 人格化战法扩展测试：突破平台 / N字 / 均线辅助 / 波段低点抬高 / 弱转强 / 反核修复。

全部确定性口径：构造明确命中与明确不命中的合成 K 线，断言信号精确出现/不出现。
"""

from __future__ import annotations

from investment_steward_core import tactics


def _bar(day: int, open_: float, close: float, high: float, low: float) -> dict[str, object]:
    return {"timestamp": f"2026-06-{day:02d}", "open": open_, "close": close, "high": high, "low": low, "volume": 1_000_000}


def _ids(signals: list[dict[str, object]]) -> set[str]:
    return {str(signal["tactic_id"]) for signal in signals}


def _flat_bars(count: int = 40, price: float = 10.0) -> list[dict[str, object]]:
    """横盘序列（无信号基线）。"""
    return [_bar((index % 28) + 1, price, price, price * 1.005, price * 0.995) for index in range(count)]


def test_catalog_contains_six_new_tactics():
    ids = {item["id"] for item in tactics.catalog()}
    assert {"platform_breakout", "n_shape_up", "ma20_support", "swing_higher_low", "weak_to_strong", "reversal_after_crash"} <= ids


def test_platform_breakout_hits_on_20day_high_with_3pct_body():
    bars = _flat_bars(40)
    # 第 41 根：大幅收阳创新高（10 → 10.5，实体 5%）
    bars.append(_bar(28, 10.05, 10.6, 10.7, 10.0))
    snap = tactics.snapshot(bars)
    assert "platform_breakout" in _ids(snap["signals"])
    # 横盘基线无突破信号
    base = tactics.snapshot(_flat_bars(40))
    assert "platform_breakout" not in _ids(base["signals"])


def test_n_shape_up_hits_on_pullback_resume_structure():
    bars = _flat_bars(40, price=10.0)
    # 构造 i=39（最后一根）满足：c[34]>c[32]（第一段涨）、c[37]<c[35]（回调）、今日收阳且 > c[37]
    bars[32] = _bar(5, 10.0, 10.0, 10.05, 9.95)
    bars[34] = _bar(7, 10.0, 10.4, 10.45, 9.95)   # 第一段涨
    bars[35] = _bar(8, 10.0, 10.4, 10.45, 9.95)   # 回调起点
    bars[37] = _bar(10, 10.0, 10.1, 10.15, 9.95)  # 回调
    bars[39] = _bar(12, 10.15, 10.5, 10.55, 10.1) # 今日收阳且越过 c[37]
    snap = tactics.snapshot(bars)
    assert "n_shape_up" in _ids(snap["signals"])


def test_ma20_support_hits_on_pullback_to_rising_ma20():
    bars: list[dict[str, object]] = []
    price = 10.0
    for index in range(40):
        price = round(price * 1.01, 3)  # 缓涨让 MA20 向上
        bars.append(_bar((index % 28) + 1, price * 0.999, price, price * 1.004, price * 0.994))
    # 最后一根：下探 MA20 附近后收阳（low 触及 ma20*1.0 以内、close > ma20）
    import math

    closes = [float(bar["close"]) for bar in bars]
    ma20 = tactics.sma(closes, 20)[-1]
    assert ma20 is not None
    bars[-1] = _bar(28, float(ma20) * 1.004, float(ma20) * 1.012, float(ma20) * 1.016, float(ma20) * 0.998)
    snap = tactics.snapshot(bars)
    assert "ma20_support" in _ids(snap["signals"])


def test_swing_higher_low_hits_on_ascending_lows():
    bars: list[dict[str, object]] = []
    # 前 20 日低点 9.9，后 10 日低点抬到 10.3，价格整体缓涨保持 MA20 向上
    price = 10.0
    for index in range(30):
        price = round(price * 1.008, 3)
        bars.append(_bar((index % 28) + 1, price * 0.999, price, price * 1.005, price * 0.99))
    for index in range(10):
        price = round(price * 1.008, 3)
        bars.append(_bar((index % 28) + 1, price * 0.999, price, price * 1.005, price * 1.003))  # 低点抬高
    snap = tactics.snapshot(bars)
    assert "swing_higher_low" in _ids(snap["signals"])


def test_weak_to_strong_hits_on_shadow_bar_then_gap_up():
    bars = _flat_bars(40, price=10.0)
    # 昨日（index 38）：长上影阴线——open 10.2, close 10.0（实体 0.2），high 10.75（上影 0.55 ≥ 2×实体）
    bars[38] = _bar(11, 10.2, 10.0, 10.75, 9.98)
    # 今日：高开 1.5%+（open ≥ 10.15）收阳
    bars[39] = _bar(12, 10.18, 10.35, 10.4, 10.12)
    snap = tactics.snapshot(bars)
    assert "weak_to_strong" in _ids(snap["signals"])
    # 低开则不命中
    bars2 = _flat_bars(40, price=10.0)
    bars2[38] = _bar(11, 10.2, 10.0, 10.75, 9.98)
    bars2[39] = _bar(12, 9.95, 10.1, 10.15, 9.9)
    assert "weak_to_strong" not in _ids(tactics.snapshot(bars2)["signals"])


def test_reversal_after_crash_hits_on_recovery():
    bars = _flat_bars(40, price=10.0)
    # 前日 10.4 → 昨日大跌 9.3（约 -10.6%）→ 今日收复 9.5（≥ 昨收）
    bars[37] = _bar(10, 10.4, 10.4, 10.45, 10.35)
    bars[38] = _bar(11, 10.4, 9.3, 10.4, 9.25)
    bars[39] = _bar(12, 9.25, 9.5, 9.55, 9.2)
    snap = tactics.snapshot(bars)
    assert "reversal_after_crash" in _ids(snap["signals"])
    # 未收复则不命中
    bars2 = _flat_bars(40, price=10.0)
    bars2[37] = _bar(10, 10.4, 10.4, 10.45, 10.35)
    bars2[38] = _bar(11, 10.4, 9.3, 10.4, 9.25)
    bars2[39] = _bar(12, 9.2, 9.28, 9.3, 9.15)
    assert "reversal_after_crash" not in _ids(tactics.snapshot(bars2)["signals"])


def test_new_tactics_are_pure_functions_same_input_same_output():
    bars = _flat_bars(40, price=10.0)
    bars[38] = _bar(11, 10.2, 10.0, 10.75, 9.98)
    bars[39] = _bar(12, 10.18, 10.35, 10.4, 10.12)
    first = tactics.snapshot(bars)
    second = tactics.snapshot(bars)
    assert first["signals"] == second["signals"]
