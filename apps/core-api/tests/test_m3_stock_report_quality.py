"""M3（D01–D05）测试：个股研报质量（2026-09-15 22:55 东瑞 001201 样报对照）。

锁定语义（对着 M0 基线的矛盾句与告警原文）：
- D01：`valuation_status=undetermined`（驾驶舱「无法判断」）时，正文不得留「低估/安全垫/偏便宜」
  类结论句——服务端加**对齐标注**（保留事实句、撤回结论语气、不代写结论）。
- D02：S5 同业位置为「高于同业」时，正文不得写「低于同业」或「安全垫」（确定性核对，
  不交给下一次模型自由发挥）。
- D03：验证点时点必须落到日或**带到期窗口的锚定事件**；只到月份但有锚点 → 补「披露后 N 个交易日
  内」；定不出锚点 → 「时点未知」，**绝不编假日期**；原话保留在 `verify_by_raw`。
- D04：摘要超出**目标上界**（165）即触发重写（原策略只在超 180 硬上限时重写）。
- D05：缺口保持缺口——不升来源等级、不为凑 A 级改来源定义。

本文件全部用合成数据，绝不真实联网。
"""

from __future__ import annotations

import pytest

from investment_steward_core import report_quality as rq


_UNDETERMINED_VALUATION = {
    "verdict": "无数据",
    "peer_position": "PS 高于同业中位数",
    "valuation_status": "undetermined",
    "limitations": ["未完成正常化验证"],
}

# M0 基线里的矛盾句原话（§综合判断）。
_DR_CONTRADICTION = (
    "日线级别放量触发多个偏多信号、周线站稳20周均线，反映短期交易资金有做多意愿，"
    "PS处于历史低位也提供了一定的交易安全垫；但基本面上亏损幅度逐季扩大，仍需观察。"
)


def _report(body: str) -> str:
    return (
        "### 技术面\n放量上行，均线多头排列。\n"
        f"### 消息面\n本期无重大公告发布。\n"
        "### 基本面\n亏损逐季扩大，现金流转负。\n"
        f"### 综合判断\n{body}\n"
    )


