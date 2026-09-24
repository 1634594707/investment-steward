"""P2-C04：个股研报 S6 接入的**回归边界**测试——全程不联网。

这个文件守的是路线图里最容易被破坏的一条约束：

> 未上榜的股票，五类来源（S1–S5）必须**逐字不变**。

S6 与 S1–S5 的本质差别不是「可能失败」，而是「没上榜就压根不该存在」。
一旦有人把它写成「总是尝试、失败记缺口」，每只没上榜的股票都会背上一条假缺口。
"""

from __future__ import annotations

import json

from investment_steward_core import report_quality

# —— 来源登记：S6 必须可缺省，且不得被当缺口 ——


def test_s6_is_registered_as_optional_source():
    assert report_quality.SEAT_SOURCE_ID == "S6"
    assert "S6" in report_quality.OPTIONAL_SOURCE_IDS


def test_s6_is_flagged_conditional():
    """S6 缺省属正常，与 S2–S5 的「取数失败」不是一回事，因此必须单独标注。"""
    assert report_quality.CONDITIONAL_SOURCE_IDS == ("S6",)
    for sid in ("S2", "S3", "S4", "S5"):
        assert sid not in report_quality.CONDITIONAL_SOURCE_IDS


def test_s1_stays_always_present():
    assert report_quality.TECH_SOURCE_ID == "S1"
    assert "S1" not in report_quality.OPTIONAL_SOURCE_IDS


def test_s6_profile_is_b_tier_and_carries_no_probability_fields():
    """东财是转载源 → B 级；且 coverage 里不得有任何胜率/概率类字段。"""
    profile = report_quality.source_profile("S6")
    assert profile["quality"] == report_quality.QUALITY_B
    assert profile["kind"] == "seat"
    assert profile["coverage"] == ()
    joined = json.dumps(profile, ensure_ascii=False).lower()
    for banned in ("probability", "rise_probability", "success_rate", "胜率", "成功率"):
        assert banned not in joined


def test_s6_label_states_it_is_a_republication():
    """局限口径必须能从来源标签读出来：转载自东财，原始披露为交易所。"""
    label = str(report_quality.source_profile("S6")["label"])
    assert "转载" in label
    assert "交易所" in label


def test_unknown_source_still_falls_back_to_d_tier():
    """未登记来源仍按 D 级处理，S6 的加入没有放宽这条兜底。"""
    profile = report_quality.source_profile("S99")
    assert profile["quality"] == report_quality.QUALITY_D
    assert profile["coverage"] == ()


def test_evidence_quality_report_includes_s6_only_when_cited():
    """来源分级明细只列实际出现的来源：没有 S6 时不列出，不产生空条目。"""
    without = report_quality.evidence_quality_report(["S1", "S2"])
    assert "S6" not in {item["id"] for item in without["sources"]}

    with_s6 = report_quality.evidence_quality_report(["S1", "S6"])
    listed = {item["id"]: item for item in with_s6["sources"]}
    assert "S6" in listed
    assert listed["S6"]["quality"] == report_quality.QUALITY_B


def test_staleness_table_covers_s6():
    assert "S6" in report_quality.STALENESS_MAX_AGE_DAYS


# —— 提示词：S6 是条件来源，不能让模型否认自己的证据 ——


def test_prompt_no_longer_claims_exactly_five_sources():
    """旧措辞写死「只提供…五类来源」，有了 S6 会让模型把真实证据说成不存在。"""
    from investment_steward_core import prompting

    rules = prompting.STOCK_REPORT_RULES_TEXT
    assert "五类来源" not in rules
    assert "S6" in rules
    assert "未上榜" in rules


def test_prompt_forbids_inferring_no_hot_money_from_missing_s6():
    from investment_steward_core import prompting

    rules = prompting.STOCK_REPORT_RULES_TEXT
    assert "游资没进" in rules


# —— S6 组装：未上榜 → None（不生成空表） ——


def test_seat_summary_none_when_not_listed(youzi_fixture_text):
    from investment_steward_core import lhb_feed as lf
    from investment_steward_core import seat_book as sb

    billboard = lf.parse_billboard_rows(
        json.loads(youzi_fixture_text("em_lhb_details_20260916.json"))
    )
    seats = lf.parse_seat_rows(
        json.loads(youzi_fixture_text("em_lhb_detailbuy_20260916.json")), lf.DIRECTION_BUY
    )
    watchlist = sb.load_watchlist()

    assert sb.build_billboard_summary("300001", billboard, seats, watchlist) is None
    assert sb.build_billboard_summary("688432", billboard, seats, watchlist) is not None


def test_seat_summary_exists_for_a_listed_code(youzi_fixture_text):
    from investment_steward_core import lhb_feed as lf
    from investment_steward_core import seat_book as sb

    billboard = lf.parse_billboard_rows(
        json.loads(youzi_fixture_text("em_lhb_details_20260916.json"))
    )
    watchlist = sb.load_watchlist()
    text = sb.build_billboard_summary("002281", billboard, [], watchlist)
    assert text is not None
    assert "002281" in text


def test_summary_never_states_absence_of_hot_money(youzi_fixture_text):
    """即便零命中，措辞也不得**断言**「游资没进」。

    「游资没进」这四个字只允许出现在**否定措辞**里（`不表示「游资没进」`），
    用来提醒读者别做反向推断；不允许作为结论句出现。
    """
    from investment_steward_core import lhb_feed as lf
    from investment_steward_core import seat_book as sb

    billboard = lf.parse_billboard_rows(
        json.loads(youzi_fixture_text("em_lhb_details_20260916.json"))
    )
    watchlist = sb.load_watchlist()
    # 只给榜单、不给席位明细 → 观察名单必然零命中
    text = sb.build_billboard_summary("002281", billboard, [], watchlist)
    assert text is not None
    assert "未命中" in text

    for line in text.splitlines():
        if "游资没进" in line:
            assert "不表示" in line, f"「游资没进」只能出现在否定措辞里：{line}"
