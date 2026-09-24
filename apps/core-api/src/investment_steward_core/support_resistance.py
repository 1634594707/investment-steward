"""支撑阻力位识别(official.support-resistance)。

摆动高低点聚类 + 触碰/守住概率统计,纯 stdlib 确定性实现:
- 摆动点:窗口内极值(左右各 window 根);
- 聚类:价位差 <= ATR*atr_multiple 归并,均价为位,累计触碰次数;
- 统计:历史回看触碰次数与守住率,得 出高/中/低 置信;
- 样本不足返回空列表,不编造(ADR-0006 口径)。
"""

from __future__ import annotations

from typing import Any


def average_true_range(bars: list[dict[str, Any]], period: int = 14) -> float:
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = float(bars[i]["high"])
        low = float(bars[i]["low"])
        prev_close = float(bars[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not trs:
        return 0.0
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window)


def _cluster(points: list[float], tolerance: float) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for point in sorted(points):
        if clusters and abs(point - clusters[-1]["price"]) <= tolerance:
            top = clusters[-1]
            top["price"] = (top["price"] * top["touches"] + point) / (top["touches"] + 1)
            top["touches"] += 1
        else:
            clusters.append({"price": point, "touches": 1})
    return clusters


def detect_levels(
    bars: list[dict[str, Any]],
    *,
    window: int = 5,
    atr_multiple: float = 0.8,
    max_levels: int = 6,
) -> list[dict[str, Any]]:
    """输入升序 OHLCV 蜡烛,输出关键位列表(kind/price/touches/samples/hold_rate/confidence)。"""
    n = len(bars)
    if n < window * 4 + 2:
        return []
    atr = average_true_range(bars)
    if atr <= 0:
        return []
    tolerance = atr * atr_multiple

    swing_highs: list[float] = []
    swing_lows: list[float] = []
    for i in range(window, n - window):
        segment = bars[i - window : i + window + 1]
        if float(bars[i]["high"]) >= max(float(b["high"]) for b in segment):
            swing_highs.append(float(bars[i]["high"]))
        if float(bars[i]["low"]) <= min(float(b["low"]) for b in segment):
            swing_lows.append(float(bars[i]["low"]))

    def hold_stats(price: float, kind: str) -> tuple[float | None, int]:
        near = 0
        held = 0
        for i in range(window, n):
            bar = bars[i]
            close = float(bar["close"])
            if kind == "support":
                touched = float(bar["low"]) <= price + tolerance / 2
                held_now = close >= price - tolerance / 2
            else:
                touched = float(bar["high"]) >= price - tolerance / 2
                held_now = close <= price + tolerance / 2
            if touched:
                near += 1
                if held_now:
                    held += 1
        return (held / near) if near else None, near

    levels: list[dict[str, Any]] = []
    for kind, points in (("support", swing_lows), ("resistance", swing_highs)):
        for cluster in _cluster(points, tolerance):
            rate, samples = hold_stats(cluster["price"], kind)
            if samples < 2:
                continue
            confidence = "高" if (rate is not None and rate >= 0.7 and samples >= 5) else ("中" if rate is not None and rate >= 0.5 else "低")
            levels.append({
                "kind": kind,
                "price": round(cluster["price"], 3),
                "touches": cluster["touches"],
                "samples": samples,
                "hold_rate": round(rate, 2) if rate is not None else None,
                "confidence": confidence,
            })
    levels.sort(key=lambda item: (-(item["hold_rate"] or 0), -item["touches"]))
    return levels[:max_levels]
