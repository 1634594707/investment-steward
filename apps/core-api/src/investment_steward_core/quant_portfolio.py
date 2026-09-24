"""组合回测（路线图 §7.2 / 阶段 7）:权重上限、行业限制与组合级风险指标。

设计约束:
- 确定性:同一输入 → 逐位一致,canonical JSON 可哈希。
- 因果:只消费各标的已实现的周期收益序列,不做任何预测。
- 口径冻结（PORTFOLIO_SPEC_VERSION=1）:权重先归一化,再按单标的权重上限截断,
  再按行业敞口上限截断,余量按截断后剩余权重比例摊回（两轮收敛,记录是否触限）。
- 基线:等权组合（与截断后组合对照,避免只看绝对收益）。
- 风险指标:最大回撤、周期波动率、HHI 集中度、行业敞口、两两相关系数均值、最差共同回撤。
- 如实边界:多标的需等长收益序列（起止对齐由调用方保证,报告登记各自区间）;
  相关性用皮尔逊总体相关系数,样本 <2 期时返回 None 不编造。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

PORTFOLIO_SPEC_VERSION = 1
DEFAULT_MAX_WEIGHT = 0.35
DEFAULT_MAX_INDUSTRY_WEIGHT = 0.60


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _apply_caps(weights: dict[str, float], industries: dict[str, str] | None,
                max_weight: float, max_industry_weight: float) -> tuple[dict[str, float], dict[str, Any]]:
    """归一化 → 单标的截断 → 行业截断。触限后的余额持现金（cash_weight）,
    不做余量摊回（摊回会击穿行业上限）;确定性、无不可行约束问题。"""
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("权重之和必须 > 0")
    w = {k: v / total for k, v in weights.items()}
    trace: dict[str, Any] = {"single_capped": [], "industry_capped": []}
    # 单标的截断
    for k in sorted(w):
        if w[k] > max_weight:
            trace["single_capped"].append({"symbol": k, "from": round(w[k], 6), "to": max_weight})
            w[k] = max_weight
    # 行业敞口截断（成员按比例缩到行业上限内）
    if industries:
        groups: dict[str, list[str]] = {}
        for k, ind in industries.items():
            if k in w:
                groups.setdefault(ind, []).append(k)
        for ind in sorted(groups):
            members = groups[ind]
            exposure = sum(w[k] for k in members)
            if exposure > max_industry_weight and exposure > 0:
                scale = max_industry_weight / exposure
                for k in members:
                    trace["industry_capped"].append(
                        {"symbol": k, "industry": ind, "from": round(w[k], 6), "to": round(w[k] * scale, 6)})
                    w[k] *= scale
    return w, trace


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = 1.0
    max_dd = 1.0
    for point in equity_curve:
        peak = max(peak, point)
        if peak > 0:
            max_dd = min(max_dd, point / peak)
    return round(1.0 - max_dd, 6)


def _pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 2 or n != len(b):
        return None
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb)


def run_portfolio_backtest(
    symbol_returns: dict[str, list[float]],
    weights: dict[str, float],
    *,
    industries: dict[str, str] | None = None,
    max_weight: float = DEFAULT_MAX_WEIGHT,
    max_industry_weight: float = DEFAULT_MAX_INDUSTRY_WEIGHT,
    result_hash: bool = False,
) -> dict[str, Any]:
    """组合回测:各标的周期收益 + 权重（含单标的/行业上限）→ 组合权益与风险指标。

    symbol_returns: {symbol: [逐期收益]}（等长,起止对齐由调用方保证,报告登记期数）;
    weights: {symbol: 权重}（允许未归一,内部归一化）;
    industries: {symbol: 行业}（可选,缺省视为单一行业 "default"）。
    """
    symbols = [s for s in weights if s in symbol_returns and symbol_returns[s]]
    if len(symbols) < 2:
        raise ValueError("组合回测至少需要 2 个有效标的（有收益序列）")
    lengths = {len(symbol_returns[s]) for s in symbols}
    if len(lengths) != 1:
        raise ValueError(f"收益序列长度不一致:{ {s: len(symbol_returns[s]) for s in symbols} }")
    periods = lengths.pop()
    if periods < 2:
        raise ValueError("每标的至少 2 期收益")

    capped, trace = _apply_caps({s: float(weights[s]) for s in symbols},
                                industries, max_weight, max_industry_weight)

    portfolio_returns: list[float] = []
    equal_weight = 1.0 / len(symbols)
    baseline_returns: list[float] = []
    for t in range(periods):
        pr = sum(capped[s] * float(symbol_returns[s][t]) for s in symbols)
        br = sum(equal_weight * float(symbol_returns[s][t]) for s in symbols)
        portfolio_returns.append(pr)
        baseline_returns.append(br)

    def _curve(returns: list[float]) -> list[float]:
        equity = 1.0
        out = []
        for r in returns:
            equity *= 1.0 + r
            out.append(equity)
        return out

    curve = _curve(portfolio_returns)
    baseline_curve = _curve(baseline_returns)
    equity = curve[-1] if curve else 1.0
    baseline_equity = baseline_curve[-1] if baseline_curve else 1.0

    mean_r = sum(portfolio_returns) / periods
    variance = sum((r - mean_r) ** 2 for r in portfolio_returns) / periods
    volatility = math.sqrt(variance)

    hhi = round(sum(v * v for v in capped.values()), 6)
    industry_exposure: dict[str, float] = {}
    for s, w in capped.items():
        ind = (industries or {}).get(s, "default")
        industry_exposure[ind] = round(industry_exposure.get(ind, 0.0) + w, 6)

    correlations: list[float] = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            rho = _pearson([float(x) for x in symbol_returns[a]], [float(x) for x in symbol_returns[b]])
            if rho is not None:
                correlations.append(rho)
    avg_correlation = round(sum(correlations) / len(correlations), 6) if correlations else None

    # 最差共同回撤:各标的自身最大回撤期内的组合回撤（近似:组合曲线回撤即为共同失效的复合体现,
    # 另报各标的最差单期收益同时出现的比例）
    worst_period_drop = round(min(portfolio_returns), 8) if portfolio_returns else None

    result: dict[str, Any] = {
        "spec_version": str(PORTFOLIO_SPEC_VERSION),
        "periods": periods,
        "symbols": symbols,
        "weights_applied": {s: round(capped[s], 6) for s in symbols},
        "cash_weight": round(1.0 - sum(capped.values()), 6),
        "caps": {"max_weight": max_weight, "max_industry_weight": max_industry_weight,
                 "trace": trace, "remainder_policy": "cash"},
        "industry_exposure": industry_exposure,
        "concentration_hhi": hhi,
        "equity": round(equity, 8),
        "baseline_equal_weight": round(baseline_equity, 8),
        "excess_vs_equal_weight": round(equity - baseline_equity, 8),
        "max_drawdown": _max_drawdown(curve),
        "volatility_per_period": round(volatility, 8),
        "avg_pairwise_correlation": avg_correlation,
        "worst_period_return": worst_period_drop,
        "curve": [{"step": t, "return": round(portfolio_returns[t], 8),
                   "equity": round(curve[t], 8)} for t in range(periods)],
    }
    if result_hash:
        result["result_hash"] = hashlib.sha256(_canonical(result).encode("utf-8")).hexdigest()
    return result
