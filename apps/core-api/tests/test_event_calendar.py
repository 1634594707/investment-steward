"""事件日历测试（信息层·事件层首版，零外部依赖）。

红线锁定：
- 只生成官方固定节奏规则可推算的事件（exact / window），绝不编造非固定节奏日期；
- FOMC 等议息事件不在结果中出现（无规则不生成）；
- note 恒含「规则推算…以官方日历为准」。
"""

from __future__ import annotations

from datetime import date

import pytest
from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用
from investment_steward_core.macro_calendar import EVENT_DEFS, nth_weekday, upcoming_events


def _file_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


# ---- 纯函数：日期规则 ----

def test_nth_weekday_known_dates():
    # 2026-09 第一个周五 = 09-04（BLS 非农发布日）
    assert nth_weekday(2026, 9, 4, 1) == date(2026, 9, 4)
    # 2026-09 第五个周五不存在
    with pytest.raises(ValueError):
        nth_weekday(2026, 9, 4, 5)
    # 2026-09 有 4 个周五，最后一个 = 09-25
    assert nth_weekday(2026, 9, 4, 4) == date(2026, 9, 25)


def test_upcoming_events_exact_and_window():
    today = date(2026, 9, 4)  # 周五，恰为 9 月第一个周五（非农日）
    events = upcoming_events(today, 14)
    keys = {(event.region, event.event_key) for event in events}
    # exact：非农 09-04（观察期内）
    assert ("us", "nfp") in keys
    nfp = next(event for event in events if event.event_key == "nfp")
    assert nfp.date == "2026-09-04" and nfp.date_type == "exact"
    # window：美 CPI 窗口 09-10..09-15 在 (09-04, 09-18] 内
    assert ("us", "us_cpi") in keys
    cpi = next(event for event in events if event.event_key == "us_cpi")
    assert cpi.date == "2026-09-10" and cpi.window_end == "2026-09-15" and cpi.date_type == "window"
    # 9 月的统计局 PMI（09-30）超出 14 天观察期 → 不出现
    assert ("cn", "cn_pmi") not in keys
    # 排序：按日期升序
    dates = [event.date for event in events]
    assert dates == sorted(dates)
    # note 恒含推算口径
    assert all("规则推算" in event.note and "以官方日历为准" in event.note for event in events)


def test_upcoming_events_window_ongoing_and_next_month():
    today = date(2026, 9, 29)
    events = upcoming_events(today, 14)
    keys = {(event.region, event.event_key) for event in events}
    # 窗口正在进行：HICP 快报窗口 09-27..09-30，today=09-29 在窗口内 → 纳入
    hicp = [event for event in events if event.event_key == "hicp_flash"]
    assert hicp and hicp[0].date == "2026-09-27" and hicp[0].window_end == "2026-09-30"
    # 次月事件：10 月非农 = 10 月第一个周五 = 2026-10-02（在 14 天内）
    assert ("us", "nfp") in keys
    oct_nfp = [event for event in events if event.event_key == "nfp" and event.date.startswith("2026-10")]
    assert oct_nfp and oct_nfp[0].date == "2026-10-02"


def test_no_fabricated_central_bank_events():
    """FOMC/ECB/日银议息等非固定节奏事件绝不出现在日历里（无规则不生成）。"""
    for definition in EVENT_DEFS:
        assert "fomc" not in definition["event_key"] and "rate" not in definition["event_key"]
    events = upcoming_events(date(2026, 9, 4), 60)
    assert all("议息" not in event.label and "FOMC" not in event.label for event in events)


# ---- 端点 ----

def test_calendar_endpoint_requires_enabled_plugin(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.get("/evidence/macro/calendar", headers=headers).status_code == 409


def test_calendar_endpoint_ok_sorted_and_days_clamped(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    response = test_client.get("/evidence/macro/calendar?days=500", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["days"] == 60  # 夹取到上限
    dates = [event["date"] for event in body["events"]]
    assert dates == sorted(dates)
    assert body["events"], "60 天窗口内必有事件"
    # days 最小夹取
    tiny = test_client.get("/evidence/macro/calendar?days=0", headers=headers).json()
    assert tiny["days"] == 1
