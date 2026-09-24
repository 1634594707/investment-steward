"""v37-宏观经济日历实际值关联测试（用户反馈修复）。

用户反馈：2026-09-14 报告把「美国 CPI 未公布」当作最大不确定性，但 BLS 已于
2026-09-11 公布 8 月 CPI（同比 3.4%、环比 0.4%、核心环比 0.3% 超预期）。
根因：`macro_calendar` 只产出「规则推算窗口」且无发布状态字段，窗口型事件在窗口期内
一律被当成「未来事件」。

本文件锁定修复后的语义：
1. `phase` 三态（upcoming / in_window / elapsed）按窗口位置正确切换；
2. `elapsed` 与 `in_window` 事件可关联实际值，`upcoming` **绝不**关联（防「把上月
   数据当本月结论」的新误解）；
3. 实际值缺失时保持 None，不填默认值（ADR-0006）。
"""

from __future__ import annotations

import json
from datetime import date

from investment_steward_core import macro_calendar as calendar_engine
from investment_steward_core import direction_research as direction_engine

TODAY = date(2026, 9, 14)


def _payload_json() -> dict:
    """最小可过闸门的研判载荷（全十节齐 + 三问作答 + 带引用）。"""
    report = "".join(
        f"## {name}\n本节按事实→推理→不确定性展开，给出可核对的数据与边界条件。\n"
        for name in direction_engine.REQUIRED_DIRECTION_SECTIONS
    )
    return {
        "title": "被低估的板块：宏观前提与四维分类",
        "executive_summary": (
            "三问作答：满足低估判定——本期无；只是跌得久未反转——房地产开发；"
            "已处高位——银行Ⅱ。最强证据 [E1]。最大不确定性：分位样本覆盖。"
        ),
        "report": report,
        "core_judgments": [
            {"id": "J1", "text": "宏观前提待核", "kind": "industry",
             "support_refs": ["E1"], "confidence": "medium"},
        ],
        "catalysts": ["盈利端修复"], "risks": ["宏观数据未接入"],
        "stock_pool": [], "data_gaps": ["宏观实际值覆盖"],
        "next_verification": "跟踪官方宏观发布",
    }


def _events(today: date, days: int = 30, readings: dict | None = None):
    return calendar_engine.calendar_snapshot(today, days=days, readings=readings).events


def _find(events, event_key: str, month_prefix: str = "2026-09"):
    return [
        e
        for e in events
        if e.event_key == event_key and e.date.startswith(month_prefix)
    ]


def _us_cpi_readings() -> dict:
    # v39：us_cpi 事件主口径已切到未季调（us:cpi_yoy_nsa，对齐 BLS headline）。
    return {
        "us:cpi_yoy_nsa": {
            "latest": 3.4,
            "obs_date": "2026-08-01",
            "unit": "%YoY",
            "source": "FRED CPIAUCNS 12 期差分",
            "status": "ok",
        }
    }


# —— 1. phase 三态 ——


def test_v37_us_cpi_in_window_on_sep_14_is_marked_in_window():
    """用户锚定日：9-14 属 BLS 窗口内 → phase=in_window（不再声称「未发布」）。"""
    events = _find(_events(TODAY), "us_cpi")
    assert len(events) == 1, events
    assert events[0].phase == "in_window"
    assert events[0].date == "2026-09-10"
    assert events[0].window_end == "2026-09-15"


def test_v37_phase_transitions_across_window_boundaries():
    """窗口边界前后 phase 依次为 upcoming → in_window → elapsed。"""
    cases = [
        (date(2026, 9, 9), "upcoming"),
        (date(2026, 9, 10), "in_window"),   # 窗口首日
        (date(2026, 9, 14), "in_window"),
        (date(2026, 9, 15), "in_window"),   # 窗口末日
        (date(2026, 9, 16), "elapsed"),     # 窗口次日
    ]
    for today, expected in cases:
        found = _find(_events(today), "us_cpi")
        assert found, f"{today} 未返回 us_cpi"
        assert found[0].phase == expected, (today, found[0].phase, expected)


