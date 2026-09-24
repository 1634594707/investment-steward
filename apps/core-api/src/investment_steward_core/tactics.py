"""股票战法识别引擎（official.stock-tactics 插件的确定性内核）。

设计口径（与全站一致）：
- 每个战法都是 K 线的纯函数：同输入同结果、可复算、不联网、不用 AI；
- 信号只描述「图形上发生了什么」，不构成任何买卖建议；
- 指标口径与主流行情软件一致：MACD(12,26,9)、KDJ(9,3,3) 递推平滑、RSI(14) Wilder 平滑；
- bars 为升序 OHLCV dict 列表（timestamp/open/high/low/close/volume），与 CandleSeries.bars 同形。
"""

from __future__ import annotations

from typing import Any, Callable

BULLISH = "bullish"
BEARISH = "bearish"
NEUTRAL = "neutral"

MIN_BARS = 35  # 指标最长窗口 MA20/MACD(26) 的最低数据要求


# ---------- 指标（全部返回与 bars 对齐的序列，数据不足处为 None） ----------

def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = sum(values[i - period + 1 : i + 1]) / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    out[period - 1] = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi_wilder(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0.0)
        losses += max(-diff, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0.0)) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast, ema_slow = ema(closes, fast), ema(closes, slow)
    dif: list[float | None] = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid = [v for v in dif if v is not None]
    dea_valid = ema(valid, signal)
    dea: list[float | None] = [None] * len(closes)
    cursor = 0
    for i, value in enumerate(dif):
        if value is None:
            continue
        dea[i] = dea_valid[cursor] if cursor < len(dea_valid) else None
        cursor += 1
    hist = [
        (d - e) * 2 if d is not None and e is not None else None
        for d, e in zip(dif, dea)
    ]
    return dif, dea, hist


def kdj(bars: list[dict[str, Any]], n: int = 9):
    """国内口径：RSV 后 K = 2/3·K' + 1/3·RSV，D = 2/3·D' + 1/3·K，J = 3K − 2D。"""
    k_out: list[float | None] = [None] * len(bars)
    d_out: list[float | None] = [None] * len(bars)
    j_out: list[float | None] = [None] * len(bars)
    k_prev, d_prev = 50.0, 50.0
    for i in range(len(bars)):
        if i < n - 1:
            continue
        window = bars[i - n + 1 : i + 1]
        high = max(float(bar["high"]) for bar in window)
        low = min(float(bar["low"]) for bar in window)
        close = float(bars[i]["close"])
        rsv = 50.0 if high == low else (close - low) / (high - low) * 100
        k_prev = k_prev * 2 / 3 + rsv / 3
        d_prev = d_prev * 2 / 3 + k_prev / 3
        k_out[i], d_out[i], j_out[i] = k_prev, d_prev, 3 * k_prev - 2 * d_prev
    return k_out, d_out, j_out


# ---------- 信号探测辅助 ----------

def _crossed_up(a: list[float | None], b: list[float | None], i: int) -> bool:
    if i < 1:
        return False
    if None in (a[i], b[i], a[i - 1], b[i - 1]):
        return False
    return a[i - 1] <= b[i - 1] and a[i] > b[i]  # type: ignore[operator]


def _crossed_down(a: list[float | None], b: list[float | None], i: int) -> bool:
    if i < 1:
        return False
    if None in (a[i], b[i], a[i - 1], b[i - 1]):
        return False
    return a[i - 1] >= b[i - 1] and a[i] < b[i]  # type: ignore[operator]


def _aligned(values_by_period: list[list[float | None]], i: int, ascending: bool) -> bool:
    pairs = zip(values_by_period, values_by_period[1:])
    for shorter, longer in pairs:
        if shorter[i] is None or longer[i] is None:
            return False
        if ascending and not shorter[i] > longer[i]:  # type: ignore[operator]
            return False
        if not ascending and not shorter[i] < longer[i]:  # type: ignore[operator]
            return False
    return True


def _rising(values: list[float | None], i: int) -> bool:
    return i >= 1 and values[i] is not None and values[i - 1] is not None and values[i] > values[i - 1]  # type: ignore[operator]


def _falling(values: list[float | None], i: int) -> bool:
    return i >= 1 and values[i] is not None and values[i - 1] is not None and values[i] < values[i - 1]  # type: ignore[operator]


def _series(bars: list[dict[str, Any]]) -> dict[str, list[float]]:
    return {
        "open": [float(bar["open"]) for bar in bars],
        "high": [float(bar["high"]) for bar in bars],
        "low": [float(bar["low"]) for bar in bars],
        "close": [float(bar["close"]) for bar in bars],
        "volume": [float(bar.get("volume") or 0.0) for bar in bars],
    }


