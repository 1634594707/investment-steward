"""v31 研报质量恢复（2026-09-13 方案）单测：三态质量状态 / 必需小节实质检查 / 引用清单一致性。

方案口径（§八.4 修正案优先）：
- 完成状态依据**实质章节、证据与必需字段**判定；字数不足/超出只有软告警，不阻断；
- 缺必需小节 / 无正文 / 无真实引用 / 全部核心判断无来源支撑 → `incomplete`；
- 四节可包含「有解释的数据不足说明」（如实说明也算该节存在）；
- incomplete 报告仍可作为草稿留痕，但不得标记「完成」。
"""

from datetime import date

from investment_steward_core import report_quality as rq

_ALLOWED = {"S1", "S2", "S3", "S4", "S5"}
_TODAY = date(2026, 9, 13)


def _validate(payload: dict, *, mode: str = "standard") -> dict:
    return rq.validate_report(payload, allowed=_ALLOWED, today=_TODAY, mode=mode)


def _full_report() -> str:
    return (
        "### 技术面\n"
        "MA20 6.35 之上运行，量能温和放大，趋势结构完好，关键支撑 6.10。\n"
        "### 消息面\n"
        "近10日无重大新闻，消息面无直接催化事件（如实说明数据不足，不算缺席）。\n"
        "### 基本面\n"
        "营收同比 +12%，净利同比 +8%；经营现金流对净利比 0.9。PE(TTM) 19.7，近5年 6.7% 分位。\n"
        "### 综合判断\n"
        "多空矛盾在于估值分位低但现金流含金量一般；总体偏多。\n"
    )


def test_complete_report_with_soft_warnings_is_needs_review():
    payload = {
        "report": _full_report(),
        "citations": ["S1", "S2"],
        "executive_summary": "摘" * 160,
        "limitations": ["1", "2", "3"],
        "confidence": "medium",
    }
    quality = _validate(payload)
    assert quality["quality_status"] == rq.QUALITY_STATUS_NEEDS_REVIEW
    assert quality["quality_status_label"] == "待复核"
    assert quality["quality_blockers"] == []
    assert quality["missing_sections"] == []


def test_status_derivation_pure_function():
    """三态判定的纯函数口径：有阻断→不完整；仅软告警→待复核；两者皆无→完整。"""
    assert rq.quality_status_of(blockers=[], warnings=[]) == rq.QUALITY_STATUS_COMPLETE
    assert rq.quality_status_of(blockers=[], warnings=["摘要偏长"]) == rq.QUALITY_STATUS_NEEDS_REVIEW
    assert rq.quality_status_of(blockers=[{"code": "missing_section", "message": "缺"}], warnings=[]) == rq.QUALITY_STATUS_INCOMPLETE


def test_full_report_has_no_blockers():
    """完整正文 + 真实引用 + 全支撑判断 → 无任何阻断项（可能有软告警，状态不落 incomplete）。"""
    payload = {
        "report": _full_report(),
        "citations": ["S1", "S2"],
        "executive_summary": "这是执行摘要，包含结论、证据、反证与验证动作四要素，共约一百五十字左右的合理长度描述。",
        "limitations": ["1", "2", "3"],
        "confidence": "medium",
        "claims": [{
            "claim_id": "C1", "text": "营收同比 +12%", "sources": ["S4"],
            "requires": ["revenue"], "importance": "high", "confidence": "high", "missing": [],
        }],
    }
    quality = _validate(payload)
    assert quality["quality_blockers"] == []
    assert quality["quality_status"] != rq.QUALITY_STATUS_INCOMPLETE


def test_501_char_tech_only_report_is_incomplete():
    """方案原始案例：只有技术面的短报告必须判 incomplete，不得当正常报告。"""
    short = "### 技术面\n" + "K线走弱，跌破支撑。说明现状。" * 30
    payload = {
        "report": short,
        "citations": ["S1", "S2"],
        "executive_summary": "摘" * 160,
        "limitations": ["1", "2", "3"],
        "confidence": "low",
    }
    quality = _validate(payload)
    assert quality["quality_status"] == rq.QUALITY_STATUS_INCOMPLETE
    assert set(quality["missing_sections"]) == {"消息面", "基本面", "综合判断"}
    codes = {blocker["code"] for blocker in quality["quality_blockers"]}
    assert codes == {"missing_section"}


def test_heading_only_section_counts_as_missing():
    """§八.4：只有标题没有实质内容 = 缺席；不为过检补标题放行。"""
    report = (
        "### 技术面\n略。\n### 消息面\n同上。\n### 基本面\n见上文。\n### 综合判断\n如前。\n"
    )
    payload = {
        "report": report,
        "citations": ["S1"],
        "executive_summary": "",
        "limitations": ["1", "2", "3"],
        "confidence": "low",
    }
    quality = _validate(payload)
    assert quality["quality_status"] == rq.QUALITY_STATUS_INCOMPLETE
    assert len(quality["missing_sections"]) == 4


def test_empty_body_and_no_citations_are_blockers():
    payload = {
        "report": "",
        "citations": ["S9"],
        "executive_summary": "摘要不能替代正文。",
        "limitations": ["1", "2", "3"],
        "confidence": "low",
    }
    quality = _validate(payload)
    codes = {blocker["code"] for blocker in quality["quality_blockers"]}
    assert "report_empty" in codes
    assert "no_citations" in codes
    assert quality["quality_status"] == rq.QUALITY_STATUS_INCOMPLETE


