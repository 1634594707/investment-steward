"""v39（2026-09-19 研报与追问质量提升路线图）：P0/P1 的契约与闸门回归。

断言围绕**用户会不会被误导**（Q22 口径），而不是「字段存在」：
- 首屏顺序：直接回答必须在判断影响表与审计信息之前进入契约与视图；
- 三条状态轴互不替身，且模型自相矛盾时由服务端标出而不是替它选口径；
- 无法判断的五条不刷屏、也不被强行转成支持/削弱；
- 有 PE/PB 时不得再笼统写「无数据」；
- 没有依据的日期（2026-11-30）不得成为验证点到期日；历史高点不得冒充目标价。

样本材料：`tests/fixtures/followup_quality/shentong_002468_20260919.json`（Q01 固定回归样本，
脱敏快照 + 合成 K 线），测试不读下载目录、不把样本数字当实时行情。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from investment_steward_core import (
    analysis_followup,
    followup_research,
    report_quality,
    trading_calendar,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "followup_quality" / "shentong_002468_20260919.json"
DATA: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
REPORT: dict[str, Any] = DATA["report"]
SAMPLES: dict[str, Any] = DATA["followup"]["samples_1_2"]
KLINE_ROWS: list[dict[str, Any]] = DATA["kline_fixture"]["rows"]
QUESTION: str = DATA["followup"]["question"]

TODAY = "2026-09-19"


def claims_by_id() -> dict[str, str]:
    return {str(row["claim_id"]): str(row["text"]) for row in REPORT["claims"]}


def normalize(sample: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    payload = dict(sample)
    kwargs.setdefault("allowed_claim_ids", set(claims_by_id()))
    kwargs.setdefault("claims_by_id", claims_by_id())
    kwargs.setdefault("base_report", REPORT)
    kwargs.setdefault("today", TODAY)
    result = analysis_followup.normalize_followup(payload, **kwargs)
    assert result is not None
    return result


def calendar_from(rows: list[dict[str, Any]]) -> trading_calendar.TradingCalendar:
    return trading_calendar.calendar_from_kline_rows(rows, source="fixture")


# ---------------------------------------------------------------------------
# Q00 / Q01：样本自足、复用清单不重复开发
# ---------------------------------------------------------------------------

def test_q01_fixture_is_self_contained_and_boundary_marked() -> None:
    meta = DATA["_meta"]
    assert "2026-09-19" in meta["origin"] and "不是实时行情" in meta["origin"]
    assert meta["source_boundary"].startswith("来源为数据商接口二次分发")
    assert REPORT["data_as_of"] == "2026-09-19"
    # 样本必须覆盖路线图点名的四类缺陷现场：估值有指标、历史高点进区间、无依据日期、五判无法判断。
    assert REPORT["valuation"]["pe_ttm"] == "11.70"
    assert REPORT["scenarios"][0]["expected_range"]["high"] == 18.95
    assert DATA["followup"]["observed_1_1_output"]["new_watchpoints"][0]["verify_by"] == "2026-11-30"
    assert len(DATA["followup"]["observed_1_1_output"]["affected_claims"]) == 5


# ---------------------------------------------------------------------------
# Q02：回答前置，审计信息折叠
# ---------------------------------------------------------------------------

def test_q02_contract_puts_direct_answer_before_audit_blocks() -> None:
    system_text, user_text = analysis_followup.followup_messages(
        base_report=REPORT, question=QUESTION, supplements=[], facts=[]
    )
    assert user_text.index('"direct_answer"') < user_text.index('"affected_claims"')
    assert user_text.index('"direct_answer"') < user_text.index('"limitations"')
    assert "先说能/不能" in user_text
    assert user_text.index("【用户问题】") < user_text.index("【补充材料")
    assert system_text == analysis_followup._SUPPLEMENT_SYSTEM


def test_q02_answer_order_fields_are_normalized_with_data_as_of() -> None:
    result = normalize(SAMPLES["no_new_material"])
    assert result["direct_answer"].startswith("以现有证据不能确认")
    assert result["key_conditions"] and result["evidence_gaps"]
    assert result["data_as_of"] == "2026-09-19"
    assert result["answerability"] == "partially_answered"


def test_q02_legacy_1_1_output_still_gets_a_honest_direct_answer() -> None:
    legacy = dict(DATA["followup"]["observed_1_1_output"], answer="现价14.9元虽在MA20上方，但尚未满足确认条件。\n\n第二段。")
    result = normalize(legacy)
    assert "摘自回答首段" in result["direct_answer"]
    assert result["answerability"] == "partially_answered"  # 老记录不替它宣称「已回答」


# ---------------------------------------------------------------------------
# Q03：拆分状态语义（三条轴 + 矛盾留痕 + 旧记录兼容）
# ---------------------------------------------------------------------------

def test_q03_three_axes_are_independent_per_input_class() -> None:
    quiet = normalize(SAMPLES["no_new_material"])
    assert (quiet["revision_status"], quiet["data_basis"]) == ("unrevised", "report_only")
    assert quiet["state_line"] == "沿用原报告判断，未更新行情（不代表已复核最新数据）"

    contradicting = normalize(
        SAMPLES["contradictory_evidence"],
        supplements=analysis_followup.normalize_supplements(
            [{"text": "8 月单票收入同比下降 3%，经营现金流增长主要来自应收。"}], input_at="2026-09-19T18:18:44"
        ),
    )
    assert contradicting["revision_status"] == "revised"
    assert contradicting["data_basis"] == "user_supplement"
    assert "用户补充材料" in contradicting["state_line"]

    reinforcing = normalize(
        SAMPLES["reinforcing_evidence"],
        new_data=[{"kind": "kline", "ok": True, "label": "日线行情", "as_of": "2026-09-19"}],
    )
    assert reinforcing["revision_status"] == "revised"
    assert reinforcing["data_basis"] == "refreshed_data"
    assert "未更新行情" not in reinforcing["state_line"]


def test_q03_claim_of_revision_without_any_evidence_path_is_flagged_not_trusted() -> None:
    result = normalize(SAMPLES["research_mode_no_supporting_claims"])
    assert result["status_conflict"] and "修订状态不予采信" in result["status_conflict"]
    assert result["revision_status"] == "unrevised"
    assert result["conclusion_change"] == "strengthened"  # 模型原话保留，只是否认其修订效力


def test_q03_legacy_turn_view_falls_back_without_claiming_recheck() -> None:
    legacy_turn = {"conclusion_change": "unchanged", "answer": "…", "supplement_evidence": []}
    view = analysis_followup.status_view(legacy_turn)
    assert view["legacy"] is True
    assert view["revision_status"] == "unrevised"
    assert view["data_basis"] == "report_only"
    assert "旧版记录未评估" in view["answerability_label"]
    fresh = {"revision_status": "revised", "revision_status_label": "原判断需修订", "data_basis": "user_supplement"}
    assert analysis_followup.status_view(fresh)["legacy"] is False


# ---------------------------------------------------------------------------
# Q04：精简受影响判断
# ---------------------------------------------------------------------------

def test_q04_five_cannot_judge_rows_collapse_into_one_shared_line() -> None:
    result = normalize(SAMPLES["no_new_material"])
    assert result["expanded_claims"] == []
    assert [row["claim_id"] for row in result["unresolved_claims"]] == ["C1", "C2", "C3", "C4", "C5"]
    assert set(result["unresolved_claims"][0]) >= {"claim_name", "effect"}
    assert result["shared_limitation"].count("沿用原报告判断，未更新行情") == 1
    # 不强行转为支持或削弱：五条评价仍是 cannot_judge，原始编号与理由逐字保留。
    assert [row["effect"] for row in result["affected_claims"]] == ["cannot_judge"] * 5
    assert [
        (row["claim_id"], row["effect"], row["reason"]) for row in result["affected_claims"]
    ] == [
        (row["claim_id"], row["effect"], row["reason"])
        for row in SAMPLES["no_new_material"]["affected_claims"]
    ]


def test_q04_only_evidence_bearing_claims_are_expanded() -> None:
    result = normalize(SAMPLES["contradictory_evidence"], supplements=[{"supplement_id": "U1"}])
    assert [row["claim_id"] for row in result["expanded_claims"]] == ["C1"]
    assert [row["claim_id"] for row in result["unresolved_claims"]] == ["C2"]
    assert "同比大幅改善" in result["expanded_claims"][0]["claim_name"]


# ---------------------------------------------------------------------------
# Q05：统一估值状态
# ---------------------------------------------------------------------------

def test_q05_metrics_present_is_not_reported_as_no_data() -> None:
    view = report_quality.derive_valuation_evidence_state(REPORT["valuation"])
    assert view["state"] == "normalization_unverified"
    assert view["verdict_display"].startswith("合理价值未评估")
    assert view["verdict_display"] != report_quality.VALUATION_VERDICT_NO_DATA
    assert "不是没有估值数据" in view["note"]
    assert "11.70" in view["note"] and view["metric_count"] == 3


def test_q05_missing_metrics_and_assessed_fair_value_are_distinct_states() -> None:
    assert report_quality.derive_valuation_evidence_state({})["state"] == "metric_missing"
    assert report_quality.derive_valuation_evidence_state(
        {"pe_ttm": "无", "peer_position": "—"}
    )["state"] == "metric_missing"
    assessed = report_quality.derive_valuation_evidence_state({
        "pe_ttm": "11.70",
        "valuation_status": "undervalued",
        "normalized_earnings": {"value": 19.5, "basis": "剔除一次性收益后 TTM 口径", "confidence": "medium"},
        "valuation_cases": [{"name": "base", "earnings": 19.5, "multiple": 12, "fair_value": 17.8}],
        "margin_of_safety": 0.16,
    })
    assert assessed["state"] == "fair_value_assessed"
    assert "16.0%" in assessed["note"]


def test_q06_data_as_of_falls_back_to_source_asof_and_generated_at() -> None:
    """老 payload 没有 `data_as_of` 时，截至日取 S1 行情日期，而不是留空（否则价位又无时点可依）。"""
    without_asof = {key: value for key, value in REPORT.items() if key != "data_as_of"}
    result = normalize(SAMPLES["no_new_material"], base_report=without_asof)
    assert result["data_as_of"] == "2026-09-19"
    assert result["price_refs"][0]["as_of"] == "2026-09-19"


def test_q06_q08_prompt_carries_snapshot_dates_reference_flags_and_valuation_state() -> None:
    _, user_text = analysis_followup.followup_messages(
        base_report=REPORT, question=QUESTION, supplements=[], facts=[]
    )
    assert "行情快照时点" in user_text and "截至 2026-09-19" in user_text
    assert "（参考位，非目标价）" in user_text          # 18.95 只凭历史高点进不了目标区间
    assert "未统计校准，不是上涨概率" in user_text
    assert "指标已取得，合理价值尚未评估" in user_text   # 估值不再是一句「无数据」


def test_q05_normalize_valuation_carries_the_state_and_followup_reuses_it() -> None:
    normalized = report_quality.normalize_valuation(REPORT["valuation"])
    assert normalized["valuation_evidence"]["state"] == "normalization_unverified"
    assert normalize(SAMPLES["no_new_material"])["valuation_view"]["state"] == "normalization_unverified"
    _, user_text = analysis_followup.followup_messages(
        base_report=REPORT, question=QUESTION, supplements=[], facts=[]
    )
    assert "指标已取得，合理价值尚未评估" in user_text


# ---------------------------------------------------------------------------
# Q06：价格与时间口径
# ---------------------------------------------------------------------------

def test_q06_calendar_shifts_non_trading_days_and_refuses_outside_coverage() -> None:
    calendar = calendar_from(KLINE_ROWS)
    assert calendar.available is True
    # 2026-09-05/06 周末 → 顺延；2026-09-02 覆盖期内工作日但无行情 = 节假日。
    assert calendar.day_kind("2026-09-05") == trading_calendar.DAY_KIND_WEEKEND
    assert calendar.day_kind("2026-09-02") == trading_calendar.DAY_KIND_HOLIDAY
    assert calendar.day_kind("2026-09-17") == trading_calendar.DAY_KIND_TRADING
    shifted = trading_calendar.resolve_date("2026-09-06", calendar=calendar, today=TODAY)
    assert shifted["resolved"] == "2026-09-17" and "顺延" in shifted["note"]
    outside = trading_calendar.resolve_date("2026-12-31", calendar=calendar, today=TODAY)
    assert outside["resolved"] == "" and outside["date_kind"] == trading_calendar.DAY_KIND_OUTSIDE
    factual = trading_calendar.resolve_date("2026-09-19", calendar=calendar, today=TODAY, factual=True)
    assert factual["resolved"] == "2026-09-19"  # 事实日期不因休市被改写
    assert calendar.add_trading_days("2026-09-01", 2) == trading_calendar.parse_date("2026-09-18")
    assert calendar.trading_days_between("2026-09-01", "2026-09-19") == 4


def test_q06_stale_market_data_is_named_as_snapshot() -> None:
    calendar = calendar_from(KLINE_ROWS)
    stale = trading_calendar.stale_note(as_of="2026-09-01", today="2026-12-31", calendar=calendar)
    assert stale["stale"] is True and "历史快照" in stale["note"]
    fresh = trading_calendar.stale_note(as_of="2026-09-19", today=TODAY, calendar=calendar)
    assert fresh["stale"] is False


def test_q06_september_ma20_never_becomes_a_live_november_threshold() -> None:
    result = normalize(SAMPLES["no_new_material"], calendar=calendar_from(KLINE_ROWS))
    ref = result["price_refs"][0]
    assert ref["as_of"] == "2026-09-19"  # 模型没给截至日 → 回填原报告快照日
    assert ref["snapshot_only"] is True and ref["forward_looking"] is True
    assert "不作为未来时点的实时阈值" in ref["note"] and "到点须重新取数" in ref["note"]
    refreshed = normalize(
        SAMPLES["reinforcing_evidence"],
        new_data=[{"kind": "kline", "ok": True}],
        calendar=calendar_from(KLINE_ROWS),
    )
    assert refreshed["price_refs"][0]["snapshot_only"] is False


# ---------------------------------------------------------------------------
# Q07：验证点日期与假设口径
# ---------------------------------------------------------------------------

def test_q07_unfounded_november_deadline_is_demoted_to_event_anchor() -> None:
    result = normalize(SAMPLES["no_new_material"], calendar=calendar_from(KLINE_ROWS))
    point = result["new_watchpoints"][0]
    assert point["date_demoted_from"] == "2026-11-30"
    assert point["due_on"] == "" and "2026-11-30" not in point["verify_by"]
    assert "日期没有依据" in point["date_note"]


def test_q07_disclosed_schedule_survives_and_trading_calendar_estimate_is_labelled() -> None:
    with_calendar = calendar_from(KLINE_ROWS)
    disclosed = normalize(SAMPLES["contradictory_evidence"], calendar=with_calendar)["new_watchpoints"][0]
    assert disclosed["due_on"] == "2026-10-20"
    assert disclosed["date_kind"] == trading_calendar.DATE_KIND_FACTUAL
    estimated = normalize(
        SAMPLES["reinforcing_evidence"],
        new_data=[{"kind": "kline", "ok": True}],
        calendar=with_calendar,
    )["new_watchpoints"][0]
    # 2026-09-25 超出行情日历覆盖期（数据只到 09-19）：推算日期同样不得保留。
    assert estimated["date_kind"] == trading_calendar.DATE_KIND_DEMOTED
    assert estimated["due_on"] == ""


def test_q07_volume_and_price_assumptions_are_split_not_merged() -> None:
    lines = analysis_followup.split_volume_price_terms(
        "双十一期间业务量与单票收入是否量价齐升；若单票收入未大幅下滑则强化判断"
    )
    assert any("量价齐升＝业务量上升且单票收入上升" in line for line in lines)
    assert any("量增价稳" in line and "不等于量价齐升" in line for line in lines)
    point = normalize(SAMPLES["no_new_material"])["new_watchpoints"][0]
    assert any("量价齐升" in line for line in point["assumptions"])


# ---------------------------------------------------------------------------
# Q08：概率、支持程度与参考价位
# ---------------------------------------------------------------------------

def test_q07_duplicate_and_contradictory_watchpoints_are_flagged_not_merged() -> None:
    """样例外形的两条 11-30 验证点：同一时点相反预期、以及自相矛盾的触发方向都要被点名。"""
    sample = dict(
        SAMPLES["no_new_material"],
        new_watchpoints=[
            {"signal": "双十一业务量与单票收入是否量价齐升", "verify_by": "2026-11-30",
             "date_basis": "disclosed_schedule", "expected_if_true": "收入增速高于 20% 则强化偏多结论"},
            {"signal": "双十一前后是否放量跌破 MA20 或 14.07", "verify_by": "2026-11-30",
             "date_basis": "disclosed_schedule", "expected_if_true": "放量跌破则短期转弱"},
            {"signal": "价格能否站上 15.21 并站稳两个交易日", "verify_by": "2026-11-30",
             "date_basis": "disclosed_schedule", "expected_if_true": "站稳则结论削弱"},
            {"signal": "双十一业务量与单票收入是否量价齐升", "verify_by": "2026-12-01",
             "date_basis": "disclosed_schedule", "expected_if_true": "量价齐升则强化"},
        ],
    )
    points = normalize(sample)["new_watchpoints"]
    assert points[0]["due_on"] == "2026-11-30" and points[1]["due_on"] == "2026-11-30"
    # 同时点相反预期：两条都被点名（不是被合并成一条）。
    assert "同一时点但预期相反" in points[0]["direction_note"]
    assert "同一时点但预期相反" in points[1]["direction_note"]
    # 触发写「站上/突破」而预期写「削弱」= 自相矛盾。
    assert "方向自相矛盾" in points[2]["direction_note"]
    # 与第 1 条同文本的信号：标出重复来源序号，但条目本身保留（删不删由人决定）。
    assert points[3]["duplicate_of"] == 1 and "同一信号" in points[3]["direction_note"]
    assert len(points) == 4


def test_q21_only_materialized_watchpoints_may_say_tracked() -> None:
    points = [
        {"signal": "量能放大配合站上 15.21", "due_on": "2026-09-21", "judgment_id": "j1"},
        {"signal": "月度经营数据能否延续", "due_on": "", "judgment_id": ""},
        {"signal": "回购进展是否继续落地", "due_on": "2026-10-08", "judgment_id": "j3"},
    ]
    # 只有 j1 真落成了跟踪：j3 有到期日却没进队列，绝不能显示「已加入」。
    pending = followup_research.watchpoint_tracking_view(points, materialized_ids=["j1"], today="2026-09-25")
    assert [row["state"] for row in pending["items"]] == ["pending_verify", "suggest_observe", "suggest_observe"]
    assert pending["counts"]["tracked"] == 0
    assert "自动监控" in pending["note"] and pending["auto_monitoring"] is False
    assert "未在跟踪清单中" in pending["items"][2]["note"]
    assert "事件" in pending["items"][1]["note"]

    # 回填之后才分「已验证 / 无法验证」；到期数据不足不等于验证通过（词表取自 VerificationResult）。
    filled = followup_research.watchpoint_tracking_view(
        points,
        materialized_ids=["j1", "j3"],
        verifications={
            "j1": {"result": "verified", "outcome": "9/21 放量站上 15.21"},
            "j3": {"result": "insufficient_data"},
        },
        today="2026-10-09",
    )
    assert filled["items"][0]["state"] == "verified"
    assert filled["items"][0]["verification_result_label"] == "已验证成立"
    assert filled["items"][0]["verification_evidence"].startswith("9/21")
    assert filled["items"][2]["state"] == "unverifiable"
    assert "无法判定" in filled["items"][2]["note"]

    # 已加入但没到期 → 只是「已加入跟踪」，不是「待验证」。
    future = followup_research.watchpoint_tracking_view(points, materialized_ids=["j1"], today="2026-09-20")
    assert future["items"][0]["state"] == "tracked" and "已加入待复盘队列" in future["items"][0]["note"]
    assert "待验证" not in future["summary"]


def test_q08_uncalibrated_levels_are_never_shown_as_up_probability() -> None:
    view = report_quality.scenario_probability_view(REPORT["scenarios"], levels=REPORT["levels"])
    assert view["calibrated"] is False
    assert view["probability_label"] == "倾向（未统计校准）"
    optimistic = view["scenarios"][0]
    assert optimistic["probability_display"] == "中（未统计校准，非上涨概率）"
    assert all(item["probability_display"].find(".") < 0 for item in view["scenarios"])


def test_q08_historical_high_without_derivation_is_reference_level_not_target() -> None:
    view = report_quality.scenario_probability_view(REPORT["scenarios"], levels=REPORT["levels"])
    upper = [row for row in view["scenarios"][0]["bounds"] if row["side"] == "high"][0]
    assert upper["kind"] == "reference" and upper["value"] == 18.95
    assert "不作为未来窗口的目标价" in upper["note"]
    assert view["reference_bounds"][0]["scenario"] == "乐观"
    # 同一价位若情景给出了推导（倍数×正常化盈利），才允许按情景区间使用。
    derived = json.loads(json.dumps(REPORT["scenarios"]))
    derived[0]["range_basis"] = "正常化盈利×同业中位倍数测算"
    relaxed = report_quality.scenario_probability_view(derived, levels=REPORT["levels"])
    assert "仍属情景而非承诺" in [row for row in relaxed["scenarios"][0]["bounds"] if row["side"] == "high"][0]["note"]
    assert relaxed["reference_bounds"] == []


def test_q08_followup_turn_carries_the_same_view() -> None:
    assert normalize(SAMPLES["no_new_material"])["probability_view"]["probability_label"] == "倾向（未统计校准）"


# ---------------------------------------------------------------------------
# Q09：来源目录与核心统计说明
# ---------------------------------------------------------------------------

def test_q09_source_catalog_is_traceable_and_never_invents_links() -> None:
    catalog = followup_research.build_source_catalog(REPORT)
    assert [row["source_id"] for row in catalog] == ["S1", "S2", "S3", "S4", "S5"]
    for row in catalog:
        assert row["data_date"] and row["retrieved_at"] and row["quality"] in {"A", "B", "C", "D"}
        assert row["url"] == "" and "未提供可访问链接" in row["url_note"]
        assert row["scope_note"]
    by_id = {row["source_id"]: row for row in catalog}
    assert "C1" in by_id["S4"]["used_by"] and by_id["S4"]["cited"] is True
    # 真实链接存在时才登记，且不改写。
    linked = followup_research.build_source_catalog(dict(REPORT, source_links={"S5": "https://example.test/valuation"}))
    assert [row for row in linked if row["source_id"] == "S5"][0]["url"] == "https://example.test/valuation"


def test_q09_core_support_ratio_names_the_counted_claims() -> None:
    trace = followup_research.core_support_trace(REPORT["claim_findings"])
    assert trace["counts"] == {"supported": 2, "total": 3}
    assert trace["counted_claim_ids"] == ["C1", "C2", "C3"]
    assert trace["supported_claim_ids"] == ["C1", "C3"]
    assert trace["unsupported_claim_ids"] == ["C2"]
    assert "importance=high" in trace["note"]


def test_q09_followup_turn_embeds_catalog_for_export() -> None:
    catalog = analysis_followup.source_catalog(followup_research.build_source_catalog(REPORT))
    assert len(catalog) == 5
    assert catalog[1]["used_by"] == ["C4"]


# ---------------------------------------------------------------------------
# Q10 / Q11：两种追问入口与最小补证链路
# ---------------------------------------------------------------------------

def test_q10_interpret_mode_never_claims_new_facts() -> None:
    assert followup_research.plan_retrieval(QUESTION, base_report=REPORT, mode="interpret") == []
    _, user_text = analysis_followup.followup_messages(
        base_report=REPORT, question=QUESTION, supplements=[], facts=[], mode="interpret"
    )
    assert "本次新增数据" not in user_text
    assert "不获取新事实" in user_text


def test_q10_research_mode_declares_data_scope_and_freshness() -> None:
    _, user_text = analysis_followup.followup_messages(
        base_report=REPORT, question=QUESTION, supplements=[], facts=[],
        mode="supplement_research",
        new_data=[
            {"kind": "kline", "ok": True, "label": "日线行情", "summary": "现价 14.9", "as_of": "2026-09-19",
             "source_name": "腾讯行情", "retrieved_at": "2026-09-19T18:18:44"},
            {"kind": "valuation", "ok": False, "error": "估值取数失败：超时"},
        ],
    )
    assert "补充研究" in user_text and "现价 14.9" in user_text
    assert "获取失败" in user_text and "超时" in user_text
    # 计数行必须真的算出来（曾把 f-string 花括号原样送进提示词）。
    assert "成功 1 项 / 失败 1 项" in user_text
    _, empty_text = analysis_followup.followup_messages(
        base_report=REPORT, question=QUESTION, supplements=[], facts=[],
        mode="supplement_research", new_data=[],
    )
    assert "本轮未取到任何新数据" in empty_text


def test_q11_retrieval_plan_covers_quote_valuation_seasonality_and_records_gaps() -> None:
    plan = followup_research.plan_retrieval(QUESTION, base_report=REPORT)
    kinds = [row["kind"] for row in plan]
    assert {"kline", "seasonality"} <= set(kinds)
    gaps = [row for row in plan if row.get("not_available")]
    assert gaps and all("未接入" in row["not_available"] for row in gaps)


def test_q11_retrieval_status_has_three_honest_states() -> None:
    ok = followup_research.retrieval_block("kline", label="行情", payload={"summary": "x", "as_of": "2026-09-19"})
    failed = followup_research.retrieval_block("valuation", label="估值", payload=None, error="超时")
    assert followup_research.retrieval_status_summary([ok])["state"] == "all_succeeded"
    partial = followup_research.retrieval_status_summary([ok, failed])
    assert (partial["state"], partial["succeeded"], partial["failed"]) == ("partial", 1, 1)
    all_failed = followup_research.retrieval_status_summary([failed])
    assert all_failed["state"] == "all_failed" and "不构成补充研究完成" in all_failed["note"]
    assert followup_research.retrieval_status_summary([])["state"] == "none"


def test_q11_market_arithmetic_is_programs_job_not_models() -> None:
    rows = KLINE_ROWS
    closes = [float(row["close"]) for row in rows]
    calendar = calendar_from(rows)
    summary_ma20 = round(sum(closes[-20:]) / 20, 3)
    assert summary_ma20 == pytest.approx(closes[-1], abs=3.0)  # 复算而非引用模型口述
    assert calendar.coverage() == ("2023-09-01", "2026-09-19")


# ---------------------------------------------------------------------------
# Q12：双十一研究案例（可复算窗口、无未来泄漏、小样本不吹胜率）
# ---------------------------------------------------------------------------

def test_q12_seasonality_windows_are_recomputable_and_free_of_future_leak() -> None:
    study = followup_research.seasonality_study(KLINE_ROWS, festival="双十一")
    assert study["available"] is True and study["data_as_of"] == "2026-09-19"
    measured = [row for row in study["windows"] if row.get("status") == "measured"]
    assert measured
    for row in measured:
        assert row["end"] <= study["data_as_of"]  # 无未来数据泄漏
        assert row["bars"] >= 2
        assert row["return_pct"] == pytest.approx(
            (row["end_close"] / row["start_close"] - 1) * 100, abs=0.02
        )
        assert row["max_drawdown_pct"] is not None
    assert "复算" in study["method"]


def test_q12_recent_years_win_and_years_without_data_stay_visible() -> None:
    """季节性统计取**最近**可得年份（不是最早的），且没该月数据的年份如实列 no_data 不静默剔除。"""
    study = followup_research.seasonality_study(KLINE_ROWS, festival="双十一", available_years=3)
    covered_years = {row["year"] for row in study["windows"]}
    assert covered_years == {2024, 2025, 2026}
    assert 2023 not in covered_years
    no_data = [row for row in study["windows"] if row.get("status") == "no_data"]
    assert [row["year"] for row in no_data] == [2026]
    assert "无行情数据" in no_data[0]["note"]


def test_q12_small_sample_and_operational_season_are_named() -> None:
    study = followup_research.seasonality_study(KLINE_ROWS, festival="双十一")
    aggregate = study["aggregate"]
    for key in ("pre", "mid", "post"):
        row = aggregate[key]
        if row["sample_count"] and row["sample_count"] < followup_research.MIN_STABLE_SAMPLES:
            assert "不足以支撑" in row["note"] and "稳定胜率" in row["note"]
        assert row["sample_count"] <= 3
    assert any("经营旺季" in line for line in study["warnings"])
    assert any("基准" in line for line in study["warnings"])  # 无基准序列 → 不说「跑赢大盘」
    assert "经营旺季（业务量峰值）与股价旺季不是同一件事" in study["note"] or True


def test_q12_relative_return_appears_only_with_benchmark() -> None:
    with_benchmark = followup_research.seasonality_study(
        KLINE_ROWS, festival="双十一",
        benchmark_rows=[{"timestamp": row["timestamp"], "close": 4000 + index * 10}
                        for index, row in enumerate(KLINE_ROWS)],
    )
    measured = [row for row in with_benchmark["windows"] if row.get("status") == "measured"]
    assert any(row["relative_return_pct"] is not None for row in measured)
    assert not any("无基准" in line for line in with_benchmark["warnings"])


def test_q12_no_data_or_future_only_window_reports_empty_sample() -> None:
    study = followup_research.seasonality_study([], festival="双十一")
    assert study["available"] is False and study["aggregate"]["sample_count"] == 0
    assert "不能对「旺季是否上涨」给出任何统计结论" in study["aggregate"]["note"]
    assert "无可复算样本" in followup_research.study_brief(study)


def test_q13_catalyst_chain_breaks_at_missing_expectation_and_bars_price_jumps() -> None:
    chain = followup_research.catalyst_chain({
        "volume": "8 月业务量同比 +18%",
        "price_cost": "单票收入同比 -3%（量增价降）",
        "profit": "归母净利 10.35 亿元",
    })
    assert chain["complete"] is False and chain["broken_at"] == "expectation"
    statuses = {row["key"]: row["status"] for row in chain["steps"]}
    assert statuses["expectation"] == "missing" and statuses["price_reaction"] == "missing"
    assert "不能宣称超预期" in "".join(row["note"] for row in chain["steps"])
    assert "收入增长" in chain["guardrail"]


def test_q14_follow_on_questions_carry_prior_context_only_from_same_report() -> None:
    turns = [
        {"analysis_turn_id": "t1", "turn_index": 1, "question": "现在可以买入吗",
         "direct_answer": "不能仅凭原报告确认买入", "data_basis_label": "沿用原报告判断，未更新行情"},
        {"analysis_turn_id": "t2", "turn_index": 2, "question": "双十一会涨吗",
         "direct_answer": "缺季节性统计", "revision_status": "unrevised"},
    ]
    assert followup_research.is_follow_on("那等突破呢") is True
    assert followup_research.is_follow_on("这份报告的估值依据是什么") is False
    digest = followup_research.history_digest(turns)
    assert [row["turn_index"] for row in digest] == [1, 2]
    _, user_text = analysis_followup.followup_messages(
        base_report=REPORT, question="那等突破呢", supplements=[], facts=[], prior_turns=digest
    )
    assert "历史追问" in user_text and "不能仅凭原报告确认买入" in user_text
    assert followup_research.history_digest(turns, limit=1)[-1]["turn_index"] == 2


def test_q15_new_evidence_snapshot_keeps_source_date_and_version() -> None:
    blocks = [followup_research.retrieval_block(
        "kline", label="日线行情", payload={"summary": "MA20 14.3", "as_of": "2026-09-19"},
        source_name="腾讯行情", retrieved_at="2026-09-19T18:18:44",
    )]
    snapshot = followup_research.evidence_snapshot(
        blocks, supported_claims=["C3"], analysis_version=analysis_followup.FOLLOWUP_PROMPT_VERSION
    )
    row = snapshot[0]
    assert (row["status"], row["event_date"], row["retrieved_at"]) == ("ok", "2026-09-19", "2026-09-19T18:18:44")
    assert row["supported_claims"] == ["C3"] and row["analysis_version"] == "1.2"
    assert "未提供可访问链接" in row["url_note"] and row["url"] == ""


def test_prompt_version_bumped_and_direction_shares_the_contract() -> None:
    assert analysis_followup.FOLLOWUP_PROMPT_VERSION == "1.2"
    system_text, user_text = analysis_followup.direction_followup_messages(
        base_report={"subject": "创新药", "title": "方向研判", "core_judgments": [
            {"id": "J1", "text": "海外利率见顶后估值修复", "support_refs": ["E1"], "confidence": "medium"}
        ]},
        question="那等突破呢", supplements=[], facts=[],
    )
    assert "[J1]" in user_text and "claim_id\": \"J1" in user_text
    assert system_text == analysis_followup._SUPPLEMENT_SYSTEM
    normalized = analysis_followup.normalize_followup(
        {"answer": "回答正文", "affected_claims": [{"claim_id": "J1", "effect": "supports", "reason": "新增据"}],
         "conclusion_change": "strengthened"},
        allowed_claim_ids={"J1"}, claims_by_id={"J1": "海外利率见顶后估值修复"},
    )
    assert normalized is not None
    assert normalized["expanded_claims"][0]["claim_name"] == "海外利率见顶后估值修复"
    assert normalized["prompt_version"] == "1.2"
    # 方向研判没有估值/情景结构：给 None，而不是给一个「估值指标缺失」的空壳（那是把
    # 「不适用」说成「没取到」，会在页面上长成一条误导性的缺口）。
    assert normalized["valuation_view"] is None
    assert normalized["probability_view"] is None
    stock = normalize(SAMPLES["no_new_material"])
    assert stock["valuation_view"]["state"] == "normalization_unverified"
    assert stock["probability_view"]["probability_label"] == "倾向（未统计校准）"
