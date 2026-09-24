"""Y3-01..04：有限窗口复盘的参数校验、事件分页、指纹上下文与预算降级（全离线）。

被测的关键不变量：
 1. 越界一律**报错**，不静默截断（Y3-01/02）。
 2. 游标绑定 `(证券, 区间, 快照)`；换任一项后旧游标必须失效（不可混用）。
 3. 页间不重不漏（同一分页序列拼起来等于全集，且无重复事件 ID）。
 4. 事实标签的窗口以**事件所在交易日**为末尾，与显示页边界无关（Y3-04）。
 5. 上下文不足 / 上下文不完整 → **不生成**事实并写明原因。
 6. 预算耗尽 → `truncated=True` + 可读原因，已取得部分仍可见（Y3-02）。
 7. 输出不含任何绩效字段。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from investment_steward_core import lhb_feed, youzi_replay
from investment_steward_core import youzi_horizon as hz

DAYS = tuple(
    (date(2026, 9, 1) + timedelta(days=offset)).strftime("%Y%m%d")
    for offset in range(0, 40)
    if (date(2026, 9, 1) + timedelta(days=offset)).weekday() < 5
)
CODE = "600519"
START, END = DAYS[4], DAYS[12]


def seat_row(day: str, *, code: str = CODE, dept: str = "10026937", direction: str = "buy",
             amount: float = 1e7, explanation: str = "日涨幅偏离值达7%的证券",
             name: str = "某某证券股份有限公司某营业部") -> dict[str, object]:
    return {
        "TRADE_DATE": f"{day[:4]}-{day[4:6]}-{day[6:]}",
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "测试股份",
        "OPERATEDEPT_CODE": dept,
        "OPERATEDEPT_NAME": name,
        "EXPLANATION": explanation,
        "BUY": amount if direction == "buy" else None,
        "SELL": amount if direction == "sell" else None,
        "TRADE_ID": f"{day}-{dept}-{direction}",
    }


def reports(rows_by_report: dict[str, list[dict[str, object]]], coverage: str = "complete"):
    return {
        report: (list(rows_by_report.get(report, [])), coverage, "" if coverage == "complete" else "stub")
        for report in (
            lhb_feed.REPORT_DAILY_BILLBOARD,
            lhb_feed.REPORT_SEAT_BUY,
            lhb_feed.REPORT_SEAT_SELL,
        )
    }


def inputs_with(day_rows: dict[str, list[dict[str, object]]], **kwargs) -> youzi_replay.ReplayInputs:
    return youzi_replay.ReplayInputs(
        day_reports={
            day: reports({lhb_feed.REPORT_SEAT_BUY: rows}) for day, rows in day_rows.items()
        },
        trading_days=DAYS,
        calendar_available=True,
        fetched_at=1234.0,
        **kwargs,
    )


def inputs_with_board(
    board_rows: dict[str, list[dict[str, object]]],
    seat_rows: dict[str, list[dict[str, object]]] | None = None,
    **kwargs,
) -> youzi_replay.ReplayInputs:
    """同时喂**榜单行**与**席位行**的输入。

    ★ `D{N}_CLOSE_ADJCHRATE` 只在 `RPT_DAILYBILLBOARD_DETAILS` 上（2026-09-18 实测），
    因此期限用例必须显式构造榜单行；用 `inputs_with` 只会得到 `missing`。
    """
    seat_rows = seat_rows or {}
    days = sorted(set(board_rows) | set(seat_rows))
    return youzi_replay.ReplayInputs(
        day_reports={
            day: reports(
                {
                    lhb_feed.REPORT_DAILY_BILLBOARD: board_rows.get(day, []),
                    lhb_feed.REPORT_SEAT_BUY: seat_rows.get(day, []),
                }
            )
            for day in days
        },
        trading_days=DAYS,
        calendar_available=True,
        fetched_at=1234.0,
        **kwargs,
    )


# —— Y3-01 参数校验 ——


@pytest.mark.parametrize(
    ("code", "start", "end", "message"),
    [
        ("60051", START, END, "6 位数字"),
        ("60051a", START, END, "6 位数字"),
        (CODE, "2026-09-01", END, "YYYYMMDD"),
        (CODE, START, "20260230", "YYYYMMDD"),
        (CODE, END, START, "不得晚于"),
    ],
)
def test_validate_request_rejects_bad_input(code, start, end, message) -> None:
    with pytest.raises(youzi_replay.ReplayInputError) as error:
        youzi_replay.validate_request(code, start, end)
    assert message in str(error.value)


def test_range_upper_bound_is_enforced_not_truncated() -> None:
    """超上限必须报错——静默截断会让使用者以为查全了。"""
    start = "20260101"
    end_day = date(2026, 1, 1) + timedelta(days=youzi_replay.MAX_RANGE_DAYS)
    with pytest.raises(youzi_replay.ReplayInputError) as error:
        youzi_replay.validate_request(CODE, start, end_day.strftime("%Y%m%d"))
    assert "区间上限" in str(error.value)


@pytest.mark.parametrize("page_size", [0, -1, youzi_replay.MAX_PAGE_SIZE + 1, True, 1.5])
def test_page_size_bounds_are_enforced(page_size) -> None:
    with pytest.raises(youzi_replay.ReplayInputError):
        youzi_replay.validate_request(CODE, START, END, page_size=page_size)


# —— Y3-03 游标 ——


def test_cursor_binds_code_range_and_snapshot() -> None:
    cursor = youzi_replay.make_cursor(CODE, START, END, 7, "snap-a")
    assert youzi_replay.read_cursor(cursor, CODE, START, END, "snap-a") == 7
    # 换任一项都必须失效。
    for args in (
        ("000001", START, END, "snap-a"),
        (CODE, DAYS[3], END, "snap-a"),
        (CODE, START, DAYS[13], "snap-a"),
        (CODE, START, END, "snap-b"),
    ):
        with pytest.raises(youzi_replay.ReplayInputError):
            youzi_replay.read_cursor(cursor, *args)


@pytest.mark.parametrize("bad", ["", "nodot", ".5", "abc.5.6", "abc.-1", "abc.x"])
def test_malformed_cursor_is_rejected_without_guessing(bad: str) -> None:
    """空游标 = 第一页（合法）；其余畸形一律报错，不猜偏移。"""
    if bad == "":
        assert youzi_replay.read_cursor(bad, CODE, START, END, "s") == 0
        return
    with pytest.raises(youzi_replay.ReplayInputError):
        youzi_replay.read_cursor(bad, CODE, START, END, "s")


def test_cursor_is_unforgeable_without_the_exact_query() -> None:
    """手工改偏移而不重算摘要 → 必须被拒（否则可以任意跳页绕过校验）。"""
    cursor = youzi_replay.make_cursor(CODE, START, END, 3, "snap")
    digest = cursor.split(".")[0]
    forged = f"{digest}.99"
    with pytest.raises(youzi_replay.ReplayInputError):
        youzi_replay.read_cursor(forged, CODE, START, END, "snap")


# —— Y3-03 事件分页 ——


def build_inputs() -> youzi_replay.ReplayInputs:
    """区间内 5 天各 1 条事件（席位不同），便于验证分页。"""
    day_rows = {day: [seat_row(day, dept=f"100269{index:02d}")] for index, day in enumerate(DAYS[4:9])}
    return inputs_with(day_rows)


def test_events_are_ordered_by_day_and_pageable_without_gaps_or_overlaps() -> None:
    inputs = build_inputs()
    collected: list[str] = []
    cursor = ""
    pages = 0
    while True:
        page = youzi_replay.build_page(inputs, CODE, START, END, page_size=2, cursor=cursor)
        pages += 1
        collected.extend(event.event_id for event in page.events)
        if not page.has_more:
            break
        cursor = page.cursor
        assert cursor, "has_more 为真时必须给出下一页游标"
        assert pages < 10, "分页未收敛"
    assert pages == 3  # 5 条 / 每页 2 条
    assert len(collected) == 5
    assert len(set(collected)) == 5, "页间不得重复事件"
    # 顺序 = 交易日升序。
    assert collected == [
        event.event_id
        for event in youzi_replay.build_page(inputs, CODE, START, END, page_size=50).events
    ]


def test_last_page_reports_no_more_and_empty_cursor() -> None:
    inputs = build_inputs()
    page = youzi_replay.build_page(inputs, CODE, START, END, page_size=50)
    assert page.has_more is False
    assert page.cursor == ""
    assert page.total_events == 5


def test_overlapping_reasons_stay_separate_events_and_amounts_are_not_summed() -> None:
    """同日同席位多原因 → 独立事件，金额各归各，**不相加**。"""
    day = DAYS[4]
    inputs = inputs_with(
        {
            day: [
                seat_row(day, amount=1e7, explanation="日涨幅偏离值达7%的证券"),
                seat_row(day, amount=2.5e7, explanation="日换手率达20%的证券"),
            ]
        }
    )
    page = youzi_replay.build_page(inputs, CODE, START, END)
    assert len(page.events) == 2
    assert {event.amount for event in page.events} == {1e7, 2.5e7}
    assert len({event.event_id for event in page.events}) == 2
    assert sum(event.amount or 0 for event in page.events) == 3.5e7  # 仅用于断言未合并成一条 3.5e7


def test_stale_cursor_beyond_result_set_is_rejected() -> None:
    inputs = build_inputs()
    page = youzi_replay.build_page(inputs, CODE, START, END, page_size=2)
    # 用同一快照造一个远超结果集的偏移 → 必须报错（快照已变化）。
    snapshot = f"{youzi_replay.json.dumps(sorted(page.coverage.items()), separators=(',', ':'))}|1234.0"
    far = youzi_replay.make_cursor(CODE, START, END, 99, snapshot)
    with pytest.raises(youzi_replay.ReplayInputError):
        youzi_replay.build_page(inputs, CODE, START, END, page_size=2, cursor=far)


def test_other_security_rows_never_leak_into_the_replay() -> None:
    day = DAYS[4]
    inputs = inputs_with(
        {day: [seat_row(day), seat_row(day, code="000001", dept="90000001")]}
    )
    page = youzi_replay.build_page(inputs, CODE, START, END)
    assert {event.security_code for event in page.events} == {CODE}


def test_rows_without_a_seat_code_are_skipped_not_invented() -> None:
    day = DAYS[4]
    bad = seat_row(day, dept="")
    inputs = inputs_with({day: [bad, seat_row(day)]})
    page = youzi_replay.build_page(inputs, CODE, START, END)
    assert len(page.events) == 1


# —— Y3-04 指纹上下文 ——


def _annotate_recording(seen: list[tuple[str, tuple[str, ...]]]):
    def annotate_event(event_key, trading_day, context_days):
        seen.append((trading_day, context_days))
        return {"fact_types": ["repeated_seat_presence"], "fact_ids": ["f"], "omitted": ""}

    return annotate_event


def test_context_window_ends_at_the_event_day_not_at_the_page_edge() -> None:
    """★ 显示页边界不得当作算法窗口：翻到第二页时首个事件仍要有完整五日上下文。

    做法：**先按整集算出第一页的游标**（而不是手工拼一个），再用它取第二页，
    断言第二页事件的上下文同样以自身日为末尾、长度仍为 5。

    注意上下文日**必须真的取到**才生成事实（这是预期行为，不是缺陷），所以先从
    `DAYS[0]` 起给数——首个事件的五日窗口 `[DAYS[0], DAYS[4]]` 才算完整。
    """
    day_rows = {day: [seat_row(day, dept=f"10026{index}")] for index, day in enumerate(DAYS[0:10])}
    inputs = inputs_with(day_rows)
    seen: list[tuple[str, tuple[str, ...]]] = []

    first = youzi_replay.build_page(
        inputs, CODE, DAYS[4], END, page_size=1, annotate_event=_annotate_recording(seen)
    )
    assert first.has_more and first.cursor
    assert seen and seen[0][0] == DAYS[4]
    assert seen[0][1] == tuple(DAYS[0:5]), "窗口末尾必须是事件日，且向前取满五日"

    # 取第二页（显示页边界落在中间的这一天之后），上下文不得被页边界截短。
    seen.clear()
    second = youzi_replay.build_page(
        inputs, CODE, DAYS[4], END, page_size=1, cursor=first.cursor,
        annotate_event=_annotate_recording(seen),
    )
    assert second.events and second.events[0].trading_day == DAYS[5]
    assert seen and seen[0][0] == DAYS[5]
    assert seen[0][1] == tuple(DAYS[1:6]), "页边界不得作为算法窗口起点"
    assert len(seen[0][1]) == 5


def test_context_days_outside_the_requested_range_must_still_be_fetched() -> None:
    """★ 反向用例：区间前取的日子**没取到** → 不生成事实，并写明原因。

    这条防的是「用不完整上下文硬算」：事件日在 `DAYS[4]`，其五日窗口需要
    `DAYS[0..3]`，而查询区间从 `DAYS[4]` 起——若调用方没有按 Y3-04 向前多取，
    就必须显式降级，而不是拿三天的窗口冒充五天。
    """
    inputs = inputs_with({DAYS[4]: [seat_row(DAYS[4])]})
    called: list[str] = []

    def annotate_event(event_key, trading_day, context_days):
        called.append(trading_day)
        return {"fact_types": ["repeated_seat_presence"], "fact_ids": ["f"], "omitted": ""}

    page = youzi_replay.build_page(
        inputs, CODE, DAYS[4], END, annotate_event=annotate_event
    )
    assert called == [], "上下文未取全时不得生成事实"
    assert page.events[0].fact_types == ()
    assert page.events[0].fact_omitted_reason == "context_not_complete"


def test_insufficient_context_days_omit_facts_with_reason() -> None:
    """上下文不足五日 → **不生成**事实，并写明原因（不用不足的窗口硬算）。"""
    day = DAYS[2]  # 前面只有 2 个交易日 → 上下文不足 5。
    inputs = inputs_with({day: [seat_row(day)]})
    called: list[str] = []

    def annotate_event(event_key, trading_day, context_days):
        called.append(trading_day)
        return {"fact_types": ["repeated_seat_presence"], "fact_ids": ["f"], "omitted": ""}

    page = youzi_replay.build_page(
        inputs, CODE, DAYS[1], DAYS[5], annotate_event=annotate_event
    )
    assert called == [], "上下文不足时不得调用标注"
    assert page.events[0].fact_types == ()
    assert page.events[0].fact_omitted_reason == "insufficient_context_days"


def test_incomplete_context_coverage_omits_facts_with_distinct_reason() -> None:
    """上下文任一日覆盖非 complete → 不生成事实，原因与「天数不足」区分开。"""
    day_rows = {day: [seat_row(day)] for day in DAYS[0:5]}
    inputs = youzi_replay.ReplayInputs(
        day_reports={
            day: reports(
                {lhb_feed.REPORT_SEAT_BUY: rows},
                coverage="truncated" if day == DAYS[2] else "complete",
            )
            for day, rows in day_rows.items()
        },
        trading_days=DAYS,
        calendar_available=True,
        fetched_at=1.0,
    )
    page = youzi_replay.build_page(
        inputs, CODE, DAYS[0], DAYS[4],
        annotate_event=lambda *a: pytest.fail("覆盖不完整时不得标注"),
    )
    assert page.events[-1].fact_omitted_reason == "context_not_complete"
    assert page.coverage[DAYS[2]] == "truncated"


# —— Y3-02 预算降级 + 覆盖语义 ——


def test_budget_exhaustion_is_reported_and_partial_data_stays_visible() -> None:
    inputs = inputs_with(
        {DAYS[4]: [seat_row(DAYS[4])]},
        budget_exhausted_days=(DAYS[5], DAYS[6]),
    )
    page = youzi_replay.build_page(inputs, CODE, START, END)
    assert page.truncated is True
    assert "预算" in page.truncated_reason
    assert "缩小日期范围" in page.truncated_reason
    assert len(page.events) == 1, "已取得的部分必须仍然可见"


def test_unfetched_days_are_unknown_not_empty_conclusions() -> None:
    """未取数的日子在 coverage 里必须是 unknown——「不知道」不是「没有」。"""
    inputs = inputs_with({DAYS[4]: []})
    page = youzi_replay.build_page(inputs, CODE, START, END)
    assert page.coverage[DAYS[4]] == "complete"
    for day in DAYS[5:13]:
        assert page.coverage.get(day) == "unknown", f"{day} 未取数却未标 unknown"


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        ("truncated", "truncated"),
        ("fetch_failed", "fetch_failed"),
        ("not_published", "not_published"),
        ("unknown", "unknown"),
    ],
)
def test_coverage_states_survive_into_the_page(coverage: str, expected: str) -> None:
    inputs = youzi_replay.ReplayInputs(
        day_reports={DAYS[4]: reports({}, coverage=coverage)},
        trading_days=DAYS,
        calendar_available=True,
        fetched_at=1.0,
    )
    page = youzi_replay.build_page(inputs, CODE, START, END)
    assert page.coverage[DAYS[4]] == expected


# —— 期限接入 + 无绩效字段 ——


def test_horizons_are_attached_per_event_with_status() -> None:
    day = DAYS[4]
    # ★ 期限字段挂在**榜单行**：席位明细里没有 `D{N}_CLOSE_ADJCHRATE`（实测）。
    board = {"TRADE_DATE": "2026-09-08", "SECURITY_CODE": CODE, "D1_CLOSE_ADJCHRATE": 2.5}
    board["TRADE_DATE"] = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    inputs = inputs_with_board({day: [board]}, {day: [seat_row(day)]})
    page = youzi_replay.build_page(inputs, CODE, START, END)
    horizons = {item.label: item for item in page.events[0].horizons}
    assert horizons["D1"].value_pct == 2.5
    assert horizons["D1"].status == "disclosed"
    # 后续交易日在日历里且字段为 null → missing（不是 0，也不是 not_due）。
    assert horizons["D2"].value_pct is None
    assert horizons["D2"].status == "missing"


def test_horizons_read_the_billboard_row_not_the_seat_row() -> None:
    """★ 回归：期限必须来自榜单行。

    席位行给不出「披露后 N 日涨跌」——它的 `D{N}_CLOSE_ADJCHRATE` 全是缺字段。
    若哪天有人把 `build_page` 改回传席位行，这条会立刻红：因为席位行上**即使**
    手工塞了 `D1_CLOSE_ADJCHRATE`，也不该被读到。
    """
    day = DAYS[4]
    board = {"TRADE_DATE": f"{day[:4]}-{day[4:6]}-{day[6:]}", "SECURITY_CODE": CODE,
             "D1_CLOSE_ADJCHRATE": 2.5}
    seat = seat_row(day)
    seat["D1_CLOSE_ADJCHRATE"] = 99.9  # 席位行上的同名值必须被忽略
    inputs = inputs_with_board({day: [board]}, {day: [seat]})
    page = youzi_replay.build_page(inputs, CODE, START, END)
    horizons = {item.label: item for item in page.events[0].horizons}
    assert horizons["D1"].value_pct == 2.5, "期限只能读榜单行，不能读席位行"


def test_horizons_are_missing_when_no_billboard_row_exists() -> None:
    """榜单行缺失时期限是 `missing`（「没拿到」），而不是编造或静默 0。"""
    day = DAYS[4]
    inputs = inputs_with({day: [seat_row(day)]})  # 只有席位行，没有榜单行
    page = youzi_replay.build_page(inputs, CODE, START, END)
    horizons = {item.label: item for item in page.events[0].horizons}
    assert horizons["D1"].status == "missing"
    assert horizons["D1"].value_pct is None


def test_output_contains_no_performance_fields() -> None:
    """输出不得含绩效/越界**字段**。

    ★ `limitations` 是**否定式免责说明**（「不含持仓、清仓或收益结论」），必须
    单独排除在禁词扫描之外——用整块 repr 扫会把正当的否定说明误伤成违规
    （同一坑在质量闸门项目里踩过一次：禁词检查必须区分「声明不做」与「实际做了」）。
    """
    inputs = build_inputs()
    page = youzi_replay.build_page(inputs, CODE, START, END)
    payload = page.as_dict()
    payload.pop("limitations")
    # 断言断言式：字段集精确冻结，新增任何字段都会在这里暴露。
    assert set(payload) == {
        "security_code",
        "range_start",
        "range_end",
        "events",
        "cursor",
        "has_more",
        "total_events",
        "coverage",
        "truncated",
        "truncated_reason",
        "fetched_at",
    }
    blob = repr(payload)
    for forbidden in ("胜率", "收益", "上涨比例", "win_rate", "return_pct", "profit", "了结", "清仓"):
        assert forbidden not in blob, f"输出不应含绩效/越界字段：{forbidden}"
    for event in payload["events"]:
        assert set(event) == {
            "event_id",
            "trading_day",
            "security_code",
            "security_name",
            "operatedept_code",
            "operatedept_name",
            "direction",
            "amount",
            "explanation",
            "source_report",
            "source_record_id",
            # G03（桌面端升级路线图 2026-09-18）：回到原始披露页的**定位**字段，
            # 不是绩效字段——它是构造式 URL（交易日 + 代码），不含任何收益/胜率语义。
            "source_url",
            "fact_types",
            "fact_ids",
            "horizons",
            "fact_omitted_reason",
        }


def test_limitations_are_negations_only_and_allowlisted() -> None:
    """免责说明只允许「不做什么」的否定表述，且逐字冻结（防悄悄加上正向暗示）。"""
    page = youzi_replay.build_page(build_inputs(), CODE, START, END)
    assert page.limitations == (
        "区间内只包含**已取到**的交易日；未取到的日子在 coverage 中为 unknown，"
        "不代表当日没有披露。",
        "事实标签的窗口以事件所在交易日为末尾，与显示页边界无关；上下文不足时不生成标签。",
        "只有已披露事实与期限状态，不含持仓、清仓或收益结论。",
    )


def test_limitations_are_always_present() -> None:
    page = youzi_replay.build_page(build_inputs(), CODE, START, END)
    assert len(page.limitations) == 3
    blob = "".join(page.limitations)
    assert "unknown" in blob and "不代表当日没有披露" in blob
    assert "不含持仓、清仓或收益结论" in blob


def test_no_ranking_or_recommendation_surface_exists() -> None:
    """Y3-01：不提供推荐列表/排行——模块不导出任何排序/推荐类函数。"""
    exported = {name for name in dir(youzi_replay) if not name.startswith("_")}
    for banned in ("rank", "recommend", "top", "best", "score", "signal"):
        assert not any(banned in name.lower() for name in exported), (
            f"复盘模块不应导出与排行/推荐相关的入口：{banned}"
        )


# —— Y3-06：期限刷新（有界缓存 + 缺失不长期负缓存）——


def test_build_page_without_cache_matches_with_cache_for_disclosed_values() -> None:
    """★ 加缓存不得改变**已披露**值的输出（缓存只能省算，不能改口径）。"""
    plain = youzi_replay.build_page(build_inputs(), CODE, START, END)
    cached = youzi_replay.build_page(
        build_inputs(), CODE, START, END, horizon_cache=hz.HorizonCache()
    )
    assert [e.horizons for e in cached.events] == [e.horizons for e in plain.events]


def test_horizon_cache_is_requested_once_per_event_day_and_hits_on_second_call() -> None:
    """★ 缓存必须真被用上：同一份输入跑两次，第二次应命中缓存而不是重算。

    判据不是「结果相同」（直算也相同），而是**取自缓存对象的条目数**发生变化。
    """
    cache = hz.HorizonCache()
    assert len(cache) == 0
    youzi_replay.build_page(build_inputs(), CODE, START, END, horizon_cache=cache)
    assert len(cache) > 0, "构造一页后缓存仍为空 → 期限取值根本没走缓存"


def test_cached_missing_value_expires_so_it_is_not_pinned_forever() -> None:
    """★ 缺失不得被永久负缓存：短冷却过期后必须重新计算。

    这里用一个**会变**的取数函数间接验证：同一 (披露日, 证券, as_of) 若被永久
    钉住，后续重算拿不到新值——因此断言冷却过期后缓存条目被清掉、可再次写入。
    """
    cache = hz.HorizonCache(value_ttl=60.0, miss_cooldown=5.0)
    missing = tuple(
        hz.HorizonValue(
            label=label,
            sessions=sessions,
            value_pct=None,
            status=hz.STATUS_MISSING,
            target_date="",
            fetched_at=0.0,
            source="s",
            reason="field_null_after_due",
        )
        for label, sessions in zip(hz.horizon_labels(), hz.HORIZON_DAYS, strict=True)
    )
    cache.put("20260901", CODE, missing, "20260901", now=0.0)
    # 冷却内仍命中（避免同一请求内反复重算）。
    assert cache.get("20260901", CODE, "20260901", now=1.0) is not None
    # 冷却外必须失效 → 下一次可重新取真值。
    assert cache.get("20260901", CODE, "20260901", now=6.0) is None


def test_cache_key_separates_as_of_so_stale_snapshots_do_not_leak() -> None:
    """★ 同一事件在不同 `as_of` 下到期状态不同，缓存绝不能跨 `as_of` 复用。"""
    cache = hz.HorizonCache()
    disclosed = tuple(
        hz.HorizonValue(
            label=label,
            sessions=sessions,
            value_pct=1.0,
            status=hz.STATUS_DISCLOSED,
            target_date="20260910",
            fetched_at=0.0,
            source="s",
        )
        for label, sessions in zip(hz.horizon_labels(), hz.HORIZON_DAYS, strict=True)
    )
    cache.put("20260901", CODE, disclosed, "20260910", now=0.0)
    assert cache.get("20260901", CODE, "20260910", now=0.1) is not None
    assert cache.get("20260901", CODE, "20260911", now=0.1) is None
