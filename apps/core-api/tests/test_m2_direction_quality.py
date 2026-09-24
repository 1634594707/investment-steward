"""M2（C01–C06）测试：方向研判质量（2026-09-15 22:55「低估板块」样报对照）。

锁定语义（全部对着 M0 基线里的告警原文）：
- C01：反方审查修订说明**不计入**执行摘要字数预算——样报里「150–220 字摘要被拼成 438 字、
  触发 260 硬上限」的告警必须消失；修订说明另立字段、单独计数，原文摘要一字不改。
- C02：`core_judgments` 必须有 `kind=industry` 一条；缺则补一条 **low 置信 + 明写「证据未覆盖」**
  的兜底条（不编景气数据）；已有的产业景气条若断言反转/修复却无中间环节证据 → 降 low + 缺口尾注。
- C03：跨层断言（利率→毛利 / CPI→盈利）就地改写为「【待验证假设】…」；已带假设标记的句子不算断言。
- C04：点名板块却引用不可核对的分类判定 → 就地改写为**不带分类词**的描述（只改命中小节）。
- C05：引用的数值不在被引条目里 → 就地**去掉该引用**（保留事实句，不保留假引用）。
- C06：PE 分位 − PB 分位背离 30–40pp → 「临界」标记（标签不变）；被写成主线（摘要最前段或
  同句「最完整/首选/优先级最高」）→ 告警 + 降 needs_review。

本文件全部用合成数据，绝不真实联网。
"""

from __future__ import annotations

import pytest

from investment_steward_core import direction_research as dr


# ---------------------------------------------------------------------------
# 合成载荷
# ---------------------------------------------------------------------------

def _full_report(extra_by_section: dict[str, str] | None = None) -> str:
    extra = extra_by_section or {}
    lines: list[str] = []
    for name in dr.REQUIRED_DIRECTION_SECTIONS:
        body = extra.get(name, "本节的推理与边界说明：给出可核对的数据与不确定性。")
        lines.append(f"## {name}\n{body}\n")
    return "\n".join(lines)


def _payload(**overrides) -> dict:
    base = {
        "title": "低估板块方向研判",
        "executive_summary": "摘要：" + "方向判断与最强证据与最大不确定性。" * 8,
        "report": _full_report(),
        "core_judgments": [
            {"id": "J1", "text": "低 PB 横截面可比", "kind": "fact",
             "support_refs": ["E1"], "confidence": "medium"},
            {"id": "J2", "text": "公司竞争力稳定", "kind": "competitiveness",
             "support_refs": [], "confidence": "medium"},
            {"id": "J3", "text": "资金情绪偏弱", "kind": "sentiment",
             "support_refs": [], "confidence": "low"},
        ],
        "catalysts": ["需求回补"], "risks": ["供给过剩"],
        "stock_pool": [], "data_gaps": ["行业月度数据未接入"],
        "next_verification": "跟踪月度运行情况公告",
    }
    base.update(overrides)
    return base