def _indicator_context(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """一次算好全部指标序列，各战法共享（scan 逐票只算一遍）。"""
    series = _series(bars)
    closes = series["close"]
    ma5, ma10, ma20 = sma(closes, 5), sma(closes, 10), sma(closes, 20)
    dif, dea, hist = macd(closes)
    k, d, j = kdj(bars)
    rsi = rsi_wilder(closes)
    vol_ma20 = sma(series["volume"], 20)
    return {
        "series": series, "ma5": ma5, "ma10": ma10, "ma20": ma20,
        "dif": dif, "dea": dea, "hist": hist, "k": k, "d": d, "j": j,
        "rsi": rsi, "vol_ma20": vol_ma20,
    }


# ---------- 各战法探测器（ctx = _indicator_context 结果） ----------

def _detect_ma_golden(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    ma5, ma20, closes = ctx["ma5"], ctx["ma20"], ctx["series"]["close"]
    for i in range(1, len(closes)):
        if _crossed_up(ma5, ma20, i):
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"MA5 上穿 MA20（{ma5[i]:.2f} > {ma20[i]:.2f}）"})
    return signals


def _detect_ma_death(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    ma5, ma20, closes = ctx["ma5"], ctx["ma20"], ctx["series"]["close"]
    for i in range(1, len(closes)):
        if _crossed_down(ma5, ma20, i):
            signals.append({"index": i, "direction": BEARISH,
                            "detail": f"MA5 下穿 MA20（{ma5[i]:.2f} < {ma20[i]:.2f}）"})
    return signals


def _detect_ma_bull_align(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    ma5, ma10, ma20 = ctx["ma5"], ctx["ma10"], ctx["ma20"]
    stacks = [ma5, ma10, ma20]
    for i in range(1, len(ctx["series"]["close"])):
        if _aligned(stacks, i, ascending=True) and all(_rising(m, i) for m in stacks):
            signals.append({"index": i, "direction": BULLISH,
                            "detail": "MA5 > MA10 > MA20 且三线向上"})
    return signals


def _detect_ma_bear_align(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    ma5, ma10, ma20 = ctx["ma5"], ctx["ma10"], ctx["ma20"]
    stacks = [ma5, ma10, ma20]
    for i in range(1, len(ctx["series"]["close"])):
        if _aligned(stacks, i, ascending=False) and all(_falling(m, i) for m in stacks):
            signals.append({"index": i, "direction": BEARISH,
                            "detail": "MA5 < MA10 < MA20 且三线向下"})
    return signals


def _detect_macd_golden(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    dif, dea = ctx["dif"], ctx["dea"]
    for i in range(1, len(dif)):
        if _crossed_up(dif, dea, i):
            below_zero = dea[i] < 0
            signals.append({"index": i, "direction": BULLISH,
                            "detail": "DIF 上穿 DEA" + ("（零轴下方，超卖区金叉）" if below_zero else "")})
    return signals


def _detect_macd_death(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    dif, dea = ctx["dif"], ctx["dea"]
    for i in range(1, len(dif)):
        if _crossed_down(dif, dea, i):
            above_zero = dea[i] > 0
            signals.append({"index": i, "direction": BEARISH,
                            "detail": "DIF 下穿 DEA" + ("（零轴上方，高位死叉）" if above_zero else "")})
    return signals


def _detect_volume_breakout(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    closes, volumes, vol_ma20 = ctx["series"]["close"], ctx["series"]["volume"], ctx["vol_ma20"]
    for i in range(21, len(closes)):
        if vol_ma20[i] is None or vol_ma20[i] <= 0:  # type: ignore[operator]
            continue
        ratio = volumes[i] / vol_ma20[i]  # type: ignore[operator]
        prior_high = max(closes[i - 20 : i])
        if ratio >= 2 and closes[i] > prior_high:
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"成交量 {ratio:.1f} 倍于 20 日均量，收盘突破前 20 日收盘高点 {prior_high:.2f}"})
    return signals


def _detect_support_hold(ctx: dict[str, Any], bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from investment_steward_core.support_resistance import detect_levels

    signals = []
    series = ctx["series"]
    n = len(series["close"])
    if n < 25:
        return signals
    # 支撑位只用「当前考察 bar 之前」的历史计算，避免未来函数。
    levels = detect_levels(bars[:-3])
    supports = [float(level["price"]) for level in levels if level.get("kind") == "support"]
    for i in range(n - 3, n):
        low, open_, close = series["low"][i], series["open"][i], series["close"][i]
        if close <= open_:
            continue
        for level in supports:
            if level * 0.995 <= low <= level * 1.005:
                signals.append({"index": i, "direction": BULLISH,
                                "detail": f"回踩支撑位 {level:.2f} 附近收阳企稳"})
                break
    return signals


def _detect_kdj_golden(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    k, d = ctx["k"], ctx["d"]
    for i in range(1, len(k)):
        if _crossed_up(k, d, i) and k[i] < 30:  # type: ignore[operator]
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"KDJ 低位金叉（K={k[i]:.1f} 上穿 D={d[i]:.1f}，K<30）"})
    return signals


def _detect_kdj_death(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    k, d = ctx["k"], ctx["d"]
    for i in range(1, len(k)):
        if _crossed_down(k, d, i) and k[i] > 70:  # type: ignore[operator]
            signals.append({"index": i, "direction": BEARISH,
                            "detail": f"KDJ 高位死叉（K={k[i]:.1f} 下穿 D={d[i]:.1f}，K>70）"})
    return signals


def _swing_extremes(values: list[float], lookback: int, i: int, find: str) -> tuple[int, int] | None:
    """在 (i-lookback, i] 窗口内找两个低点/高点（较早者、较晚者），晚点须落在最近 10 根内。"""
    window = values[max(0, i - lookback) : i + 1]
    if len(window) < 20:
        return None
    offset = max(0, i - lookback)
    earlier = window[:-10]
    later = window[-10:]
    if not earlier or not later:
        return None
    pick = min if find == "low" else max
    first = pick(earlier)
    second = pick(later)
    i1 = offset + earlier.index(first)
    i2 = offset + len(earlier) + later.index(second)
    return (i1, i2) if i1 < i2 else None


def _detect_rsi_bullish_divergence(ctx: dict[str, Any], lookback: int = 60) -> list[dict[str, Any]]:
    signals = []
    closes, rsi = ctx["series"]["close"], ctx["rsi"]
    for i in range(20, len(closes)):
        if rsi[i] is None:
            continue
        pair = _swing_extremes(closes, lookback, i, "low")
        if pair is None:
            continue
        i1, i2 = pair
        if i2 != i or rsi[i1] is None or rsi[i2] is None:
            continue
        if closes[i2] < closes[i1] and rsi[i2] > rsi[i1] + 2:  # type: ignore[operator]
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"底背离：价创新低（{closes[i2]:.2f} < {closes[i1]:.2f}）而 RSI 抬高（{rsi[i2]:.1f} > {rsi[i1]:.1f}）"})
    return signals


def _detect_rsi_bearish_divergence(ctx: dict[str, Any], lookback: int = 60) -> list[dict[str, Any]]:
    signals = []
    highs, rsi = ctx["series"]["high"], ctx["rsi"]
    for i in range(20, len(highs)):
        if rsi[i] is None:
            continue
        pair = _swing_extremes(highs, lookback, i, "high")
        if pair is None:
            continue
        i1, i2 = pair
        if i2 != i or rsi[i1] is None or rsi[i2] is None:
            continue
        if highs[i2] > highs[i1] and rsi[i2] < rsi[i1] - 2:  # type: ignore[operator]
            signals.append({"index": i, "direction": BEARISH,
                            "detail": f"顶背离：价创新高（{highs[i2]:.2f} > {highs[i1]:.2f}）而 RSI 走低（{rsi[i2]:.1f} < {rsi[i1]:.1f}）"})
    return signals


def _detect_doji(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    open_, close, high, low = ctx["series"]["open"], ctx["series"]["close"], ctx["series"]["high"], ctx["series"]["low"]
    for i in range(len(close)):
        spread = high[i] - low[i]
        if spread <= 0:
            continue
        body = abs(close[i] - open_[i])
        if body <= spread / 3:
            signals.append({"index": i, "direction": NEUTRAL,
                            "detail": "十字星：多空拉锯，方向待选择"})
    return signals


def _detect_bullish_engulfing(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    open_, close = ctx["series"]["open"], ctx["series"]["close"]
    for i in range(1, len(close)):
        prev_bearish = close[i - 1] < open_[i - 1]
        cur_bullish = close[i] > open_[i]
        if prev_bearish and cur_bullish and close[i] >= open_[i - 1] and open_[i] <= close[i - 1]:
            signals.append({"index": i, "direction": BULLISH,
                            "detail": "阳吞阴：阳线实体完全包住前一日阴线"})
    return signals


def _detect_bearish_engulfing(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    signals = []
    open_, close = ctx["series"]["open"], ctx["series"]["close"]
    for i in range(1, len(close)):
        prev_bullish = close[i - 1] > open_[i - 1]
        cur_bearish = close[i] < open_[i]
        if prev_bullish and cur_bearish and close[i] <= open_[i - 1] and open_[i] >= close[i - 1]:
            signals.append({"index": i, "direction": BEARISH,
                            "detail": "阴吞阳：阴线实体完全包住前一日阳线"})
    return signals


# ---------- 人格化战法扩展（2026-09-07 用户追加；均为 K 线确定性口径） ----------

def _detect_platform_breakout(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """突破：收盘创近 20 日收盘新高且当日实体涨幅 ≥ 3%（纯价格口径，不要求放量）。"""
    signals = []
    open_, close = ctx["series"]["open"], ctx["series"]["close"]
    for i in range(20, len(close)):
        prior_high = max(close[i - 20 : i])
        gain = (close[i] - open_[i]) / open_[i] * 100 if open_[i] > 0 else 0.0
        if close[i] > prior_high and gain >= 3.0:
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"突破 20 日平台：收盘 {close[i]:.2f} 创 20 日新高，实体涨幅 {gain:.1f}%"})
    return signals


def _detect_n_shape(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """N 字上涨：第一段涨（5 日前 > 7 日前）→ 回调（2 日前 < 4 日前）→ 今日收阳且收盘越过回调起点。"""
    signals = []
    open_, close = ctx["series"]["open"], ctx["series"]["close"]
    for i in range(7, len(close)):
        first_leg_up = close[i - 5] > close[i - 7]
        pullback = close[i - 2] < close[i - 4]
        resume = close[i] > close[i - 2] and close[i] > open_[i]
        if first_leg_up and pullback and resume:
            signals.append({"index": i, "direction": BULLISH,
                            "detail": "N 字上涨：涨 → 回调 → 再收阳启动，回调未破前低结构"})
    return signals


def _detect_ma20_support(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """均线辅助：MA20 向上，今日最低回踩 MA20 ±1.5% 以内，收盘站上 MA20 且收阳。"""
    signals = []
    low, close, open_ = ctx["series"]["low"], ctx["series"]["close"], ctx["series"]["open"]
    ma20 = ctx["ma20"]
    for i in range(1, len(close)):
        if ma20[i] is None or ma20[i - 1] is None:
            continue
        ma = float(ma20[i])  # type: ignore[arg-type]
        rising = float(ma20[i]) > float(ma20[i - 1])  # type: ignore[arg-type]
        touched = low[i] <= ma * 1.015 and low[i] >= ma * 0.985
        held = close[i] > ma and close[i] > open_[i]
        if rising and touched and held:
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"均线辅助：回踩上行的 MA20（{ma:.2f}）后收阳企稳"})
    return signals


def _detect_swing_higher_low(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """波段：近 10 日最低价高于之前 10 日最低价（低点抬高），且 MA20 向上——波段上行结构。"""
    signals = []
    low = ctx["series"]["low"]
    ma20 = ctx["ma20"]
    for i in range(20, len(low)):
        if ma20[i] is None or ma20[i - 1] is None:
            continue
        recent_low = min(low[i - 10 : i])
        prior_low = min(low[i - 20 : i - 10])
        rising_ma = float(ma20[i]) > float(ma20[i - 1])  # type: ignore[arg-type]
        if recent_low > prior_low and rising_ma:
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"波段低点抬高：近 10 日低点 {recent_low:.2f} > 前 10 日低点 {prior_low:.2f}，MA20 向上"})
    return signals


def _detect_weak_to_strong(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """弱转强（近似口径）：昨日长上影阴线（上影 ≥ 实体 2 倍）→ 今日高开 ≥ 1.5% 且收阳。

    如实声明：正统弱转强需要涨停板/封板强度数据，此处为纯 K 线近似。
    """
    signals = []
    open_, close, high = ctx["series"]["open"], ctx["series"]["close"], ctx["series"]["high"]
    for i in range(1, len(close)):
        prev_body = open_[i - 1] - close[i - 1]
        prev_shadow = high[i - 1] - open_[i - 1]
        prev_bearish_with_shadow = close[i - 1] < open_[i - 1] and prev_body > 0 and prev_shadow >= prev_body * 2
        gap_open = (open_[i] - close[i - 1]) / close[i - 1] * 100 if close[i - 1] > 0 else 0.0
        if prev_bearish_with_shadow and gap_open >= 1.5 and close[i] > open_[i]:
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"弱转强（K 线近似）：昨日长上影阴线，今日高开 {gap_open:.1f}% 收阳"})
    return signals


def _detect_reversal_after_crash(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """反核（近似口径）：昨日大跌 ≥ 5%，今日盘中翻上昨收（收复大跌），代表承接力量。

    如实声明：正统反核需要跌停/封单数据，此处为纯 K 线近似。
    """
    signals = []
    close = ctx["series"]["close"]
    for i in range(1, len(close)):
        prev_drop = (close[i - 1] - close[i - 2]) / close[i - 2] * 100 if i >= 2 and close[i - 2] > 0 else 0.0
        recovered = close[i] >= close[i - 1]
        if i >= 2 and prev_drop <= -5.0 and recovered:
            signals.append({"index": i, "direction": BULLISH,
                            "detail": f"反核修复（K 线近似）：昨日大跌 {prev_drop:.1f}%，今日收复至 {close[i]:.2f}"})
    return signals


# ---------- 战法注册表（id / 名称 / 方向 / 说明 / 探测器） ----------

# 战法权重版本（v24）：weight / min_bars 随代码发布、任何人可复算。
# 变更任何 weight 或 min_bars 都必须递增此版本号，并在评分结果里如实回传。
TACTIC_WEIGHT_VERSION = "v1"

# 战法语义版本（v27，方案 §4.2）：新增 cooldown_bars / confirmation_rule / invalidation_rule。
# 变更这三项中的任意一项都必须递增此版本号——它们同样影响信号的可复算解释。
TACTIC_SEMANTICS_VERSION = "v1"

# weight 口径：形态的「证据强度」，不是收益预测、不是买卖建议。
# 依据（可逐条对照探测器实现）：
#   - 需要多指标共振或量价配合确认的形态权重高（volume_breakout 8.0：量 ≥2 倍且创 20 日新高）；
#   - 单指标交叉次之（MA 金叉 7.0 / MACD 6.5 / KDJ 4.5，KDJ 阈值宽松故更低）；
#   - 纯 K 线近似口径权重最低（weak_to_strong / reversal_after_crash 3.5，模块内已声明需涨停板数据）；
#   - 十字星为方向不确定的观望形态，权重 1.0，避免高频触发把计数膨胀成「信号丰富」。
# min_bars 口径：该探测器产出首个有效信号所需的最少 K 线数，由各 detect 函数的循环起点与
#   指标窗口推导（例如 _detect_volume_breakout 从 range(21,·) 起 → 22；_detect_support_hold 要求 n≥25）。
#
# v27 三个语义字段（都是**文字化的可验证条件**，不是新的分数来源）：
#   cooldown_bars      —— 同一形态在多少根 K 线内视为「同一次触发」（重复出现不视为新信号；
#                         评分侧另由 tactics_score 的「同战法只取最近一次」兜底）；
#   confirmation_rule  —— 什么算「被后续行情确认」（没被确认的信号应如实标为待确认）；
#   invalidation_rule  —— 什么条件下该信号作废（**方案 §3.5 的失效条件由此派生**）。
# ★ 诚实声明：confirmation/invalidation 目前是**规则文本 + 关键价位**，尚未做逐信号的
#   「已确认/已失效」状态回填——那需要后验链路（方案第二期），本期不假装已经做到。
TACTICS: list[dict[str, Any]] = [
    {"id": "ma_golden_cross", "name": "均线金叉", "direction": BULLISH, "weight": 7.0, "min_bars": 21,
     "cooldown_bars": 5, "confirmation_rule": "未来 3 根 K 线至少 2 根收在 MA5 上方",
     "invalidation_rule": "MA5 重新下穿 MA20（收盘价口径）",
     "description": "MA5 上穿 MA20，短线动能转强的经典起点信号。", "category": "ma", "detect": _detect_ma_golden,},
    {"id": "ma_death_cross", "name": "均线死叉", "direction": BEARISH, "weight": 7.0, "min_bars": 21,
     "cooldown_bars": 5, "confirmation_rule": "未来 3 根 K 线至少 2 根收在 MA5 下方",
     "invalidation_rule": "MA5 重新上穿 MA20（收盘价口径）",
     "description": "MA5 下穿 MA20，短线动能转弱的经典起点信号。", "category": "ma", "detect": _detect_ma_death,},
    {"id": "ma_bullish_alignment", "name": "均线多头排列", "direction": BULLISH, "weight": 6.0, "min_bars": 21,
     "cooldown_bars": 3, "confirmation_rule": "三线维持 MA5>MA10>MA20 且 MA20 不下拐",
     "invalidation_rule": "MA5 跌破 MA10",
     "description": "MA5 > MA10 > MA20 且三线向上，趋势行情的持有确认形态。", "category": "ma", "detect": _detect_ma_bull_align,},
    {"id": "ma_bearish_alignment", "name": "均线空头排列", "direction": BEARISH, "weight": 6.0, "min_bars": 21,
     "cooldown_bars": 3, "confirmation_rule": "三线维持 MA5<MA10<MA20 且 MA20 不上拐",
     "invalidation_rule": "MA5 升破 MA10",
     "description": "MA5 < MA10 < MA20 且三线向下，下行趋势的规避信号。", "category": "ma", "detect": _detect_ma_bear_align,},
    {"id": "macd_golden_cross", "name": "MACD 金叉", "direction": BULLISH, "weight": 6.5, "min_bars": 35,
     "cooldown_bars": 5, "confirmation_rule": "DIF 与 DEA 同步上行、柱状体转正并放大",
     "invalidation_rule": "DIF 重新下穿 DEA",
     "description": "DIF 上穿 DEA；零轴下方出现时代表超卖区修复，权重更高。", "category": "momentum", "detect": _detect_macd_golden,},
    {"id": "macd_death_cross", "name": "MACD 死叉", "direction": BEARISH, "weight": 6.5, "min_bars": 35,
     "cooldown_bars": 5, "confirmation_rule": "DIF 与 DEA 同步下行、柱状体转负并放大",
     "invalidation_rule": "DIF 重新上穿 DEA",
     "description": "DIF 下穿 DEA；零轴上方出现时代表高位转弱，需要警惕。", "category": "momentum", "detect": _detect_macd_death,},
    {"id": "volume_breakout", "name": "放量突破", "direction": BULLISH, "weight": 8.0, "min_bars": 22,
     "cooldown_bars": 5, "confirmation_rule": "次日收盘不回落至被突破的前 20 日高点之下",
     "invalidation_rule": "收盘跌回前 20 日高点之下且量能萎缩",
     "description": "成交量 ≥ 2 倍 20 日均量且收盘突破前 20 日收盘高点，量价齐升。", "category": "volume_price", "detect": _detect_volume_breakout,},
    {"id": "support_hold", "name": "回踩支撑企稳", "direction": BULLISH, "weight": 6.0, "min_bars": 25,
     "cooldown_bars": 3, "confirmation_rule": "未来 3 根 K 线收盘不破该支撑位",
     "invalidation_rule": "收盘有效跌破该支撑位（幅度 >1%）",
     "description": "近 3 日回踩关键支撑位（聚类算法识别）±0.5% 后收阳，未破位。", "category": "volume_price", "detect": _detect_support_hold,},
    {"id": "kdj_golden_cross", "name": "KDJ 低位金叉", "direction": BULLISH, "weight": 4.5, "min_bars": 10,
     "cooldown_bars": 3, "confirmation_rule": "K 值继续上行并站上 50 中轴",
     "invalidation_rule": "K 值重新下穿 D 值",
     "description": "K 值在 30 以下上穿 D 值，超卖区短线反转信号。", "category": "momentum", "detect": _detect_kdj_golden,},
    {"id": "kdj_death_cross", "name": "KDJ 高位死叉", "direction": BEARISH, "weight": 4.5, "min_bars": 10,
     "cooldown_bars": 3, "confirmation_rule": "K 值继续下行并跌破 50 中轴",
     "invalidation_rule": "K 值重新上穿 D 值",
     "description": "K 值在 70 以上下穿 D 值，超买区短线回落信号。", "category": "momentum", "detect": _detect_kdj_death,},
    {"id": "rsi_bullish_divergence", "name": "RSI 底背离", "direction": BULLISH, "weight": 6.5, "min_bars": 21,
     "cooldown_bars": 10, "confirmation_rule": "价格不再创新低且 RSI 维持上行",
     "invalidation_rule": "价格再创新低且 RSI 同步走低（背离被推倒）",
     "description": "价格创阶段新低而 RSI 低点抬高，下跌动能衰竭（启发式识别，60 日窗口）。", "category": "momentum", "detect": _detect_rsi_bullish_divergence,},
    {"id": "rsi_bearish_divergence", "name": "RSI 顶背离", "direction": BEARISH, "weight": 6.5, "min_bars": 21,
     "cooldown_bars": 10, "confirmation_rule": "价格不再创新高且 RSI 维持下行",
     "invalidation_rule": "价格再创新高且 RSI 同步走高（背离被推倒）",
     "description": "价格创阶段新高而 RSI 高点走低，上涨动能衰竭（启发式识别，60 日窗口）。", "category": "momentum", "detect": _detect_rsi_bearish_divergence,},
    {"id": "doji", "name": "十字星", "direction": NEUTRAL, "weight": 1.0, "min_bars": 1,
     "cooldown_bars": 1, "confirmation_rule": "次日以放量阳线（或阴线）确认方向",
     "invalidation_rule": "次日继续窄幅震荡，方向仍未选择",
     "description": "实体 ≤ 全振幅 1/3，多空拉锯；趋势末端出现时观察方向选择。", "category": "volume_price", "detect": _detect_doji,},
    {"id": "bullish_engulfing", "name": "阳吞阴", "direction": BULLISH, "weight": 5.0, "min_bars": 2,
     "cooldown_bars": 3, "confirmation_rule": "次日收盘不低于该阳线实体中点",
     "invalidation_rule": "次日收盘跌回被吞没阴线的实体之内",
     "description": "阳线实体完全包住前一日阴线，短线做多动能占优。", "category": "volume_price", "detect": _detect_bullish_engulfing,},
    {"id": "bearish_engulfing", "name": "阴吞阳", "direction": BEARISH, "weight": 5.0, "min_bars": 2,
     "cooldown_bars": 3, "confirmation_rule": "次日收盘不高于该阴线实体中点",
     "invalidation_rule": "次日收盘升回被吞没阳线的实体之内",
     "description": "阴线实体完全包住前一日阳线，短线做空动能占优。", "category": "volume_price", "detect": _detect_bearish_engulfing,},
    # —— 人格化战法扩展（2026-09-07 用户追加：波段/突破/N字/均线辅助/弱转强/反核；反包=阳吞阴已有） ——
    {"id": "platform_breakout", "name": "突破平台", "direction": BULLISH, "weight": 7.0, "min_bars": 21,
     "cooldown_bars": 5, "confirmation_rule": "未来 3 根至少 2 根收盘站稳平台高点",
     "invalidation_rule": "收盘跌回平台区间内",
     "description": "收盘创近 20 日收盘新高且实体涨幅 ≥ 3%（纯价格突破口径，不要求放量）。", "category": "volume_price", "detect": _detect_platform_breakout,},
    {"id": "n_shape_up", "name": "N 字上涨", "direction": BULLISH, "weight": 5.5, "min_bars": 8,
     "cooldown_bars": 5, "confirmation_rule": "后续放量上行且不破回调段低点",
     "invalidation_rule": "收盘跌破回调段低点",
     "description": "涨 → 回调 → 再收阳启动的 N 字结构，回调未破前低（固定 7 日窗口，可复现）。", "category": "structure", "detect": _detect_n_shape,},
    {"id": "ma20_support", "name": "均线辅助回踩", "direction": BULLISH, "weight": 5.5, "min_bars": 21,
     "cooldown_bars": 3, "confirmation_rule": "未来 3 根 K 线收在 MA20 上方",
     "invalidation_rule": "收盘跌破 MA20",
     "description": "MA20 向上时回踩 MA20 ±1.5% 后收阳企稳——均线辅助买点的确定性口径。", "category": "ma", "detect": _detect_ma20_support,},
    {"id": "swing_higher_low", "name": "波段低点抬高", "direction": BULLISH, "weight": 5.5, "min_bars": 21,
     "cooldown_bars": 5, "confirmation_rule": "低点继续抬高且 MA20 维持向上",
     "invalidation_rule": "收盘跌破最近一个抬高的低点",
     "description": "近 10 日最低价高于前 10 日最低价且 MA20 向上，波段上行结构。", "category": "structure", "detect": _detect_swing_higher_low,},
    {"id": "weak_to_strong", "name": "弱转强（近似）", "direction": BULLISH, "weight": 3.5, "min_bars": 2,
     "cooldown_bars": 3, "confirmation_rule": "当日封住涨停或收盘逼近涨停且量能配合",
     "invalidation_rule": "次日回补高开缺口并收在昨日收盘之下",
     "description": "昨日长上影阴线，今日高开 ≥ 1.5% 收阳。K 线近似口径——正统弱转强需涨停板/封板强度数据。", "category": "sentiment", "detect": _detect_weak_to_strong,},
    {"id": "reversal_after_crash", "name": "反核修复（近似）", "direction": BULLISH, "weight": 3.5, "min_bars": 3,
     "cooldown_bars": 3, "confirmation_rule": "次日不跌破当日低点",
     "invalidation_rule": "次日收在当日收盘之下",
     "description": "昨日大跌 ≥ 5% 后今日收复昨收。K 线近似口径——正统反核需跌停/封单数据。", "category": "sentiment", "detect": _detect_reversal_after_crash,},
]

_TACTIC_BY_ID = {tactic["id"]: tactic for tactic in TACTICS}


def catalog() -> list[dict[str, Any]]:
    """战法目录（给前端展示说明用，不含探测函数；含分类、权重与 v27 语义字段）。

    `family` 是 `category` 的对等别名（方案 §4.2 的术语），显式回传以免前端各写一套映射；
    `confirmation_rule` / `invalidation_rule` / `cooldown_bars` 供 UI 直接展示
    「什么算被确认、什么算失效、多近算同一次触发」。
    """
    return [
        {
            "id": tactic["id"], "name": tactic["name"], "direction": tactic["direction"],
            "description": tactic["description"], "category": tactic["category"],
            "family": tactic["category"], "weight": tactic["weight"], "min_bars": tactic["min_bars"],
            "cooldown_bars": tactic["cooldown_bars"],
            "confirmation_rule": tactic["confirmation_rule"],
            "invalidation_rule": tactic["invalidation_rule"],
        }
        for tactic in TACTICS
    ]


def snapshot(bars: list[dict[str, Any]], *, recent: int = 120, tactic_ids: set[str] | None = None) -> dict[str, Any]:
    """单票全景：指标当前值 + 最近 recent 根 K 线内全部战法信号。

    bars 升序 OHLCV dict；数据不足 MIN_BARS 时 signals 为空并如实标注。
    tactic_ids 非空时只跑该子集（市场雷达战法选择），None/空集 = 全部战法。
    """
    n = len(bars)
    ctx = _indicator_context(bars)
    closes = ctx["series"]["close"]
    active_ids: set[str] | None = set(tactic_ids) if tactic_ids else None
    signals: list[dict[str, Any]] = []
    for tactic in TACTICS:
        if active_ids is not None and tactic["id"] not in active_ids:
            continue
        try:
            found = tactic["detect"](ctx)
        except Exception:  # 单个战法异常不拖垮整体；如实丢弃而不是编造。
            continue
        for signal in found:
            index = int(signal["index"])
            # v27：信号自带「触发价」与「年龄」——方案 §4.2 要求探测器返回可直接复算的价位，
            # 而不是让前端/AI 自己从 K 线里猜。价格按展示精度（2 位）收口，避免裸浮点混进证据文本。
            trigger_price: float | None = None
            if 0 <= index < len(closes):
                try:
                    trigger_price = round(float(closes[index]), 2)
                except (TypeError, ValueError):
                    trigger_price = None
            signals.append({
                "tactic_id": tactic["id"],
                "tactic_name": tactic["name"],
                "direction": signal["direction"],
                "detail": signal["detail"],
                "date": str(bars[index]["timestamp"])[:10] if 0 <= index < len(bars) else "",
                "family": tactic["category"],
                "trigger_price": trigger_price,
                "age_bars": max(0, n - 1 - index),
                "cooldown_bars": tactic["cooldown_bars"],
                "confirmation_rule": tactic["confirmation_rule"],
                "invalidation_rule": tactic["invalidation_rule"],
            })
    signals.sort(key=lambda item: str(item["date"]))
    cutoff = signals[-recent:] if len(signals) > recent else signals
    latest = lambda series: series[-1] if series and series[-1] is not None else None  # noqa: E731
    return {
        "bars_count": n,
        "sufficient": n >= MIN_BARS,
        "indicators": {
            "close": closes[-1] if closes else None,
            "ma5": latest(ctx["ma5"]), "ma10": latest(ctx["ma10"]), "ma20": latest(ctx["ma20"]),
            "macd_dif": latest(ctx["dif"]), "macd_dea": latest(ctx["dea"]),
            "kdj_k": latest(ctx["k"]), "kdj_d": latest(ctx["d"]), "kdj_j": latest(ctx["j"]),
            "rsi14": latest(ctx["rsi"]),
            "volume": ctx["series"]["volume"][-1] if n else None,
            "volume_ma20": latest(ctx["vol_ma20"]),
        },
        "signals": [
            {key: value for key, value in signal.items() if key != "index"}
            for signal in cutoff
        ],
    }

