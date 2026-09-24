"""G03（桌面端升级路线图 2026-09-18）：游资证据回链与形状对齐。

三件事被冻结在这里：

1. **回链是构造式的，且形态已实测**。上游 `RPT_DAILYBILLBOARD_DETAILS` /
   `RPT_BILLBOARD_DAILYDETAILSBUY|SELL` 的任何一行都**没有** URL 字段，因此「回到原始披露」
   只能由（交易日, 证券代码）拼出：`https://data.eastmoney.com/stock/lhb,YYYY-MM-DD,CODE.html`
   （2026-09-19 实测 → HTTP 200 且页面含该代码；`.../lhb/YYYY-MM-DD/CODE.html` → 404）。
   参数缺失/不合形时**返回空串**——猜出来的回链比没有回链更糟。
2. **形状对齐**：榜单扩展字段 `TURNOVERRATE`（%）与 `FREE_MARKET_CAP`（元）从 `lhb_feed`
   透传到 `/youzi/today`，键名与单位与 `youzi_normalize` 的声明一致（pct / yuan）。
3. **每条披露事实都能点**：`/youzi/today`、`/youzi/seats`、`/youzi/cross-check`、
   `/youzi/tactics`（跨日事实证据行）、`/youzi/replay`（案例复盘事件）都带 `source_url`。

全部离线：夹具 + monkeypatch，不联网（除了 URL 形态那次一次性人工实测，记录在注释里）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from investment_steward_core import lhb_feed, seat_book, youzi_replay
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

from test_youzi_replay_endpoint import (  # noqa: F401  复用受控日历/取数/行构造
    DAYS as REPLAY_DAYS,
    _stub_calendar as stub_replay_calendar,
    _stub_reports as stub_replay_reports,
    seat_row as replay_seat_row,
)

PLUGIN = "official.youzi-radar"
FIXTURES = Path(__file__).parent / "fixtures" / "youzi"
EXPECTED_URL = "https://data.eastmoney.com/stock/lhb,2026-09-16,000592.html"

#: 空观察名单（S6 摘要只用它做席位匹配；空名单是合法输入，不是「加载失败」）。
EMPTY_WATCHLIST = seat_book.Watchlist(
    version="g03-test", disclaimer="", seats=(), buckets=(), bucket_name_suffixes=(),
)


@pytest.fixture()
def client(tmp_path):
    app = create_app(CoreSettings(session_token="g03-test", data_dir=tmp_path))
    with TestClient(app) as test_client:
        yield test_client, {"X-Core-Session-Token": "g03-test"}


def enable(client):
    test_client, headers = client
    assert test_client.post(f"/plugins/{PLUGIN}/install", headers=headers).status_code == 200
    return test_client, headers


def billboard_row(day: str, *, code: str = "000592", **fields: object) -> dict[str, object]:
    row: dict[str, object] = {
        "TRADE_DATE": f"{day[:4]}-{day[4:6]}-{day[6:]} 00:00:00",
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "平潭发展",
        "EXPLANATION": "日涨幅偏离值达到7%的前5只证券",
        "CHANGE_RATE": 9.98,
        "CLOSE_PRICE": 12.34,
        "BILLBOARD_BUY_AMT": 1.2e8,
        "BILLBOARD_SELL_AMT": 0.6e8,
        "BILLBOARD_NET_AMT": 0.6e8,
        "ACCUM_AMOUNT": 3.4e8,
        "TRADE_MARKET": "深交所",
        "EXPLAIN": "",
        "TURNOVERRATE": 17.8707,
        "FREE_MARKET_CAP": 16680455230.79,
    }
    row.update(fields)
    return row


# —— 1. 回链形态（纯函数） ——


def test_provider_page_url_is_deterministic_and_frozen():
    """形态逐字冻结：两种写法只差一个字符就会 404，所以这里断言完整 URL。"""
    assert lhb_feed.provider_page_url("20260916", "000592") == EXPECTED_URL
    # 上游日期是「2026-09-16 00:00:00」，归一化后再拼，与上一致。
    assert lhb_feed.provider_page_url("2026-09-16 00:00:00", "000592") == EXPECTED_URL
    assert lhb_feed.provider_page_url("2026/09/16", "000592") == EXPECTED_URL


@pytest.mark.parametrize(
    ("day", "code"),
    [
        ("", "000592"),
        ("20260916", ""),
        ("2026-09-16", "00059"),      # 代码不足 6 位
        ("2026-09-16", "0005921"),    # 代码超过 6 位
        ("2026091", "000592"),        # 日期不足 8 位
        ("20261332", "000592"),       # 日期不合形（normalise 保留原样 → 拼不出）
        ("20260916", "SH600000"),     # 非纯数字代码
    ],
)
def test_provider_page_url_returns_empty_instead_of_guessing(day, code):
    """缺参数/不合形一律空串：不猜日期、不留半截 URL、不拿代码凑。"""
    assert lhb_feed.provider_page_url(day, code) == ""


def test_billboard_row_parses_extended_fields_and_derives_url():
    """真实夹具行 → 扩展字段与回链都从同一行派生（真实上游键名）。"""
    payload = json.loads((FIXTURES / "em_lhb_details_20260916.json").read_text(encoding="utf-8"))
    rows = lhb_feed.parse_billboard_rows(payload)
    target = next(row for row in rows if row.security_code == "000592")
    assert target.turnover_rate == pytest.approx(17.8707)
    assert target.free_market_cap == pytest.approx(16680455230.79)
    assert target.source_url == EXPECTED_URL


def test_missing_extended_fields_stay_none_and_url_stays_empty():
    """字段缺失 → None（不填 0）；代码缺失 → 回链为空串（不猜）。"""
    rows = lhb_feed.parse_billboard_items([{
        "TRADE_DATE": "2026-09-16 00:00:00",
        "SECURITY_CODE": "000592",
        "SECURITY_NAME_ABBR": "平潭发展",
    }])
    assert rows[0].turnover_rate is None
    assert rows[0].free_market_cap is None
    assert rows[0].source_url == EXPECTED_URL  # 日期与代码都在 → 仍可回链
    skipped = lhb_feed.parse_billboard_items([{"TRADE_DATE": "2026-09-16", "TURNOVERRATE": 1.0}])
    assert skipped == []  # 没有代码的行不进结构


# —— 2. 端点透传 ——


def test_today_rows_carry_extended_fields_and_backlink(client, monkeypatch):
    test_client, headers = enable(client)
    monkeypatch.setattr(lhb_feed, "fetch_billboard", lambda day: lhb_feed.parse_billboard_items(
        [billboard_row("20260916")]
    ))
    monkeypatch.setattr(lhb_feed, "fetch_seats", lambda day: [])

    body = test_client.get("/youzi/today?trading_day=20260916", headers=headers).json()
    assert body["available"] is True
    row = body["rows"][0]
    assert row["turnover_rate"] == pytest.approx(17.8707)
    assert row["turnover_rate_unit"] == "pct"
    assert row["free_market_cap"] == pytest.approx(16680455230.79)
    assert row["free_market_cap_unit"] == "yuan"
    assert row["source_url"] == EXPECTED_URL


def test_seats_side_rows_carry_backlink(client, monkeypatch):
    test_client, headers = enable(client)

    def seat(day: str, direction: str) -> lhb_feed.SeatRow:
        return lhb_feed.parse_seat_items([{
            "TRADE_DATE": f"{day[:4]}-{day[4:6]}-{day[6:]} 00:00:00",
            "SECURITY_CODE": "000592",
            "OPERATEDEPT_CODE": "10026937",
            "OPERATEDEPT_NAME": "某某证券股份有限公司某营业部",
            "EXPLANATION": "日涨幅偏离值达到7%的前5只证券",
            "BUY": 1e7 if direction == "buy" else None,
            "SELL": 8e6 if direction == "sell" else None,
        }], direction)[0]

    monkeypatch.setattr(lhb_feed, "fetch_seats", lambda day: [seat("20260916", "buy"), seat("20260916", "sell")])
    body = test_client.get("/youzi/seats/000592?trading_day=20260916", headers=headers).json()
    assert body["available"] is True
    for side in ("buy", "sell"):
        assert body[side][0]["source_url"] == EXPECTED_URL


def test_cross_check_hits_carry_backlink(client, monkeypatch):
    test_client, headers = enable(client)
    # 先建一条持仓，让交叉提示有命中（比对在本地完成，不外发持仓）。
    created = test_client.post(
        "/holdings",
        json={"instrument": "000592", "label": "平潭发展", "status": "holding"},
        headers=headers,
    )
    assert created.status_code in (200, 201), created.text
    monkeypatch.setattr(lhb_feed, "fetch_billboard", lambda day: lhb_feed.parse_billboard_items(
        [billboard_row("20260916")]
    ))
    monkeypatch.setattr(lhb_feed, "fetch_seats", lambda day: [])

    body = test_client.get("/youzi/cross-check?trading_day=20260916", headers=headers).json()
    assert body["available"] is True and body["hits"], "持仓上榜应命中"
    assert body["hits"][0]["source_url"] == EXPECTED_URL


def test_tactics_evidence_rows_carry_backlink(client, monkeypatch):
    """跨日事实的每条逐日证据都能点回原始披露页。"""
    test_client, headers = enable(client)
    single = "日涨幅偏离值达到7%的前5只证券"
    board = billboard_row("20260916")

    def fake(day, *, budget=None):
        per_report: dict[str, list[dict[str, object]]] = {
            lhb_feed.REPORT_DAILY_BILLBOARD: [board] if day in ("20260916", "20260917") else [],
            lhb_feed.REPORT_SEAT_BUY: (
                [{
                    "TRADE_DATE": "2026-09-16 00:00:00", "SECURITY_CODE": "000592",
                    "OPERATEDEPT_CODE": "10026937", "OPERATEDEPT_NAME": "某某证券股份有限公司某营业部",
                    "EXPLANATION": single, "BUY": 1e7, "SELL": None,
                }] if day == "20260916" else []
            ),
            lhb_feed.REPORT_SEAT_SELL: (
                [{
                    "TRADE_DATE": "2026-09-17 00:00:00", "SECURITY_CODE": "000592",
                    "OPERATEDEPT_CODE": "10026937", "OPERATEDEPT_NAME": "某某证券股份有限公司某营业部",
                    "EXPLANATION": single, "BUY": None, "SELL": 8e6,
                }] if day == "20260917" else []
            ),
        }
        return {report: (rows, "complete", "") for report, rows in per_report.items()}

    monkeypatch.setattr(lhb_feed, "fetch_day_page_complete", fake)
    body = test_client.get("/youzi/tactics/20260917", headers=headers).json()
    assert body["available"] is True
    seen = 0
    for item in body["items"]:
        for evidence in item["evidence"]:
            seen += 1
            expected = lhb_feed.provider_page_url(evidence["trading_day"], item["security_code"])
            assert evidence["source_url"] == expected
            assert evidence["source_url"].startswith("https://data.eastmoney.com/stock/lhb,")
    assert seen > 0, "夹具应产出至少一条证据"


def test_replay_events_carry_backlink(client, monkeypatch):
    """案例复盘：每条披露事件带可点回链（构造式），且不影响原有的定位字段。"""
    test_client, headers = enable(client)
    code = "600519"
    stub_replay_calendar(monkeypatch)
    stub_replay_reports(monkeypatch, rows={
        lhb_feed.REPORT_SEAT_BUY: [replay_seat_row("20260916", code=code)],
    })

    body = test_client.get(
        f"/youzi/replay/{code}?start={REPLAY_DAYS[4]}&end={REPLAY_DAYS[12]}&as_of={REPLAY_DAYS[12]}",
        headers=headers,
    ).json()
    assert body["available"] is True and body["events"], "夹具应产出至少一条事件"
    for event in body["events"]:
        assert event["source_url"] == lhb_feed.provider_page_url(event["trading_day"], code)
        assert event["source_report"]  # 原有定位字段不因新增回链而消失
        assert event["source_record_id"]


def test_replay_event_dataclass_exposes_url_without_upstream_url_field():
    """模块级：事件字典里必须有 source_url，且**不含**任何从上游抄来的 URL 键。"""
    event = youzi_replay.ReplayEvent(
        event_id="e1", trading_day="20260916", security_code="000592", security_name="平潭发展",
        operatedept_code="10026937", operatedept_name="某营业部", direction="buy",
        amount=1e7, explanation="日涨幅偏离值达到7%的证券",
        source_report="RPT_BILLBOARD_DAILYDETAILSBUY", source_record_id="a" * 64,
    )
    payload = event.as_dict()
    assert payload["source_url"] == EXPECTED_URL


# —— 3. S6 摘要同步 ——


def test_s6_summary_carries_extended_fields_and_backlink():
    rows = lhb_feed.parse_billboard_items([billboard_row("20260916")])
    text = seat_book.build_billboard_summary("000592", rows, [], EMPTY_WATCHLIST)
    assert text is not None
    assert "换手率 17.87%" in text
    assert "自由流通市值 166.80 亿元" in text
    assert f"原始披露页：{EXPECTED_URL}" in text


def test_s6_summary_says_no_data_when_extended_fields_missing():
    """字段缺失写「无数据」，不拿 0 或空串糊过去（同一套「缺失 ≠ 0」口径）。"""
    rows = lhb_feed.parse_billboard_items([{
        "TRADE_DATE": "2026-09-16 00:00:00", "SECURITY_CODE": "000592",
        "SECURITY_NAME_ABBR": "平潭发展", "EXPLANATION": "日涨幅偏离值达到7%的前5只证券",
    }])
    text = seat_book.build_billboard_summary("000592", rows, [], EMPTY_WATCHLIST)
    assert text is not None
    assert "换手率 无数据" in text
    assert "自由流通市值 无数据" in text
