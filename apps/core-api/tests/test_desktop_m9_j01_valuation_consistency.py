"""J01（桌面端升级路线图 2026-09-18）：估值 `verdict` 与四态状态必须同口径。

锁定的缺陷（样报 002468 / 2026-09-18 18:30）：同一张估值表里出现
「结论 = 偏便宜」与「估值状态 = 无法判断（未完成正常化验证）」——
`derive_valuation_status` 只降级 `valuation_status`，从不触碰 `verdict`；
D01 只扫正文句子；模型自报 `undetermined` 时连降级告警都不触发。

修复语义：状态为 undetermined 时结论词一律归一为「无数据」，模型原话保留在
`verdict_raw`、原因写进 `verdict_note`，并计入软告警（→ needs_review）。
"""

from __future__ import annotations

from investment_steward_core import report_quality as rq
from test_m3_stock_report_quality import _UNDETERMINED_VALUATION, _payload


def _valuation(**extra) -> dict:
    base = dict(_UNDETERMINED_VALUATION)
    base.update(extra)
    return base


# ———————— 归一化本体 ————————

def test_undetermined_status_normalizes_cheap_verdict_but_keeps_original_word():
    normalized = rq.normalize_valuation(_valuation(verdict="偏便宜", valuation_status="undetermined"))
    assert normalized["valuation_status"] == "undetermined"
    assert normalized["verdict"] == rq.VALUATION_VERDICT_NO_DATA          # 不再与状态打脸
    assert normalized["verdict_raw"] == "偏便宜"                            # 模型原话不删
    assert "缺少正常化盈利支撑" in normalized["verdict_note"]


def test_verified_status_leaves_verdict_untouched():
    normalized = rq.normalize_valuation(_valuation(
        verdict="偏便宜",
        valuation_status="undervalued",
        normalized_earnings={"value": 12.5, "basis": "剔一次性损益", "confidence": "medium"},
    ))
    assert normalized["valuation_status"] == "undervalued"
    assert normalized["verdict"] == "偏便宜"
    assert normalized["verdict_note"] == ""


def test_downgrade_path_normalizes_verdict_as_well_as_status():
    """模型声称低估却拿不出正常化盈利：状态与结论词必须一起倒向「无法判断」口径。"""
    normalized = rq.normalize_valuation(_valuation(
        verdict="合理", valuation_status="undervalued", normalized_earnings={"value": None, "basis": "", "confidence": ""},
    ))
    assert normalized["valuation_status"] == "undetermined"
    assert normalized["valuation_status_raw"] == "undervalued"
    assert normalized["verdict"] == rq.VALUATION_VERDICT_NO_DATA
    assert normalized["verdict_raw"] == "合理"


def test_explicit_no_data_verdict_is_left_alone():
    normalized = rq.normalize_valuation(_valuation(verdict="无数据", valuation_status="undetermined"))
    assert normalized["verdict"] == "无数据"
    assert normalized["verdict_note"] == ""


# ———————— 进质量闸门 ————————

def test_conflict_is_recorded_as_warning_and_needs_review():
    quality = rq.validate_report(
        _payload(valuation=_valuation(verdict="偏便宜", valuation_status="undetermined")),
        allowed={"S1"},
    )
    conflict = [warning for warning in quality["warnings"] if "估值结论与估值状态冲突" in warning]
    assert conflict and quality["valuation"]["verdict"] == rq.VALUATION_VERDICT_NO_DATA
    # 该夹具自带 D01 正文矛盾句（属阻断项），整体状态为 incomplete；这里单独钉机制：
    # 只有这条冲突告警、没有别的告警时，报告也必须落到「待复核」而不是「完整」。
    assert rq.quality_status_of(blockers=[], warnings=conflict) == rq.QUALITY_STATUS_NEEDS_REVIEW


def test_no_conflict_warning_when_verdict_is_supported():
    quality = rq.validate_report(
        _payload(valuation=_valuation(
            verdict="偏便宜",
            valuation_status="undervalued",
            normalized_earnings={"value": 12.5, "basis": "剔一次性损益", "confidence": "medium"},
        )),
        allowed={"S1"},
    )
    assert quality["valuation"]["verdict"] == "偏便宜"
    assert not any("估值结论与估值状态冲突" in warning for warning in quality["warnings"])
