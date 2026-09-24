"""战法质量评分（v24 起；v2 于 2026-09-11 升级）。

设计铁律（与全站一致）：
- 纯函数、确定性：同输入同结果，不联网、不用 AI、不读系统时间；
- **每个分项都可在结果里逐股展开**（score_parts / hit_detail），用户可自行复算；
- 反向信号只做对冲扣减，**绝不作为数量加分**（旧口径把金叉+死叉当成「信号丰富」）；
- 同一战法重复触发只取最近一次（避免同一形态刷分），并在 hit_detail 里保留其实际日期；
- 分项权重随代码发布并版本化（tactics.TACTIC_WEIGHT_VERSION），改动必须升版本；
- 本模块只描述「图形证据的强度与新鲜度」，不构成任何买卖建议，也不预测收益。

评分公式 v2（相对 v1 的四处升级，2026-09-11 用户要求「提高扫描质量评分」；
v1 的问题是真机扫描里前 5 名出现 3 个满分，区分度不足）：

    freshness    = 0.5 ** (age_bars / HALF_LIFE)      （age_bars = 信号 K 线距最新 K 线根数）
    contribution = weight × freshness                  （weight 见 tactics.TACTICS[·]["weight"]）
    base         = 同向各命中 contribution 之和，**同族（category）只取最高的一条**
    resonance    = 1.0 ×（占优方向计分到的 category 数 − 1）
    conflict     = 0.5 × min(base_bull, base_bear)     （多空同时存在时的对冲扣减）
    neutral      = 中性形态贡献之和，按 20% 计入，不参与方向分
    bonus_scale  = 占优方向计分信号里最大的 freshness   （新维度随信号新鲜度缩放：
                   没有新鲜信号的票不该靠量价/位置/板块拿分）
    volume_bonus = 2.0 × clamp((量比 − 0.7) / (1.5 − 0.7), 0, 1) × bonus_scale
    position_bonus：最新收盘在近 60 根 high-low 区间的相对位置 pos
                   pos ≤ 0.5 → +1.5；0.5 < pos < 0.9 线性过渡到 −1.0；pos ≥ 0.9 → −1.0（× bonus_scale）
    sector_bonus = 3.0 × 同向票占比（仅在扫描提供板块上下文、且方向与占优方向一致时）× bonus_scale
    raw          = max(base_bull, base_bear) + resonance + 0.2 × neutral − conflict
                   + volume_bonus + position_bonus + sector_bonus
    tactic_score = 100 × (1 − exp(−raw / SATURATION_SCALE))   ← 饱和曲线，高分段仍有区分度

为什么用饱和曲线而不是线性截断：v1 的 `min(100, raw/45×100)` 在强势股多形态共振时直接贴顶，
前 5 名出现多个 100 分（真机截图），排名退化。指数饱和让 100 分成为「理论上限」而非常客，
中高分段（60–95）保持斜率——这是扫描排序最需要的区间。

新鲜度采用**固定半衰期**（HALF_LIFE 根 K 线），而不是「按窗口长度线性衰减」。原因（实测）：
后者在长窗口下旧信号仍保留很高新鲜度，多条叠加会迅速把分数顶到 100 —— 用真实行情跑 60 根以上
窗口时几乎恒为满分，失去区分度。固定半衰期下分数随信号变旧而收敛，因此**与窗口长度无关**，
窗口只决定「哪些信号还算数」，这也是 recent_bars 的本来语义。
"""

from __future__ import annotations

from typing import Any

from investment_steward_core.tactics import TACTIC_SEMANTICS_VERSION, TACTIC_WEIGHT_VERSION, TACTICS

_WEIGHT_BY_ID: dict[str, float] = {str(item["id"]): float(item["weight"]) for item in TACTICS}
_CATEGORY_BY_ID: dict[str, str] = {str(item["id"]): str(item["category"]) for item in TACTICS}
_MIN_BARS_BY_ID: dict[str, int] = {str(item["id"]): int(item["min_bars"]) for item in TACTICS}
# v27：语义字段（方案的 §4.2「信号语义补全」）——从注册表取，保证与 tactics.catalog() 同源。
_COOLDOWN_BY_ID: dict[str, int] = {str(item["id"]): int(item["cooldown_bars"]) for item in TACTICS}
_CONFIRMATION_BY_ID: dict[str, str] = {str(item["id"]): str(item["confirmation_rule"]) for item in TACTICS}
_INVALIDATION_BY_ID: dict[str, str] = {str(item["id"]): str(item["invalidation_rule"]) for item in TACTICS}

# 公式版本：随任何系数/结构改动递增；结果里如实返回，用户据此判断可比性。
SCORE_FORMULA_VERSION = "v2"

# 新鲜度半衰期（K 线根数）：信号每老 HALF_LIFE 根 K 线，贡献折半。10 根 ≈ 两周交易日。
FRESHNESS_HALF_LIFE_BARS = 10.0