def _counter(**overrides) -> dict:
    base = {
        "ok": True, "requested": True, "verdict_robustness": "mixed",
        "strongest_counter": "需求前置透支，件量增速与收入增速背离",
        "affected_conclusions": [{"id": "J2", "effect": "weakens", "reason": "中间数据缺失"}],
        "pool_challenges": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# C01：反方修订说明不计入摘要字数预算
# ---------------------------------------------------------------------------

def test_c01_counter_check_records_revision_separately():
    parsed = _payload(executive_summary="方向判断：先研究轨交设备Ⅱ。")
    out, record = dr.apply_counter_check(parsed, _counter())
    assert record["applied"] is True
    # 修订说明单独成字段；正文摘要原话仍在（一字不改）
    assert out["executive_summary_revision"].startswith(dr.COUNTER_SUMMARY_PREFIX)
    assert out["executive_summary"] == f"{out['executive_summary_revision']}方向判断：先研究轨交设备Ⅱ。"


def test_c01_summary_budget_excludes_revision_and_hard_warning_disappears():
    """样报场景复现：原文摘要 200 字 + 修订说明 ~180 字 = 438 字，但预算只看 200 字。"""
    base_summary = "方向判断与最强证据与最大不确定性。" * 11  # 198 字
    parsed = _payload(executive_summary=base_summary)
    out, _ = dr.apply_counter_check(parsed, _counter())
    total = len(dr._WHITESPACE.sub("", out["executive_summary"]))
    assert total > dr.DIRECTION_SUMMARY_HARD_MAX          # 拼起来确实超 260
    quality = dr.validate_direction(out, mode="standard", research_mode="knowledge", evidence_ids=set())
    assert quality["summary_chars"] == len(dr._WHITESPACE.sub("", base_summary))
    assert quality["summary_revision_chars"] > 0
    assert quality["summary_chars_total"] == total
    assert not any("超过 260 字上限" in w for w in quality["quality_warnings"])


def test_c01_summary_overflow_still_flagged_when_body_summary_itself_too_long():
    """预算放宽只针对修订说明：原文摘要自身超限仍然要告警（不放过真超限）。"""
    parsed = _payload(executive_summary="摘" * (dr.DIRECTION_SUMMARY_HARD_MAX + 12))
    out, _ = dr.apply_counter_check(parsed, _counter())
    quality = dr.validate_direction(out, mode="standard", research_mode="knowledge", evidence_ids=set())
    assert any(f"执行摘要 {dr.DIRECTION_SUMMARY_HARD_MAX + 12} 字" in w for w in quality["quality_warnings"])


def test_c01_legacy_prefixed_summary_splits_at_strongest_counter():
    """历史稿件（只有拼接文本、无独立字段）也能切出修订说明——按「最强反方论点：…。」切。"""
    revision = f"{dr.COUNTER_SUMMARY_PREFIX}经反方审查，撤下 1 条判断（J1）。最强反方论点：需求前置透支。"
    parsed = {"executive_summary": f"{revision}方向判断：先研究轨交设备Ⅱ。"}
    got_revision, body = dr.split_summary_revision(parsed)
    assert got_revision == revision
    assert body == "方向判断：先研究轨交设备Ⅱ。"


def test_c01_unseparable_legacy_summary_counts_in_full():
    """切不出可靠边界就不切（宁严不松）：整段计入预算。"""
    parsed = {"executive_summary": f"{dr.COUNTER_SUMMARY_PREFIX}经反方审查，撤下 1 条判断（J1）。"}
    revision, body = dr.split_summary_revision(parsed)
    assert revision == ""
    assert body == parsed["executive_summary"]


# ---------------------------------------------------------------------------
# C02：产业景气判断层
# ---------------------------------------------------------------------------

def test_c02_missing_industry_layer_is_added_as_low_confidence_gap_entry():
    parsed = _payload(core_judgments=[
        {"id": "J1", "text": "低 PB 横截面可比", "kind": "fact", "support_refs": ["E1"]},
        {"id": "J2", "text": "公司竞争力稳定", "kind": "competitiveness", "support_refs": []},
        {"id": "J3", "text": "资金情绪偏弱", "kind": "sentiment", "support_refs": []},
    ])
    record = dr.ensure_industry_layer(parsed, evidence_index={"E1": "板块 PB 分位 12.0%"})
    assert record["added"] is True
    entry = next(item for item in parsed["core_judgments"] if item["kind"] == "industry")
    assert entry["id"] == record["added_id"]
    assert entry["confidence"] == "low"
    assert "证据未覆盖" in entry["text"]
    assert "不能从低 PB 推出景气修复" in entry["text"]
    assert entry["support_refs"] == []
    # 不得冒充已验证的盈利反转
    assert "反转" not in entry["text"]


def test_c02_added_layer_clears_validate_warning():
    parsed = _payload(core_judgments=[
        {"id": "J1", "text": "低 PB 横截面可比", "kind": "fact", "support_refs": ["E1"]},
        {"id": "J2", "text": "公司竞争力稳定", "kind": "competitiveness", "support_refs": []},
        {"id": "J3", "text": "资金情绪偏弱", "kind": "sentiment", "support_refs": []},
    ])
    before = dr.validate_direction(parsed, mode="standard", research_mode="knowledge", evidence_ids=set())
    assert any("核心判断缺少「产业景气」层" in w for w in before["quality_warnings"])
    dr.ensure_industry_layer(parsed, evidence_index=None)
    after = dr.validate_direction(parsed, mode="standard", research_mode="knowledge", evidence_ids=set())
    assert not any("核心判断缺少「产业景气」层" in w for w in after["quality_warnings"])


def test_c02_reversal_claim_without_bridge_evidence_is_demoted():
    """产业景气条断言「景气修复」但引用条目里没有任何中间环节数据 → 降 low + 加缺口尾注。"""
    parsed = _payload(core_judgments=[
        {"id": "J1", "text": "轨交设备Ⅱ景气修复已经确立", "kind": "industry",
         "support_refs": ["E1"], "confidence": "high"},
        {"id": "J2", "text": "公司竞争力稳定", "kind": "competitiveness", "support_refs": []},
        {"id": "J3", "text": "资金情绪偏弱", "kind": "sentiment", "support_refs": []},
    ])
    record = dr.ensure_industry_layer(
        parsed, evidence_index={"E1": "轨交设备Ⅱ PB 分位 12.0%，PE 分位 48.9%"}
    )
    assert record["demoted_ids"] == ["J1"]
    entry = next(item for item in parsed["core_judgments"] if item["id"] == "J1")
    assert entry["confidence"] == "low"
    assert dr.INDUSTRY_UNSUPPORTED_TAIL in entry["text"]
    assert record["added"] is False


def test_c02_reversal_claim_with_bridge_evidence_is_kept():
    parsed = _payload(core_judgments=[
        {"id": "J1", "text": "轨交设备Ⅱ景气修复", "kind": "industry",
         "support_refs": ["E1"], "confidence": "high"},
    ])
    record = dr.ensure_industry_layer(
        parsed, evidence_index={"E1": "轨交设备Ⅱ 订单同比 +18.4%，产能利用率 82%"}
    )
    assert record["demoted_ids"] == []
    assert parsed["core_judgments"][0]["confidence"] == "high"


def test_c02_no_judgments_does_not_invent_layer():
    """没有核心判断时由硬阻断处理，不靠补一条判断掩盖空判断（不掩盖真问题）。"""
    parsed = _payload(core_judgments=[])
    record = dr.ensure_industry_layer(parsed, evidence_index=None)
    assert record["added"] is False and record["reason"]


# ---------------------------------------------------------------------------
# C03：跨层断言 → 待验证假设
# ---------------------------------------------------------------------------

def test_c03_cross_layer_sentence_rewritten_into_hypothesis():
    report = _full_report({
        "下一验证动作": "若政策利率继续下行，轨交设备Ⅱ的毛利将确定性改善。",
    })
    hits = dr.find_cross_layer_claims(report)
    assert len(hits) == 1
    new_report, changes = dr.demote_cross_layer_sentences(report, hits)
    assert len(changes) == 1
    assert dr.CROSS_LAYER_HYPOTHESIS_MARK in new_report
    assert dr.CROSS_LAYER_HYPOTHESIS_TAIL in new_report
    # 改写后不再是「断言」：闸门不再命中
    assert dr.find_cross_layer_claims(new_report) == []


def test_c03_rewrite_is_idempotent_and_scoped_to_hit_section():
    report = _full_report({
        "下一验证动作": "若政策利率继续下行，轨交设备Ⅱ的毛利将确定性改善。",
        "景气阶段": "本节引用 [E2] 的估值数值。",
    })
    hits = dr.find_cross_layer_claims(report)
    once, _ = dr.demote_cross_layer_sentences(report, hits)
    twice, changes = dr.demote_cross_layer_sentences(once, hits)
    assert twice == once and changes == []
    # 未命中小节一字不动
    assert "本节引用 [E2] 的估值数值。" in twice


def test_c03_hypothesis_marker_already_present_is_not_a_claim():
    report = _full_report({
        "下一验证动作": "待验证假设：利率下行或推动毛利改善，须补单票收入证据。",
    })
    assert dr.find_cross_layer_claims(report) == []


def test_c03_bridge_evidence_still_clears_claim():
    report = _full_report({
        "下一验证动作": "利率下行叠加运价回升、订单增长，毛利有望改善。",
    })
    assert dr.find_cross_layer_claims(report) == []


def test_c03_gross_margin_is_not_the_policy_rate():
    """G02/G05 真实样报回归发现的假阳性：「毛利率」含子串「利率」。

    M0 与 G02 重跑里 2/3 条「利率 → 毛利」告警其实来自「验证…营收与毛利率是否改善」
    这类**验证动作**句；若不修，`apply_quality_repairs` 会把正常的工作项改写成
    「待验证假设」，属于把句子改坏——而不是把断言改软。
    """
    assert dr.macro_terms_in("验证轨交设备Ⅱ的营收与毛利率是否改善") == []
    assert dr.macro_terms_in("净利率与费用率同比改善") == []
    assert dr.is_cross_layer_claim("验证轨交、白电、综合Ⅱ代表股营收与毛利率是否改善") is False
    # 真·利率仍然要命中（前置字不是财务比率语素）
    assert dr.macro_terms_in("若政策利率继续下行") == ["利率"]
    assert dr.is_cross_layer_claim("若政策利率继续下行，轨交设备Ⅱ的毛利将确定性改善")
    # 假阳性句进正文也不会被改写
    report = _full_report({"下一验证动作": "重点看轨交设备Ⅱ的营收、毛利率、合同负债。"})
    fixed = dr.apply_quality_repairs(
        {"report": report, "core_judgments": [], "catalysts": [], "risks": []}
    )
    assert fixed["cross_layer_demotions"] == []


def test_c03_meta_denial_sentence_is_not_a_claim():
    """明确写「不以宏观替代行业验证」的**元陈述**也不是断言（M0 告警 8 的真实句）。"""
    meta = "日本CPI [E6]窗口未开始，若作宏观背景判断，须查总务省官方发布，但不宜替代板块盈利验证"
    assert dr.is_cross_layer_claim(meta) is False
    assert dr.find_cross_layer_claims(_full_report({"下一验证动作": meta + "。"})) == []
    # 否定不能反过来放过真断言：普通否定句（无 meta 词组）仍要命中
    assert dr.is_cross_layer_claim("CPI 回落将拖累企业盈利表现") is True


# ---------------------------------------------------------------------------
# C04：引用不可核对的分类判定 → 去分类词
# ---------------------------------------------------------------------------

_BOARD_EVIDENCE = {"轨交设备Ⅱ": {"E3"}}


def test_c04_unsupported_classification_words_are_neutralized():
    report = _full_report({
        "催化与反证": "轨交设备Ⅱ属低估候选，高位板块的资金正在流出。",
    })
    problems = dr.judgment_refs_cover_boards(dr.find_judgment_claims(report), _BOARD_EVIDENCE)
    assert problems and problems[0]["reason"] == "no_refs"
    new_report, changes = dr._rewrite_sections(
        report, {str(item["section"]) for item in problems}, dr._judgment_claim_rewriter
    )
    assert changes and changes[0]["section"] == "催化与反证"
    section = next(item for item in dr.section_texts(new_report) if item["name"] == "催化与反证")
    for word in dr.VALUATION_JUDGMENT_CLAIM_WORDS:
        assert word not in str(section["text"])
    # 改写后闸门清空（分类判定不再以断言形式出现）
    assert dr.judgment_refs_cover_boards(dr.find_judgment_claims(new_report), _BOARD_EVIDENCE) == []


def test_c04_supported_classification_is_untouched():
    report = _full_report({
        "催化与反证": "轨交设备Ⅱ属低估候选 [E3]，PB 分位 12.0%。",
    })
    problems = dr.judgment_refs_cover_boards(dr.find_judgment_claims(report), _BOARD_EVIDENCE)
    assert problems == []
    assert "低估候选" in report


def test_c04_rewrite_does_not_touch_other_sections():
    report = _full_report({
        "催化与反证": "轨交设备Ⅱ属低估候选。",
        "候选公司差异": "候选中属于低估候选的比例为 3/6 [E3]。",
    })
    problems = dr.judgment_refs_cover_boards(dr.find_judgment_claims(report), _BOARD_EVIDENCE)
    new_report, _ = dr._rewrite_sections(
        report, {str(item["section"]) for item in problems}, dr._judgment_claim_rewriter
    )
    kept = next(item for item in dr.section_texts(new_report) if item["name"] == "候选公司差异")
    assert "低估候选" in str(kept["text"])


# ---------------------------------------------------------------------------
# C05：引用数字必须落在被引条目内
# ---------------------------------------------------------------------------

def test_c05_mismatched_reference_is_removed_but_fact_kept():
    # 注：E6 内容里刻意不出现数字 4（否则「4」成为条目子串而误判为已对应）。
    index = {"E6": "轨交设备Ⅱ PB 中位数 1.23（截至 2026-09-11）"}
    report = _full_report({
        "下一验证动作": "关注 4 家整车厂的招标节奏 [E6]。",
    })
    problems = dr.check_reference_support(report, index)
    assert len(problems) == 1 and problems[0]["refs"] == ["E6"]
    new_report, changes = dr._rewrite_sections(
        report, {str(item["section"]) for item in problems}, dr._reference_rewriter(index)
    )
    assert changes and changes[0]["section"] == "下一验证动作"
    assert "[E6]" not in new_report
    assert "关注 4 家整车厂的招标节奏" in new_report
    assert dr.check_reference_support(new_report, index) == []


def test_c05_matching_reference_is_kept():
    index = {"E6": "轨交设备Ⅱ PB 中位数 1.23（截至 2026-09-11）"}
    report = _full_report({
        "下一验证动作": "轨交设备Ⅱ PB 中位数 1.23 [E6]。",
    })
    assert dr.check_reference_support(report, index) == []


def test_c05_derivation_sentence_is_exempt():
    index = {"E6": "板块 PB 中位数 1.24"}
    report = _full_report({
        "下一验证动作": "按股本折算后约 4,300 万元 [E6]。",
    })
    assert dr.check_reference_support(report, index) == []


# ---------------------------------------------------------------------------
# C06：临界低估与弱趋势降权
# ---------------------------------------------------------------------------

def _board(**kw) -> dr.BoardValuationInput:
    base = dict(
        board_code="BK_TEST", board_name="白色家电", pb_percentile_median=20.0,
        pe_percentile_median=None, loss_ratio=0.0, trend=None,
        pb_percentile_sample_count=5, loss_count=0, loss_total=100, trade_date="2026-09-11",
    )
    base.update(kw)
    return dr.BoardValuationInput(**base)


@pytest.mark.parametrize(
    "gap,expected",
    [(29.9, False), (30.0, True), (36.9, True), (39.9, True), (40.0, False), (None, False)],
)
def test_c06_critical_band_boundaries(gap, expected):
    pe = None if gap is None else 20.0 + gap
    board = _board(pe_percentile_median=pe)
    assert dr.board_is_critical_valuation(board) is expected


def test_c06_critical_board_rationale_carries_marker_but_keeps_label():
    """白电 36.9pp 实测场景：标签仍是「低估候选」（分类总表不变），但依依据文本必须标临界。"""
    board = _board(pb_percentile_median=20.0, pe_percentile_median=56.9, loss_ratio=0.0)
    label, rationale = dr.classify_board_valuation(board)
    assert label == dr.VALUATION_JUDGMENT_LOW
    assert dr.VALUATION_CRITICAL_FLAG in rationale
    assert "36.9pp" in rationale
    assert "不得写成最完整" in rationale


def test_c06_non_critical_board_rationale_has_no_marker():
    _, rationale = dr.classify_board_valuation(_board(pb_percentile_median=20.0, pe_percentile_median=25.0))
    assert dr.VALUATION_CRITICAL_FLAG not in rationale


def test_c06_hype_gate_detects_head_and_ranking_words():
    # 摘要最前 60 字内点名某临界板块 → 视为摆在结论位置；60 字之后且无拔高词 → 不算。
    filler = "本报告讨论低估值板块的研究顺序与证据边界。" * 3
    summary = f"方向判断：白色家电是最完整的估值修复故事。{filler}综合Ⅱ趋势最弱但可留观察。"
    hits = dr.find_critical_board_hype(summary, {"白色家电"})
    assert len(hits) == 1
    assert hits[0]["in_head"] is True
    assert "最完整" in hits[0]["hype_terms"]
    assert dr.find_critical_board_hype(summary, {"综合Ⅱ"}) == []
    # 摘要里根本没提到的临界板块不算命中
    assert dr.find_critical_board_hype(summary, {"银行Ⅱ"}) == []


def test_c06_hype_terms_exclude_contract_heading_and_soft_mentions():
    """拔高词表不含摘要契约的「最强证据」小标题；60 字之后且无拔高词 → 不判拔高。"""
    assert all(term != "最强证据" for term in dr.CRITICAL_BOARD_HYPE_TERMS)
    assert "最完整" in dr.CRITICAL_BOARD_HYPE_TERMS
    filler = "本报告讨论低估值板块的研究顺序与证据边界。" * 3
    text = f"结论先行：先研究证据最完整的板块。{filler}轨交设备Ⅱ为临界板块，仍需核对盈利端背离。"
    assert dr.find_critical_board_hype(text, {"轨交设备Ⅱ"}) == []


def test_c06_validate_warns_and_downgrades_on_critical_hype():
    summary = "方向判断：白色家电是最完整的修复主线，先研究它。"
    quality = dr.validate_direction(
        _payload(executive_summary=summary + "补充说明。" * 10),
        mode="standard", research_mode="knowledge", evidence_ids=set(),
        critical_boards={"白色家电"},
    )
    assert quality["critical_board_hype"] and quality["critical_board_hype"][0]["board"] == "白色家电"
    assert quality["critical_boards"] == ["白色家电"]
    assert any("临界板块「白色家电」被执行摘要拔高" in w for w in quality["quality_warnings"])
    assert quality["quality_status"] == "needs_review"
    assert any("白色家电" in item for item in quality["missing_information"])


def test_c06_normal_candidate_board_is_not_warned():
    quality = dr.validate_direction(
        _payload(),
        mode="standard", research_mode="knowledge", evidence_ids=set(),
        critical_boards=set(),
    )
    assert quality["critical_board_hype"] == []
    assert not any("临界板块" in w for w in quality["quality_warnings"])


# ---------------------------------------------------------------------------
# 编排：apply_quality_repairs
# ---------------------------------------------------------------------------

def test_apply_quality_repairs_end_to_end_and_idempotent():
    """样报式四面命中的合成稿：一次改写后四类问题全部消失，再跑一次无新增改动。"""
    index = {
        "E3": "轨交设备Ⅱ PB 分位 12.0%，亏损面 6.7%",
        "E6": "轨交设备Ⅱ PB 中位数 1.23（截至 2026-09-11）",
    }
    report = _full_report({
        "催化与反证": "轨交设备Ⅱ属低估候选，高位板块资金流出。",
        "下一验证动作": "CPI 回升将推动盈利改善。另关注 4 家整车厂招标 [E6]。",
    })
    parsed = _payload(
        report=report,
        core_judgments=[
            {"id": "J2", "text": "公司竞争力稳定", "kind": "competitiveness", "support_refs": []},
            {"id": "J3", "text": "资金情绪偏弱", "kind": "sentiment", "support_refs": []},
        ],
    )
    record = dr.apply_quality_repairs(
        parsed, evidence_index=index, board_evidence={"轨交设备Ⅱ": {"E3"}}
    )
    assert record["applied"] is True
    assert record["policy_version"] == dr.QUALITY_REPAIR_POLICY_VERSION
    assert record["industry_layer"]["added"] is True
    assert record["cross_layer_demotions"]
    assert record["judgment_claim_rewrites"]
    assert record["reference_removals"]

    new_report = str(parsed["report"])
    assert dr.find_cross_layer_claims(new_report) == []
    assert dr.judgment_refs_cover_boards(dr.find_judgment_claims(new_report), {"轨交设备Ⅱ": {"E3"}}) == []
    assert dr.check_reference_support(new_report, index) == []

    again = dr.apply_quality_repairs(
        parsed, evidence_index=index, board_evidence={"轨交设备Ⅱ": {"E3"}}
    )
    assert again["applied"] is False
    assert again["cross_layer_demotions"] == []
    assert again["reference_removals"] == []


def test_apply_quality_repairs_is_noop_on_clean_report():
    parsed = _payload(
        core_judgments=[
            {"id": "J1", "text": "产业景气证据未覆盖", "kind": "industry",
             "support_refs": [], "confidence": "low"},
        ]
    )
    record = dr.apply_quality_repairs(parsed, evidence_index=None, board_evidence=None)
    assert record["applied"] is False
    assert record["industry_layer"]["added"] is False
