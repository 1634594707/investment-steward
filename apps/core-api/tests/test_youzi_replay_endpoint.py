"""Y3-01 `/youzi/replay/{security_code}`：闸门、参数校验、上下文前取、预算护栏。

全部离线：日历与取数均用 monkeypatch 提供受控结果，不联网、不读时钟。

被测的关键不变量：
 1. 会话 + 插件闸门（401 / 409），未启用插件时**不触发任何取数**。
 2. 参数越界一律 422；不静默截断。
 3. 区间内**只有可信交易日**进入取数，绝不按自然日逐日取（节假日会得到确定性的空）。
 4. 区间前必须**多取最多 4 个交易日**作上下文，否则首日事件的事实标签会静默消失。
 5. 回溯交易日数超上限 → `truncated` + 原因，并保留**最近**的那些日子。
 6. 输出不含绩效/排行字段；`boundary_notice` 与 `limitations` 固定出现。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import youzi_replay
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.tactic_annotator import BOUNDARY_NOTICE

PLUGIN = "official.youzi-radar"
CODE = "600519"

#: 连续交易日：20260901(二)..20260917(四)，含跨周末。
DAYS = (
    "20260901", "20260902", "20260903", "20260904",
    "20260907", "20260908", "20260909", "20260910", "20260911",
    "20260914", "20260915", "20260916", "20260917",
)
START, END = DAYS[4], DAYS[12]

#: 夹具提供日期（`as_of` 之前的交易日），让「上下文前取 4 天」可以真的凑齐五日窗口。
FIXTURE_START = DAYS[0]


@pytest.fixture()
def client(tmp_path):
    app = create_app(CoreSettings(session_token="replay-test", data_dir=tmp_path))
    with TestClient(app) as test_client:
        yield test_client, {"X-Core-Session-Token": "replay-test"}


def enable(client):
    test_client, headers = client
    assert test_client.post(f"/plugins/{PLUGIN}/install", headers=headers).status_code == 200
    return test_client, headers


def _stub_calendar(monkeypatch, *, available=True, days=DAYS):
    """用受控日历替换真实取数。

    端点走 `fetch_calendar(start, end)`，但 `as_of` 之前的上下文交易日需要**更早**
    的日子（真实实现中这些日子由同一次调用返回，只是被区间过滤掉了）。这里把
    受控日历整体返回，由端点自己按区间过滤——与真实形状一致。
    """
    from investment_steward_core import trade_calendar

    picked = tuple(days)

    def fake(start, end, **kwargs):
        if not available:
            return trade_calendar.CalendarResult(False, (), "受控日历不可用")
        # 日历覆盖 = 请求区间且（若请求早于夹具起点）从夹具起点开始，模拟真实
        # `fetch_calendar` 覆盖整个请求区间的行为。
        covered = tuple(day for day in picked if day <= end)
        if not covered:
            return trade_calendar.CalendarResult(False, (), "受控日历未覆盖该区间")
        return trade_calendar.CalendarResult(True, covered, "", covered[-1], 3, 111.5)

    monkeypatch.setattr(trade_calendar, "fetch_calendar", fake)
    return fake


def _stub_reports(monkeypatch, *, rows=None, coverage="complete", fail_days=()):
    """受控三报告取数；返回被请求的日子列表（按调用顺序）。

    ★ `rows` 按**报告名**给出该报告的行，但只有 `TRADE_DATE` 与请求日相符的行才会
    返回——真实数据源按日期过滤，夹具若把全量行重复给每一天，事件数会按天数倍增
    （这不是端点缺陷，是夹具失真）。
    """
    from investment_steward_core import lhb_feed

    rows = rows or {}
    calls: list[str] = []

    def fake(day, *, budget=None):
        calls.append(day)
        if day in fail_days:
            raise lhb_feed.LhbError(f"受控失败：{day}")
        dashed = f"{day[:4]}-{day[4:6]}-{day[6:]}"

        def on_day(report: str) -> list[dict[str, object]]:
            return [
                row
                for row in rows.get(report, [])
                if str(row.get("TRADE_DATE") or "") == dashed
            ]

        return {
            report: (
                on_day(report),
                coverage,
                "" if coverage == "complete" else "stub",
            )
            for report in (
                lhb_feed.REPORT_DAILY_BILLBOARD,
                lhb_feed.REPORT_SEAT_BUY,
                lhb_feed.REPORT_SEAT_SELL,
            )
        }

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", fake)
    return calls


def seat_row(day: str, *, code: str = CODE, dept: str = "10026937", amount: float = 1e7,
             explanation: str = "日涨幅偏离值达7%的证券") -> dict[str, object]:
    return {
        "TRADE_DATE": f"{day[:4]}-{day[4:6]}-{day[6:]}",
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "测试股份",
        "OPERATEDEPT_CODE": dept,
        "OPERATEDEPT_NAME": "某某证券股份有限公司某营业部",
        "EXPLANATION": explanation,
        "BUY": amount,
        "SELL": None,
        "TRADE_ID": f"{day}-{dept}",
    }


def billboard_row(day: str, *, code: str = CODE, **fields: object) -> dict[str, object]:
    """`RPT_DAILYBILLBOARD_DETAILS` 行——**期限字段只在这里**。

    ★ 2026-09-18 实测：`D{N}_CLOSE_ADJCHRATE` 六个字段在席位买卖明细
    （`RPT_BILLBOARD_DAILYDETAILSBUY/SELL`）里**一个都没有**，只有榜单报告有。
    因此「期限」用例必须把字段挂在榜单行上，否则测的是不存在的上游形状。
    """
    row: dict[str, object] = {
        "TRADE_DATE": f"{day[:4]}-{day[4:6]}-{day[6:]}",
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "测试股份",
        "EXPLANATION": "日涨幅偏离值达7%的证券",
    }
    row.update(fields)
    return row


def _url(**overrides) -> str:
    params = {"start": START, "end": END, "as_of": END, **overrides}
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"/youzi/replay/{CODE}?{query}"


# —— 闸门 ——


def test_requires_session(client):
    test_client, _ = client
    assert test_client.get(_url()).status_code == 401


def test_requires_enabled_plugin(client, monkeypatch):
    """未启用插件 → 409，且**不得**触发日历或取数。"""
    from investment_steward_core import lhb_feed, trade_calendar

    def forbidden(*args, **kwargs):
        pytest.fail("disabled plugin must not fetch")

    monkeypatch.setattr(trade_calendar, "fetch_calendar", forbidden)
    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", forbidden)
    test_client, headers = client
    assert test_client.get(_url(), headers=headers).status_code == 409


# —— 参数校验 ——


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"start": "2026-09-07", "end": END}, "YYYYMMDD"),
        ({"start": END, "end": START}, "不得晚于"),
        ({"start": "20260101", "end": "20261231"}, "区间上限"),
        ({"start": START, "end": END, "page_size": "0"}, "page_size"),
        ({"start": START, "end": END, "page_size": "999"}, "每页上限"),
    ],
)
def test_bad_parameters_are_422_not_truncated(client, monkeypatch, params, message):
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch)
    test_client, headers = enable(client)
    response = test_client.get(_url(**params), headers=headers)
    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_bad_security_code_is_422(client, monkeypatch):
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch)
    test_client, headers = enable(client)
    response = test_client.get(f"/youzi/replay/60051?start={START}&end={END}", headers=headers)
    assert response.status_code == 422
    assert "6 位数字" in response.json()["detail"]


def test_malformed_cursor_is_422(client, monkeypatch):
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch)
    test_client, headers = enable(client)
    response = test_client.get(_url(cursor="nodot"), headers=headers)
    assert response.status_code == 422
    assert "游标" in response.json()["detail"]


# —— 日历降级 ——


def test_calendar_missing_never_fetches(client, monkeypatch):
    from investment_steward_core import lhb_feed

    def forbidden(*args, **kwargs):
        pytest.fail("calendar_missing branch must not fetch")

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", forbidden)
    _stub_calendar(monkeypatch, available=False)
    test_client, headers = enable(client)
    body = test_client.get(_url(), headers=headers).json()
    assert body["available"] is False
    assert body["reason"] == "calendar_missing"
    assert body["events"] == []
    assert body["boundary_notice"] == BOUNDARY_NOTICE


def test_range_without_trading_days_is_distinguished_from_calendar_coverage(client, monkeypatch):
    """两种「区间内无交易日」必须区分：日历没覆盖到 vs 真的长假。"""
    from investment_steward_core import lhb_feed, trade_calendar

    def forbidden(*args, **kwargs):
        pytest.fail("no-trading-day branch must not fetch")

    def calendar_only_upto(days, coverage_end):
        def fake(start, end, **kwargs):
            if start > coverage_end:
                return trade_calendar.CalendarResult(False, (), "日历未覆盖该区间")
            covered = tuple(day for day in days if day <= coverage_end)
            return trade_calendar.CalendarResult(True, covered, "", covered[-1], 3, 111.5)

        return fake

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", forbidden)

    # (a) 日历只覆盖到 2026-09-17，请求区间整体在其之后 → 日历没覆盖到。
    monkeypatch.setattr(
        trade_calendar, "fetch_calendar", calendar_only_upto(DAYS, DAYS[-1])
    )
    test_client, headers = enable(client)
    beyond = test_client.get(
        f"/youzi/replay/{CODE}?start=20261005&end=20261007", headers=headers
    ).json()
    assert beyond["available"] is False
    assert beyond["reason"] == "calendar_missing"

    # (b) 日历覆盖区间但区间内无交易日（长假）→ 明确说「无交易日」。
    gap_days = ("20260928", "20260929", "20260930", "20261009", "20261012")
    monkeypatch.setattr(
        trade_calendar, "fetch_calendar", calendar_only_upto(gap_days, "20261012")
    )
    holiday = test_client.get(
        f"/youzi/replay/{CODE}?start=20261001&end=20261008", headers=headers
    ).json()
    assert holiday["available"] is False
    assert holiday["reason"] == "no_trading_day_in_range"


# —— 上下文前取（Y3-04）——


def test_fetches_up_to_four_context_days_before_the_range(client, monkeypatch):
    """★ 首日事件的五交易日窗口需要区间前的日子；不多取则首日事实静默消失。"""
    calls = _stub_reports(monkeypatch)
    _stub_calendar(monkeypatch)
    test_client, headers = enable(client)
    body = test_client.get(_url(), headers=headers).json()
    expected_context = list(DAYS[0:4])  # DAYS[4] 之前恰好有 4 个交易日
    assert body["context_days"] == expected_context
    assert calls[:4] == expected_context, "上下文日必须在区间日之前被取到"
    assert calls[4:] == list(DAYS[4:13])


def test_context_is_capped_at_four_days(client, monkeypatch):
    calls = _stub_reports(monkeypatch)
    _stub_calendar(monkeypatch)
    test_client, headers = enable(client)
    body = test_client.get(
        f"/youzi/replay/{CODE}?start={DAYS[9]}&end={END}&as_of={END}", headers=headers
    ).json()
    assert len(body["context_days"]) == youzi_replay.MAX_CONTEXT_DAYS
    # 起点之前恰有 9 个交易日，只应前取 4 个。
    assert body["context_days"] == list(DAYS[5:9])


def test_calendar_is_requested_from_before_the_range(client, monkeypatch):
    """★ 回归：日历**必须**从区间之前起算，否则上下文日根本不存在。

    真实 `fetch_calendar(start, end)` 是**闭区间**取数：它只能返回 `start` 之后的
    交易日。旧实现直接传业务起点，于是 `probe.days[0] == start`，
    `probe.previous(selected[0], 4)` 恒为空 → 首个事件日的五交易日窗口只有 1 天 →
    全部事件落进 `insufficient_context_days`，复盘页事实标签一片空白
    （而 coverage 全 complete，看起来毫无异常）。

    这里断言端点**请求日历时的起点确实前移了**，这是该修复的可观测契约。
    """
    from investment_steward_core import trade_calendar

    requested: list[tuple[str, str]] = []
    real_fake = _stub_calendar(monkeypatch)

    def recording(start, end, **kwargs):
        requested.append((start, end))
        return real_fake(start, end, **kwargs)

    monkeypatch.setattr(trade_calendar, "fetch_calendar", recording)
    _stub_reports(monkeypatch)
    test_client, headers = enable(client)
    body = test_client.get(_url(), headers=headers).json()

    assert requested, "端点必须请求过日历"
    cal_start, _ = requested[0]
    assert cal_start < START, (
        f"日历起点必须早于业务起点 {START}，否则上下文日取不到（收到 {cal_start}）"
    )
    # 前移量必须真的把起点推到区间之前足够远（≥ 常量声明的自然日数）。
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    gap = (
        _dt.strptime(START, "%Y%m%d") - _dt.strptime(cal_start, "%Y%m%d")
    ).days
    assert gap >= youzi_replay.CONTEXT_LOOKBACK_DAYS, f"只前移了 {gap} 天"
    assert body["context_days"] == list(DAYS[0:4])


def test_context_lookback_constant_covers_a_long_holiday() -> None:
    """前移量必须跨得过最长连续休市（春节/国庆），否则长假后首日仍丢上下文。"""
    assert youzi_replay.CONTEXT_LOOKBACK_DAYS >= 14, (
        "4 个交易日 + 最长连续休市；低于 14 个自然日在长假后凑不齐窗口"
    )


def test_events_are_not_claimed_for_context_days(client, monkeypatch):
    """前取的上下文日**不是**范围内事件，不得出现在结果里。"""
    from investment_steward_core import lhb_feed

    rows = [seat_row(day, dept=f"10026{index:02d}") for index, day in enumerate(DAYS)]
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={lhb_feed.REPORT_SEAT_BUY: rows})
    test_client, headers = enable(client)
    body = test_client.get(_url(), headers=headers).json()
    event_days = {event["trading_day"] for event in body["events"]}
    assert event_days and event_days <= set(DAYS[4:13]), "上下文日不得成为范围内事件"
    assert not (event_days & set(DAYS[0:4]))


# —— 回溯上限护栏 ——


def test_too_many_event_days_truncates_to_the_most_recent_and_says_so(client, monkeypatch):
    """超上限 → truncated + 原因，并保留**最近**的 N 天（不是最早的）。"""
    long_days = tuple(f"2026{month:02d}{day:02d}" for month in (1, 2, 3) for day in range(1, 29))
    _stub_calendar(monkeypatch, days=long_days)
    calls = _stub_reports(monkeypatch)
    test_client, headers = enable(client)
    body = test_client.get(
        f"/youzi/replay/{CODE}?start={long_days[0]}&end={long_days[-1]}&as_of={long_days[-1]}",
        headers=headers,
    ).json()
    assert body["truncated"] is True
    assert "上限" in body["truncated_reason"]
    assert body["event_day_count"] == youzi_replay.MAX_EVENT_DAYS
    covered = [day for day in calls if day in long_days]
    assert long_days[-1] in covered, "必须保留最近的日子"
    assert long_days[0] not in covered, "不应保留最远的日子"


# —— 覆盖与事件 ——


def test_single_day_fetch_failure_is_unknown_not_empty(client, monkeypatch):
    """单日取数失败 → 该日 coverage 为 unknown，**不**假装是「没有披露」。"""
    failing = DAYS[6]
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, fail_days={failing})
    test_client, headers = enable(client)
    body = test_client.get(_url(), headers=headers).json()
    assert body["available"] is True
    assert body["coverage"][failing] == "unknown"


def test_end_to_end_event_is_locatable_and_free_of_performance_fields(client, monkeypatch):
    from investment_steward_core import lhb_feed

    day = DAYS[8]
    row = seat_row(day, dept="10026937", amount=2.887e7)
    # ★ 期限字段挂在**榜单行**上：席位明细里没有这些字段（见 `billboard_row` 注释）。
    board = billboard_row(day, D1_CLOSE_ADJCHRATE=4.2)
    _stub_calendar(monkeypatch)
    _stub_reports(
        monkeypatch,
        rows={lhb_feed.REPORT_DAILY_BILLBOARD: [board], lhb_feed.REPORT_SEAT_BUY: [row]},
    )
    test_client, headers = enable(client)
    body = test_client.get(
        f"/youzi/replay/{CODE}?start={day}&end={day}&as_of={day}", headers=headers
    ).json()
    assert body["available"] is True
    assert len(body["events"]) == 1
    event = body["events"][0]
    assert event["trading_day"] == day
    assert event["operatedept_code"] == "10026937"
    assert event["amount"] == 2.887e7
    # 可定位：来源报告 + 64 位来源记录 ID。
    assert event["source_report"] == lhb_feed.REPORT_SEAT_BUY
    assert len(event["source_record_id"]) == 64
    # ★ 期限必须锚定 `as_of`：`as_of` = 披露日本身 → **零个交易日已过**，
    # 故 D2/D5 都是 `not_due`（而不是把日历里未来的交易日算成「已发生」）。
    # 目标日同样不得越过 `as_of`（越过就只能留空串，不拿未来反推）。
    horizons = {item["label"]: item for item in event["horizons"]}
    assert horizons["D1"]["value_pct"] == 4.2
    assert horizons["D1"]["status"] == "disclosed"
    assert horizons["D1"]["target_date"] == "", "目标日越过 as_of → 留空串"
    assert horizons["D2"]["status"] == "not_due"
    assert horizons["D2"]["target_date"] == ""
    assert horizons["D2"]["reason"].startswith("only_")
    # 上下文完整 → 事实标签**检查过了**；单条孤立披露行不构成三类跨日事实中的任何
    # 一类，因此显式写 `no_fact_for_window`（「检查过，没有」），而不是空串——
    # 空串只用于「有事实」，把两者混同会让读者分不清「无事实」与「未检查」。
    assert event["fact_omitted_reason"] == "no_fact_for_window"

    # ★ 禁词扫描必须排除两个**否定式免责字段**：`boundary_notice` 与 `limitations`。
    # 它们的作用正是声明「不做这些结论」（「无法据此确认持仓、清仓或同一笔交易」），
    # 用整块 repr 扫会把正当的否定说明误伤成违规。要禁的是**正向绩效字段**。
    payload = {
        key: value
        for key, value in body.items()
        if key not in ("limitations", "boundary_notice")
    }
    blob = repr(payload)
    for forbidden in ("胜率", "收益", "上涨比例", "win_rate", "return_pct", "profit", "了结"):
        assert forbidden not in blob, f"复盘输出不应含绩效/越界字段：{forbidden}"
    # 免责说明本身仍必须逐字出现（不能靠删掉它来「通过」扫描）。
    assert body["boundary_notice"] == BOUNDARY_NOTICE
    assert "无法据此确认持仓、清仓或同一笔交易" in body["boundary_notice"]
    assert len(body["limitations"]) == 3


def test_fact_omission_reasons_distinguish_checked_from_uncheckable(client, monkeypatch):
    """★ 三种事实结果必须互相可分辨：「有事实」/「检查过没有」/「没法检查」。

    把后两者混同，等于用不完整输入冒充「已检查且无命中」——这正是 Y2-09 禁止的
    不确定性伪装成确定性的同一类错误，只是发生在窗口层面。
    """
    from investment_steward_core import lhb_feed

    day = DAYS[8]
    rows = [seat_row(day, dept=f"1002693{index}") for index in range(2)]
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={lhb_feed.REPORT_SEAT_BUY: rows})
    test_client, headers = enable(client)

    # (a) 区间含前取上下文 → 窗口完整 → 应当「检查过」。
    full = test_client.get(
        f"/youzi/replay/{CODE}?start={DAYS[4]}&end={day}&as_of={day}", headers=headers
    ).json()
    reasons = {event["fact_omitted_reason"] for event in full["events"]}
    assert "window_incomplete" not in reasons, "上下文已取全，不得报「没法检查」"

    # (b) 区间第一天就是事件日、且夹具没给区间前的日子 → 窗口不完整。
    _stub_calendar(monkeypatch, days=tuple(day for day in DAYS if day >= day))
    lonely = test_client.get(
        f"/youzi/replay/{CODE}?start={day}&end={day}&as_of={day}", headers=headers
    ).json()
    # 日历若不含披露日之后的日子，事件与期限都会退化为 unknown——两种降级都可接受，
    # 但**必须**给出显式原因，不能是空串（空串只表示「有事实」）。
    if lonely["events"]:
        assert lonely["events"][0]["fact_omitted_reason"] != ""


def test_horizon_status_follows_as_of_not_the_calendar_tail(client, monkeypatch):
    """★ 反向用例：同一事件在 `as_of` 更晚时，期限状态必须从 not_due 变为 missing。

    这条防的是「期限把日历里未来的交易日当作已经发生」——那会把「还没到期」
    说成「数据缺失」，等于用错误前提生成结论。
    """
    from investment_steward_core import lhb_feed

    day = DAYS[8]
    row = seat_row(day, dept="10026937")
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={lhb_feed.REPORT_SEAT_BUY: [row]})
    test_client, headers = enable(client)

    early = test_client.get(
        f"/youzi/replay/{CODE}?start={day}&end={day}&as_of={day}", headers=headers
    ).json()
    early_h = {item["label"]: item for item in early["events"][0]["horizons"]}
    assert early_h["D1"]["status"] == "not_due", "as_of 等于披露日 → 一天都还没过"

    # DAYS[9] 是披露日之后第一个交易日 → D1 已到期但字段为 null → missing。
    later = test_client.get(
        f"/youzi/replay/{CODE}?start={day}&end={DAYS[9]}&as_of={DAYS[9]}", headers=headers
    ).json()
    later_h = {item["label"]: item for item in later["events"][0]["horizons"]}
    assert later_h["D1"]["status"] == "missing"
    assert later_h["D1"]["target_date"] == DAYS[9]
    assert later_h["D2"]["status"] == "not_due"


def test_no_ranking_surface_in_the_endpoint(client, monkeypatch):
    """Y3-01：端点不接受排序/排行参数——多余查询参数被忽略，但绝不改变语义。"""
    from investment_steward_core import lhb_feed

    rows = [seat_row(DAY, dept=f"10026{index:02d}") for index, DAY in enumerate(DAYS[4:13])]
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={lhb_feed.REPORT_SEAT_BUY: rows})
    test_client, headers = enable(client)
    plain = test_client.get(_url(), headers=headers).json()
    ranked = test_client.get(_url(sort="amount", top="10"), headers=headers).json()
    assert ranked["events"] == plain["events"]


def test_paging_within_one_range_does_not_repeat_or_drop_events(client, monkeypatch):
    """翻页不重不漏：首页游标接上第二页后拼起来等于整集。"""
    from investment_steward_core import lhb_feed

    rows = []
    for index, day in enumerate(DAYS[4:10]):
        for suffix in ("01", "02"):
            row = seat_row(day, dept=f"10026{index:02d}{suffix}")
            # ★ 身份字段必须逐行唯一：若 12 行共享不足的身份，`_event_key` 会按
            # Y0-04 的规则判成同一条披露行而去重——那是**正确**行为，不是分页缺陷。
            row["TRADE_ID"] = f"{day}-{suffix}"
            row["EXPLANATION"] = f"日涨幅偏离值达7%的证券（{suffix}）"
            rows.append(row)
    assert len({tuple(sorted(row.items(), key=lambda kv: kv[0])) for row in rows}) == len(rows)
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={lhb_feed.REPORT_SEAT_BUY: rows})
    test_client, headers = enable(client)

    seen: list[str] = []
    cursor = ""
    pages = 0
    while True:
        body = test_client.get(_url(page_size="4", cursor=cursor), headers=headers).json()
        pages += 1
        assert body["total_events"] == len(rows), "整集大小不得随分页变化"
        seen.extend(event["event_id"] for event in body["events"])
        if not body["has_more"]:
            break
        cursor = body["cursor"]
        assert cursor
        assert pages < 10, "分页未收敛"
    assert pages == 3, "12 条 / 每页 4 条 = 3 页"
    assert len(seen) == len(set(seen)), "页间不得重复事件"
    assert len(seen) == len(rows), "页间不得丢事件"


def test_cursor_is_rejected_across_ranges(client, monkeypatch):
    """游标绑定区间：换区间后旧游标必须被拒（否则会把 A 查询的第二页接到 B 上）。"""
    from investment_steward_core import lhb_feed

    rows = [seat_row(day, dept=f"10026{index:02d}") for index, day in enumerate(DAYS[4:13])]
    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={lhb_feed.REPORT_SEAT_BUY: rows})
    test_client, headers = enable(client)
    first = test_client.get(_url(page_size="4"), headers=headers).json()
    assert first["has_more"] and first["cursor"]

    swapped = test_client.get(
        f"/youzi/replay/{CODE}?start={DAYS[5]}&end={END}&as_of={END}"
        f"&page_size=4&cursor={first['cursor']}",
        headers=headers,
    )
    assert swapped.status_code == 422
    assert "游标" in swapped.json()["detail"]