def test_v37_exact_event_phase_is_upcoming_then_elapsed():
    """exact 型（非农）无窗口：事件日当天即 elapsed（当日已发布），此前为 upcoming。

    注意 exact 事件只在 [today, horizon] 内被纳入，故事件日过后不再出现在列表里
    —— 这是 `upcoming_events` 的既有口径（只列「观察期内的发布」）。
    """
    before = _find(_events(date(2026, 10, 1)), "nfp", "2026-10")
    assert before and before[0].phase == "upcoming"
    assert before[0].date == "2026-10-02"  # 10 月第一个周五

    on_day = _find(_events(date(2026, 10, 2)), "nfp", "2026-10")
    assert on_day and on_day[0].phase == "elapsed"

    # 事件日之后不再纳入（既有口径，非本次改动）
    assert _find(_events(date(2026, 10, 3)), "nfp", "2026-10") == []


def test_v37_elapsed_window_event_is_still_listed_within_lookback():
    """窗口刚过的窗口型事件仍纳入（避免已发布数据完全消失），并标 elapsed。"""
    events = _find(_events(date(2026, 9, 18)), "us_cpi")
    assert events, "窗口过后 3 天应仍可见 us_cpi（lookback 内）"
    assert events[0].phase == "elapsed"


# —— 2. 实际值关联 ——


def test_v37_in_window_event_receives_actual_value():
    """窗口内事件接上实际值 —— 修复后报告能拿到 8 月 CPI 同比 3.4%。"""
    found = _find(_events(TODAY, readings=_us_cpi_readings()), "us_cpi")
    assert found[0].actual_value == 3.4
    assert found[0].actual_unit == "%YoY"
    assert found[0].actual_obs_date == "2026-08-01"
    assert "FRED" in (found[0].actual_source or "")


def test_v37_upcoming_event_never_receives_actual_value():
    """关键守卫：未发布事件绝不接实际值（否则把上月数据当本月结论）。"""
    readings = _us_cpi_readings()
    # 9-9：us_cpi 尚未进入窗口（upcoming）
    early = _find(_events(date(2026, 9, 9), readings=readings), "us_cpi")
    assert early[0].phase == "upcoming"
    assert early[0].actual_value is None, "upcoming 事件不应带实际值"

    # 10 月的事件同样是 upcoming，即便 readings 里有数据也不接
    october = _find(_events(TODAY, readings=readings), "us_cpi", "2026-10")
    assert october and october[0].phase == "upcoming"
    assert october[0].actual_value is None


def test_v37_missing_reading_keeps_actual_none_without_default():
    """实际值缺失时保持 None，绝不填默认值（ADR-0006 不编造）。"""
    found = _find(_events(TODAY, readings={}), "us_cpi")
    assert found[0].phase == "in_window"
    assert found[0].actual_value is None
    assert found[0].actual_unit is None
    assert found[0].actual_obs_date is None


def test_v37_pending_reading_is_not_attached():
    """status != ok 的读数（pending/degraded）不得被当作实际值使用。"""
    readings = {
        "us:cpi_yoy_nsa": {
            "latest": None,
            "obs_date": None,
            "unit": "%YoY",
            "source": "pending",
            "status": "pending",
        }
    }
    found = _find(_events(TODAY, readings=readings), "us_cpi")
    assert found[0].actual_value is None


def test_v37_event_without_linked_series_stays_none():
    """未配置 linked_series 的事件（如日本 CPI 尚无实拉序列）保持 None。"""
    readings = _us_cpi_readings()
    found = _find(_events(TODAY, readings=readings), "jp_cpi")
    assert found, found
    assert found[0].actual_value is None


# —— 3. 契约与配置 ——


def test_v37_all_linked_series_use_region_indicator_shape():
    """linked_series 必须是 '{region}:{indicator}' 形状（与 macro_feed 缓存键同构）。"""
    for definition in calendar_engine.EVENT_DEFS:
        series = definition.get("linked_series")
        if series is None:
            continue
        region, _, indicator = series.partition(":")
        assert region in calendar_engine.REGION_LABELS, series
        assert indicator, series


def test_v37_calendar_endpoint_signature_accepts_readings():
    """`calendar_snapshot` 必须支持 readings 入参（端点据此传入实际值）。"""
    import inspect

    params = inspect.signature(calendar_engine.calendar_snapshot).parameters
    assert "readings" in params
    assert params["readings"].default is None


