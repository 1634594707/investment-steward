"""档 A：从三大报表（S4）派生盈利质量与估值指标（纯函数，零新增数据源）。

背景（2026-09-10 用户要求「三档一起执行」）：
估值证据源（`valuation_evidence`，S5）给出权威 PE/PB/PS、历史分位与同业，但**没有 ROE**，
也没有「利润含金量」这类需要跨报表联算的指标。本模块只用手上已有的 S4 行项目派生：

- **TTM 营收 / 归母净利 / 经营现金流**：东财 F10 是**报告期累计值**（年初至今），
  直接拿同比会把「半年报 vs 年报」混为一谈。TTM = 最新累计 + 上年年报 − 去年同期累计；
- **ROE(TTM)** = TTM 归母净利 / 平均净资产（有去年同期净资产时取均值，否则用最新期并标注口径）；
- **利润含金量** = TTM 经营现金流 / TTM 归母净利（<1 提示「纸面利润」，负值提示经营失血）；
- **派生 PE(TTM)** = 最新收盘价 / (TTM 归母净利 / 总股本)，用于与 S5 权威值**交叉校验**。

口径铁律：累计差分必须按**同月日**匹配（2025-09-30 对 2024-09-30），匹配不到基数时
TTM 置 None 并写明原因，**绝不用「单期年化」冒充 TTM**，也不把负值伪装成正常比率。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class DerivedMetricsError(RuntimeError):
    """报表行不足以派生任何指标时抛出（调用方如实降级）。"""


@dataclass(frozen=True)
class DerivedMetrics:
    """从三大报表派生的 TTM 与比率指标（每一项都带口径说明）。"""

    latest_period: str = ""
    prior_same_period: str = ""
    prior_annual_period: str = ""
    ttm_revenue: float | None = None
    ttm_net_profit: float | None = None
    ttm_operating_cash_flow: float | None = None
    roe_ttm: float | None = None
    roe_basis: str = ""
    cash_conversion: float | None = None
    derived_eps_ttm: float | None = None
    derived_pe_ttm: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result


def _month_day(period: str) -> str:
    return period[5:10] if len(period) >= 10 else ""


def _prior_year_period(period: str) -> str:
    """同月日的上一年（2025-09-30 → 2024-09-30）；无法解析返回空串。"""
    if len(period) < 10 or not period[:4].isdigit():
        return ""
    return f"{int(period[:4]) - 1}{period[4:]}"


def _items_by_period(rows: list[Any], statement_type: str) -> dict[str, dict[str, str]]:
    """{报告期: 行项目} —— 只保留该报表类型；同报告期后到覆盖先到（更完整）。"""
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if getattr(row, "statement_type", None) != statement_type:
            continue
        period = str(getattr(row, "period_end", "") or "")
        items = getattr(row, "items", None)
        if period and isinstance(items, dict):
            out[period] = items
    return out


def _period_context(latest: str) -> tuple[str, str]:
    """返回 (去年同期报告期, 上年年报报告期)——只由最新报告期决定，与任何字段无关。

    关键：这两个基准期必须**独立**于某个字段是否可用；否则「营收缺失」会连带把
    净利/ROE 的同期基准一起丢掉（曾因此把平均净资产退化成最新净资产）。
    """
    if len(latest) < 10 or not latest[:4].isdigit():
        return "", ""
    if _month_day(latest) == "12-31":  # 最新期本身是年报 → 无「去年同期」概念
        return "", f"{int(latest[:4]) - 1}-12-31"
    return _prior_year_period(latest), f"{int(latest[:4]) - 1}-12-31"


def _ttm(
    by_period: dict[str, dict[str, str]],
    field_name: str,
    latest: str,
    prior_same: str,
    prior_annual: str,
) -> tuple[float | None, str]:
    """返回 (TTM 值, 口径说明)。累计差分：最新累计 + 上年年报 − 去年同期累计。"""
    current = _num(by_period.get(latest, {}).get(field_name))
    if current is None:
        return None, f"最新报告期 {latest} 缺 {field_name}"
    if _month_day(latest) == "12-31":  # 年报本身就是完整年度
        return current, f"{latest} 年报累计（即 TTM）"
    same_value = _num(by_period.get(prior_same, {}).get(field_name)) if prior_same else None
    annual_value = _num(by_period.get(prior_annual, {}).get(field_name)) if prior_annual else None
    if same_value is None or annual_value is None:
        return (
            None,
            f"缺基数（去年同期 {prior_same or '未知'}={same_value}，"
            f"上年年报 {prior_annual or '未知'}={annual_value}），TTM 不可算",
        )
    return (
        current + annual_value - same_value,
        f"{latest} 累计 + {prior_annual} 年报 − {prior_same} 同期累计",
    )


def derive_fundamental_metrics(
    rows: list[Any], *, total_shares: float | None = None, latest_price: float | None = None
) -> DerivedMetrics:
    """从 `FinancialStatementRow` 列表派生 TTM 与比率；行不足则抛 `DerivedMetricsError`。"""
    by_income = _items_by_period(rows, "income")
    by_balance = _items_by_period(rows, "balance")
    by_cash = _items_by_period(rows, "cash_flow")

    all_periods = sorted(set(by_income) | set(by_balance) | set(by_cash))
    if not all_periods:
        raise DerivedMetricsError("没有任何可用的报表报告期，无法派生指标")
    latest = all_periods[-1]

    notes: list[str] = []
    prior_same, prior_annual = _period_context(latest)
    ttm_revenue, rev_note = _ttm(by_income, "revenue", latest, prior_same, prior_annual)
    ttm_net, net_note = _ttm(by_income, "net_profit", latest, prior_same, prior_annual)
    ttm_ocf, ocf_note = _ttm(by_cash, "operating_cash_flow", latest, prior_same, prior_annual)

    roe: float | None = None
    roe_basis = ""
    equity_latest = _num(by_balance.get(latest, {}).get("total_equity"))
    equity_prior: float | None = None
    prior_equity_period = ""
    for candidate in (prior_same, prior_annual):
        if not candidate:
            continue
        candidate_equity = _num(by_balance.get(candidate, {}).get("total_equity"))
        if candidate_equity is not None:
            equity_prior, prior_equity_period = candidate_equity, candidate
            break
    if ttm_net is None:
        roe_basis = "TTM 归母净利不可算，ROE 置空"
    elif not equity_latest or equity_latest <= 0:
        roe_basis = f"最新净资产缺失或非正（{equity_latest}），ROE 置空"
    elif equity_prior and equity_prior > 0:
        roe = round(ttm_net / ((equity_latest + equity_prior) / 2) * 100, 2)
        roe_basis = f"平均净资产（{latest} 与 {prior_equity_period} 净资产均值）"
    else:
        roe = round(ttm_net / equity_latest * 100, 2)
        roe_basis = f"最新净资产（{latest}；缺上一期净资产，未取均值，口径随增发/回购而偏）"

    conversion: float | None = None
    if ttm_net is not None and ttm_ocf is not None and ttm_net > 0:
        conversion = round(ttm_ocf / ttm_net, 2)

    eps_ttm: float | None = None
    pe_derived: float | None = None
    if ttm_net is not None and total_shares and total_shares > 0:
        eps_ttm = round(ttm_net / total_shares, 4)
        if latest_price and latest_price > 0 and eps_ttm > 0:
            pe_derived = round(latest_price / eps_ttm, 2)
    elif total_shares is None:
        notes.append("未取得总股本，无法派生 EPS/PE(TTM)（交叉校验缺失）。")

    if "不可算" in net_note:
        notes.append(f"TTM 归母净利：{net_note}")
    if "不可算" in ocf_note:
        notes.append(f"TTM 经营现金流：{ocf_note}")

    return DerivedMetrics(
        latest_period=latest,
        prior_same_period=prior_same,
        prior_annual_period=prior_annual,
        ttm_revenue=ttm_revenue,
        ttm_net_profit=ttm_net,
        ttm_operating_cash_flow=ttm_ocf,
        roe_ttm=roe,
        roe_basis=roe_basis,
        cash_conversion=conversion,
        derived_eps_ttm=eps_ttm,
        derived_pe_ttm=pe_derived,
        notes=tuple(notes),
    )


def _yi(value: float | None) -> str:
    return "无数据" if value is None else f"{value / 1e8:.2f} 亿元"


def describe_derived(metrics: DerivedMetrics) -> str:
    """渲染为证据包 S4 的「派生指标」附加段（每项带口径，缺失如实写「无数据」）。"""
    lines = [f"【派生指标（由上述报表现算，最新报告期 {metrics.latest_period or '未知'}）】"]
    lines.append(
        f"- TTM 营收 {_yi(metrics.ttm_revenue)}；TTM 归母净利 {_yi(metrics.ttm_net_profit)}；"
        f"TTM 经营现金流 {_yi(metrics.ttm_operating_cash_flow)}"
    )
    lines.append(
        f"- ROE(TTM) {'无数据' if metrics.roe_ttm is None else f'{metrics.roe_ttm:.2f}%'}"
        + (f"（{metrics.roe_basis}）" if metrics.roe_basis else "")
    )
    if metrics.cash_conversion is None:
        lines.append("- 利润含金量（经营现金流/净利）：无数据（净利非正或现金流缺失，不做比值）")
    else:
        flag = "偏低（利润含金量不足，注意应收/存货）" if metrics.cash_conversion < 0.8 else "正常"
        lines.append(f"- 利润含金量（TTM 经营现金流 ÷ TTM 归母净利）：{metrics.cash_conversion:.2f}（{flag}）")
    if metrics.derived_pe_ttm is not None:
        lines.append(
            f"- 派生 PE(TTM)：{metrics.derived_pe_ttm:.2f}"
            f"（= 最新价 ÷ 派生 EPS(TTM) {metrics.derived_eps_ttm} 元/股）；"
            "口径为现算近似，需与估值快照中的权威 PE(TTM) 交叉校验，不一致时以估值快照为准"
        )
    for note in metrics.notes:
        lines.append(f"- 口径说明：{note}")
    return "\n".join(lines)
