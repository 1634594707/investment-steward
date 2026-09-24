"""v39：美国 CPI 多口径并列（未季调主口径 + 季调并列读数）测试。

背景（2026-09-14 实测）：
- E01 报告给美国 CPI 3.7%（FRED CPIAUCSL 季调指数 12 期差分），此前 web 检索称 BLS
  官方 headline 为 3.4%，触发「口径不一致」疑虑；
- 决定性交叉验证：FRED `CPIAUCNS`（**未季调**，即 BLS headline 的基准指数）8 月同比
  **同为 3.7%**，季调口径也 3.7%——两口径一致，管道数值与 BLS 官方口径无冲突；
  「3.4%」无法从 BLS 自家指数序列复现（BLS API v1/v2 对本机 403，以 FRED 代理官方序列）。

本文件锁定 v39 语义：
1. `_region_indicator_defs("us")` 同时含季调/未季调两个 CPI 指标（并列展示的数据基础）；
2. `_series_transform` 对两个 CPI 指标都返回 `cpi_yoy` 变换，其他指标 `none`；
3. `us_cpi` 事件主口径 = `us:cpi_yoy_nsa`（对齐 BLS headline），主口径名称 = 「未季调」；
4. `attach_actual_values` 收集并列口径读数进 `actual_variants`（主口径也在 variants 里，
   证据文本渲染时按 series 剔除主口径、只展示**其他**口径）；
5. `upcoming` 事件守卫不变：主值与并列口径都不接；
6. 规则 17 增补：多口径条目必须以官方对齐口径为主、必要时并列说明，不得只挑一个数字。
"""

from __future__ import annotations

from datetime import date

import investment_steward_core.macro_feed as macro_feed
from investment_steward_core import direction_research as direction_engine
from investment_steward_core import macro_calendar as calendar_engine

TODAY = date(2026, 9, 14)


def _find(events, event_key: str, month_prefix: str = "2026-09"):
    return [
        e for e in events
        if e.event_key == event_key and e.date.startswith(month_prefix)
    ]


# —— 1. 指标定义并列 ——


def test_us_region_has_both_cpi_calibers():
    defs = {d["indicator"]: d for d in macro_feed._region_indicator_defs("us")}
    assert defs["cpi_yoy"]["fred"] == "CPIAUCSL"
    assert defs["cpi_yoy_nsa"]["fred"] == "CPIAUCNS"
    # 未季调口径的说明必须指明与 BLS headline 对齐
    assert "未季调" in defs["cpi_yoy_nsa"]["label"]
    assert "BLS" in defs["cpi_yoy_nsa"]["note"]


def test_series_transform_applies_to_both_cpi_calibers():
    assert macro_feed._series_transform("us", "cpi_yoy") == "cpi_yoy"
    assert macro_feed._series_transform("us", "cpi_yoy_nsa") == "cpi_yoy"
    # 其他指标不变换
    assert macro_feed._series_transform("us", "nfp") == "none"
    assert macro_feed._series_transform("cn", "cpi_yoy") == "none"


# —— 2. 事件口径映射 ——


def test_us_cpi_event_links_nsa_as_primary():
    us_cpi = next(d for d in calendar_engine.EVENT_DEFS if d["event_key"] == "us_cpi")
    assert us_cpi["linked_series"] == "us:cpi_yoy_nsa"
    assert us_cpi["primary_variant_label"] == "未季调"
    labels = calendar_engine.event_primary_variant_labels()
    assert labels.get("us_cpi") == "未季调"


def test_variant_map_contains_both_calibers_for_us_cpi():
    vmap = calendar_engine.event_variant_map()
    specs = vmap.get("us_cpi") or []
    series = {s["series"] for s in specs}
    assert series == {"us:cpi_yoy_nsa", "us:cpi_yoy"}


# —— 3. 并列读数收集 ——


def _readings(nsa: float | None = 3.7, sa: float | None = 3.5) -> dict:
    out: dict = {}
    if nsa is not None:
        out["us:cpi_yoy_nsa"] = {
            "latest": nsa, "obs_date": "2026-08-01", "unit": "%YoY",
            "source": "CPI 同比（未季调）", "status": "ok",
        }
    if sa is not None:
        out["us:cpi_yoy"] = {
            "latest": sa, "obs_date": "2026-08-01", "unit": "%YoY",
            "source": "CPI 同比（季调）", "status": "ok",
        }
    return out


def test_in_window_event_collects_variants_and_primary():
    events = calendar_engine.calendar_snapshot(TODAY, days=30, readings=_readings()).events
    found = _find(events, "us_cpi")
    assert found and found[0].phase == "in_window"
    ev = found[0]
    # 主口径 = 未季调
    assert ev.actual_value == 3.7
    assert ev.actual_variant_label == "未季调"
    # variants 同时含两口径（主口径由证据文本按 series 剔除）
    series = {item["series"] for item in ev.actual_variants}
    assert series == {"us:cpi_yoy_nsa", "us:cpi_yoy"}
    by_series = {item["series"]: item for item in ev.actual_variants}
    assert by_series["us:cpi_yoy"]["value"] == 3.5
    assert by_series["us:cpi_yoy_nsa"]["value"] == 3.7


def test_variant_missing_series_is_skipped_not_invented():
    """并列口径某一路缺失 → 只跳过该路，不编造（ADR-0006）。"""
    events = calendar_engine.calendar_snapshot(TODAY, days=30, readings=_readings(sa=None)).events
    ev = _find(events, "us_cpi")[0]
    assert ev.actual_value == 3.7  # 主口径仍在
    assert {item["series"] for item in ev.actual_variants} == {"us:cpi_yoy_nsa"}


def test_upcoming_event_gets_neither_primary_nor_variants():
    """关键守卫保持：upcoming 事件不接主值，也不接并列口径（防「上月数据当本月结论」）。"""
    events = calendar_engine.calendar_snapshot(date(2026, 9, 9), days=30, readings=_readings()).events
    ev = _find(events, "us_cpi")[0]
    assert ev.phase == "upcoming"
    assert ev.actual_value is None
    assert ev.actual_variants == []


def test_events_without_variant_spec_keep_empty_variants():
    """无 variant_series 的事件（如非农）variants 恒为空列表。"""
    events = calendar_engine.calendar_snapshot(date(2026, 10, 1), days=30, readings=_readings()).events
    nfp = _find(events, "nfp", "2026-10")
    assert nfp and nfp[0].actual_variants == []


# —— 4. 提示词规则 17 增补 ——


def test_macro_timing_rule_requires_official_caliber_priority():
    rule = direction_engine.MACRO_TIMING_RULE
    assert "多个统计口径" in rule
    assert "与官方发布对齐" in rule
    assert "不得只挑一个数字" in rule
    assert "口径差异当成数据矛盾" in rule


def test_macro_timing_rule_injected_for_style_topic():
    """规则 17 仍注入风格分支（v37 语义保持）。"""
    system_text, user_text = direction_engine.build_messages(
        topic="被低估的板块",
        question="",
        mode=direction_engine.direction_mode_budget("standard")[0],
        research_mode="evidence",
        evidence_block="【E1】测试证据",
        unavailable_note="",
        style_terms=["低估"],
        theme_kind="style",
    )
    assert "宏观数据时效性" in user_text
    assert "不得只挑一个数字" in user_text