# —— 4. 提示词时效性规则 ——


def test_v37_macro_timing_rule_is_verbatim_present():
    """规则 17 文本必须明确禁止把「窗口已过」写成「数据未公布」。"""
    from investment_steward_core import direction_research as dr

    text = dr.MACRO_TIMING_RULE
    assert "17." in text
    assert "窗口已过" in text and "窗口进行中" in text
    assert "不得" in text and "未公布" in text
    assert "实际值未接入本管道" in text
    assert "窗口未开始" in text


def test_v37_macro_timing_rule_injected_into_both_modes():
    """两条分支（evidence / knowledge）的提示词都带规则 17。"""
    from investment_steward_core import direction_research as dr

    for research_mode in ("evidence", "knowledge"):
        _system, user = dr.build_messages(
            topic="被低估的板块",
            question="",
            evidence_block="[E1] 示例",
            unavailable_note="",
            mode=None,
            research_mode=research_mode,
            style_terms=["低估"],
        )
        assert "宏观数据时效性" in user, research_mode
        assert "窗口已过" in user, research_mode


def test_v37_macro_timing_rule_applies_to_industry_topics_too():
    """非风格主题（纯产业）也应带规则 17（宏观前提是公共输入）。"""
    from investment_steward_core import direction_research as dr

    _system, user = dr.build_messages(
        topic="海运板块景气",
        question="",
        evidence_block="[E1] 示例",
        unavailable_note="",
        mode=None,
        research_mode="evidence",
        style_terms=[],
    )
    assert "宏观数据时效性" in user


# —— 5. 端到端：证据文本带阶段与实际值 ——


def test_v37_evidence_text_carries_phase_and_actual_value(tmp_path, monkeypatch):
    """方向研判证据块的宏观日历条目须含发布阶段与（可得的）实际值，而非只给窗口日期。"""
    from fastapi.testclient import TestClient

    from investment_steward_core import model_client as mc
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    client = TestClient(app)
    headers = {"X-Core-Session-Token": token}

    client.put("/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers)
    profile = client.post(
        "/model-profiles",
        json={"name": "方案A", "base_url": "https://api.a.com", "model": "model-a",
              "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    client.post(f"/model-profiles/{profile['profile_id']}/activate", headers=headers)

    captured: dict = {}

    def _fake(_base_url, payload, _key, _timeout):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": json.dumps(_payload_json())}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)
    client.post(
        "/plugins/official.macro-radar/install", json={"source": "bundled"}, headers=headers
    )

    # 打桩宏观读数：美国 CPI 同比（v39 起主口径为未季调 nsa，另有季调并列）
    class _Row:
        def __init__(self, indicator, latest, obs_date, unit, note):
            self.indicator = indicator
            self.label = indicator
            self.dim = "inflation"
            self.status = "ok"
            self.latest = latest
            self.obs_date = obs_date
            self.unit = unit
            self.as_of = "2026-09-14"
            self.source = "fred"
            self.dataset_version = "fred:CPIAUCNS:2026-08-01"
            self.note = note

    class _Snap:
        def __init__(self):
            self.region = "us"
            self.label = "美国"
            self.indicators = [
                _Row("cpi_yoy_nsa", 3.7, "2026-08-01", "%YoY", "FRED CPIAUCNS 12 期差分"),
                _Row("cpi_yoy", 3.7, "2026-08-01", "%YoY", "FRED CPIAUCSL 12 期差分"),
            ]
            self.background = []

    import investment_steward_core.macro_feed as mf

    monkeypatch.setattr(mf, "get_region_snapshot", lambda *a, **k: _Snap())
    monkeypatch.setattr(api_app.macro_feed, "get_region_snapshot", lambda *a, **k: _Snap())

    body = client.post(
        "/evidence/direction-research",
        json={"topic": "被低估的板块", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True, body

    user_prompt = captured["payload"]["messages"][-1]["content"]
    assert "宏观数据时效性" in user_prompt
    # 阶段词必须出现在提示词里（此处窗口随真实日期变化，断言机制而非具体值）
    assert "窗口" in user_prompt
