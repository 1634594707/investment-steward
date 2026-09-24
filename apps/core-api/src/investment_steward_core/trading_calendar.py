"""交易日历与「时点口径」工具（2026-09-19 研报与追问质量路线图 Q06/Q07）。

要解决的问题：研报与追问里出现「未来 5-10 个交易日」「双十一前」这类时间表述时，
没有可靠的交易日历就会退化成自然日推算或模型随口给的日期——9 月的 MA20 因此被当成
11 月的实时阈值，`2026-11-30` 这类无依据截止日也被写进验证点。本模块把时间口径收敛成
确定性计算，规则如下：

- **日历来源只有真实行情**：交易日集合来自 K 线行的 `timestamp`（`calendar_from_kline_rows`），
  与市场数据同源，不内置节假日表（交易所调休无法从公开规则推出，内置等于猜）；
- **覆盖期外不推算**：目标日期落在日历覆盖之外（未来、或数据尚未刷新到）时一律返回
  `outside_coverage`，调用方必须降级为**事件锚定**表述（「11 月经营简报披露后」），
  不得凭空给出具体日（Q07 验收）；
- **周末/节假日可区分**：周六周日 = `weekend`，覆盖期内的工作日但无行情 = `holiday`，
  两者都可确定性顺延到下一个交易日并留痕（`shifted`）；
- **事实时间与预计时间分开**：来自输入材料/已披露公告的日期记 `factual`，由日历推算的
  未来日期记 `estimated`（Q07：预计时间与事实时间分开）。

设计铁律（与全站一致）：纯函数、无 IO；行情行由调用方（app.py）传入。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

ISO_FORMAT = "%Y-%m-%d"

_DATE_PATTERNS = (
    re.compile(r"(?:19|20)\d{2}[-/年]\s*\d{1,2}[-/月]\s*\d{1,2}"),
)
_YEAR_MONTH_DAY = re.compile(r"^((?:19|20)\d{2})[-/年]\s*(\d{1,2})[-/月](\d{1,2})")

# 时点口径取值：日历判定结果与「日期从哪来」两轴共用一份词表，避免前后端各写一套。
DAY_KIND_TRADING = "trading_day"
DAY_KIND_WEEKEND = "weekend"
DAY_KIND_HOLIDAY = "holiday"
DAY_KIND_OUTSIDE = "outside_coverage"
DAY_KIND_UNKNOWN = "unknown"

DATE_KIND_FACTUAL = "factual"  # 已披露/输入材料里的事实日期
DATE_KIND_ESTIMATED = "estimated"  # 由交易日历推算出来的预计日期
DATE_KIND_EVENT = "event_anchor"  # 只能锚定事件，给不出日期
DATE_KIND_UNKNOWN = "unknown"  # 连日期都解析不出来（与 DAY_KIND_UNKNOWN 同为「未知」，但两轴不混用）
DATE_KIND_OUTSIDE = "outside_coverage"  # 日期超出行情日历覆盖期：不可用作实时口径
DATE_KIND_DEMOTED = "demoted"  # 模型给了日期但依据不可靠，已降级为事件锚定


def parse_date(value: Any) -> date | None:
    """宽松解析常见中文/ISO 日期写法；解析不出返回 None（不猜一个日期）。"""
    text = str(value or "").strip()
    if not text:
        return None
    match = _YEAR_MONTH_DAY.match(text)
    if match is None:
        for pattern in _DATE_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                break
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except (TypeError, ValueError):
        return None


def format_day(value: date | None) -> str:
    return value.strftime(ISO_FORMAT) if value is not None else ""


class TradingCalendar:
    """以真实行情日期为唯一依据的交易日历。空集合时 `available` 为 False，调用方须如实降级。"""

    __slots__ = ("days", "ordered", "source")

    def __init__(self, trading_days: Iterable[Any], *, source: str = "") -> None:
        parsed = {day for day in (parse_date(item) for item in trading_days) if day is not None}
        self.days = frozenset(parsed)
        self.ordered = sorted(self.days)
        self.source = str(source or "").strip()[:120]

    @property
    def available(self) -> bool:
        return bool(self.ordered)

    def coverage(self) -> tuple[str, str]:
        """(最早, 最晚) 已知交易日；无数据时返回 ("", "")。"""
        if not self.ordered:
            return ("", "")
        return (format_day(self.ordered[0]), format_day(self.ordered[-1]))

    def is_trading_day(self, value: Any) -> bool | None:
        """True/False 可判定时返回；落在覆盖期外或无法解析返回 None（未知，不假设）。"""
        day = value if isinstance(value, date) else parse_date(value)
        if day is None or not self.available:
            return None
        first, last = self.ordered[0], self.ordered[-1]
        if day < first or day > last:
            return None
        return day in self.days

    def day_kind(self, value: Any) -> str:
        day = value if isinstance(value, date) else parse_date(value)
        if day is None:
            return DAY_KIND_UNKNOWN
        if not self.available:
            return DAY_KIND_OUTSIDE
        first, last = self.ordered[0], self.ordered[-1]
        if day < first or day > last:
            return DAY_KIND_OUTSIDE
        if day in self.days:
            return DAY_KIND_TRADING
        return DAY_KIND_WEEKEND if day.weekday() >= 5 else DAY_KIND_HOLIDAY

    def next_trading_day(self, value: Any, *, after: bool = True) -> date | None:
        """≥（或 >）给定日期的第一个**已知**交易日；覆盖期外返回 None。"""
        day = value if isinstance(value, date) else parse_date(value)
        if day is None or not self.available:
            return None
        for candidate in self.ordered:
            if candidate > day or (candidate == day and not after):
                return candidate
        return None

    def add_trading_days(self, value: Any, count: int) -> date | None:
        """从给定日期起顺延 `count` 个交易日（count=1 → 下一交易日）。

        起点不在日历内时先归位到下一个已知交易日再计数；任何一步落在覆盖期外都返回
        None——宁缺（事件锚定）勿造（Q07）。
        """
        day = value if isinstance(value, date) else parse_date(value)
        if day is None or not self.available or count < 0:
            return None
        anchor = self.next_trading_day(day, after=count >= 1)
        if anchor is None:
            return None
        if count == 0:
            return anchor
        target_index = self.ordered.index(anchor) + count - 1
        if target_index >= len(self.ordered):
            return None
        return self.ordered[target_index]

    def trading_days_between(self, start: Any, end: Any) -> int | None:
        """区间内交易日根数（含端点）；任一端不可解析或超出覆盖期返回 None。"""
        first, last = parse_date(start), parse_date(end)
        if first is None or last is None or first > last or not self.available:
            return None
        if first < self.ordered[0] or last > self.ordered[-1]:
            return None
        return sum(1 for day in self.ordered if first <= day <= last)


def calendar_from_kline_rows(rows: Iterable[Any], *, source: str = "") -> TradingCalendar:
    """从 K 线行提取交易日（行内 `timestamp` 为 ISO 串，取日期部分）。"""
    days: list[str] = []
    for row in rows if isinstance(rows, list) else rows or []:
        if not isinstance(row, dict):
            continue
        stamp = str(row.get("timestamp") or row.get("date") or "").strip()
        if stamp:
            days.append(stamp[:10])
    return TradingCalendar(days, source=source)


def resolve_date(
    value: Any,
    *,
    calendar: TradingCalendar,
    today: Any = None,
    factual: bool = False,
) -> dict[str, Any]:
    """把「模型/材料给出的日期」换算成带口径的结果（Q06/Q07 的确定性闸门）。

    返回 {input, resolved, date_kind, day_kind, note}：
    - `factual=True`（输入材料或已披露公告里的日期）→ 原样采信，只补 `day_kind`；
      事实日期落在周末/节假日也如实标注，因为公告日期本就不受交易日约束；
    - 非事实日期：解析失败或落在日历覆盖期外 → `resolved=""`（`date_kind=outside_coverage`
      或 `unknown`），调用方必须改用事件锚定，**不得保留原日期**；
    - 覆盖期内的周末/节假日 → 顺延到下一个交易日并留痕（`day_kind=shifted`）。
    """
    text = str(value or "").strip()
    day = parse_date(text)
    result: dict[str, Any] = {
        "input": text[:40],
        "resolved": format_day(day),
        "date_kind": DATE_KIND_FACTUAL if factual else DATE_KIND_ESTIMATED,
        "day_kind": calendar.day_kind(day),
        "note": "",
    }
    if day is None:
        result["resolved"] = ""
        result["date_kind"] = DATE_KIND_UNKNOWN
        result["day_kind"] = DAY_KIND_UNKNOWN
        result["note"] = "未能解析出具体日期，按事件锚定处理。"
        return result
    kind = calendar.day_kind(day)
    if factual:
        result["note"] = {
            DAY_KIND_TRADING: "交易日（来自已披露事实）。",
            DAY_KIND_WEEKEND: "落在周末：为事实日期，不按交易日顺延。",
            DAY_KIND_HOLIDAY: "落在非交易日（节假日或休市）：为事实日期，不顺延。",
            DAY_KIND_OUTSIDE: "超出行情日历覆盖期：事实日期保留，但不用于推算行情窗口。",
        }[kind]
        return result
    if kind == DAY_KIND_TRADING:
        result["note"] = "行情日历内交易日。"
        return result
    if kind in (DAY_KIND_WEEKEND, DAY_KIND_HOLIDAY):
        shifted = calendar.next_trading_day(day)
        if shifted is not None:
            result["resolved"] = format_day(shifted)
            result["day_kind"] = f"{kind}→shifted"
            result["note"] = (
                f"原时点 {format_day(day)} 休市，按交易日历顺延至 {format_day(shifted)}。"
            )
            return result
        # 覆盖期内休市但后面没有交易日（数据已到尽头）：同样不能当实时阈值用。
        result["resolved"] = ""
        result["date_kind"] = DATE_KIND_OUTSIDE
        result["note"] = f"原时点 {format_day(day)} 休市，且行情已无后续交易日，改用事件锚定。"
        return result
    result["resolved"] = ""
    result["date_kind"] = DATE_KIND_OUTSIDE
    earliest, latest = calendar.coverage()
    result["note"] = (
        f"日期 {format_day(day)} 超出行情日历覆盖期（{earliest or '—'}~{latest or '—'}），"
        "无交易日历依据，改用事件锚定表述。"
    )
    reference = parse_date(today)
    if reference is not None and day > reference:
        result["note"] += "（晚于当前日期）"
    return result


def stale_note(
    *,
    as_of: Any,
    today: Any,
    calendar: TradingCalendar,
    max_trading_days: int = 5,
) -> dict[str, Any]:
    """数据新鲜度口径（Q06「数据过期场景」）：行情截至日离今天几个交易日。"""
    first, last = parse_date(as_of), parse_date(today)
    out: dict[str, Any] = {"as_of": format_day(first), "age_trading_days": None, "stale": False, "note": ""}
    if first is None:
        out["note"] = "未取得行情截至日期，任何价位都只能按历史快照引用。"
        out["stale"] = True
        return out
    if last is None or not calendar.available:
        out["note"] = f"行情截至 {out['as_of']}（无交易日历，无法判断是否过期）。"
        return out
    age = calendar.trading_days_between(first, last)
    out["age_trading_days"] = age
    if age is None:
        out["note"] = f"行情截至 {out['as_of']}，超出日历覆盖期，按历史快照引用。"
        out["stale"] = True
    elif age > max_trading_days:
        out["note"] = f"行情截至 {out['as_of']}，已 {age} 个交易日未刷新，只能引用历史快照。"
        out["stale"] = True
    else:
        out["note"] = f"行情截至 {out['as_of']}（{age} 个交易日前）。"
    return out
