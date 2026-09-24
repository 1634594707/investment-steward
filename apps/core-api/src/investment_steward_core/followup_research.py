"""追问补证研究与季节性统计（2026-09-19 研报与追问质量路线图 P1：Q11—Q15）。

职责边界（与全站一致的分工）：**程序负责路由、重试、时点与数值计算，模型只负责解释证据**。
本模块因此全是纯函数——IO（行情、估值、公告）由 app.py 完成后把结果交进来；
取数失败、样本不足、缺预期数据这些情况都在此处**确定性落成文字与状态**，
不允许被模型的空话盖过去（Q11 验收：不把失败包装成研究完成）。

三块能力：
1. **补证链**（Q11）：`plan_retrieval` 决定该取什么，`retrieval_block` 统一成功/失败口径，
   `retrieval_status_summary` 给出「成功 / 部分成功 / 全部失败」的如实表述；
2. **季节性研究**（Q12）：`seasonality_study` 以交易日历切出节前/节中/节后窗口，
   逐窗口给出可复算的起止日、涨跌幅、区间最大回撤、相对基准超额与样本根数；
   样本 < `MIN_STABLE_SAMPLES` 时明确「不足以支撑稳定胜率」，且窗口只用**已收盘**K 线（不泄漏未来数据）；
3. **催化事件证据链**（Q13）：`catalyst_chain` 按「业务量→价格与成本→利润→市场预期→价格反应」
   逐环判定有无证据，缺哪环就标哪环，禁止从收入增长直接跳到股价必涨。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from investment_steward_core import trading_calendar

# 样本量下限：低于此数只做描述统计，不给「胜率」类结论（Q12 验收）。
MIN_STABLE_SAMPLES = 5
# 未接入的来源（Q11：缺失来源单独记录为接入任务，不静默跳过）。
MISSING_SOURCE_TASKS: dict[str, str] = {
    "announcements": "一手公告/财报原文检索未接入（需交易所或巨潮源）",
    "consensus_estimates": "卖方一致预期数据未接入（无法判断是否超预期）",
    "monthly_operations": "月度经营简报的结构化取数未接入（现为新闻二次摘要）",
}

# 季节性事件窗口（相对锚点的**交易日**偏移，可复算；锚点为事件所在月份的第一个交易日）。
FESTIVAL_WINDOWS: dict[str, dict[str, Any]] = {
    "双十一": {
        "anchor_month": 11,
        "pre_days": 20,
        "mid_days": 10,
        "post_days": 20,
        "label": "双十一（11 月）",
        "note": "以 11 月首个交易日为锚点，节前 20 / 节中 10 / 节后 20 个交易日；"
                "口径是股价区间，不等于快递业务旺季本身。",
    },
    "春节": {
        "anchor_month": 2,
        "pre_days": 20,
        "mid_days": 5,
        "post_days": 15,
        "label": "春节（2 月）",
        "note": "以 2 月首个交易日为锚点；含春节休市周，窗口按交易日历跳过非交易日。",
    },
    "618": {
        "anchor_month": 6,
        "pre_days": 20,
        "mid_days": 10,
        "post_days": 20,
        "label": "618 大促（6 月）",
        "note": "以 6 月首个交易日为锚点，节前 20 / 节中 10 / 节后 20 个交易日。",
    },
}

_WINDOW_STAGES = (("pre", "节前"), ("mid", "节中/当季"), ("post", "节后"))

# Q13 证据链环节（顺序即因果方向，不得倒推）。
CATALYST_CHAIN_STEPS: tuple[tuple[str, str], ...] = (
    ("volume", "业务量"),
    ("price_cost", "价格与成本"),
    ("profit", "利润"),
    ("expectation", "市场预期"),
    ("price_reaction", "价格反应"),
)

_QUESTION_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("seasonality", ("双十一", "双11", "旺季", "节前", "节后", "春节", "618", "季节性", "几月", "月份")),
    ("quote", ("现价", "买点", "突破", "站上", "跌破", "均线", "买点", "买入", "止盈", "仓位")),
    ("valuation", ("估值", "贵", "便宜", "安全边际", "合理价值", "目标价", "市盈率", "PE", "PB")),
    ("operations", ("业务量", "单票", "经营数据", "月度", "收入", "成本", "利润", "现金流")),
    ("announcement", ("公告", "财报", "回购", "减持", "定增", "业绩预告", "披露")),
)


# ---------------------------------------------------------------------------
# Q11：补证链
# ---------------------------------------------------------------------------

def question_topics(question: str) -> list[str]:
    """问题命中的补证主题（确定性关键词路由，不用模型再判一遍要取什么数）。"""
    body = str(question or "")
    return [key for key, words in _QUESTION_TOPICS if any(word in body for word in words)]


def detect_festival(question: str) -> str:
    """问题命中的季节性事件（Q12）；命中不到时回落「双十一」（本轮只统计被问到的窗口）。"""
    body = str(question or "")
    if any(word in body for word in ("双11", "双十一", "光棍")):
        return "双十一"
    if "春节" in body:
        return "春节"
    if "618" in body or "年中" in body:
        return "618"
    for key in FESTIVAL_WINDOWS:
        if key in body:
            return key
    return "双十一"


def study_brief(study: dict[str, Any]) -> str:
    """季节性统计的单行播报（进提示词与页面，数字全部可复算，不含形容词加工）。"""
    if not isinstance(study, dict) or not study.get("available"):
        warning = "；".join(str(item) for item in (study or {}).get("warnings") or [])
        return f"{(study or {}).get('label') or '季节性'}：无可复算样本，不能给出旺季是否上涨的统计结论。{warning}"
    aggregate = study.get("aggregate") or {}
    parts: list[str] = []
    for key in ("pre", "mid", "post"):
        row = aggregate.get(key) or {}
        if not row.get("sample_count"):
            parts.append(f"{row.get('stage_label') or key}：无可复算样本")
            continue
        relative = row.get("mean_relative_return_pct")
        parts.append(
            f"{row['stage_label']}：均值 {row['mean_return_pct']}%、上涨 {row['up_ratio_display']}"
            f"{'、相对基准 ' + str(relative) + '%' if relative is not None else ''}"
            f"（样本 {row['sample_count']} 年：{row['note']}）"
        )
    return f"{study.get('label')}（行情截至 {study.get('data_as_of') or '未标注'}）：" + "；".join(parts)



def plan_retrieval(
    question: str,
    *,
    base_report: dict[str, Any] | None,
    mode: str = "supplement_research",
    available_years: int = 3,
) -> list[dict[str, Any]]:
    """按问题决定本轮要取的数据（Q11 的最小补证链路）。

    只规划**系统已有能力**能取到的项：行情 K 线、估值快照；命中季节性话题时加历史统计；
    命中公告/预期等尚无来源的项，直接落成 `not_available` 计划并写明缺什么接入任务
    （宁标缺口，不假装取到）。
    """
    report = base_report if isinstance(base_report, dict) else {}
    symbol = str(report.get("symbol") or report.get("subject") or "").strip()
    topics = question_topics(question)
    plan: list[dict[str, Any]] = []
    if not symbol:
        return plan
    if "interpret" == str(mode or "").strip():
        return plan
    if {"quote", "seasonality", "valuation"} & set(topics) or not topics:
        plan.append({
            "kind": "kline",
            "label": "日线行情（现价、均线、区间收益）",
            "reason": "、".join(topics) or "问题涉及价格与时间条件，需要最新行情",
            "params": {"symbol": symbol, "limit": max(260, 250 * max(1, available_years)), "period": "day"},
            "source_name": "行情源（腾讯/东财，按 market_feed 顺序）",
        })
    if "valuation" in topics:
        plan.append({
            "kind": "valuation",
            "label": "估值快照（PE/PB/历史分位/同业位次）",
            "reason": "估值口径问题需要重新取数，不能沿用旧快照",
            "params": {"symbol": symbol},
            "source_name": "估值源（valuation_evidence）",
        })
    if "seasonality" in topics:
        plan.append({
            "kind": "seasonality",
            "label": "季节性区间统计（节前/节中/节后相对大盘）",
            "reason": "问的是「某个时点是否会涨」，必须有历史区间统计而非印象",
            "params": {"symbol": symbol, "available_years": available_years},
            "source_name": "由本轮行情 K 线复算（不额外取数）",
        })
    for topic in topics:
        missing = {
            "announcement": MISSING_SOURCE_TASKS["announcements"],
            "operations": MISSING_SOURCE_TASKS["monthly_operations"],
        }.get(topic)
        if topic == "seasonality":
            missing = MISSING_SOURCE_TASKS["consensus_estimates"]
        if missing:
            plan.append({
                "kind": f"missing_{topic}",
                "label": {
                    "announcement": "一手公告/财报原文",
                    "operations": "月度经营简报结构化数据",
                    "seasonality": "卖方一致预期（判断是否超预期）",
                }.get(topic, topic),
                "reason": "问题命中该口径，但系统尚无对应来源",
                "params": {},
                "source_name": "未接入",
                "not_available": missing,
            })
    return plan


def retrieval_block(
    kind: str,
    *,
    label: str = "",
    payload: dict[str, Any] | None,
    error: str = "",
    source_name: str = "",
    url: str = "",
    retrieved_at: str = "",
) -> dict[str, Any]:
    """把一次取数结果统一成带时点与口径的块（成功/失败同构，供提示词与页面复用）。"""
    data = payload if isinstance(payload, dict) else {}
    ok = bool(data) and not error
    return {
        "kind": str(kind or "").strip()[:40],
        "label": str(label or data.get("label") or kind or "").strip()[:120],
        "ok": ok,
        "summary": str(data.get("summary") or "").strip()[:1200],
        "as_of": str(data.get("as_of") or "").strip()[:10],
        "retrieved_at": str(retrieved_at or data.get("retrieved_at") or "").strip()[:32],
        "source_name": str(source_name or data.get("source_name") or "").strip()[:120],
        "url": str(url or data.get("url") or "").strip()[:500],
        "error": str(error or "").strip()[:200],
        "metrics": {key: value for key, value in data.items() if isinstance(value, (int, float)) or value is None},
        "rows": data.get("rows") if isinstance(data.get("rows"), list) else [],
        "trust_label": "系统本次获取（未独立核验原始口径）" if ok else "获取失败",
    }


def retrieval_status_summary(blocks: Iterable[Any]) -> dict[str, Any]:
    """补证结果三态（Q11 验收：成功 / 部分成功 / 全部失败都如实展示）。"""
    rows = [row for row in (blocks or []) if isinstance(row, dict)]
    if not rows:
        return {"requested": 0, "succeeded": 0, "failed": 0, "state": "none", "note": "本轮未安排任何取数。"}
    succeeded = [row for row in rows if row.get("ok")]
    failed = [row for row in rows if not row.get("ok")]
    if not failed:
        state = "all_succeeded"
    elif succeeded:
        state = "partial"
    else:
        state = "all_failed"
    notes = {
        "all_succeeded": f"{len(succeeded)} 项数据全部取到（截至日见各项标注）。",
        "partial": f"{len(succeeded)} 项取到、{len(failed)} 项失败：{ '；'.join(str(r.get('error') or r.get('label')) for r in failed) }。"
                   "失败项不得当作已知事实。",
        "all_failed": f"{len(failed)} 项数据全部获取失败：本次回答只能沿用原报告口径，不构成补充研究完成。",
    }
    return {
        "requested": len(rows),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "state": state,
        "note": notes[state],
        "failed_items": [
            {"kind": row.get("kind"), "label": row.get("label"), "error": row.get("error") or "未记录原因"}
            for row in failed
        ],
    }


# ---------------------------------------------------------------------------
# Q12：季节性区间统计
# ---------------------------------------------------------------------------

def _price_rows(kline_rows: Iterable[Any]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for row in kline_rows if isinstance(kline_rows, list) else kline_rows or []:
        if not isinstance(row, dict):
            continue
        stamp = str(row.get("timestamp") or row.get("date") or "").strip()[:10]
        try:
            close = float(row.get("close", row.get("price", row.get("adjusted_close"))))  # type: ignore[arg-type]
        except (TypeError, ValueError, KeyError):
            continue
        if stamp and close > 0:
            out.append((stamp, close))
    return out


def max_drawdown(closes: list[float]) -> float | None:
    """区间内最大回撤（正值百分比）；不足两根 K 线返回 None（不编一个数）。"""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for price in closes:
        peak = max(peak, price)
        if peak > 0:
            worst = max(worst, (peak - price) / peak)
    return round(worst * 100.0, 2)


def seasonality_study(
    kline_rows: Iterable[Any],
    *,
    festival: str = "双十一",
    benchmark_rows: Iterable[Any] | None = None,
    available_years: int = 3,
) -> dict[str, Any]:
    """事件季节性统计（Q12）：窗口可复算、基准可比、小样本不吹胜率。

    只用**已收盘**的 K 线（窗口末端超过最新 K 线日期的窗口整段丢弃 → 无未来数据泄漏），
    窗口边界全部由真实交易日历（同一批 K 线的日期）推出，非交易日不占位。
    """
    spec = FESTIVAL_WINDOWS.get(str(festival or "").strip()) or FESTIVAL_WINDOWS["双十一"]
    pairs = _price_rows(kline_rows)
    dates = [day for day, _ in pairs]
    calendar = trading_calendar.TradingCalendar(dates)
    benchmark_pairs = _price_rows(benchmark_rows or [])
    result: dict[str, Any] = {
        "festival": str(festival),
        "label": str(spec["label"]),
        "method": (
            f"锚点＝{spec['label']}所在月首个交易日；节前 {spec['pre_days']}、节中 {spec['mid_days']}、"
            f"节后 {spec['post_days']} 个交易日；收益按收盘价首尾相除，回撤取窗口内峰值回落最大值。"
            "全部窗口边界与数字均可由行情序列复算。"
        ),
        "note": str(spec["note"]),
        "windows": [],
        "aggregate": {},
        "warnings": [],
        "data_as_of": dates[-1] if dates else "",
        "available": False,
    }
    if not calendar.available:
        result["warnings"].append("未取得行情日历或收盘序列，无法计算季节性窗口。")
        result["aggregate"] = {
            "sample_count": 0,
            "note": "样本 0：本轮未取得可复算的历史区间，不能对「旺季是否上涨」给出任何统计结论。",
        }
        return result

    latest_day = trading_calendar.parse_date(dates[-1])
    # 取**最近**可得的若干个年份（不是最早）：问「双十一会不会涨」的人要的是近年经验，
    # 按升序取前 N 年会把最新一年整年丢掉；没有该月数据的年份仍如实列 `no_data`，不静默剔除。
    known_years = sorted({
        day.year for day in (trading_calendar.parse_date(item) for item in dates) if day is not None
    }, reverse=True)
    years = sorted(known_years[: max(1, int(available_years))])
    windows: list[dict[str, Any]] = []
    for year in years:
        anchor_month_first = next(
            (
                day
                for day in (trading_calendar.parse_date(item) for item in dates)
                if day is not None and day.year == year and day.month == int(spec["anchor_month"])
            ),
            None,
        )
        if anchor_month_first is None:
            windows.append({
                "year": year,
                "status": "no_data",
                "note": f"{year} 年 {spec['anchor_month']} 月无行情数据（日历覆盖不足或当时未上市）。",
            })
            continue
        for key, stage in _WINDOW_STAGES:
            span = {"pre": spec["pre_days"], "mid": spec["mid_days"], "post": spec["post_days"]}[key]
            if key == "pre":
                end = anchor_month_first
                start_index = max(0, calendar.ordered.index(anchor_month_first) - int(span))
                start = calendar.ordered[start_index]
            elif key == "mid":
                start = anchor_month_first
                end = calendar.add_trading_days(anchor_month_first, int(span))
            else:
                start = calendar.add_trading_days(anchor_month_first, int(spec["mid_days"]))
                end = None
                if start is not None:
                    end = calendar.add_trading_days(start, int(span) - 1)
            if start is None or end is None:
                windows.append({
                    "year": year, "stage": key, "stage_label": stage, "status": "incomplete",
                    "note": "窗口超出行情覆盖期（交易日历不足以推出该窗口），不计入统计。",
                })
                continue
            if latest_day is not None and end > latest_day:
                windows.append({
                    "year": year, "stage": key, "stage_label": stage, "status": "in_the_future",
                    "note": f"窗口末端 {trading_calendar.format_day(end)} 晚于最新行情日 "
                            f"{trading_calendar.format_day(latest_day)}，为避免未来数据泄漏不计入。",
                })
                continue
            segment = [(day, price) for day, price in pairs
                       if start <= trading_calendar.parse_date(day) <= end]  # type: ignore[operator]
            if len(segment) < 2:
                windows.append({
                    "year": year, "stage": key, "stage_label": stage, "status": "insufficient_bars",
                    "note": f"窗口内仅 {len(segment)} 根 K 线，不足以计算区间收益。",
                })
                continue
            closes = [price for _, price in segment]
            first_day, last_day = segment[0][0], segment[-1][0]
            change = round((closes[-1] / closes[0] - 1.0) * 100.0, 2)
            item: dict[str, Any] = {
                "year": year,
                "stage": key,
                "stage_label": stage,
                "status": "measured",
                "start": first_day,
                "end": last_day,
                "bars": len(closes),
                "start_close": closes[0],
                "end_close": closes[-1],
                "return_pct": change,
                "max_drawdown_pct": max_drawdown(closes),
                "benchmark_return_pct": None,
                "relative_return_pct": None,
            }
            bench = [(day, price) for day, price in benchmark_pairs
                     if trading_calendar.parse_date(start) <= trading_calendar.parse_date(day)
                     <= trading_calendar.parse_date(end)]
            if len(bench) >= 2:
                bench_change = round((bench[-1][1] / bench[0][1] - 1.0) * 100.0, 2)
                item["benchmark_return_pct"] = bench_change
                item["relative_return_pct"] = round(change - bench_change, 2)
                item["benchmark"] = "基准指数同期收益"
            else:
                item["benchmark_note"] = "未取得基准同期序列：只报绝对收益，不给「跑赢大盘」结论。"
            windows.append(item)

    measured = [row for row in windows if row.get("status") == "measured"]
    result["windows"] = windows
    result["available"] = bool(measured)
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for row in measured:
        by_stage.setdefault(str(row["stage"]), []).append(row)
    stats: dict[str, Any] = {}
    for key, stage in _WINDOW_STAGES:
        rows = by_stage.get(key) or []
        if not rows:
            stats[key] = {"stage_label": stage, "sample_count": 0, "note": "无可复算样本。"}
            continue
        returns = [float(row["return_pct"]) for row in rows]
        up = sum(1 for value in returns if value > 0)
        stats[key] = {
            "stage_label": stage,
            "sample_count": len(rows),
            "years": [row["year"] for row in rows],
            "mean_return_pct": round(sum(returns) / len(returns), 2),
            "up_count": up,
            "up_ratio_display": f"{up}/{len(rows)}",
            "mean_relative_return_pct": (
                round(
                    sum(float(row["relative_return_pct"]) for row in rows if row.get("relative_return_pct") is not None)
                    / max(1, sum(1 for row in rows if row.get("relative_return_pct") is not None)),
                    2,
                )
                if any(row.get("relative_return_pct") is not None for row in rows) else None
            ),
            "worst_drawdown_pct": max(
                (float(row["max_drawdown_pct"]) for row in rows if row.get("max_drawdown_pct") is not None),
                default=None,
            ),
        }
        if len(rows) < MIN_STABLE_SAMPLES:
            stats[key]["note"] = (
                f"样本仅 {len(rows)} 个年份：只作历史描述，不足以支撑「旺季大概率上涨」的稳定胜率表述。"
            )
        else:
            stats[key]["note"] = f"样本 {len(rows)} 个年份：可按历史频率描述，仍非对未来的承诺。"
    result["aggregate"] = stats
    if not benchmark_pairs:
        result["warnings"].append("无基准序列：结果只有绝对收益，不能表述为相对大盘/行业的超额。")
    result["warnings"].append("经营旺季（业务量峰值）与股价旺季不是同一件事：本节只统计价格区间。")
    return result


# ---------------------------------------------------------------------------
# Q13：催化事件证据链
# ---------------------------------------------------------------------------

def catalyst_chain(evidence: Any) -> dict[str, Any]:
    """按「业务量→价格与成本→利润→市场预期→价格反应」逐环判定证据（缺环显式标出）。

    `evidence` 接受 {volume, price_cost, profit, expectation, price_reaction} 五键，
    值为文本/数值列表或 None。判定只看该环有没有**具体口径**（数字或来源），
    有则 `evidenced`、无则 `missing` 并写明补什么来源（Q13 验收）。
    """
    data = evidence if isinstance(evidence, dict) else {}
    labels = dict(CATALYST_CHAIN_STEPS)
    steps: list[dict[str, Any]] = []
    broken_at = ""
    for key, _label in CATALYST_CHAIN_STEPS:
        value = data.get(key)
        text = value if isinstance(value, str) else " ".join(str(item) for item in (value or [])) if isinstance(value, list) else str(value or "")
        has_number = bool(re.search(r"\d", text))
        evidenced = bool(text.strip()) and (has_number or "来源" in text or "公告" in text)
        note = ""
        if not evidenced:
            note = {
                "volume": "缺业务量数据（月度经营简报/邮政局口径），无法确认旺季是否真的带来量增。",
                "price_cost": "缺单票收入与单票成本口径，量增不等于价格与成本同步改善。",
                "profit": "缺利润拆解（一次性/基数/毛利），无法把量价变化接到利润。",
                "expectation": "缺一致预期或已披露预告，**不能宣称超预期**（MISSING：卖方一致预期未接入）。",
                "price_reaction": "缺区间价格反应统计，无法说明市场是否已经计价。",
            }.get(key, "缺该环节证据。")
            if not broken_at:
                broken_at = key
        steps.append({
            "key": key,
            "label": labels[key],
            "status": "evidenced" if evidenced else "missing",
            "evidence": text.strip()[:300],
            "note": note,
        })
    return {
        "steps": steps,
        "complete": not broken_at,
        "broken_at": broken_at,
        "note": (
            "五环齐备才可以说「催化→业绩→预期→价格」成立；任一环缺失时结论只能写到该环为止。"
            if broken_at else "五环均有证据，可按完整链条表述，但仍属条件化判断而非承诺。"
        ),
        "guardrail": "禁止由「收入增长」直接推导「股价必涨」；利润改善被市场计价后价格反应可能相反。",
    }


# ---------------------------------------------------------------------------
# Q21：验证点复盘五态（建议观察 / 已加入跟踪 / 待验证 / 已验证 / 无法验证）
# ---------------------------------------------------------------------------

TRACKING_VOCAB = ("suggest_observe", "tracked", "pending_verify", "verified", "unverifiable")
TRACKING_LABELS: dict[str, str] = {
    "suggest_observe": "建议观察",
    "tracked": "已加入跟踪",
    "pending_verify": "待验证",
    "verified": "已验证",
    "unverifiable": "无法验证",
}
# 回填结果词表**取自领域模型 `VerificationResult`**（verified/refuted/insufficient_data）：
# 复盘端点写什么，这里就读什么，不再自造一套 confirmed/denied 让两边对不上。
VERIFICATION_RESULT_LABELS: dict[str, str] = {
    "verified": "已验证成立",
    "refuted": "已验证不成立",
    "insufficient_data": "到期数据不足，无法判定",
}
_VERIFIED_RESULTS = ("verified", "refuted")

TRACKING_NOTE = (
    "本表只登记**已落成**的跟踪与人工回填结果：事件到期不会自动监控，"
    "「已加入跟踪」仅在 `verifiable_judgments` 里确有该判断时出现。"
)


def watchpoint_tracking_view(
    watchpoints: Iterable[Any],
    *,
    materialized_ids: Iterable[str] = (),
    verifications: dict[str, dict[str, Any]] | None = None,
    today: str = "",
) -> dict[str, Any]:
    """验证点复盘五态（Q21 验收：只有跟踪成功才显示「已加入」）。

    输入由调用方给出：`materialized_ids` = 本轮 review-queue 真正确认存在的判断 id
    （G01 幂等物化的结果），`verifications` = 已回填结果（`{id: {result, evidence, note}}`）。
    本函数**不查库、不猜**：没给 id 就当没落成跟踪。

    判定顺序（确定性）：
    1. 有回填结果：`confirmed/denied` → `verified`；`no_data` 或其他 → `unverifiable`；
    2. id 在 `materialized_ids` 里 → 有 `due_on` 且已到期 → `pending_verify`，未到 → `tracked`；
    3. 没有 `due_on`（事件锚定）→ `suggest_observe`，说明「等事件触发后再落成跟踪」；
    4. 有 `due_on` 但没落成跟踪（例如物化失败）→ `suggest_observe` + 如实写明未入库原因。
    """
    rows = [row for row in (watchpoints or []) if isinstance(row, dict)]
    materialized = {str(item).strip() for item in (materialized_ids or []) if str(item).strip()}
    recorded = verifications if isinstance(verifications, dict) else {}
    today_text = str(today or "").strip()[:10]

    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {key: 0 for key in TRACKING_VOCAB}
    for index, row in enumerate(rows):
        judgment_id = str(row.get("judgment_id") or "").strip()
        due_on = str(row.get("due_on") or "").strip()[:10]
        result_row = recorded.get(judgment_id) if judgment_id else None
        result = str((result_row or {}).get("result") or "").strip().lower()
        evidence_text = str(
            (result_row or {}).get("outcome")
            or (result_row or {}).get("evidence")
            or ""
        ).strip()
        note = ""
        if result in _VERIFIED_RESULTS:
            state = "verified"
            note = f"已回填「{VERIFICATION_RESULT_LABELS[result]}」，判断变化见复盘记录。"
        elif result:
            state = "unverifiable"
            note = f"到期回填为「{VERIFICATION_RESULT_LABELS.get(result, result)}」，无法判定该验证点。"
        elif judgment_id and judgment_id in materialized:
            if due_on and today_text and due_on <= today_text:
                state = "pending_verify"
                note = f"已到验证时点（{due_on}），等待人工回填。"
            else:
                state = "tracked"
                note = f"已加入待复盘队列{f'（到期 {due_on}）' if due_on else ''}。"
        elif not due_on:
            state = "suggest_observe"
            note = "验证时点锚定事件、无确切日期，未落成跟踪记录：等事件发生后再加入待复盘。"
        else:
            state = "suggest_observe"
            note = f"有到期日（{due_on}）但未在跟踪清单中：本轮未物化成功，不能显示为「已加入」。"
        counts[state] += 1
        items.append({
            "index": index + 1,
            "signal": str(row.get("signal") or "").strip(),
            "due_on": due_on,
            "judgment_id": judgment_id,
            "state": state,
            "state_label": TRACKING_LABELS[state],
            "verification_result": result,
            "verification_result_label": VERIFICATION_RESULT_LABELS.get(result, ""),
            "verification_evidence": evidence_text[:500],
            "note": note,
        })

    summary = "、".join(f"{TRACKING_LABELS[key]} {counts[key]}" for key in TRACKING_VOCAB if counts[key]) or "无验证点"
    return {
        "version": "v1",
        "items": items,
        "counts": counts,
        "summary": summary,
        "note": TRACKING_NOTE,
        "auto_monitoring": False,
    }


def catalyst_evidence_from(
    blocks: Iterable[Any],
    supplements: Iterable[Any] = (),
) -> dict[str, str]:
    """把本轮材料按关键词摊到证据链五个环节上（Q13 的输入准备）。

    只做**搬运**：哪段文本属于哪一环由关键词确定，环与环之间的因果关系仍要有数字支撑，
    取不到文本的环节留空 → `catalyst_chain` 会把该环标成 missing（缺哪环一目了然）。
    """
    texts: list[str] = []
    for row in blocks or []:
        if isinstance(row, dict) and row.get("ok"):
            texts.append(f"{row.get('label') or ''} {row.get('summary') or ''}")
    for row in supplements or []:
        if isinstance(row, dict):
            texts.append(str(row.get("text") or ""))
    body = " ".join(texts)
    buckets = {
        "volume": ("业务量", "件量", "票量", "单量", "处理量"),
        "price_cost": ("单票收入", "单票成本", "价格战", "均价", "成本"),
        "profit": ("净利", "利润", "毛利", "业绩"),
        "expectation": ("一致预期", "预期", "预告", "上调", "下调"),
        "price_reaction": ("收盘", "均线", "涨幅", "回撤", "区间", "成交"),
    }
    return {
        key: " ".join(segment for segment in re.split(r"[；;。\n]", body) if any(word in segment for word in words))[:300]
        for key, words in buckets.items()
    }


def catalyst_chain_overreach_flags(answer_text: str, chain: dict[str, Any]) -> list[str]:
    """结论越过证据链时的如实标注（Q13 验收：不由收入增长推导股价必涨、无预期数据不称超预期）。"""
    body = str(answer_text or "")
    statuses = {str(row.get("key")): str(row.get("status")) for row in chain.get("steps") or []}
    flags: list[str] = []
    if any(word in body for word in ("超预期", "超出预期", "业绩超预期")) and statuses.get("expectation") != "evidenced":
        flags.append(
            "正文出现「超预期」表述，但本轮无卖方一致预期或已披露预告可依（一致预期来源未接入）："
            "该说法只能按「绝对值改善」理解，不构成超预期判断。"
        )
    if any(word in body for word in ("必涨", "一定会涨", "稳涨", "必然上涨")) and not chain.get("complete"):
        flags.append(
            f"正文出现「必涨/一定会涨」式断言，而证据链在「{chain.get('broken_at') or '未知'}」环节即缺证据："
            "经营改善不等于价格必然反应，该断言不予采信。"
        )
    return flags


# ---------------------------------------------------------------------------
# Q14/Q15：连续追问上下文与新增证据快照
# ---------------------------------------------------------------------------

_FOLLOW_ON = re.compile(r"^(那|那么|继续|再说|还是|等等|等突破|刚才|上面)|呢[？?]?$")


def is_follow_on(question: str) -> bool:
    """是否为承接式追问（「那等突破呢」）：需要在同一报告的历史轮次里解析所指条件。"""
    return bool(_FOLLOW_ON.search(str(question or "").strip()))


def history_digest(turns: Iterable[Any], *, limit: int = 3) -> list[dict[str, Any]]:
    """历史追问摘要（Q14）：只带问题、直接回答、状态轴与证据编号，控制上下文大小。"""
    rows = [row for row in (turns or []) if isinstance(row, dict)]
    out: list[dict[str, Any]] = []
    for row in rows[-max(1, limit):]:
        out.append({
            "analysis_turn_id": row.get("analysis_turn_id"),
            "turn_index": row.get("turn_index"),
            "question": str(row.get("question") or "")[:200],
            "direct_answer": str(row.get("direct_answer") or row.get("answer") or "")[:400],
            "revision_status": row.get("revision_status") or "",
            "data_basis_label": row.get("data_basis_label") or row.get("state_line") or "",
            "evidence_refs": [
                str(item.get("url") or item.get("source_name") or item.get("kind") or "")
                for item in (row.get("evidence_snapshot") or []) if isinstance(item, dict)
            ][:6],
        })
    return out


def evidence_snapshot(blocks: Iterable[Any], *, supported_claims: Iterable[str] = (), analysis_version: str = "") -> list[dict[str, Any]]:
    """新增证据快照（Q15）：来源、事件日期、获取时间、可信状态、所支撑判断与分析版本。

    快照写进附录 payload，后续刷新不会覆盖——用户能回看「当时依据什么改变判断」。
    """
    refs = [str(item).strip() for item in supported_claims if str(item).strip()]
    out: list[dict[str, Any]] = []
    for row in blocks or []:
        if not isinstance(row, dict):
            continue
        out.append({
            "kind": row.get("kind"),
            "label": row.get("label"),
            "source_name": row.get("source_name") or "",
            "url": row.get("url") or "",
            "url_note": "" if row.get("url") else "来源未提供可访问链接",
            "event_date": row.get("as_of") or "",
            "retrieved_at": row.get("retrieved_at") or "",
            "trust_label": row.get("trust_label") or "未独立核验",
            "status": "ok" if row.get("ok") else "failed",
            "error": row.get("error") or "",
            "supported_claims": refs,
            "analysis_version": analysis_version,
        })
    return out


# ---------------------------------------------------------------------------
# Q09：来源目录与核心支撑统计回溯（研报与追问共用）
# ---------------------------------------------------------------------------

_SOURCE_LABELS: dict[str, str] = {
    "S1": "行情与技术面（K 线/指标/战法）",
    "S2": "近期新闻",
    "S3": "近期公告（标题口径）",
    "S4": "财务摘要与派生指标",
    "S5": "估值与历史分位",
    "S6": "龙虎榜席位",
}


def build_source_catalog(report: Any) -> list[dict[str, Any]]:
    """把研报 payload 里的来源登记成可回查目录（Q09）。

    只搬运 payload 里**确实存在**的信息：提供方来自 `source_meta`，数据日期来自 `source_asof`，
    链接只有 payload 里带（`source_links` 或证据行 `source_uri`）才写，取不到就如实标
    「未提供可访问链接」——**不猜 URL、不拼 URL**。`used_by` 由 claims 的引用关系反推，
    这样「2/3」这类统计能顺着编号回到判断与来源。
    """
    data = report if isinstance(report, dict) else {}
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    asof = data.get("source_asof") if isinstance(data.get("source_asof"), dict) else {}
    meta = data.get("source_meta") if isinstance(data.get("source_meta"), dict) else {}
    links = data.get("source_links") if isinstance(data.get("source_links"), dict) else {}
    cited = {str(item).strip() for item in (data.get("citations") or []) if str(item).strip()}
    evidence_rows = [row for row in (data.get("evidence") or []) if isinstance(row, dict)]
    url_by_type: dict[str, str] = {}
    for row in evidence_rows:
        uri = str(row.get("source_uri") or row.get("url") or "").strip()
        kind = str(row.get("evidence_type") or "").strip()
        if uri and kind and kind not in url_by_type:
            url_by_type[kind] = uri
    kind_for_source = {"S2": "analysis", "S3": "announcement"}

    findings = data.get("claim_findings") if isinstance(data.get("claim_findings"), dict) else {}
    claims = findings.get("claims") if isinstance(findings.get("claims"), list) else (data.get("claims") or [])
    used_by: dict[str, list[str]] = {}
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("claim_id") or "").strip()
        if not claim_id:
            continue
        for sid in claim.get("sources") or []:
            used_by.setdefault(str(sid).strip(), []).append(claim_id)

    tiers = (
        data.get("evidence_quality", {}).get("tiers")
        if isinstance(data.get("evidence_quality"), dict) else {}
    ) or {}
    quality_of = {
        str(sid): tier
        for tier, ids in tiers.items()
        for sid in (ids or [])
    }

    ids = sorted(set(sources) | set(asof) | set(meta) | set(cited) | set(used_by))
    out: list[dict[str, Any]] = []
    for source_id in ids:
        raw_block = sources.get(source_id)
        first_line = str(raw_block or "").strip().splitlines()[0] if str(raw_block or "").strip() else ""
        name = first_line.split("：")[0][:120] if first_line else _SOURCE_LABELS.get(source_id, "")
        url = str(links.get(source_id) or url_by_type.get(kind_for_source.get(source_id, ""), "")).strip()
        entry = {
            "source_id": source_id,
            "name": name or _SOURCE_LABELS.get(source_id, ""),
            "url": url[:500],
            "url_note": "" if url else "来源未提供可访问链接（原始数据在本地证据包内，未对外再分发）",
            "data_date": str(asof.get(source_id) or "")[:10],
            "retrieved_at": str((meta.get(source_id) or {}).get("retrieved_at") if isinstance(meta.get(source_id), dict) else "")[:32],
            "provider": str((meta.get(source_id) or {}).get("provider") if isinstance(meta.get(source_id), dict) else "")[:120],
            "quality": str(quality_of.get(source_id) or ""),
            "scope_note": "",
            "cited": source_id in cited or bool(used_by.get(source_id)),
            "used_by": sorted(set(used_by.get(source_id) or []))[:12],
        }
        entry["scope_note"] = _source_scope_note(source_id, entry)
        out.append(entry)
    return out


def _source_scope_note(source_id: str, entry: dict[str, Any]) -> str:
    """口径限制（Q09）：每类来源能用到什么程度，说死它不能用什么。"""
    notes = {
        "S1": "行情为数据商接口二次分发；指标与战法按收盘口径，不含盘中实时。",
        "S2": "新闻为媒体摘要，只能作线索与情绪，不能当作公司事实。",
        "S3": "公告仅标题口径，正文数字须回链交易所原文核对。",
        "S4": "财务为数据商 F10 摘要（二次分发）：可核对绝对值与同比，不构成审计级证据。",
        "S5": "估值同口径不含股息率；分位窗口受历史长度限制，同业仅取盈利可比样本。",
        "S6": "龙虎榜席位为交易所公布样本营业部口径，不等于全市场资金归属。",
    }
    base = notes.get(source_id, "")
    if not entry.get("data_date"):
        base = (base + " 未记录数据日期。").strip()
    return base[:200]


def core_support_trace(claim_findings: Any) -> dict[str, Any]:
    """把「部分支撑（2/3）」这类统计摊开成可追溯清单（Q09 验收）。"""
    findings = claim_findings if isinstance(claim_findings, dict) else {}
    claims = [row for row in (findings.get("claims") or []) if isinstance(row, dict)]
    core = [row for row in claims if str(row.get("importance") or "").strip().lower() == "high"] or claims
    supported = [
        row for row in core
        if str(row.get("support") or "").strip() == "full"
    ]
    return {
        "counted_claim_ids": [str(row.get("claim_id") or "").strip() for row in core],
        "supported_claim_ids": [str(row.get("claim_id") or "").strip() for row in supported],
        "unsupported_claim_ids": [
            str(row.get("claim_id") or "").strip() for row in core
            if str(row.get("claim_id") or "").strip()
            not in {str(item.get("claim_id") or "").strip() for item in supported}
        ],
        "counts": {
            "supported": len(supported),
            "total": len(core),
        },
        "state": findings.get("core_conclusion_state") or "",
        "note": str(
            findings.get("core_support_note")
            or "核心口径：importance=high 的判断全数支撑才算「有支撑」；未标 importance 时全部判断视为核心。"
        )[:300],
    }