def _payload(**extra) -> dict:
    base = {
        "report": _report(_DR_CONTRADICTION),
        "citations": ["S1"],
        "confidence": "medium",
        "valuation": dict(_UNDETERMINED_VALUATION),
        "watchpoints": [
            {"signal": "生猪销售", "verify_by": "2026年9月生猪销售简报披露后", "expected_if_true": "出栏量回升"},
        ],
        "claims": [],
        "limitations": ["生猪日度价未接入", "最新机构持仓未取到", "三季报未披露"],
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# D01：正文估值表述对齐 valuation_status
# ---------------------------------------------------------------------------

def test_d01_contradicting_sentence_is_detected_under_undetermined():
    hits = rq.valuation_conclusion_hits(_report(_DR_CONTRADICTION), "undetermined", "")
    assert hits and "安全垫" in "、".join(hits[0]["reasons"])
    assert "PS处于历史低位" in hits[0]["sentence"]


def test_d01_align_adds_note_and_clears_gate():
    aligned, changes = rq.align_valuation_language(_report(_DR_CONTRADICTION), "undetermined", "")
    assert len(changes) == 1
    assert rq.VALUATION_ALIGN_NOTE in aligned
    # 事实句保留（不删原文），只撤回结论语气
    assert "PS处于历史低位" in aligned
    assert rq.valuation_conclusion_hits(aligned, "undetermined", "") == []


def test_d01_no_action_when_status_is_not_undetermined():
    report = _report(_DR_CONTRADICTION)
    assert rq.valuation_conclusion_hits(report, "undervalued", "") == []
    aligned, changes = rq.align_valuation_language(report, "undervalued", "")
    assert aligned == report and changes == []


def test_d01_self_denying_sentences_are_not_touched():
    """「低 PE 分位不等于低估」这类正确表述不能被误伤。"""
    body = "低 PB 分位不等于低估，本期不构成安全垫，估值仅作观察。"
    assert rq.valuation_conclusion_hits(_report(body), "undetermined", "") == []
    aligned, changes = rq.align_valuation_language(_report(body), "undetermined", "")
    assert changes == [] and aligned == _report(body)


def test_d01_validate_report_flags_unrepaired_contradiction():
    quality = rq.validate_report(
        _payload(valuation=dict(_UNDETERMINED_VALUATION, peer_position="")),
        allowed={"S1"},
    )
    assert quality["valuation_language_conflicts"]
    assert any("正文估值表述与结构化口径相反" in w for w in quality["warnings"])
    assert quality["valuation"]["valuation_status"] == "undetermined"      # 驾驶舱仍是「无法判断」


# ---------------------------------------------------------------------------
# D02：S5 比较句不得与来源相反
# ---------------------------------------------------------------------------

def test_d02_peer_above_with_safety_cushion_is_contradiction():
    """验收原文：S5=PS 高于同业 + 正文含安全垫 → 命中（可被改写）。"""
    report = _report("公司 PS 低于同业中位数，具备安全边际与安全垫。")
    hits = rq.valuation_conclusion_hits(report, "fair", "PS 高于同业中位数")
    assert hits
    reasons = "、".join(hits[0]["reasons"])
    assert "高于同业" in reasons and "低于同业" in reasons


def test_d02_align_marks_both_reasons_and_is_idempotent():
    report = _report("公司 PS 低于同业中位数，具备安全垫。")
    aligned, changes = rq.align_valuation_language(report, "fair", "PS 高于同业中位数")
    assert changes and rq.PEER_CONTRADICT_NOTE in aligned
    assert rq.valuation_conclusion_hits(aligned, "fair", "PS 高于同业中位数") == []
    again, changes2 = rq.align_valuation_language(aligned, "fair", "PS 高于同业中位数")
    assert again == aligned and changes2 == []


def test_d02_peer_position_parser_rejects_negation():
    assert rq.peer_position_is_above("高于同业中位数") is True
    assert rq.peer_position_is_above("不高于同业中位数") is False
    assert rq.peer_position_is_above("低于同业") is False
    assert rq.peer_position_is_above("") is False


def test_d02_no_action_when_peer_is_below():
    report = _report("公司 PS 低于同业中位数，具备安全垫。")
    assert rq.valuation_conclusion_hits(report, "fair", "PS 低于同业中位数") == []


def test_d02_pb_below_peer_is_not_a_ps_contradiction():
    """G05 真实样报回归发现的假阳性：同句写「PB 低于同业」但 S5 讲的是 **PS**。

    东瑞原文：「PB 处于历史中等偏低位置、显著低于同业中位数，但 PS 处于历史极低分位、
    高于同业中位数」——PB 那句是真话且与 PS 口径无关，不该被标成「与来源相反」。
    """
    probe = "PB 处于历史中等偏低位置、显著低于同业中位数，但 PS 处于历史极低分位、高于同业中位数"
    assert rq.peer_contradiction_words(probe) == []
    # 贴近 PS 的「低于同业」仍要命中（重叠词一并列出，不丢证据）
    assert set(rq.peer_contradiction_words("公司 PS 低于同业中位数")) == {
        "低于同业",
        "低于同业中位数",
    }
    # 「安全垫」不受 PS 邻近限制（它本身就是结论禁词）
    assert rq.peer_contradiction_words("PS 处于历史低位提供安全垫") == ["安全垫"]


# —— R01（2026-09-18 排版与研报呈现一致性路线图）：D02 必须认准指标 ——

def test_d02_peer_above_metrics_parses_named_metric_per_clause():
    # 样报 B `:57`：合并串讲的是 **PE** 高于同业，不是 PS。
    assert rq.peer_above_metrics("高于工业金属同业PE(TTM)中位数8.76（11只可比）") == {"pe"}
    # 未点名指标的「高于」退回 PS（D02 原始默认，向后兼容）。
    assert rq.peer_above_metrics("高于同业中位数") == {"ps"}
    # 「不高于」不计入。
    assert rq.peer_above_metrics("不高于同业中位数") == set()
    # 多指标：PE 与 PS 分别高于、PB 低于 → 只有被点名的高于指标入集合。
    assert rq.peer_above_metrics("PE 高于同业，PB 低于同业，PS 高于同业") == {"pe", "ps"}


def test_d02_peer_above_pe_does_not_flag_ps_below_real_sentence():
    """样报 B 回归：S5 是 PE 高于同业，正文「PB/PS 低于同业中位数」是真话，不得加注。

    旧实现只看合并串里有无「高于」，把 PE 的「高于」误当成 PS，逼出自相矛盾的 D02 注记。
    """
    peer_position = "高于工业金属同业PE(TTM)中位数8.76（11只可比）"
    body = (
        "本股PE(TTM)高于同业中位数约126.9%，PB/PS低于同业中位数。"
        "PS 0.38、同业中位 1.35，PB/PS 低于同业中位数。"
    )
    assert rq.valuation_conclusion_hits(_report(body), "undetermined", peer_position) == []
    aligned, changes = rq.align_valuation_language(_report(body), "undetermined", peer_position)
    # 仅剩 D01（无法判断）标注，绝不出现 D02 的「PS 高于同业」矛盾注记。
    assert "S5 显示" not in aligned
    assert all(_D02_NOTE not in c["sentence"] for c in changes)


def test_d02_peer_above_ps_flags_ps_below_with_metric_named_note():
    """把 peer_position 改成 PS 高于同业时，正文的 PS 低于必须命中，且注记写明是 PS。"""
    peer_position = "高于工业金属同业PS(TTM)中位数1.35（20只可比）"
    body = "本股 PB/PS 低于同业中位数。"
    hits = rq.valuation_conclusion_hits(_report(body), "fair", peer_position)
    assert hits
    aligned, changes = rq.align_valuation_language(_report(body), "fair", peer_position)
    assert "PS 高于同业中位数" in aligned
    assert rq._D02_NOTE_MARKER in aligned
    # 幂等：重复执行不再叠加。
    again, changes2 = rq.align_valuation_language(aligned, "fair", peer_position)
    assert again == aligned and changes2 == []


_D02_NOTE = "S5 显示"


def test_d01_denial_enumeration_is_not_a_conclusion():
    """G05 真实样报回归发现的假阳性：「无法简单判定估值为偏贵、合理或偏便宜」。

    句子本身已自我否认；若把它当结论句，服务端会给一句正确的合规句加「不成立」标注。
    """
    probe = (
        "当前无法简单判定估值为偏贵、合理或偏便宜，需等待盈利趋势验证。"
    )
    assert rq.is_valuation_denial(probe) is True
    assert rq.valuation_conclusion_hits(_report(probe), "undetermined", "") == []
    # 转折句不被放过：否定只覆盖到转折词之前
    turn = "无法判断当前估值，但 PS 处于历史低位提供安全垫。"
    assert rq.is_valuation_denial(turn) is False
    assert rq.valuation_conclusion_hits(_report(turn), "undetermined", "")
    # 无否定的普通结论句照旧命中
    assert rq.valuation_conclusion_hits(_report("PS 处于历史低位提供安全垫。"), "undetermined", "")


def test_d01_g03_real_denial_sentences_are_not_annotated():
    """G03 真实重跑（2026-09-15，001201 正文原话）暴露的同族假阳性三条。

    这三句是**正确的否认句**，却因禁词字面命中被加了「本句不成立」标注——对合规句加标注
    等于给用户制造噪声，且会让「正文无结论句」无法干净成立。逐句锁定：
    """
    cases = [
        # 「不能…证明低估」：推理动词不止 判定/判断/给出/确定
        "两个分位数方向矛盾：PB低但PS偏高，叠加公司连续亏损，低PB不能单独证明低估",
        # 「无法得出低估结论」
        "估值方面，PE(TTM)、PE(静态)、PCF(TTM)均为负，PB低但PS高于同业，正常化盈利未验证前无法得出低估结论 [S5]",
        # 「而非偏便宜」：对比否定，紧跟禁词
        "正常化盈利未由现有数据验证，估值结论应为undetermined而非偏便宜/偏贵 [S5][S4]",
        # 「不能单独作为低估依据」（反方原文）
        "低PB分位不能单独作为低估依据，PS偏高与盈利为负形成矛盾，正常化盈利未验证前估值应写undetermined",
    ]
    for probe in cases:
        assert rq.is_valuation_denial(probe) is True, probe
        assert rq.valuation_conclusion_hits(_report(probe), "undetermined", "") == [], probe
        aligned, changes = rq.align_valuation_language(_report(probe), "undetermined", "")
        assert changes == [] and aligned == _report(probe), probe


def test_d01_half_denial_sentence_is_not_wholly_exempted():
    """逐处判定：句内一部分禁词被否认，另一部分没有 → 整句仍须命中。

    否则「…应为undetermined而非偏便宜，但 PS 提供安全垫」会靠前半句的对比否定把后半句的
    真结论一起放过去。
    """
    probe = "估值应为undetermined而非偏便宜，但 PS 提供安全垫。"
    assert rq.is_valuation_denial(probe) is False
    hits = rq.valuation_conclusion_hits(_report(probe), "undetermined", "")
    assert hits and "安全垫" in "、".join(hits[0]["reasons"])
    assert "偏便宜" not in "、".join(hits[0]["reasons"])   # 被否认的那处不再计为结论词


def test_d01_negation_with_infer_verb_is_not_annotated():
    """G03 最终重跑（4f2599b2）真实正文：动词表缺「推断」导致该否定句被加标注。

    「…正常化盈利未验证，低PB/PS分位不能推断低估」——否定 + 推断动词 + 禁词，
    是自我否认句；修复后不得再打 D01 行内标注。
    """
    probe = "综合判断为“无数据/无法判断”，因为正常化盈利未验证，低PB/PS分位不能推断低估"
    assert rq.is_valuation_denial(probe) is True
    assert rq.undoubted_ban_phrases(probe) == []
    hits = rq.valuation_conclusion_hits(_report(probe), "undetermined", "")
    assert hits == []
    aligned, changes = rq.align_valuation_language(_report(probe), "undetermined", "")
    assert changes == [] and aligned == _report(probe)


# ---------------------------------------------------------------------------
# D03：验证点必须落到日或锚定事件
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026年9月生猪销售简报披露后", ("2026年9月生猪销售简报披露后 5 个交易日内", "anchored_window")),
        ("9月生猪销售简报披露后", ("9月生猪销售简报披露后 5 个交易日内", "anchored_window")),
        ("20月均线", ("20月均线", "")),                                    # 技术指标写法不误判
        ("2026-10 前", (rq.VERIFY_BY_UNKNOWN_TEXT, "unknown_timepoint")),
        ("下月", (rq.VERIFY_BY_UNKNOWN_TEXT, "unknown_timepoint")),
        ("2026-10-31", ("2026-10-31", "")),                                # 已有日期
        ("三季报披露后", ("三季报披露后", "")),                              # 已是锚定事件
        ("三季报披露后 5 个交易日内", ("三季报披露后 5 个交易日内", "")),      # 已带窗口
    ],
)
def test_d03_repair_verify_by(value, expected):
    assert rq.repair_verify_by(value) == expected