def test_short_body_still_not_a_length_blocker():
    """§八.4：取消「低于长度阈值即硬失败」——字数只有软告警，不产生阻断项。"""
    report = (
        "### 技术面\n趋势向下运行，反弹动能不足，等待量能与关键位双重确认。\n"
        "### 消息面\n近30日无重大新闻与公告，消息面暂无直接催化事件。\n"
        "### 基本面\n营收同比下滑，经营现金流为负，利润质量偏弱。\n"
        "### 综合判断\n技术面与基本面同向偏空，核心矛盾在估值已低但趋势未止跌。\n"
    )
    payload = {
        "report": report,
        "citations": ["S1"],
        "executive_summary": "执行摘要内容。",
        "limitations": ["1", "2", "3"],
        "confidence": "low",
    }
    quality = _validate(payload)
    assert quality["quality_status"] == rq.QUALITY_STATUS_NEEDS_REVIEW
    assert all(blocker["code"] != "report_empty" for blocker in quality["quality_blockers"])
    # 长度软告警仍在（低于模式下限）
    assert any("低于标准版建议下限" in warning for warning in quality["warnings"])


def test_citation_list_inconsistency_is_flagged_not_fabrication():
    """§八.3：清单 S1/S2 但正文引用 S4/S5（均属可用来源）= 漏登记，不是虚构。"""
    report = _full_report() + "估值依据见 [S5]，公告详见 [S4]。\n"
    payload = {
        "report": report,
        "citations": ["S1", "S2"],
        "executive_summary": "摘要引用 [S4]。",
        "valuation": {"verdict": "偏便宜", "basis": "PE 分位低 [S5]"},
        "limitations": ["1", "2", "3"],
        "confidence": "medium",
    }
    quality = _validate(payload)
    consistency = quality["citation_consistency"]
    assert set(consistency["undeclared_used"]) == {"S4", "S5"}
    assert consistency["unavailable_used"] == []
    assert any("引用清单不一致" in warning for warning in quality["warnings"])


def test_unavailable_inline_reference_is_flagged():
    payload = {
        "report": _full_report() + "据传利好 [S7]。\n",
        "citations": ["S1", "S2"],
        "executive_summary": "",
        "limitations": ["1", "2", "3"],
        "confidence": "low",
    }
    quality = _validate(payload)
    assert quality["citation_consistency"]["unavailable_used"] == ["S7"]
    assert any("不可用" in warning for warning in quality["warnings"])


def test_all_claims_no_source_is_blocker():
    payload = {
        "report": _full_report(),
        "citations": ["S1", "S2"],
        "executive_summary": "",
        "limitations": ["1", "2", "3"],
        "confidence": "low",
        "claims": [
            {"claim_id": "C1", "text": "判断一", "sources": ["S9"], "requires": [], "importance": "high", "confidence": "high", "missing": []},
            {"claim_id": "C2", "text": "判断二", "sources": ["S8"], "requires": [], "importance": "medium", "confidence": "high", "missing": []},
        ],
    }
    quality = _validate(payload)
    codes = {blocker["code"] for blocker in quality["quality_blockers"]}
    assert "claims_all_no_source" in codes
    assert quality["quality_status"] == rq.QUALITY_STATUS_INCOMPLETE


def test_section_bodies_merge_duplicate_headings():
    report = "### 技术面\n第一段内容足够长。\n### 其他\n中间节。\n### 技术面\n第二段补充内容。\n"
    bodies = rq.section_bodies(report)
    tech = next(item for item in bodies if item["name"] == "技术面")
    assert tech["chars"] > 0  # 同名小节正文合并计数


# —— v31 §2.4 修复策略与小节合并（纯函数口径） ——


def test_repair_strategy_selection():
    assert rq.repair_strategy_of(missing_sections=[], summary_missing=False) is None
    assert rq.repair_strategy_of(missing_sections=[], summary_missing=True) == rq.REPAIR_STRATEGY_SUMMARY
    assert rq.repair_strategy_of(missing_sections=["基本面"], summary_missing=False) == rq.REPAIR_STRATEGY_SECTION
    assert rq.repair_strategy_of(missing_sections=["基本面", "消息面"], summary_missing=False) == rq.REPAIR_STRATEGY_FULL_RERUN
    assert rq.repair_strategy_of(missing_sections=["基本面"], summary_missing=True) == rq.REPAIR_STRATEGY_FULL_RERUN


def test_merge_section_replaces_heading_only_section():
    report = "### 技术面\n趋势向下运行中。\n### 基本面\n见上文。\n### 综合判断\n总体偏空观望。\n"
    merged = rq.merge_section(report, "基本面", "### 基本面\n营收下滑，现金流为负，利润质量偏弱。[S4]")
    assert "营收下滑" in merged
    assert "见上文" not in merged
    assert "### 综合判断" in merged  # 后续小节保留
    assert "趋势向下运行中" in merged  # 前序小节保留


def test_merge_section_appends_when_absent():
    report = "### 技术面\n趋势向下运行中。\n"
    merged = rq.merge_section(report, "消息面", "### 消息面\n近30日无重大公告，消息面无催化。[S3]")
    assert "### 消息面" in merged and "近30日无重大公告" in merged
    assert "### 技术面" in merged


def test_merge_section_empty_content_is_noop():
    report = "### 技术面\n趋势向下运行中。\n"
    assert rq.merge_section(report, "消息面", "  ") == report
