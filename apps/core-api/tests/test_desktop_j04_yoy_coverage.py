"""J04（桌面端升级路线图 2026-09-18）：同比/增速纳入覆盖词表的闸门行为测试。

背景（样报 002468 §1.5）：C1 判断正文写「同比增长 128.31%」（新闻口径 S2），但 requires 只有
net_income/cash_flow，同比数字逃过覆盖校验，判断仍得「有支撑/B 级」。J04 之后：
- 单季同比字段（net_income_yoy/revenue_yoy）入 COVERAGE_VOCAB，由 S4 财报数据覆盖；
- 正文含「同比 + 百分比」而未声明同比字段的判断按既有规则降级「未独立验证」；
- 提示词覆盖清单由 SOURCE_PROFILES 生成（prompting.py），改词表自动同步，无需另改。
"""

from __future__ import annotations

from investment_steward_core import prompting, report_quality


def test_yoy_fields_in_vocab_and_s4_coverage():
    assert "net_income_yoy" in report_quality.COVERAGE_VOCAB_SET
    assert "revenue_yoy" in report_quality.COVERAGE_VOCAB_SET
    s4 = report_quality.SOURCE_PROFILES["S4"]
    assert "net_income_yoy" in s4["coverage"] and "revenue_yoy" in s4["coverage"]
    # S2（新闻）刻意不覆盖同比：媒体口径的增速不能冒充财报口径。
    assert "net_income_yoy" not in report_quality.SOURCE_PROFILES["S2"]["coverage"]
    # 提示词覆盖清单从 SOURCE_PROFILES 生成，自动带出（单一来源，不两处手抄）。
    assert "net_income_yoy" in prompting._COVERAGE_REFERENCE


def test_news_only_growth_number_is_downgraded():
    """样报场景复现：正文含同比数字、requires 无同比字段、只引 S2 → 必须降级。"""
    result = report_quality._evaluate_claim_sources(
        ["S2"],
        ["net_income", "cash_flow"],
        allowed={"S2", "S3", "S4", "S5"},
        text="主业进入放量周期，新闻口径为同比增长 128.31%",
    )
    assert result["status"] == "downgraded"
    assert any("同比数字" in note for note in result["notes"])
    # 同比缺口是软缺口：不把 net_income_yoy/revenue_yoy 伪造成 missing 字段清单条目。
    assert "net_income_yoy" not in result["missing"] and "revenue_yoy" not in result["missing"]


def test_declared_yoy_with_s4_is_supported():
    """把 128.31% 换成 S4 单季 yoy 后能升回 B：声明同比字段并引用 S4。"""
    result = report_quality._evaluate_claim_sources(
        ["S4"],
        ["net_income_yoy"],
        allowed={"S2", "S3", "S4", "S5"},
        text="单季净利润同比增长 128.31%（财报口径）",
    )
    assert result["status"] == "supported"
    assert result["missing"] == []


def test_declared_yoy_without_s4_still_downgraded():
    """声明了同比字段但只引 S2：S2 不覆盖同比 → 走既有 missing 降级。"""
    result = report_quality._evaluate_claim_sources(
        ["S2"],
        ["net_income_yoy"],
        allowed={"S2", "S4"},
        text="单季净利润同比增长 128.31%",
    )
    assert result["status"] == "downgraded"
    assert "net_income_yoy" in result["missing"]


def test_yoy_detection_requires_both_marker_and_number():
    """只出现「同比」无数字、或只有百分比无「同比」，都不触发（避免误伤）。"""
    no_number = report_quality._evaluate_claim_sources(
        ["S4"], ["net_income"], allowed={"S4"}, text="利润同比改善，环比提升明显"
    )
    assert no_number["status"] == "supported"

    no_marker = report_quality._evaluate_claim_sources(
        ["S4"], ["net_income"], allowed={"S4"}, text="净利率 12.5%，毛利率提升 3 个百分点"
    )
    assert no_marker["status"] == "supported"


def test_fact_layer_yoy_gap_still_downgraded():
    """fact 层豁免的是等级门槛，不是覆盖缺口：新闻口径同比进 fact 层同样降级。"""
    result = report_quality._evaluate_claim_sources(
        ["S2"],
        ["news_event"],
        allowed={"S2"},
        layer="fact",
        text="媒体报道同比增长 128.31%",
    )
    assert result["status"] == "downgraded"
    assert any("同比数字" in note for note in result["notes"])
