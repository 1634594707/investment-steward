"""有限窗口案例复盘（youzi-radar 二期 Y3-01..04）。

## 设计边界（路线图 §6）

- **代码输入查询，不做推荐列表/排行。** 这个模块只接受一个证券代码 + 区间，
  不提供「最活跃席位」「收益率最高」之类入口。
- **有界**：区间上限、每页事件数上限、总请求预算上限都有显式常量；超限**报错**
  而不是悄悄扩展历史。
- **事件分页**：以**披露事件**（= 同一证券 × 交易日 × 席位 × 原因 × 方向 下的
  原始披露行）为单位，而不是按日期切。游标绑定 `(证券, 区间, 快照)`，换任一项
  后旧游标必须失效——否则会把 A 查询的第二页接到 B 查询上。
- **指纹上下文**：每个事件必须取得截至当日的完整五交易日输入才生成事实标签；
  查询起点最多前取 4 个交易日作上下文，但**不**把这些日子显示为范围内事件。
- **不显示「路径全部走完」或「是否了结」。** 只有已披露事实与离散期限状态。

本模块的取数与日历**全部由调用方注入**（`fetch_day`、`trading_days`），因此纯离线
可测；生产接线在 `api/app.py`。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from investment_steward_core import youzi_horizon

#: 区间上限（自然日）：超过直接拒绝，不截断——截断会让使用者以为查全了。
MAX_RANGE_DAYS = 120
#: 上下文前取上限：为事件凑齐五交易日窗口所需的前序交易日数。
MAX_CONTEXT_DAYS = 4
#: 日历取数的**自然日**前移量：必须 ≥ 一个长假（春节/国庆最长连续休市）。
#:
#: 用途是让 `fetch_calendar` 的起点落在区间之前，使 `probe.previous()` 有处可取。
#: 传交易日的 4 倍（约 2 周）不足以跨过国庆+中秋这类长休，故取 20 个自然日
#: （≥ 14 个自然日即可覆盖 4 个交易日 + 最长连续休市）。
CONTEXT_LOOKBACK_DAYS = 20
#: 每页事件数上限。
MAX_PAGE_SIZE = 50
#: 单次复盘请求允许的**可回溯**交易日上限（预算护栏）。
MAX_EVENT_DAYS = 60


class ReplayInputError(ValueError):
    """复盘请求参数不合法（代码、日期、区间、分页、游标）。"""


class ReplayBudgetExhausted(RuntimeError):
    """预算耗尽：调用方应提示使用者**缩小范围**，而不是继续扩展历史。"""


@dataclass(frozen=True)
class ReplayEvent:
    """一条披露事件：一个交易日、一个席位、一个方向、一条原始原因。"""

    event_id: str
    trading_day: str
    security_code: str
    security_name: str
    operatedept_code: str
    operatedept_name: str
    direction: str
    amount: float | None
    explanation: str
    source_report: str
    source_record_id: str
    #: 该事件的五交易日窗口内产生的跨日事实标签（`FactTag` 形式的只读投影）。
    fact_types: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    #: 六个离散期限（Y3-05）。上下文中未取全时为空元组（不生成假期限）。
    horizons: tuple[youzi_horizon.HorizonValue, ...] = ()
    #: 未生成事实时的显式原因（窗口不完整 / 取数失败）。
    fact_omitted_reason: str = ""

    @property
    def source_url(self) -> str:
        """该事件回到原始披露页的可点回链（G03）。

        上游行里没有 URL 字段，回链由（交易日, 证券代码）确定性拼出——形态与实测记录见
        `lhb_feed.provider_page_url`。模块内**延迟导入** lhb_feed，避免与取数层形成导入环。
        """
        from investment_steward_core import lhb_feed

        return lhb_feed.provider_page_url(self.trading_day, self.security_code)

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "trading_day": self.trading_day,
            "security_code": self.security_code,
            "security_name": self.security_name,
            "operatedept_code": self.operatedept_code,
            "operatedept_name": self.operatedept_name,
            "direction": self.direction,
            "amount": self.amount,
            "explanation": self.explanation,
            "source_report": self.source_report,
            "source_record_id": self.source_record_id,
            "source_url": self.source_url,
            "fact_types": list(self.fact_types),
            "fact_ids": list(self.fact_ids),
            "horizons": [item.as_dict() for item in self.horizons],
            "fact_omitted_reason": self.fact_omitted_reason,
        }


@dataclass(frozen=True)
class ReplayPage:
    """一页事件 + 绑定快照的游标 + 覆盖说明。"""

    security_code: str
    range_start: str
    range_end: str
    events: tuple[ReplayEvent, ...]
    cursor: str  # 下一页游标；无下一页时为空串
    has_more: bool
    total_events: int
    coverage: dict[str, str]
    truncated: bool
    truncated_reason: str
    fetched_at: float
    limitations: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "security_code": self.security_code,
            "range_start": self.range_start,
            "range_end": self.range_end,
            "events": [item.as_dict() for item in self.events],
            "cursor": self.cursor,
            "has_more": self.has_more,
            "total_events": self.total_events,
            "coverage": dict(self.coverage),
            "truncated": self.truncated,
            "truncated_reason": self.truncated_reason,
            "fetched_at": self.fetched_at,
            "limitations": list(self.limitations),
        }


def _valid_day(day: object) -> bool:
    if not isinstance(day, str) or len(day) != 8 or not day.isdigit():
        return False
    from datetime import date

    try:
        date(int(day[:4]), int(day[4:6]), int(day[6:8]))
    except ValueError:
        return False
    return True


def _valid_code(code: object) -> bool:
    return isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit()


def _day_ordinal(day: str) -> int:
    from datetime import date

    return date(int(day[:4]), int(day[4:6]), int(day[6:8])).toordinal()


def validate_request(
    security_code: str, start: str, end: str, *, page_size: int = MAX_PAGE_SIZE
) -> None:
    """参数校验（Y3-01）。任何越界都显式报错，不静默截断。"""
    if not _valid_code(security_code):
        raise ReplayInputError("证券代码必须为 6 位数字")
    if not _valid_day(start) or not _valid_day(end):
        raise ReplayInputError("起止日期必须为有效 YYYYMMDD")
    if start > end:
        raise ReplayInputError("起始日期不得晚于结束日期")
    span = _day_ordinal(end) - _day_ordinal(start) + 1
    if span > MAX_RANGE_DAYS:
        raise ReplayInputError(f"区间上限为 {MAX_RANGE_DAYS} 个自然日（收到 {span}）")
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
        raise ReplayInputError("page_size 必须为正整数")
    if page_size > MAX_PAGE_SIZE:
        raise ReplayInputError(f"每页上限为 {MAX_PAGE_SIZE} 条（收到 {page_size}）")


def make_cursor(security_code: str, start: str, end: str, offset: int, snapshot: str) -> str:
    """游标：绑定 `(证券, 区间, 快照)` + 偏移，形如 `<摘要>.<偏移>`。

    ★ 快照参与摘要——跨快照复用游标是**不允许**的（不同抓取时刻可能新增/修订
    披露行，按旧偏移取新数据会漏行或重行）。调用方应把上游 `fetched_at` 或
    区间内覆盖状态指纹作为 `snapshot`。

    偏移明文放在尾部而不是靠枚举反解：反解需要 O(上限) 次哈希，既慢又容易在
    上限变化后静默失效；明文偏移 + 摘要校验既廉价又同样不可伪造（摘要不匹配即拒）。
    """
    raw = json.dumps([security_code, start, end, offset, snapshot], separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{digest}.{offset}"


def read_cursor(cursor: str, security_code: str, start: str, end: str, snapshot: str) -> int:
    """解析游标为偏移；游标与当前查询不匹配（含空游标）→ 抛错。

    用**恒定时间**校验：重算摘要比对，而不是枚举所有偏移。
    """
    if not cursor:
        return 0
    digest, _, tail = cursor.partition(".")
    if not digest or not tail:
        raise ReplayInputError("游标格式不合法")
    try:
        offset = int(tail)
    except ValueError as error:
        raise ReplayInputError("游标偏移不合法") from error
    if offset < 0:
        raise ReplayInputError("游标偏移必须为非负整数")
    expected = make_cursor(security_code, start, end, offset, snapshot)
    if cursor != expected:
        raise ReplayInputError("游标与当前查询不匹配（证券/区间/快照已变更或游标失效）")
    return offset


def make_event_id(parts: tuple[object, ...]) -> str:
    """稳定事件 ID：同一原始披露行在任何分页顺序下得到同一个 ID。"""
    raw = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# —— Y3-02/03/04：装配（取数与日历由调用方注入，纯离线可测）——


@dataclass(frozen=True)
class ReplayInputs:
    """调用方注入的取数与日历结果。

    - `day_reports`: `交易日 → {报告名: (rows, coverage, reason)}`。**只包含实际
      取过的日子**；缺失的日子按 `unknown` 处理（不默认成空）。
    - `trading_days`: 显式可信交易日序列（升序）。空 → 无日历，期限一律 `unknown`。
    - `calendar_available`: 可信日历是否可用。
    - `context_start`: 为凑齐首个事件的五交易日窗口而前取的首个交易日（可空）。
    """

    day_reports: dict[str, dict[str, tuple[list[dict[str, object]], str, str]]] = field(
        default_factory=dict
    )
    trading_days: tuple[str, ...] = ()
    calendar_available: bool = False
    context_start: str = ""
    budget_exhausted_days: tuple[str, ...] = ()
    fetched_at: float = 0.0
    #: 期限判定的「此刻」（YYYYMMDD）。空 → 用 `trading_days` 末位。
    #: ★ 必须显式传：复盘是历史查询，若不给，期限会把日历里**未来**的交易日
    #: 也算作「已发生」，把 `not_due` 误判成 `missing`（反之亦然）。
    as_of_trading_day: str = ""


def _day_state(reports: dict[str, tuple[list[dict[str, object]], str, str]]) -> str:
    """把一日内各报告的覆盖状态归并成整日状态（严重性优先）。

    ★ `unknown` 优先于 `fetch_failed`：`fetch_failed` 是「确定这次没取到」，
    `unknown` 是「无法判断取没取到」。把后者折进前者等于把不确定伪装成确定，
    与 `api/app.py::_worst_coverage` 同一口径。
    """
    states = [state for _, state, _ in reports.values()]
    if all(state == "complete" for state in states):
        return "complete"
    if "unknown" in states:
        return "unknown"
    if "fetch_failed" in states:
        return "fetch_failed"
    if "truncated" in states:
        return "truncated"
    return "not_published"


def _row_text(row: dict[str, object], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _row_amount(row: dict[str, object], direction: str) -> float | None:
    key = "BUY" if direction == "buy" else "SELL"
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def collect_events(
    inputs: ReplayInputs,
    security_code: str,
    start: str,
    end: str,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """按交易日升序收集区间内的原始披露事件（未分页、未附事实标签）。

    返回 `(events_raw, coverage)`；`events_raw` 每项形如
    `{"trading_day", "report", "direction", "row"}`，保持来源报告与原始行，
    使事件可以被逐条定位（Y3-03 要求「以披露事件而非简单日期组织」）。
    """
    from investment_steward_core import lhb_feed

    collected: list[dict[str, object]] = []
    coverage: dict[str, str] = {}
    for day in inputs.trading_days:
        reports = inputs.day_reports.get(day)
        if day < start or day > end:
            # ★ 区间外的日子**不收集事件**，但必须照常写 `coverage`：Y3-04 要求
            # 查询起点向前多取最多 4 个交易日作上下文，而 `build_page` 判定
            # 「上下文是否完整」看的正是 `coverage`。若在这里 `continue`，前取的
            # 上下文日永远取不到覆盖状态，所有首日事件都会被误判成
            # `context_not_complete`——事实标签在大半个区间上静默消失。
            if reports is not None:
                coverage[day] = _day_state(reports)
            continue
        if reports is None:
            coverage[day] = "unknown"
            continue
        coverage[day] = _day_state(reports)
        for report, direction in (
            (lhb_feed.REPORT_SEAT_BUY, lhb_feed.DIRECTION_BUY),
            (lhb_feed.REPORT_SEAT_SELL, lhb_feed.DIRECTION_SELL),
        ):
            rows = reports.get(report, ([], "", ""))[0]
            for row in rows:
                if _row_text(row, "SECURITY_CODE") != security_code:
                    continue
                code = _row_text(row, "OPERATEDEPT_CODE")
                if not code:
                    continue
                collected.append(
                    {"trading_day": day, "report": report, "direction": direction, "row": row}
                )
    collected.sort(
        key=lambda item: (
            str(item["trading_day"]),
            str(item["report"]),
            _row_text(item["row"], "OPERATEDEPT_CODE"),  # type: ignore[arg-type]
            str(item["direction"]),
        )
    )
    return collected, coverage


def _event_key(item: dict[str, object]) -> tuple[object, ...]:
    """事件身份 = 交易日 + 报告 + 席位 + 方向 + 原因 + 金额 + 来源记录 ID。

    刻意**不用** `TRADE_ID`：Y0-04 实测 730 行只有 73 个唯一值，不能当主键。
    同一原始披露行在任意分页顺序下必须得到同一个键。
    """
    row = item["row"]
    assert isinstance(row, dict)
    return (
        item["trading_day"],
        item["report"],
        _row_text(row, "OPERATEDEPT_CODE"),
        item["direction"],
        _row_text(row, "EXPLANATION"),
        _row_amount(row, str(item["direction"])),
        _row_text(row, "TRADE_ID"),
    )


def _index_billboard_rows(
    inputs: ReplayInputs, security_code: str
) -> dict[str, dict[str, object]]:
    """`交易日 → 该证券的榜单行`，供期限解析使用。

    ★ 为什么必须单独索引：`D{N}_CLOSE_ADJCHRATE`（六个期限字段）只在
    `RPT_DAILYBILLBOARD_DETAILS` 上；席位买卖明细（BUY/SELL）里没有任何一个。
    席位行能给出「谁在买/卖」，但给不出「披露后 N 日涨跌」——后者只能来自榜单行。
    同一天同一只证券在榜单里可能有多行（多个上榜原因），取任一行即可：六个期限
    字段是**证券级**的，与上榜原因无关。
    """
    from investment_steward_core import lhb_feed

    index: dict[str, dict[str, object]] = {}
    for day, reports in inputs.day_reports.items():
        rows = reports.get(lhb_feed.REPORT_DAILY_BILLBOARD, ([], "", ""))[0]
        for row in rows:
            if _row_text(row, "SECURITY_CODE") != security_code:
                continue
            index.setdefault(day, row)
    return index


def _resolve_horizons_cached(
    cache: youzi_horizon.HorizonCache | None,
    disclosure_day: str,
    row: object,
    trading_days: object,
    *,
    calendar_available: bool,
    as_of_trading_day: str,
    fetched_at: float | None,
) -> tuple[youzi_horizon.HorizonValue, ...]:
    """按 Y3-06 取期限值：命中缓存就直接返回，否则算完写回。

    ★ 缓存键含 `as_of` 与**输入指纹**（`HorizonCache.fingerprint`）：`as_of` 决定
    哪些交易日算「已发生」；六个期限字段与交易日序列决定具体结论。少了任何一项，
    上游补数或换 `as_of` 之后就会把旧结论当新结论返回。

    ★★ 2026-09-18 修：`row` 必须是**榜单行**（`RPT_DAILYBILLBOARD_DETAILS`），
    不是席位行。`D{N}_CLOSE_ADJCHRATE` 六个字段只存在于榜单报告上；席位买卖明细
    报告里**一个都没有**。旧实现把席位行传进来，于是每个期限都被判成 `missing`
    ——「上游没给」和「我拿错了行」在输出上长得一模一样，跨日事实的期限列全空。
    调用方负责传入正确的行（见 `build_page` 的 `billboard_row`）。
    """
    if cache is None:
        return youzi_horizon.resolve_horizons(
            disclosure_day,
            row,
            trading_days,
            calendar_available=calendar_available,
            as_of_trading_day=as_of_trading_day,
            fetched_at=fetched_at,
        )
    code = _row_text(row, "SECURITY_CODE")
    fingerprint = youzi_horizon.HorizonCache.fingerprint(row, trading_days)
    cached = cache.get(disclosure_day, code, as_of_trading_day, fingerprint)
    if cached is not None:
        return cached
    values = youzi_horizon.resolve_horizons(
        disclosure_day,
        row,
        trading_days,
        calendar_available=calendar_available,
        as_of_trading_day=as_of_trading_day,
        fetched_at=fetched_at,
    )
    cache.put(disclosure_day, code, values, as_of_trading_day, fingerprint)
    return values


def build_page(
    inputs: ReplayInputs,
    security_code: str,
    start: str,
    end: str,
    *,
    page_size: int = MAX_PAGE_SIZE,
    cursor: str = "",
    annotate_event=None,
    horizon_cache: youzi_horizon.HorizonCache | None = None,
) -> ReplayPage:
    """装配一页事件（Y3-03）。

    `annotate_event(event_key, trading_day, context_days) -> dict` 由调用方注入：
    返回 `{"fact_types": [...], "fact_ids": [...], "omitted": ""}`。为 `None`
    时事件不带事实标签（复盘页仍可展示原始披露）。

    `horizon_cache`（Y3-06）可选：传入 `HorizonCache` 时，**已披露**期限走 60s
    正缓存、**未到期/缺失**只走 5s 短冷却——一次取数抖动不能把某个期限永久钉成
    缺失，而同一份快照内的已定值也不必反复重算。为 `None` 时退回逐事件直算
    （行为与加缓存前完全一致，便于测试与首次取值）。
    """
    validate_request(security_code, start, end, page_size=page_size)

    all_raw, coverage = collect_events(inputs, security_code, start, end)
    # 去重：同键（可含多原因）保留为**独立事件**，只去掉完全相同的重复抓取行。
    seen: dict[tuple[object, ...], dict[str, object]] = {}
    for item in all_raw:
        seen.setdefault(_event_key(item), item)
    ordered = list(seen.values())

    snapshot = json.dumps(sorted(coverage.items()), separators=(",", ":")) + f"|{inputs.fetched_at}"
    offset = read_cursor(cursor, security_code, start, end, snapshot)
    if offset > len(ordered):
        raise ReplayInputError("游标偏移超出当前结果集（快照已变化）")

    page_raw = ordered[offset : offset + page_size]
    has_more = offset + page_size < len(ordered)
    next_cursor = (
        make_cursor(security_code, start, end, offset + page_size, snapshot) if has_more else ""
    )

    days = list(inputs.trading_days)
    #: ★ 榜单行索引：`D{N}_CLOSE_ADJCHRATE` 只存在于 `RPT_DAILYBILLBOARD_DETAILS`，
    #: 席位买卖明细里没有这些字段。期限必须用**榜单行**解析，否则恒 `missing`。
    billboard_rows = _index_billboard_rows(inputs, security_code)
    events: list[ReplayEvent] = []
    for item in page_raw:
        row = item["row"]
        assert isinstance(row, dict)
        day = str(item["trading_day"])
        direction = str(item["direction"])
        report = str(item["report"])
        # 五交易日上下文：以**事件所在日**为窗口末尾（不是以显示页边界！）。
        context: list[str] = []
        if day in days:
            index = days.index(day)
            context = days[max(0, index - 4) : index + 1]
        facts: dict[str, object] = {"fact_types": [], "fact_ids": [], "omitted": ""}
        if annotate_event is not None:
            if len(context) < 5:
                facts["omitted"] = "insufficient_context_days"
            elif any(coverage.get(item_day) != "complete" for item_day in context):
                facts["omitted"] = "context_not_complete"
            else:
                facts = annotate_event(_event_key(item), day, tuple(context))

        horizons = _resolve_horizons_cached(
            horizon_cache,
            day,
            billboard_rows.get(day, {}),
            inputs.trading_days,
            calendar_available=inputs.calendar_available,
            as_of_trading_day=inputs.as_of_trading_day,
            fetched_at=inputs.fetched_at or None,
        )
        events.append(
            ReplayEvent(
                event_id=make_event_id(_event_key(item)),
                trading_day=day,
                security_code=security_code,
                security_name=_row_text(row, "SECURITY_NAME_ABBR"),
                operatedept_code=_row_text(row, "OPERATEDEPT_CODE"),
                operatedept_name=_row_text(row, "OPERATEDEPT_NAME"),
                direction=direction,
                amount=_row_amount(row, direction),
                explanation=_row_text(row, "EXPLANATION"),
                source_report=report,
                source_record_id=make_event_id(("source", *_event_key(item))),
                fact_types=tuple(str(x) for x in facts.get("fact_types", [])),  # type: ignore[union-attr]
                fact_ids=tuple(str(x) for x in facts.get("fact_ids", [])),  # type: ignore[union-attr]
                horizons=horizons,
                fact_omitted_reason=str(facts.get("omitted") or ""),
            )
        )

    exhausted = sorted(inputs.budget_exhausted_days)
    return ReplayPage(
        security_code=security_code,
        range_start=start,
        range_end=end,
        events=tuple(events),
        cursor=next_cursor,
        has_more=has_more,
        total_events=len(ordered),
        coverage=coverage,
        truncated=bool(exhausted),
        truncated_reason=(
            f"预算在 {len(exhausted)} 个交易日上耗尽（{exhausted[0]}..{exhausted[-1]}）；"
            "已取得的部分仍然可见，请缩小日期范围后重试。"
            if exhausted
            else ""
        ),
        fetched_at=inputs.fetched_at,
        limitations=(
            (
                "区间内只包含**已取到**的交易日；未取到的日子在 coverage 中为 unknown，"
                "不代表当日没有披露。"
            ),
            (
                "事实标签的窗口以事件所在交易日为末尾，与显示页边界无关；"
                "上下文不足时不生成标签。"
            ),
            "只有已披露事实与期限状态，不含持仓、清仓或收益结论。",
        ),
    )