def test_d03_repaired_watchpoint_no_longer_month_only():
    """验收：今晚那条「只写到月份」的告警消失（锚点 + 到期窗口即可安排跟踪）。"""
    parsed = _payload()
    repairs = rq.repair_watchpoints(parsed)
    assert repairs and repairs[0]["reason"] == "anchored_window"
    assert parsed["watchpoints"][0]["verify_by"] == "2026年9月生猪销售简报披露后 5 个交易日内"
    assert parsed["watchpoints"][0]["verify_by_raw"] == "2026年9月生猪销售简报披露后"
    quality = rq.validate_report(parsed, allowed={"S1"})
    assert not any("只写到月份" in w for w in quality["warnings"])


def test_d03_unknown_timepoint_is_not_a_fake_date():
    parsed = _payload(watchpoints=[
        {"signal": "生猪日度价", "verify_by": "2026-10 前", "expected_if_true": "x"},
        {"signal": "机构持仓", "verify_by": "后续观察", "expected_if_true": "y"},
    ])
    repairs = rq.repair_watchpoints(parsed)
    assert [item["reason"] for item in repairs] == ["unknown_timepoint", "unknown_timepoint"]
    values = [item["verify_by"] for item in parsed["watchpoints"]]
    assert values == [rq.VERIFY_BY_UNKNOWN_TEXT, rq.VERIFY_BY_UNKNOWN_TEXT]
    # 不得出现任何日期
    assert not any(ch.isdigit() for ch in values[0])
    quality = rq.validate_report(parsed, allowed={"S1"})
    assert not any("只写到月份" in w for w in quality["warnings"])
    assert not any("相对时间词" in w for w in quality["warnings"])


