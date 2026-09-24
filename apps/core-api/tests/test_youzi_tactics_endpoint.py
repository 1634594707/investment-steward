"""Y2-08 `/youzi/tactics/{day}`：降级分支 + 成功计算分支。

全部离线：日历与取数均用 monkeypatch 提供受控结果，不联网、不读时钟。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.tactic_annotator import BOUNDARY_NOTICE

PLUGIN = "official.youzi-radar"

#: 六个连续交易日（含跨周末 09-11 周五 → 09-14 周一）。
#: 端点以「请求日」为窗口末尾，因此夹具多留一天，使 09-17 请求能得到完整五日窗口。
CALENDAR_DAYS = ("20260910", "20260911", "20260914", "20260915", "20260916", "20260917")
#: 以 20260917 为截至日的五日窗口（端点实际使用的输入）。
WINDOW = ("20260911", "20260914", "20260915", "20260916", "20260917")
REQUEST_DAY = "20260917"


@pytest.fixture()
def client(tmp_path):
    app = create_app(CoreSettings(session_token="tactics-test", data_dir=tmp_path))
    with TestClient(app) as test_client:
        yield test_client, {"X-Core-Session-Token": "tactics-test"}


def enable(client):
    test_client, headers = client
    assert test_client.post(f"/plugins/{PLUGIN}/install", headers=headers).status_code == 200
    return test_client, headers


def _stub_calendar(monkeypatch, *, available=True, days=CALENDAR_DAYS):
    """用受控日历替换真实取数，避免联网。"""
    from investment_steward_core import trade_calendar

    result = trade_calendar.CalendarResult(
        available, tuple(days), "", days[-1] if days else "", 3, 0.0
    )
    monkeypatch.setattr(trade_calendar, "recent_trading_days", lambda *a, **k: result)
    return result


def _stub_reports(monkeypatch, *, coverage="complete", rows=None):
    """受控三报告取数：`rows` 为 {report: [row, ...]}。"""
    from investment_steward_core import lhb_feed

    rows = rows or {}
    calls: list[str] = []

    def fake(day, *, budget=None):
        calls.append(day)
        return {
            report: (list(rows.get(report, [])), coverage, "" if coverage == "complete" else "stub")
            for report in (
                lhb_feed.REPORT_DAILY_BILLBOARD,
                lhb_feed.REPORT_SEAT_BUY,
                lhb_feed.REPORT_SEAT_SELL,
            )
        }

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", fake)
    return calls


def test_requires_session(client):
    test_client, _ = client
    assert test_client.get("/youzi/tactics/20260916").status_code == 401


def test_requires_enabled_plugin(client):
    test_client, headers = client
    assert test_client.get("/youzi/tactics/20260916", headers=headers).status_code == 409


def test_no_calendar_never_guesses_effective_day_or_fetches(client, monkeypatch):
    """日历不可用 → 显式 calendar_missing，且**绝不**触发取数或标注。"""
    from investment_steward_core import lhb_feed, tactic_annotator, trade_calendar

    def forbidden(*args, **kwargs):
        pytest.fail("calendar_missing branch must not fetch or annotate")

    monkeypatch.setattr(
        trade_calendar,
        "recent_trading_days",
        lambda *a, **k: trade_calendar.CalendarResult(False, (), "基准证券不足"),
    )
    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", forbidden)
    monkeypatch.setattr(lhb_feed, "fetch_billboard", forbidden)
    monkeypatch.setattr(lhb_feed, "fetch_seats", forbidden)
    monkeypatch.setattr(tactic_annotator, "annotate", forbidden)
    test_client, headers = enable(client)
    response = test_client.get("/youzi/tactics/20260916", headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "request_day": "20260916",
        "effective_day": None,
        "window_days": [],
        "coverage": {},
        "available": False,
        "items": [],
        "raw_billboard": [],
        "reason": "calendar_missing",
        "conflicts": [],
        "excluded": {},
        "boundary_notice": BOUNDARY_NOTICE,
    }


@pytest.mark.parametrize("day", ["20260919", "20990101"])
def test_non_trading_day_is_not_guessed(client, monkeypatch, day):
    """`day` 不在可信日历中（休市/未来）→ not_a_trading_day，不借相邻日顶替。"""
    from investment_steward_core import lhb_feed

    def forbidden(*args, **kwargs):
        pytest.fail("non-trading day must not fetch")

    _stub_calendar(monkeypatch)
    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", forbidden)
    test_client, headers = enable(client)
    body = test_client.get(f"/youzi/tactics/{day}", headers=headers).json()
    assert body["available"] is False
    assert body["reason"] == "not_a_trading_day"
    assert body["effective_day"] is None
    assert body["window_days"] == []


def test_incomplete_window_degrades_but_returns_raw_rows(client, monkeypatch):
    """任一窗口日取数不完整 → 不生成事实，但**仍返回已取得的原始榜单行**。"""
    from investment_steward_core import lhb_feed, tactic_annotator

    _stub_calendar(monkeypatch)
    rows = {
        lhb_feed.REPORT_DAILY_BILLBOARD: [
            {"SECURITY_CODE": "000592", "SECURITY_NAME_ABBR": "平潭发展"}
        ]
    }
    _stub_reports(monkeypatch, coverage="truncated", rows=rows)
    monkeypatch.setattr(
        tactic_annotator,
        "annotate",
        lambda *a, **k: pytest.fail("incomplete window must not annotate"),
    )
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()
    assert body["available"] is False
    assert body["reason"] == "incomplete_window"
    assert body["items"] == []
    assert body["window_days"] == list(WINDOW)
    assert set(body["coverage"].values()) == {"truncated"}
    assert body["raw_billboard"], "已取得的原始披露必须降级展示"
    assert body["raw_billboard"][0]["security_code"] == "000592"
    # 请求日已确认为交易日 → 有效日是**已知事实**，必须如实回报（前端据此显示日期）。
    assert body["effective_day"] == "20260917"


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({"unknown", "complete"}, "unknown"),
        ({"fetch_failed", "complete"}, "fetch_failed"),
        ({"truncated", "fetch_failed"}, "fetch_failed"),
        ({"unknown", "fetch_failed"}, "unknown"),
        ({"unknown", "truncated"}, "unknown"),
    ],
)
def test_worst_coverage_never_hides_unknown_behind_failure(client, monkeypatch, states, expected):
    """Y2-09：`unknown` 不得被折进 `fetch_failed`——「无法判断」≠「确定性失败」。

    「取数失败」（我知道我失败了）与「无法判断」（连空响应是不是真的空都说不准）
    对使用者是两件事。把后者折进前者，等于用一个确定的结论掩盖不确定性。
    """
    from investment_steward_core import lhb_feed

    _stub_calendar(monkeypatch)
    order = (
        lhb_feed.REPORT_DAILY_BILLBOARD,
        lhb_feed.REPORT_SEAT_BUY,
        lhb_feed.REPORT_SEAT_SELL,
    )
    state_list = sorted(states)

    def fake(day, *, budget=None):
        return {
            report: ([], state_list[index % len(state_list)], "stub")
            for index, report in enumerate(order)
        }

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", fake)
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()
    assert body["available"] is False
    assert body["reason"] == "incomplete_window"
    assert set(body["coverage"].values()) == {expected}


def test_complete_window_with_no_match_is_available_true(client, monkeypatch):
    """完整窗口但无命中 → available=true, items=[]（「未检出」的合法表达）。"""

    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={})
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()
    assert body["available"] is True
    assert body["reason"] == ""
    assert body["items"] == []
    assert body["effective_day"] == "20260917"
    assert body["window_days"] == list(WINDOW)
    assert set(body["coverage"].values()) == {"complete"}


def test_adjacent_buy_sell_fact_end_to_end(client, monkeypatch):
    """真实形状输入跑通成功分支：D 日买入席位 → 下一交易日卖出席位 → adjacent_buy_sell。"""
    from investment_steward_core import lhb_feed

    _stub_calendar(monkeypatch)

    def seat(code, dept, name, direction, amount, explanation, day):
        return {
            "TRADE_DATE": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            "SECURITY_CODE": code,
            "OPERATEDEPT_CODE": dept,
            "OPERATEDEPT_NAME": name,
            "EXPLANATION": explanation,
            "BUY": amount if direction == lhb_feed.DIRECTION_BUY else None,
            "SELL": amount if direction == lhb_feed.DIRECTION_SELL else None,
        }

    single = "日涨幅偏离值达到7%的前5只证券"
    # 09-16 买入 → 09-17 卖出，同一营业部同一证券。
    board = {"SECURITY_CODE": "000592", "SECURITY_NAME_ABBR": "平潭发展"}

    def fake(day, *, budget=None):
        per_report: dict[str, list[dict[str, object]]] = {
            lhb_feed.REPORT_DAILY_BILLBOARD: [board] if day in ("20260916", "20260917") else [],
            lhb_feed.REPORT_SEAT_BUY: (
                [
                    seat(
                        "000592",
                        "10026937",
                        "某某证券股份有限公司某营业部",
                        "buy",
                        1e7,
                        single,
                        day,
                    )
                ]
                if day == "20260916"
                else []
            ),
            lhb_feed.REPORT_SEAT_SELL: (
                [
                    seat(
                        "000592",
                        "10026937",
                        "某某证券股份有限公司某营业部",
                        "sell",
                        8e6,
                        single,
                        day,
                    )
                ]
                if day == "20260917"
                else []
            ),
        }
        return {report: (rows, "complete", "") for report, rows in per_report.items()}

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", fake)
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()

    assert body["available"] is True
    kinds = {item["fact_type"] for item in body["items"]}
    assert "adjacent_buy_sell" in kinds
    fact = next(i for i in body["items"] if i["fact_type"] == "adjacent_buy_sell")
    assert fact["security_code"] == "000592"
    assert fact["operatedept_code"] == "10026937"
    assert fact["days"] == ["20260916", "20260917"]
    assert fact["fact_id"]
    assert fact["evidence"], "证据必须回链到参与披露"

    # Y2-06：证据必须能定位回原始披露——来源报告名 + 来源记录 ID 缺一不可。
    # 只有日期和金额是无法回到原文的（同席位同日同方向会有多行）。
    for item in fact["evidence"]:
        assert item["source_report"] in (
            lhb_feed.REPORT_SEAT_BUY,
            lhb_feed.REPORT_SEAT_SELL,
        ), f"来源报告名必须落到具体报告：{item['source_report']!r}"
        assert len(item["source_record_id"]) == 64, "来源记录 ID 应为 sha256 十六进制"
        assert item["evidence_id"]
        assert item["explanation"]
        assert item["amount_reason"] is None, "有效金额不应带降级原因"

    buy_side = next(i for i in fact["evidence"] if i["direction"] == "buy")
    sell_side = next(i for i in fact["evidence"] if i["direction"] == "sell")
    assert buy_side["source_report"] == lhb_feed.REPORT_SEAT_BUY
    assert sell_side["source_report"] == lhb_feed.REPORT_SEAT_SELL
    # 逐条展示 D 日买额与下一交易日卖额；不计算比例、不计算收益（Y2-03）。
    assert buy_side["trading_day"] == "20260916" and buy_side["amount"] == 1e7
    assert sell_side["trading_day"] == "20260917" and sell_side["amount"] == 8e6


def test_evidence_locators_survive_output_whitelist(client, monkeypatch):
    """证据回链字段必须通过 `evidence_chain` 白名单，且不带数据商统计字段。"""
    from investment_steward_core import lhb_feed

    _stub_calendar(monkeypatch)
    single = "日涨幅偏离值达到7%的前5只证券"

    def fake(day, *, budget=None):
        bought = day == "20260916"
        per_report: dict[str, list[dict[str, object]]] = {
            lhb_feed.REPORT_DAILY_BILLBOARD: (
                [{"SECURITY_CODE": "000592", "SECURITY_NAME_ABBR": "平潭发展"}] if bought else []
            ),
            lhb_feed.REPORT_SEAT_BUY: (
                [
                    {
                        "TRADE_DATE": "2026-09-16",
                        "SECURITY_CODE": "000592",
                        "OPERATEDEPT_CODE": "10026937",
                        "OPERATEDEPT_NAME": "某某证券股份有限公司某营业部",
                        "EXPLANATION": single,
                        "BUY": 1e7,
                        "SELL": None,
                        # 数据商夹带统计：只允许留在原文，不许成为我方字段。
                        "RISE_PROBABILITY_3DAY": 0.2887,
                    }
                ]
                if bought
                else []
            ),
            lhb_feed.REPORT_SEAT_SELL: [],
        }
        return {report: (rows, "complete", "") for report, rows in per_report.items()}

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", fake)
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics", headers=headers).json()
    for item in body["items"]:
        for evidence in item["evidence"]:
            assert set(evidence) == {
                "trading_day",
                "direction",
                "amount",
                "evidence_id",
                "explanation",
                "source_report",
                "source_record_id",
                # G03（桌面端升级路线图 2026-09-18）：可点回链（构造式 URL，非绩效字段）。
                "source_url",
                "amount_reason",
            }
            # RISE_PROBABILITY_3DAY 不得以任何形式漏进我方结构。
            assert "0.2887" not in repr(evidence)


def test_bucket_identity_is_excluded(client, monkeypatch):
    """`机构专用` 是聚合桶 → 不参与任何跨日事实，但在 excluded 里可见。"""
    from investment_steward_core import lhb_feed

    _stub_calendar(monkeypatch)
    single = "日涨幅偏离值达到7%的前5只证券"

    def fake(day, *, budget=None):
        per_report: dict[str, list[dict[str, object]]] = {
            lhb_feed.REPORT_DAILY_BILLBOARD: (
                [{"SECURITY_CODE": "000592", "SECURITY_NAME_ABBR": "平潭发展"}]
                if day == "20260916"
                else []
            ),
            lhb_feed.REPORT_SEAT_BUY: (
                [
                    {
                        "TRADE_DATE": "2026-09-16",
                        "SECURITY_CODE": "000592",
                        "OPERATEDEPT_CODE": "0",
                        "OPERATEDEPT_NAME": "机构专用",
                        "EXPLANATION": single,
                        "BUY": 1e7,
                        "SELL": None,
                    }
                ]
                if day == "20260916"
                else []
            ),
            lhb_feed.REPORT_SEAT_SELL: [],
        }
        return {report: (rows, "complete", "") for report, rows in per_report.items()}

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", fake)
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()
    assert body["available"] is True
    assert body["items"] == []
    assert body["excluded"], "被排除的桶必须计数可见"


def test_output_contains_no_performance_fields(client, monkeypatch):
    """输出不得出现涨跌/收益/胜率等绩效字段（产品边界铁律 4）。"""

    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={})
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()
    blob = repr(body)
    for forbidden in ("收益", "胜率", "上涨比例", "命中率", "chg", "return_pct", "win_rate"):
        assert forbidden not in blob, f"输出不应含绩效字段：{forbidden}"


def test_five_day_window_shares_one_budget(client, monkeypatch):
    """Y2-07：五日窗口 × 三报告必须共用**同一个**预算实例。

    若在循环内新建 `Budget()`，「40 次尝试」会按日/按报告倍增——那等于没有预算。
    这里捕获每次取数收到的 budget 对象身份，断言全程只有一个。
    """
    from investment_steward_core import lhb_feed

    _stub_calendar(monkeypatch)
    seen: list[int] = []

    def fake(day, *, budget=None):
        assert budget is not None, "五日窗口取数必须携带共享预算"
        seen.append(id(budget))
        return {
            report: ([], "complete", "")
            for report in (
                lhb_feed.REPORT_DAILY_BILLBOARD,
                lhb_feed.REPORT_SEAT_BUY,
                lhb_feed.REPORT_SEAT_SELL,
            )
        }

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", fake)
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()
    assert body["available"] is True
    assert len(seen) == 5, "五日窗口应逐日各调用一次（每日内含三报告）"
    assert len(set(seen)) == 1, "窗口内所有取数必须共用同一个预算实例"


def test_reason_missing_budget_when_calendar_absent(client, monkeypatch):
    """日历缺失时连预算都不建——不发起任何取数（用 id() 计数做不到，改用调用计数）。"""
    from investment_steward_core import lhb_feed, trade_calendar

    calls: list[str] = []

    def counting(day, *, budget=None):
        calls.append(day)
        return {}

    monkeypatch.setattr(
        trade_calendar,
        "recent_trading_days",
        lambda *a, **k: trade_calendar.CalendarResult(False, (), "基准证券不足"),
    )
    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", counting)
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()
    assert body["reason"] == "calendar_missing"
    assert calls == []


@pytest.mark.parametrize(
    "day", ["2026-09-16", "20260230", "20261301", "00000000", "2026091", "２０２６０９１６"]
)
def test_rejects_invalid_dates(client, day):
    test_client, headers = enable(client)
    response = test_client.get(f"/youzi/tactics/{day}", headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"] == "日期必须为有效 YYYYMMDD"


# —— 无日期的默认入口 `/youzi/tactics`（Y2-10 事实页签首次进入用）——


def test_latest_requires_session(client):
    test_client, _ = client
    assert test_client.get("/youzi/tactics").status_code == 401


def test_latest_requires_enabled_plugin(client):
    test_client, headers = client
    assert test_client.get("/youzi/tactics", headers=headers).status_code == 409


def test_latest_route_is_not_shadowed_by_day_route(client, monkeypatch):
    """`/youzi/tactics`（无日）与 `/youzi/tactics/{day}` 必须各自路由到对的处理器。

    注册顺序写反时，无日请求会 404（或 `day=""` 触发 422），且只有端到端请求
    才能发现——所以这里显式断言两条路径的成功分支。
    """

    _stub_calendar(monkeypatch)
    _stub_reports(monkeypatch, rows={})
    test_client, headers = enable(client)
    latest = test_client.get("/youzi/tactics", headers=headers)
    assert latest.status_code == 200
    # 无日入口的有效日 = 受控日历的最后一天（本夹具里就是 09-17）。
    assert latest.json()["effective_day"] == CALENDAR_DAYS[-1]
    assert latest.json()["window_days"] == list(WINDOW)

    explicit = test_client.get("/youzi/tactics/20260917", headers=headers)
    assert explicit.status_code == 200
    assert explicit.json() == latest.json()


def test_latest_uses_last_confirmed_day_not_today(client, monkeypatch):
    """有效日取日历末位，**不**取系统当天——否则周末/节假日会把休市日当交易日。"""

    _stub_calendar(monkeypatch, days=("20260910", "20260911", "20260914"))
    calls = _stub_reports(monkeypatch, rows={})
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics", headers=headers).json()
    assert body["effective_day"] == "20260914"
    # 窗口只有三天 → 不足五日 → 显式降级（而不是偷偷补齐或回退自然日）。
    assert body["available"] is False
    assert body["reason"] == "insufficient_trading_history"
    assert calls == [], "窗口不足时不应发起取数"


def test_latest_without_calendar_never_guesses(client, monkeypatch):
    """无可信日历 → calendar_missing；不触发取数，也不回退系统日期。"""
    from investment_steward_core import lhb_feed, trade_calendar

    def forbidden(*args, **kwargs):
        pytest.fail("calendar_missing branch must not fetch")

    monkeypatch.setattr(
        trade_calendar,
        "recent_trading_days",
        lambda *a, **k: trade_calendar.CalendarResult(False, (), "基准证券不足"),
    )
    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", forbidden)
    test_client, headers = enable(client)
    body = test_client.get("/youzi/tactics", headers=headers).json()
    assert body["available"] is False
    assert body["reason"] == "calendar_missing"
    assert body["effective_day"] is None
    assert body["request_day"] == ""
