"""宏观定位引擎（M5）：确定性规则打分，全部阈值随代码发布、任何人可复算（方案 §3）。

- 四维：增长 35% / 就业 25% / 通胀 20% / 货币 20%（默认权重版本 v1，用户自定义权重 M5.5 接入）。
- 每维从 50 起步，按显式规则加减分，clamp 到 0-100；某维无 ok 指标时不参与加权（剩余维再归一）。
- 综合状态带：≥70 扩张 / 45-69 放缓 / 30-44 承压 / <30 衰退风险；距边界 ≤5 分提示「接近换档」。
- AI 不参与打分（ADR：打分是确定性规则，AI 只做摘要）；本模块永不输出买卖建议。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from investment_steward_core.domain import MacroDimScore, MacroPositioning

if TYPE_CHECKING:
    from investment_steward_core.domain import MacroIndicatorReading

# 默认权重版本 v1（方案 §3 定稿；M5.5 引入 weight_version 版本链后可变）。
DEFAULT_WEIGHTS: dict[str, float] = {"growth": 0.35, "employment": 0.25, "inflation": 0.20, "monetary": 0.20}
WEIGHT_VERSION = "v1"

# 状态带阈值（方案 §3）：下限含边界值，即 [70,100]=扩张、[45,70)=放缓、[30,45)=承压、[0,30)=衰退风险。
BANDS: list[tuple[float, str]] = [(70.0, "扩张"), (45.0, "放缓"), (30.0, "承压"), (0.0, "衰退风险")]
NEAR_BOUNDARY = 5.0
_SCORE_FLOOR, _SCORE_CEIL = 0.0, 100.0
NEUTRAL = 50.0


def _observations(values: dict[str, dict[str, Any]] | None, key: str) -> list[dict[str, Any]]:
    """取某指标的全部观测（最新在前）；payload 缺失返回空。"""
    if not values:
        return []
    payload = values.get(key)
    return list(payload["observations"]) if payload else []


def _diffs(observations: list[dict[str, Any]]) -> list[float]:
    """相邻期差分（最新在前）：diffs[0] = 最新期 - 上期。"""
    return [
        round(observations[i]["value"] - observations[i + 1]["value"], 4)
        for i in range(len(observations) - 1)
    ]


def _clamp(score: float) -> float:
    return round(max(_SCORE_FLOOR, min(_SCORE_CEIL, score)), 1)


def _latest(observations: list[dict[str, Any]]) -> float | None:
    return observations[0]["value"] if observations else None


# ---- 各维规则（每条 fired 时记下「描述 + 实际值」，保证结论可回答）----

def _score_employment_us(readings: dict[str, MacroIndicatorReading], values: dict[str, dict[str, Any]]) -> tuple[float, list[str]]:
    score, fired = NEUTRAL, []
    unrate_obs = _observations(values, "unemployment")
    unrate = _latest(unrate_obs)
    if unrate is not None:
        if unrate >= 5.0:
            score -= 30; fired.append(f"失业率 {unrate}% ≥ 5.0 → -30")
        elif unrate >= 4.5:
            score -= 15; fired.append(f"失业率 {unrate}% ≥ 4.5 → -15")
        elif unrate <= 4.0:
            score += 10; fired.append(f"失业率 {unrate}% ≤ 4.0 → +10")
        if len(unrate_obs) >= 3 and all(unrate_obs[i]["value"] > unrate_obs[i + 1]["value"] for i in range(2)):
            score -= 20; fired.append(f"失业率 3 期连升（{unrate_obs[2]['value']}→{unrate_obs[0]['value']}）→ -20")
    nfp_diffs = _diffs(_observations(values, "nfp"))
    if nfp_diffs:
        if nfp_diffs[0] < 0:
            score -= 35; fired.append(f"非农最新月增 {nfp_diffs[0]} 千人 < 0 → -35")
        if len(nfp_diffs) >= 2 and nfp_diffs[0] < 100 and nfp_diffs[1] < 100:
            score -= 25; fired.append(f"非农连续两期月增 < 100 千人（{nfp_diffs[1]}、{nfp_diffs[0]}）→ -25")
    return _clamp(score), fired


def _score_inflation(readings: dict[str, MacroIndicatorReading], values: dict[str, dict[str, Any]], cpi_key: str) -> tuple[float, list[str]]:
    """通胀维：对 2% 目标的偏离度（规则相同，复用于美国与 WB 各国）。"""
    score, fired = NEUTRAL, []
    cpi = _latest(_observations(values, cpi_key))
    if cpi is not None:
        dev = abs(cpi - 2.0)
        if dev <= 1.0:
            score += 20; fired.append(f"CPI 同比 {cpi}% 偏离目标 ≤1pp → +20")
        elif dev <= 2.0:
            fired.append(f"CPI 同比 {cpi}% 偏离目标 ≤2pp → 0")
        elif dev <= 3.0:
            score -= 20; fired.append(f"CPI 同比 {cpi}% 偏离目标 ≤3pp → -20")
        else:
            score -= 35; fired.append(f"CPI 同比 {cpi}% 偏离目标 >3pp → -35")
    return _clamp(score), fired


def _score_growth_us(readings: dict[str, MacroIndicatorReading], values: dict[str, dict[str, Any]]) -> tuple[float, list[str]]:
    score, fired = NEUTRAL, []
    nfp_diffs = _diffs(_observations(values, "nfp"))
    if nfp_diffs:
        avg3 = round(sum(nfp_diffs[:3]) / min(3, len(nfp_diffs)), 1)
        if avg3 >= 150:
            score += 15; fired.append(f"非农 3 期月均 {avg3} 千人 ≥ 150 → +15")
        elif avg3 < 0:
            score -= 30; fired.append(f"非农 3 期月均 {avg3} 千人 < 0 → -30")
        elif avg3 <= 50:
            score -= 15; fired.append(f"非农 3 期月均 {avg3} 千人 ≤ 50 → -15")
    spread = _latest(_observations(values, "yield_10y2y"))
    if spread is not None:
        if spread < 0:
            score -= 25; fired.append(f"10y-2y 利差 {spread}pp 倒挂 → -25")
        elif spread < 0.3:
            score -= 10; fired.append(f"10y-2y 利差 {spread}pp < 0.3 → -10")
    return _clamp(score), fired


def _score_monetary_us(readings: dict[str, MacroIndicatorReading], values: dict[str, dict[str, Any]]) -> tuple[float, list[str]]:
    score, fired = NEUTRAL, []
    ff_obs = _observations(values, "fedfunds")
    if len(ff_obs) >= 3:
        if all(ff_obs[i]["value"] < ff_obs[i + 1]["value"] for i in range(2)):
            score += 15; fired.append(f"联邦基金利率 3 期连降（{ff_obs[2]['value']}→{ff_obs[0]['value']}）→ +15")
        elif all(ff_obs[i]["value"] > ff_obs[i + 1]["value"] for i in range(2)):
            score -= 10; fired.append(f"联邦基金利率 3 期连升（{ff_obs[2]['value']}→{ff_obs[0]['value']}）→ -10")
    spread = _latest(_observations(values, "yield_10y2y"))
    if spread is not None and spread < 0:
        score -= 15; fired.append(f"10y-2y 利差 {spread}pp 倒挂 → -15")
    return _clamp(score), fired


def _score_growth_wb(readings: dict[str, MacroIndicatorReading], values: dict[str, dict[str, Any]]) -> tuple[float, list[str]]:
    score, fired = NEUTRAL, []
    gdp = _latest(_observations(values, "gdp_yoy"))
    if gdp is not None:
        if gdp >= 4.0:
            score += 20; fired.append(f"GDP 同比 {gdp}% ≥ 4 → +20")
        elif gdp >= 2.0:
            score += 5; fired.append(f"GDP 同比 {gdp}% ≥ 2 → +5")
        elif gdp >= 0.0:
            score -= 10; fired.append(f"GDP 同比 {gdp}% ≥ 0 → -10")
        else:
            score -= 35; fired.append(f"GDP 同比 {gdp}% < 0 → -35")
    return _clamp(score), fired


def _score_employment_wb(readings: dict[str, MacroIndicatorReading], values: dict[str, dict[str, Any]], key: str) -> tuple[float, list[str]]:
    score, fired = NEUTRAL, []
    unrate_obs = _observations(values, key)
    unrate = _latest(unrate_obs)
    if unrate is not None:
        if unrate >= 8.0:
            score -= 30; fired.append(f"失业率 {unrate}% ≥ 8 → -30")
        elif unrate >= 6.0:
            score -= 15; fired.append(f"失业率 {unrate}% ≥ 6 → -15")
        elif unrate <= 4.0:
            score += 10; fired.append(f"失业率 {unrate}% ≤ 4 → +10")
        if len(unrate_obs) >= 3 and all(unrate_obs[i]["value"] > unrate_obs[i + 1]["value"] for i in range(2)):
            score -= 20; fired.append(f"失业率 3 期连升（{unrate_obs[2]['value']}→{unrate_obs[0]['value']}）→ -20")
    return _clamp(score), fired


def _score_us(readings: dict[str, MacroIndicatorReading], values: dict[str, dict[str, Any]]) -> list[MacroDimScore]:
    rules = {
        "employment": lambda: _score_employment_us(readings, values),
        "inflation": lambda: _score_inflation(readings, values, "cpi_yoy"),
        "growth": lambda: _score_growth_us(readings, values),
        "monetary": lambda: _score_monetary_us(readings, values),
    }
    dims: list[MacroDimScore] = []
    for dim, scorer in rules.items():
        used = [r.indicator for r in readings.values() if r.status == "ok" and r.dim == dim]
        if not used:
            dims.append(MacroDimScore(dim=dim, score=None, rules_fired=[], indicators_used=[]))
            continue
        score, fired = scorer()
        dims.append(MacroDimScore(dim=dim, score=score, rules_fired=fired, indicators_used=used))
    return dims


def _score_wb(readings: dict[str, MacroIndicatorReading], values: dict[str, dict[str, Any]], unemployment_key: str) -> list[MacroDimScore]:
    dims: list[MacroDimScore] = []
    plan = {
        "growth": lambda: _score_growth_wb(readings, values),
        "employment": lambda: _score_employment_wb(readings, values, unemployment_key),
        "inflation": lambda: _score_inflation(readings, values, "cpi_yoy"),
    }
    for dim, scorer in plan.items():
        used = [r.indicator for r in readings.values() if r.status == "ok" and r.dim == dim]
        if not used:
            dims.append(MacroDimScore(dim=dim, score=None, rules_fired=[], indicators_used=[]))
            continue
        score, fired = scorer()
        dims.append(MacroDimScore(dim=dim, score=score, rules_fired=fired, indicators_used=used))
    dims.append(MacroDimScore(dim="monetary", score=None, rules_fired=[], indicators_used=[]))
    return dims


def band_of(composite: float) -> str:
    """综合分 → 状态带（含边界：下限值归本带）。"""
    for floor, label in BANDS:
        if composite >= floor:
            return label
    return BANDS[-1][1]


def severe_rules(positioning: MacroPositioning, threshold: int = 25) -> list[str]:
    """提取重度命中规则（|分值变动| ≥ threshold）作为 M6 异动候选；规则文本末尾带「→ N」。"""
    import re

    out: list[str] = []
    for dim in positioning.dims:
        for rule in dim.rules_fired:
            match = re.search(r"→\s*(-?\d+)\s*$", rule)
            if match and abs(int(match.group(1))) >= threshold:
                out.append(f"{dim.dim} 维：{rule}")
    return out


def _near_boundary_note(composite: float) -> str | None:
    """距相邻带边界 ≤5 分时提示接近换档（方案 §3：显示提示而非直接跳带）。"""
    for floor, _ in BANDS:
        if floor > 0 and abs(composite - floor) <= NEAR_BOUNDARY:
            lower = band_of(floor - 0.1)
            upper = band_of(floor)
            if composite >= floor:
                return f"综合 {composite} 分距「{lower}」带边界（{floor}）仅 {round(composite - floor, 1)} 分，接近降档"
            return f"综合 {composite} 分距「{upper}」带边界（{floor}）仅 {round(floor - composite, 1)} 分，接近升档"
    return None


def compute_positioning(
    region: str,
    readings: list[MacroIndicatorReading],
    payload_values: dict[str, dict[str, Any]],
    *,
    weights: dict[str, float] | None = None,
    weight_version: str = WEIGHT_VERSION,
) -> MacroPositioning | None:
    """规则打分入口：无任何 ok 指标时返回 None（不编造定位）。

    `weights` 缺省用默认版本 v1；传入用户权重时 weight_version 同步标注——
    同一份数据 + 同一版权重永远算出同一状态带（历史可精确复算，测试锁定）。
    """
    by_indicator = {r.indicator: r for r in readings}
    if not any(r.status == "ok" for r in readings):
        return None
    active_weights = weights or DEFAULT_WEIGHTS
    if region == "us":
        dims = _score_us(by_indicator, payload_values)
    else:
        unemployment_key = "unemployment_ilo" if region == "cn" else "unemployment"
        dims = _score_wb(by_indicator, payload_values, unemployment_key)

    scored = [(d.score, active_weights[d.dim]) for d in dims if d.score is not None]
    if not scored:
        return None
    weight_total = sum(w for _, w in scored)
    composite = round(sum(s * w for s, w in scored) / weight_total, 1)
    band = band_of(composite)

    limitations: list[str] = []
    missing = [d.dim for d in dims if d.score is None]
    if missing:
        limitations.append(f"{'、'.join(missing)} 维无 ok 指标，未参与加权（剩余维权重再归一，合计 {round(weight_total, 2)}）")
    if region != "us":
        limitations.append("世界银行数据为年更口径（截至上年），对月度边际变化不敏感")
    if region == "cn":
        limitations.append("就业维基于 ILO 模型估算口径，与城镇调查口径不同")
    if weights is not None and weight_version != WEIGHT_VERSION:
        personalized = [d for d in DEFAULT_WEIGHTS if abs((active_weights.get(d, 0.0) - DEFAULT_WEIGHTS[d]) * 100) > 20]
        if personalized:
            limitations.append(
                f"当前为高度个性化权重（{'、'.join(personalized)} 维偏离默认 >20pp），与其他口径的结果不可直接对比"
            )
        else:
            limitations.append(f"权重为用户自定义版本 {weight_version}（偏离默认在 20pp 内）")
    else:
        limitations.append(f"权重为默认版本 {WEIGHT_VERSION}（增长 35% / 就业 25% / 通胀 20% / 货币 20%）")
    limitations.append("打分为确定性规则（随插件发布、可复算），只描述宏观状态，不构成任何买卖建议")

    return MacroPositioning(
        region=region,
        weight_version=weight_version,
        composite=composite,
        band=band,
        near_boundary_note=_near_boundary_note(composite),
        dims=dims,
        limitations=limitations,
        generated_at=datetime.now(UTC).isoformat(),
    )