def test_d03_specific_verify_by_is_untouched():
    parsed = _payload(watchpoints=[
        {"signal": "a", "verify_by": "2026-10-31", "expected_if_true": "x"},
        {"signal": "b", "verify_by": "三季报披露后", "expected_if_true": "y"},
    ])
    assert rq.repair_watchpoints(parsed) == []
    assert [item["verify_by"] for item in parsed["watchpoints"]] == ["2026-10-31", "三季报披露后"]


def test_d03_bare_month_anchor_is_not_a_timepoint():
    """真实 G03 重跑暴露（report_id ecf4c835）：模型写「9月生猪销售简报披露后」。

    旧口径只认「4 位年份 + 月」，该写法既不告警也不补窗口 → 从闸门漏过。
    本用例把它钉进「粒度不足 + 锚定事件」分支：告警可见，补到期窗口后消失（不编日期）。
    """
    value = "9月生猪销售简报披露后"
    assert rq.month_only_verify_by(value) is True

    raw = _payload(watchpoints=[{"signal": "生猪销售简报", "verify_by": value, "expected_if_true": "x"}])
    assert any("只写到月份" in w for w in rq.validate_report(raw, allowed={"S1"})["warnings"])

    repairs = rq.repair_watchpoints(raw)
    assert [item["reason"] for item in repairs] == ["anchored_window"]
    assert raw["watchpoints"][0]["verify_by"] == "9月生猪销售简报披露后 5 个交易日内"
    assert raw["watchpoints"][0]["verify_by_raw"] == value
    assert not any("只写到月份" in w for w in rq.validate_report(raw, allowed={"S1"})["warnings"])