# —— 饱和曲线尺度（v2）——
# score = 100 × (1 − exp(−raw / SATURATION_SCALE))。标定锚点：
#   raw=8  → 24.9；raw=15 → 41.5；raw=25 → 59.0；raw=40 → 76.0；raw=60 → 88.2；raw=90 → 96.0。
# 即：单一高权重形态（权重 7.0）当日命中 ≈ 22 分；跨 3 族共振 + 中等 bonus ≈ 60 分；
# 90 分以上需要「多族强共振 + 量价位置确认」，100 分在数学上不可达（渐近线）。
SATURATION_SCALE = 28.0
RESONANCE_STEP = 1.0
CONFLICT_DAMPING = 0.5
NEUTRAL_FACTOR = 0.2

# —— 同族合并（v2）——
# 同 category 的信号只计贡献最高的一条（其余在 hit_detail 里标注 family_merged=True 且不计分）。
# 反例（v1）：均线金叉 + 均线多头排列 + 均线辅助回踩 三条 ma 族信号叠加成 18.5 分，
# 但它们描述的是同一件事（均线走强），属于「同一形态刷分」。
FAMILY_MERGE = True

# —— 量能确认（v2）——
# 量比 = 计分信号当日成交量 / 该日之前 VOLUME_LOOKBACK_BARS 根的均量。
VOLUME_BONUS_MAX = 2.0
VOLUME_RATIO_LOW = 0.7
VOLUME_RATIO_HIGH = 1.5
VOLUME_LOOKBACK_BARS = 20

# —— 位置维度（v2）——
# pos = (最新收盘 − 近 POSITION_WINDOW_BARS 根最低) / (最高 − 最低)；低位形态更有价值，追高衰减。
POSITION_WINDOW_BARS = 60
POSITION_LOW_THRESHOLD = 0.5
POSITION_HIGH_THRESHOLD = 0.9
POSITION_BONUS_LOW = 1.5
POSITION_BONUS_HIGH = -1.0

# —— 板块共振（v2）——
# 扫描同一板块时，按「同向票占比」给板块内个股加分（横截面因子，单票扫描时不可用，如实置 0）。
SECTOR_RESONANCE_MAX = 3.0

# 默认命中窗口（K 线根数）：窗口只决定「哪些信号还算数」，不参与归一化。
# DEFAULT_WINDOW_BARS 与 /tactics/scan 的 recent_bars 默认值同源；改动必须同步两端。
DEFAULT_WINDOW_BARS = 5
# 单票详情默认窗口（≈ 一个季度，与 recent_bars 的取值上限一致）。
DETAIL_WINDOW_BARS = 60

DIRECTION_BULLISH = "bullish"
DIRECTION_BEARISH = "bearish"
DIRECTION_NEUTRAL = "neutral"
DIRECTION_CONFLICT = "conflict"


def _bar_date(bar: Any) -> str:
    """K 线日期（ISO 8601 的日期段）；缺失时返回空串，调用方按「未知」处理。"""
    return str(bar.get("timestamp", ""))[:10] if isinstance(bar, dict) else ""


def signal_window(
    signals: list[dict[str, Any]], bars: list[dict[str, Any]], recent_bars: int
) -> tuple[list[dict[str, Any]], str | None]:
    """按 **K 线日期下界** 过滤信号（修正 v23 按「最近 N 条信号」截断的语义偏差）。

    recent_bars 是「最近 N 根 K 线」，因此下界 = bars[-N] 的日期；窗口内信号按日期升序返回。
    bars 为空时返回 (全部信号, None)，由调用方如实标注窗口未知。
    """
    if not bars:
        return list(signals), None
    take = max(1, min(int(recent_bars), len(bars)))
    from_date = _bar_date(bars[-take])
    if not from_date:
        return list(signals), None
    windowed = [item for item in signals if str(item.get("date", ""))[:10] >= from_date]
    return windowed, from_date


def _age_in_bars(signal_date: str, bars: list[dict[str, Any]]) -> int:
    """信号 K 线距最新 K 线的根数：0 = 最新一根，越大越旧。日期晚于最新 K 线按 0 处理。"""
    if not signal_date:
        return 0
    index = -1
    for i, bar in enumerate(bars):
        date = _bar_date(bar)
        if date and date <= signal_date:
            index = i
        elif date:
            break
    if index < 0:
        return 0
    return max(0, len(bars) - 1 - index)