# ---------------------------------------------------------------------------
# D04：摘要压回 150–165
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "chars,expected",
    [(120, False), (165, False), (166, True), (176, True), (181, True)],
)
def test_d04_needs_summary_rewrite_between_target_and_hard_max(chars, expected):
    summary = "摘" * chars
    assert rq.needs_summary_rewrite(summary) is expected
    # 176 字（东瑞实测）在原策略下不触发重写、在新策略下必须触发
    if chars == 176:
        assert rq.report_char_count(summary) > rq.SUMMARY_TARGET_MAX


def test_d04_validate_report_keeps_format_warning_when_still_over_target():
    quality = rq.validate_report(_payload(executive_summary="摘" * 176), allowed={"S1"})
    assert any("超出目标区间" in w for w in quality["warnings"])
    assert quality["summary_budget"]["target_max"] == 165


# ---------------------------------------------------------------------------
# D05：缺口保持缺口（不升来源等级）
# ---------------------------------------------------------------------------

def test_d05_source_grades_unchanged_no_a_grade_inflation():
    """D05：本轮不为凑 A 级改来源定义——公告源仍是 B 级，证据质量如实呈现「A 级无」。"""
    assert rq.source_profile("S3")["quality"] == rq.QUALITY_B
    assert rq.SOURCE_PROFILES["S3"]["label"]
    report = rq.evidence_quality_report(["S1", "S3", "S5"])
    assert rq.QUALITY_A not in {item["quality"] for item in report["sources"]}
    assert report["tiers"][rq.QUALITY_A] == []
    assert "A 级" in report["note"]