def _latest_per_tactic(windowed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一战法在窗口内可能多次触发：只保留最近一次，避免同一形态重复加分。"""
    latest: dict[str, dict[str, Any]] = {}
    for signal in windowed:
        tactic_id = str(signal.get("tactic_id", ""))
        if not tactic_id:
            continue
        current = latest.get(tactic_id)
        if current is None or str(signal.get("date", "")) > str(current.get("date", "")):
            latest[tactic_id] = signal
    return sorted(latest.values(), key=lambda item: (str(item.get("date", "")), str(item.get("tactic_id", ""))))


def _lookup_bar(bars: list[dict[str, Any]], signal_date: str) -> dict[str, Any] | None:
    """按日期取 K 线（同一日期取最后一根，兼容同日多根口径）。"""
    found: dict[str, Any] | None = None
    for bar in bars:
        if _bar_date(bar) == signal_date:
            found = bar
    return found


def _volume_ratio(bars: list[dict[str, Any]], signal_date: str) -> float | None:
    """信号日量比 = 当日成交量 / 该日之前 VOLUME_LOOKBACK_BARS 根均量。

    数据缺失（无 volume 字段、前置根数不足、均量为 0）返回 None，调用方按「不可用」处理。
    """
    bar = _lookup_bar(bars, signal_date)
    if bar is None:
        return None
    try:
        volume = float(bar.get("volume"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if volume <= 0:
        return None
    index = bars.index(bar)
    history = bars[max(0, index - VOLUME_LOOKBACK_BARS) : index]
    values: list[float] = []
    for item in history:
        try:
            values.append(float(item.get("volume")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    if len(values) < 5:
        return None
    average = sum(values) / len(values)
    if average <= 0:
        return None
    return round(volume / average, 3)


def _position_in_range(bars: list[dict[str, Any]]) -> tuple[float | None, int]:
    """最新收盘价在近 POSITION_WINDOW_BARS 根 high-low 区间中的相对位置（0=最低，1=最高）。

    返回（pos, 参与计算的根数）。区间退化（最高=最低）或数据不足 10 根时 pos 为 None。
    """
    window = bars[-POSITION_WINDOW_BARS:]
    highs: list[float] = []
    lows: list[float] = []
    for bar in window:
        try:
            highs.append(float(bar.get("high")))  # type: ignore[arg-type]
            lows.append(float(bar.get("low")))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    if len(highs) < 10 or not bars:
        return None, len(highs)
    top, bottom = max(highs), min(lows)
    if top <= bottom:
        return None, len(highs)
    try:
        close = float(bars[-1].get("close"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, len(highs)
    return round(max(0.0, min(1.0, (close - bottom) / (top - bottom))), 3), len(highs)


def _position_bonus_factor(pos: float | None) -> float:
    """位置系数（未乘 bonus_scale）：低位加满、中位线性过渡、高位扣减。"""
    if pos is None:
        return 0.0
    if pos <= POSITION_LOW_THRESHOLD:
        return POSITION_BONUS_LOW
    if pos >= POSITION_HIGH_THRESHOLD:
        return POSITION_BONUS_HIGH
    span = POSITION_HIGH_THRESHOLD - POSITION_LOW_THRESHOLD
    ratio = (pos - POSITION_LOW_THRESHOLD) / span
    return POSITION_BONUS_LOW + (POSITION_BONUS_HIGH - POSITION_BONUS_LOW) * ratio


def _volume_bonus_factor(ratio: float | None) -> float:
    """量能系数（未乘 bonus_scale）：量比 ≤0.7 无加成，≥1.5 加满 VOLUME_BONUS_MAX。"""
    if ratio is None:
        return 0.0
    span = VOLUME_RATIO_HIGH - VOLUME_RATIO_LOW
    scaled = (ratio - VOLUME_RATIO_LOW) / span
    return VOLUME_BONUS_MAX * max(0.0, min(1.0, scaled))


# v27（方案 §4.4）：数据质量标记——「这只票的分数该不该被信任」与分数本身同等重要。
_BAR_FIELDS = ("open", "high", "low", "close", "volume")


def _missing_bar_fields(bars: list[dict[str, Any]], tail: int = 60) -> list[str]:
    """近 tail 根 K 线里缺失或不可解析的 OHLCV 字段（去重保序）；用于如实标注数据质量。"""
    missing: list[str] = []
    for bar in bars[-tail:]:
        if not isinstance(bar, dict):
            continue
        for field in _BAR_FIELDS:
            if field in missing:
                continue
            try:
                float(bar.get(field))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                missing.append(field)
    return missing


def data_quality(
    bars: list[dict[str, Any]],
    *,
    hit_tactic_ids: list[str],
    insufficient_tactics: list[str],
) -> dict[str, Any]:
    """样本充足度与字段完整性（纯函数；**不含**「距今几天」这类需要系统时间的判断）。

    `latest_bar_date` 如实给出最新 K 线日期，行情新鲜度由调用方（知道「今天」的一层）
    据此计算——本模块不读系统时间，保证同输入同结果。
    """
    bars_count = len(bars)
    required = max([_MIN_BARS_BY_ID.get(tid, 0) for tid in hit_tactic_ids] or [0])
    sample_sufficient = bars_count >= required if required else True
    if not sample_sufficient:
        level = "insufficient"
    elif required and bars_count < int(required * 1.5):
        level = "thin"
    else:
        level = "ok"
    return {
        "bars_count": bars_count,
        "required_bars": required,
        "sample_sufficient": sample_sufficient,
        "insufficient_tactics": list(insufficient_tactics),
        "latest_bar_date": _bar_date(bars[-1]) if bars else "",
        "missing_fields": _missing_bar_fields(bars),
        "level": level,
    }


# ---------------------------------------------------------------------------
# v28 二次方案 §3.2「从战法分升级为信号状态机」+ §3.6「质量闸门」。
#
# 状态由注册表里的 confirmation_rule / invalidation_rule 出发、用**确定性规则**计算，
# 不由模型文字判断（本模块不联网、不调模型）。四种状态：
#   new         距最新 K 线太近，尚无后续 K 线可观察 → 等待确认；
#   pending     已有后续 K 线，但既未满足确认条件、也未触发失效条件；
#   confirmed   观察窗口内「至少 CONFIRM_MIN_BARS 根收盘站稳触发价」且量能不快速萎缩；
#   invalidated 观察窗口内跌回触发日平台（多头跌破触发日最低 / 空头突破触发日最高）且伴随放量。
#
# 口径边界（如实声明）：
# - 判定只在**触发日后最多 CONFIRM_WINDOW_BARS 根**内进行；超出窗口仍未确认 → 保持 pending，
#   不擅自判成 invalidated（「没确认」不等于「已失效」）；
# - 中性形态（十字星等）没有多空方向，无法套用确认/失效规则 → 只标 new/pending，
#   并在 reason 里说明「中性形态不参与确认判定」；
# - 缺 volume 字段时，量能条件按「不可判定」处理 → 该次不给 confirmed（保守）。
# ---------------------------------------------------------------------------

SIGNAL_STATE_VERSION = "v1"
STATE_NEW = "new"
STATE_PENDING = "pending"
STATE_CONFIRMED = "confirmed"
STATE_INVALIDATED = "invalidated"
STATE_LABELS = {
    STATE_NEW: "新触发",
    STATE_PENDING: "待确认",
    STATE_CONFIRMED: "已确认",
    STATE_INVALIDATED: "已失效",
}

CONFIRM_WINDOW_BARS = 3
CONFIRM_MIN_BARS = 2
CONFIRM_VOLUME_RATIO = 0.6
INVALIDATION_VOLUME_RATIO = 1.2

GATE_VERSION = "v1"
# 数据质量因子：insufficient = 0.0（不可排名）；thin 折价；ok 全额。
QUALITY_FACTORS: dict[str, float] = {"ok": 1.0, "thin": 0.85, "insufficient": 0.0}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def bar_date_issues(bars: list[dict[str, Any]]) -> list[str]:
    """K 线日期字段缺失 / 重复 / 非升序（§3.6 硬性「不可排名」判据，可复算）。"""
    issues: list[str] = []
    dates = [_bar_date(bar) for bar in bars]
    if any(not value for value in dates):
        issues.append("部分 K 线缺少日期字段")
    seen: set[str] = set()
    if len({value for value in dates if value}) != len([value for value in dates if value]):
        issues.append("K 线日期存在重复")
    previous = ""
    for value in dates:
        if not value:
            continue
        if previous and value < previous:
            issues.append("K 线日期非升序")
            break
        previous = value
    return issues


def signal_state(
    *,
    direction: str,
    signal_date: str,
    trigger_price: float | None,
    bars: list[dict[str, Any]],
) -> dict[str, Any]:
    """单条信号的确定性状态（new / pending / confirmed / invalidated）。

    `trigger_price` 为该信号登记在注册表里的触发价；缺失时退回「触发日收盘价」作为参照
    （不猜一个价位出来），并在 reason 里如实说明用的是收盘价。
    """
    base: dict[str, Any] = {
        "state": STATE_NEW,
        "label": STATE_LABELS[STATE_NEW],
        "reason": "",
        # v29：触发日随状态一起下发，前端据此把状态精确挂到「该战法最近一次触发」那条信号上，
        # 避免把窗口内主信号的状态误标到同战法更早的历史信号（冷却期合并的那几条）。
        "signal_date": signal_date,
        "observed_bars": 0,
        "window_bars": CONFIRM_WINDOW_BARS,
        "min_bars": CONFIRM_MIN_BARS,
        "volume_ratio_threshold": CONFIRM_VOLUME_RATIO,
        "invalidation_volume_ratio": INVALIDATION_VOLUME_RATIO,
        "version": SIGNAL_STATE_VERSION,
    }
    index = -1
    for i, bar in enumerate(bars):
        if _bar_date(bar) == signal_date:
            index = i
    if index < 0:
        base["reason"] = "触发日不在当前 K 线序列内，无法判定状态。"
        return base
    following = bars[index + 1 : index + 1 + CONFIRM_WINDOW_BARS]
    base["observed_bars"] = len(following)
    if not following:
        base["reason"] = f"触发于最新一根 K 线，尚无后续 K 线可观察（需 {CONFIRM_MIN_BARS} 根站稳）。"
        return base
    if direction not in (DIRECTION_BULLISH, DIRECTION_BEARISH):
        base["state"] = STATE_PENDING
        base["label"] = STATE_LABELS[STATE_PENDING]
        base["reason"] = "中性形态无多空方向，不参与确认/失效判定。"
        return base

    trigger_bar = bars[index]
    reference = trigger_price
    price_note = ""
    if reference is None:
        reference = _as_float(trigger_bar.get("close"))
        price_note = "（该战法未登记触发价，按触发日收盘价参照）"
    if reference is None:
        base["state"] = STATE_PENDING
        base["label"] = STATE_LABELS[STATE_PENDING]
        base["reason"] = "触发日收盘价缺失，无法判定状态。"
        return base

    trigger_low = _as_float(trigger_bar.get("low"))
    trigger_high = _as_float(trigger_bar.get("high"))
    trigger_volume = _as_float(trigger_bar.get("volume"))

    def _volume_ok(bar: dict[str, Any], threshold: float) -> bool:
        """量能条件：缺数据 → 不可判定（返回 False，保守不放行）。"""
        current = _as_float(bar.get("volume"))
        if current is None or trigger_volume is None or trigger_volume <= 0:
            return False
        return current >= trigger_volume * threshold

    holds = 0
    for bar in following:
        close = _as_float(bar.get("close"))
        if close is None:
            continue
        if direction == DIRECTION_BULLISH and close >= reference:
            holds += 1
        elif direction == DIRECTION_BEARISH and close <= reference:
            holds += 1

    last = following[-1]
    last_close = _as_float(last.get("close"))
    broke = (
        last_close is not None
        and trigger_low is not None
        and trigger_high is not None
        and (
            (direction == DIRECTION_BULLISH and last_close < trigger_low)
            or (direction == DIRECTION_BEARISH and last_close > trigger_high)
        )
    )
    if broke and _volume_ok(last, INVALIDATION_VOLUME_RATIO):
        base["state"] = STATE_INVALIDATED
        base["label"] = STATE_LABELS[STATE_INVALIDATED]
        base["reason"] = (
            f"观察窗口内跌破触发日{'最低' if direction == DIRECTION_BULLISH else '最高'} "
            f"{trigger_low if direction == DIRECTION_BULLISH else trigger_high} 且量能放大"
            f"（≥触发日 {INVALIDATION_VOLUME_RATIO} 倍）。"
        )
        return base
    if holds >= CONFIRM_MIN_BARS and _volume_ok(last, CONFIRM_VOLUME_RATIO):
        base["state"] = STATE_CONFIRMED
        base["label"] = STATE_LABELS[STATE_CONFIRMED]
        base["reason"] = (
            f"观察窗口内 {holds} 根收盘站稳 {round(reference, 2)}{price_note}，"
            f"且量能未快速萎缩（≥触发日 {CONFIRM_VOLUME_RATIO} 倍）。"
        )
        return base
    base["state"] = STATE_PENDING
    base["label"] = STATE_LABELS[STATE_PENDING]
    missing_bits: list[str] = []
    if holds < CONFIRM_MIN_BARS:
        missing_bits.append(f"仅有 {holds} 根收盘站稳（需 {CONFIRM_MIN_BARS} 根）")
    if not _volume_ok(last, CONFIRM_VOLUME_RATIO):
        missing_bits.append("量能已明显萎缩或量能字段缺失")
    base["reason"] = "尚未确认：" + "；".join(missing_bits) + "。"
    return base


def score(
    windowed: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    *,
    sector_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对窗口内信号打质量分；windowed 应为 signal_window 的返回值（已按日期过滤）。

    分数与窗口长度无关（新鲜度用固定半衰期），因此不同窗口、不同标的之间可直接横向比较；
    `bars` 只用于把信号日期换算成「距最新 K 线的根数」、量比与位置，不参与归一化。

    `sector_context`（可选，板块扫描时注入）：`{"direction": "bullish"|"bearish", "ratio": 0-1}`，
    ratio = 该板块内同向票数 / 有效票数。单票扫描不传 → 板块共振项为 0 并标注 unavailable。

    v28 追加返回（二次方案 §3.1/§3.2/§3.6）：
    - `signal_states` / `primary_signal_state`：逐条与主信号的四态状态机结果；
    - `rank_score` = signal_score × 数据质量因子（后验不可用则**不参与**，如实标注）；
    - `eligible` / `gating_reasons`：是否允许进入**默认榜单**及被挡下的原因；
    - `direction_display`：方向展示文本（冲突时展示「冲突」，不显示偏多/偏空）。
    """
    total_bars = len(bars)

    bull = bear = neutral = 0.0
    categories: dict[str, set[str]] = {DIRECTION_BULLISH: set(), DIRECTION_BEARISH: set()}
    detail: list[dict[str, Any]] = []
    insufficient: list[str] = []

    # v28（§3.2）：同一战法在窗口内多次触发时只保留**最近一次**作为主信号，其余计为
    # 「冷却期内合并」（旧信号转历史记录），并把合并数量如实带出，避免用户以为信号被吞。
    latest_signals = _latest_per_tactic(windowed)
    cooldown_merged: dict[str, int] = {}
    for signal in windowed:
        tactic_id = str(signal.get("tactic_id", ""))
        if tactic_id:
            cooldown_merged[tactic_id] = cooldown_merged.get(tactic_id, 0) + 1
    cooldown_merged = {key: value - 1 for key, value in cooldown_merged.items() if value - 1 > 0}

    candidates: list[dict[str, Any]] = []
    for signal in latest_signals:
        tactic_id = str(signal.get("tactic_id", ""))
        direction = str(signal.get("direction", DIRECTION_NEUTRAL))
        weight = _WEIGHT_BY_ID.get(tactic_id, 0.0)
        category = _CATEGORY_BY_ID.get(tactic_id, "other")
        date = str(signal.get("date", ""))[:10]
        age = _age_in_bars(date, bars)
        freshness = 0.5 ** (age / FRESHNESS_HALF_LIFE_BARS)
        contribution = weight * freshness
        candidates.append({
            "tactic_id": tactic_id,
            "tactic_name": str(signal.get("tactic_name", tactic_id)),
            "direction": direction,
            "category": category,
            "date": date,
            "age_bars": age,
            "freshness": freshness,
            "weight": weight,
            "contribution": contribution,
            # v27：探测器直接给出的触发价（缺省 None = 该信号未带价位，不猜）。
            "trigger_price": signal.get("trigger_price"),
        })

    # 同族合并判定：同方向同 category 内，贡献最高者计分，其余标注不计分。
    if FAMILY_MERGE:
        best_index: dict[tuple[str, str], int] = {}
        for index, item in enumerate(candidates):
            direction = str(item["direction"])
            if direction not in (DIRECTION_BULLISH, DIRECTION_BEARISH):
                continue
            key = (direction, str(item["category"]))
            current = best_index.get(key)
            if current is None or float(item["contribution"]) > float(candidates[current]["contribution"]):
                best_index[key] = index
        scored_ids: set[int] = set(best_index.values())
        scored_ids.update(
            index for index, item in enumerate(candidates)
            if str(item["direction"]) not in (DIRECTION_BULLISH, DIRECTION_BEARISH)
        )
    else:
        scored_ids = set(range(len(candidates)))

    for index, item in enumerate(candidates):
        counted = index in scored_ids
        direction = str(item["direction"])
        contribution = float(item["contribution"]) if counted else 0.0

        if direction == DIRECTION_BEARISH:
            bear += contribution
            if counted:
                categories[DIRECTION_BEARISH].add(str(item["category"]))
        elif direction == DIRECTION_BULLISH:
            bull += contribution
            if counted:
                categories[DIRECTION_BULLISH].add(str(item["category"]))
        else:
            neutral += contribution

        tactic_id = str(item["tactic_id"])
        min_bars = _MIN_BARS_BY_ID.get(tactic_id, 0)
        if total_bars < min_bars:
            insufficient.append(tactic_id)

        # v27（方案 §4.2）：每条命中带「触发价 + 失效条件 + 确认条件 + 冷却窗口」——
        # 用户可以据此在图上直接核对，而不用自己从 K 线里猜信号说了什么。
        trigger_price = item.get("trigger_price")
        try:
            trigger_price = round(float(trigger_price), 2) if trigger_price is not None else None
        except (TypeError, ValueError):
            trigger_price = None

        detail.append({
            "tactic_id": tactic_id,
            "tactic_name": str(item["tactic_name"]),
            "direction": direction,
            "category": str(item["category"]),
            "weight": round(float(item["weight"]), 2),
            "date": str(item["date"]),
            "age_bars": int(item["age_bars"]),
            "freshness": round(float(item["freshness"]), 3),
            "contribution": round(contribution, 2),
            "raw_contribution": round(float(item["contribution"]), 2),
            "counted": counted,
            "family_merged": bool(not counted and direction in (DIRECTION_BULLISH, DIRECTION_BEARISH)),
            "family": str(item["category"]),
            "min_bars": min_bars,
            # —— v27 语义字段 ——
            "trigger_price": trigger_price,
            "cooldown_bars": _COOLDOWN_BY_ID.get(tactic_id, 0),
            "confirmation_rule": _CONFIRMATION_BY_ID.get(tactic_id, ""),
            "invalidation_rule": _INVALIDATION_BY_ID.get(tactic_id, ""),
        })

    dominance_bull = bull >= bear
    dominant_score = bull if dominance_bull else bear
    dominant_direction = DIRECTION_BULLISH if dominance_bull else DIRECTION_BEARISH
    resonance = RESONANCE_STEP * max(0, len(categories[dominant_direction]) - 1)
    conflict = CONFLICT_DAMPING * min(bull, bear) if (bull > 0 and bear > 0) else 0.0

    # bonus 缩放：只随「占优方向计分信号的最大新鲜度」缩放（无新鲜信号则无 bonus）
    dominant_freshness = 0.0
    dominant_signals = [
        item for index, item in enumerate(candidates)
        if index in scored_ids and str(item["direction"]) == dominant_direction
    ]
    if dominant_signals:
        dominant_freshness = max(float(item["freshness"]) for item in dominant_signals)
    bonus_scale = round(dominant_freshness, 3)

    # 量能确认：取占优方向贡献最高信号当日的量比
    volume_ratio: float | None = None
    if dominant_signals:
        strongest = max(dominant_signals, key=lambda item: float(item["contribution"]))
        volume_ratio = _volume_ratio(bars, str(strongest["date"]))
    volume_bonus = _volume_bonus_factor(volume_ratio) * bonus_scale

    # 位置：最新收盘在近 60 根区间的位置（个股自身状态，同样随新鲜度缩放）
    position, position_bars = _position_in_range(bars)
    position_bonus = _position_bonus_factor(position) * bonus_scale

    # 板块共振：横截面因子，仅在扫描提供板块上下文且方向一致时生效
    sector_ratio: float | None = None
    sector_bonus = 0.0
    if sector_context is not None:
        try:
            context_direction = str(sector_context.get("direction") or "")
            raw_ratio = float(sector_context.get("ratio") or 0.0)
        except (TypeError, ValueError):
            context_direction, raw_ratio = "", 0.0
        if context_direction == dominant_direction and dominant_score > 0:
            sector_ratio = max(0.0, min(1.0, raw_ratio))
            sector_bonus = SECTOR_RESONANCE_MAX * sector_ratio * bonus_scale

    raw = dominant_score + resonance + NEUTRAL_FACTOR * neutral - conflict
    raw += volume_bonus + position_bonus + sector_bonus
    raw = max(0.0, raw)
    # 饱和曲线：100 分是渐近上限，高分段不贴顶（v1 线性截断导致多个满分、排名退化）。
    tactic_score = 100.0 * (1.0 - pow(2.718281828459045, -raw / SATURATION_SCALE))

    if bull > 0 and bear > 0:
        direction_bias = DIRECTION_CONFLICT
    elif bull > 0 or bear > 0:
        direction_bias = dominant_direction
    else:
        direction_bias = DIRECTION_NEUTRAL

    merged_families = sorted({
        str(item["family"]) for item in detail if item["family_merged"]
    })

    # v27（方案 §4.1/§4.4）：把「图形质量分」与并列的可信度指标显式拆开命名。
    # signal_score 是本模块算的**图形证据强度**；historical_edge（历史超额）属后验链路（第二期），
    # 本期**不编造**——如实返回 None 并在字段说明里标明原因。
    quality = data_quality(
        bars,
        hit_tactic_ids=[str(item["tactic_id"]) for item in detail],
        insufficient_tactics=insufficient,
    )
    counted_items = [item for item in detail if item["counted"]]
    latest_signal_date = max((str(item["date"]) for item in counted_items), default="")
    latest_signal_age = min((int(item["age_bars"]) for item in counted_items), default=None)

    # —— v28 信号状态机（§3.2）：逐条判定，再取主信号状态用于榜单闸门 ——
    states: dict[str, dict[str, Any]] = {}
    for item in detail:
        states[str(item["tactic_id"])] = signal_state(
            direction=str(item["direction"]),
            signal_date=str(item["date"]),
            trigger_price=item.get("trigger_price"),
            bars=bars,
        )
    for item in detail:
        state = states[str(item["tactic_id"])]
        item["signal_state"] = state["state"]
        item["signal_state_label"] = state["label"]
        item["signal_state_detail"] = state

    primary_tactic = ""
    if dominant_signals:
        primary_tactic = str(
            max(dominant_signals, key=lambda entry: float(entry["contribution"]))["tactic_id"]
        )
    primary_state = states.get(primary_tactic) or {
        "state": STATE_NEW, "label": STATE_LABELS[STATE_NEW],
        "reason": "窗口内没有计分的占优方向信号，无主信号可判定。",
        "observed_bars": 0, "window_bars": CONFIRM_WINDOW_BARS, "version": SIGNAL_STATE_VERSION,
    }

    # —— v28 质量闸门（§3.6）：硬性不可排名 / 不进默认榜单，全部给出可读原因 ——
    quality_level = str(quality["level"])
    quality_factor = QUALITY_FACTORS.get(quality_level, 0.0)
    gating_reasons: list[str] = []
    hard_blocked = False
    if quality["missing_fields"]:
        hard_blocked = True
        gating_reasons.append(f"K 线字段缺失（{'、'.join(quality['missing_fields'])}）——硬性不可排名")
    for issue in bar_date_issues(bars):
        hard_blocked = True
        gating_reasons.append(f"{issue}——硬性不可排名")
    if quality_level == "insufficient":
        gating_reasons.append(
            f"样本不足（{quality['bars_count']} 根 < 该战法所需 {quality['required_bars']} 根）："
            "允许展示命中，但不得进入默认榜单"
        )
    if primary_state.get("state") == STATE_INVALIDATED:
        gating_reasons.append("主信号已失效：移出默认榜单，保留在历史信号")
    eligible = not gating_reasons

    direction_display = {
        DIRECTION_BULLISH: "偏多", DIRECTION_BEARISH: "偏空",
        DIRECTION_NEUTRAL: "中性", DIRECTION_CONFLICT: "冲突",
    }[direction_bias]

    # 板块共振「不可用」必须与「0 分」区分（§3.6）：前者说明原因，后者才是真的没有加成。
    if sector_context is None:
        sector_info: dict[str, Any] = {
            "available": False,
            "reason": "单票扫描不提供板块上下文，板块共振不可用（不是 0 分）。",
        }
    elif sector_ratio is None:
        sector_info = {
            "available": False,
            "reason": "板块内同向占比未达阈值或样本不足，板块共振不可用（不是 0 分）。",
        }
    else:
        sector_info = {
            "available": True,
            "ratio": sector_ratio,
            "bonus": round(sector_bonus, 2),
        }

    rank_score = round(tactic_score * quality_factor, 1)
    rank_basis = (
        f"综合分 = 图形分 {round(tactic_score, 1)} × 数据质量因子 {quality_factor}（{quality_level}）；"
        "后验因子当前不可用，未参与合成。"
    )

    return {
        "tactic_score": round(tactic_score, 1),
        # —— v27 并列指标（口径互不覆盖，前端并列展示）——
        "signal_score": round(tactic_score, 1),
        "historical_edge": None,
        "historical_edge_note": "后验指标（同市场状态下的历史超额）属第二期后验闭环，本期未计算，不用 0 冒充。",
        "final_rank_score": rank_score,
        "rank_score_note": rank_basis,
        # —— v28 榜单闸门与状态机 ——
        "rank_score": rank_score,
        "rank_basis": rank_basis,
        "quality_factor": quality_factor,
        "eligible": eligible,
        "hard_blocked": hard_blocked,
        "gating_reasons": gating_reasons,
        "gate_version": GATE_VERSION,
        "direction_display": direction_display,
        "sector_resonance": sector_info,
        "posterior": {
            "available": False,
            "factor": None,
            "note": "暂无后验：样本与市场状态口径尚未建立（属 P1），不参与预测式排序，也不当作 0 或 1。",
        },
        "signal_states": states,
        "primary_signal_state": primary_state,
        "signal_state_version": SIGNAL_STATE_VERSION,
        "cooldown_merged": cooldown_merged,
        "cooldown_note": "同一战法在窗口内多次触发时只保留最近一次作为主信号，其余计为冷却期合并（旧信号转历史记录）。",
        "data_quality": quality,
        "latest_signal_date": latest_signal_date,
        "latest_signal_age_bars": latest_signal_age,
        "score_parts": {
            "bullish": round(bull, 2),
            "bearish": round(bear, 2),
            "neutral": round(neutral, 2),
            "resonance": round(resonance, 2),
            "conflict": round(conflict, 2),
            "volume_bonus": round(volume_bonus, 2),
            "position_bonus": round(position_bonus, 2),
            "sector_bonus": round(sector_bonus, 2),
            "bonus_scale": bonus_scale,
            "raw": round(raw, 2),
            # v1 字段保留（语义改为饱和尺度，避免旧前端读数缺失）
            "norm_scale": SATURATION_SCALE,
            "saturation_scale": SATURATION_SCALE,
        },
        "direction_bias": direction_bias,
        # hit_tactics = **窗口内命中了哪些战法**（信息完整，不因同族合并而丢失）；
        # 同族合并只影响计分（contribution=0），不影响「命中可见性」——
        # 用户需要看到「均线系三条都命中了，但只按最高一条计分」。
        # counted_tactics = 实际参与计分的战法（v2 新增，供筛选/解释用）。
        "hit_tactics": [item["tactic_id"] for item in detail],
        "counted_tactics": [item["tactic_id"] for item in detail if item["counted"]],
        "hit_detail": detail,
        "insufficient_tactics": insufficient,
        "weight_version": TACTIC_WEIGHT_VERSION,
        "tactic_semantics_version": TACTIC_SEMANTICS_VERSION,
        "score_formula_version": SCORE_FORMULA_VERSION,
        "freshness_half_life_bars": FRESHNESS_HALF_LIFE_BARS,
        "bars_count": total_bars,
        # —— v2 新维度的取值痕迹（可复算、可解释）——
        "family_merge": FAMILY_MERGE,
        "merged_families": merged_families,
        "volume_ratio": volume_ratio,
        "volume_lookback_bars": VOLUME_LOOKBACK_BARS,
        "position": position,
        "position_window_bars": position_bars,
        "sector_ratio": sector_ratio,
    }


def score_for_bars(
    snap: dict[str, Any],
    bars: list[dict[str, Any]],
    recent_bars: int,
    *,
    sector_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一次算完：命中窗口（按日期）+ 质量分（含可解释分项与逐条命中明细）。

    窗口只决定「哪些信号还算数」（按 K 线日期下界），不参与归一化——所以同一只票
    在不同窗口下的分数差异只来自「是否有更早的信号被纳入」，不会因窗口变大而虚高。
    sector_context 透传给 score（板块扫描时的横截面共振因子）。
    """
    windowed, from_date = signal_window(
        snap.get("signals") if isinstance(snap.get("signals"), list) else [], bars, recent_bars
    )
    result = score(windowed, bars, sector_context=sector_context)
    result["signal_window_from"] = from_date
    result["signal_window_bars"] = max(1, min(int(recent_bars), len(bars) or 1))
    result["window_signals"] = windowed
    return result


def sector_contexts(
    rows: list[dict[str, Any]], *, min_sample: int = 4, threshold: float = 0.6
) -> dict[str, dict[str, Any]]:
    """板块扫描的横截面因子：板块内同向票占比 ≥ threshold 且样本 ≥ min_sample 时给出共振上下文。

    输入 `rows` 为同一板块内已完成基础评分的行（需含 `direction_bias`）。
    返回 `{"bullish": {"direction": "bullish", "ratio": 0.75, "bullish_count": 9, "sample": 12}}`，
    无方向占优或样本不足时返回空 dict（调用方据此把所有票的板块共振记为不可用）。
    """
    bullish = sum(1 for row in rows if str(row.get("direction_bias")) == DIRECTION_BULLISH)
    bearish = sum(1 for row in rows if str(row.get("direction_bias")) == DIRECTION_BEARISH)
    sample = bullish + bearish
    if sample < min_sample:
        return {}
    if bullish >= bearish:
        ratio, direction, count = bullish / sample, DIRECTION_BULLISH, bullish
    else:
        ratio, direction, count = bearish / sample, DIRECTION_BEARISH, bearish
    if ratio < threshold:
        return {}
    return {
        direction: {
            "direction": direction,
            "ratio": round(ratio, 3),
            "count": count,
            "sample": sample,
            "min_sample": min_sample,
            "threshold": threshold,
        }
    }
