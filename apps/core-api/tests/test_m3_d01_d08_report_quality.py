"""M3（D01–D08）测试：研究判断与报告质量（2026-09-15 路线图）。

锁定语义：
- D01 报告形态：证据不足时输出**短篇阶段性研究 + 补证动作**，不再为凑十节反复写
  「无法判断」；必需小节随形态收窄（非必需小节缺失只作软告警）；缺省形态=完整研判
  （与历史行为逐字一致，老调用方零影响）。
- D02 前置行业比较表：结构化 `industry_comparison`，优先级只认高/中/低且必须有依据，
  不强制编造数值评分。
- D03 跨层因果：同句「宏观词 + 盈利词」且无中间环节证据 → 命中（低 CPI 不直接推出
  快递件量下降 / 高美国 CPI 不直接推出航运需求增长）。
- D04 周期审查：周期类板块判「低估候选」但未披露正常化口径 → 命中（低 PE 陷阱）。
- D05 反方审查进入定稿：撤下/降级 + 正文行内标注（前 14 字探针 → 最长公共片段回退）；
  仍无法定位的判断在正文开头集中列出（不留白、不谎称已标注）；摘要与风险同步；未完成如实标注。
- D06 双池分离：待验证研究对象不得出现在低估候选语境。
- D07 引用支持：挂了 [E#] 的句子数值不在该条目中 → 命中（推算类豁免）。
- D08 缺口集中：同一缺口在 ≥3 小节重复 → 命中；缺口清单去重。

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
        "title": "某主题方向研判",
        "executive_summary": "摘要：" + "方向判断与最强证据与最大不确定性。" * 8,
        "report": _full_report(),
        "core_judgments": [
            {"id": "J1", "text": "产业景气回升", "kind": "industry",
             "support_refs": [], "confidence": "medium"},
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


def _candidate(**overrides) -> dict:
    base = {
        "symbol": "600233", "symbol_raw": "600233", "name": "圆通速递",
        "identity_status": dr.IDENTITY_VERIFIED, "duplicate_of_pool": False,
        "sector": "物流", "business_link": "快递件量发运", "profit_path": "单票收入-成本",
        "valuation_ref": "东财·估值分析 / 600233", "valuation_status": "available",
        "gaps": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# D01：报告形态
# ---------------------------------------------------------------------------

def test_d01_choose_form_stage_when_no_evidence():
    assert dr.choose_direction_form(research_mode="knowledge", evidence_count=0) == dr.DIRECTION_FORM_STAGE
    assert dr.choose_direction_form(research_mode="evidence", evidence_count=0) == dr.DIRECTION_FORM_STAGE


def test_d01_choose_form_partial_on_primary_failure_or_many_unavailable():
    assert dr.choose_direction_form(
        research_mode="evidence", evidence_count=5, primary_ok=False
    ) == dr.DIRECTION_FORM_PARTIAL
    assert dr.choose_direction_form(
        research_mode="evidence", evidence_count=5, unavailable_count=2
    ) == dr.DIRECTION_FORM_PARTIAL
    # 证据条目足够但不足「充分」阈值 → 局部研判
    assert dr.choose_direction_form(
        research_mode="evidence", evidence_count=2
    ) == dr.DIRECTION_FORM_PARTIAL


def test_d01_choose_form_full_when_enough_evidence():
    assert dr.choose_direction_form(
        research_mode="evidence", evidence_count=dr.DIRECTION_FORM_FULL_MIN_EVIDENCE,
        primary_ok=True, unavailable_count=1,
    ) == dr.DIRECTION_FORM_FULL


def test_d01_stage_form_does_not_block_on_missing_sections():
    """阶段性研究只写两节即可交付——不再因缺十节里的八节而判不完整。"""
    report = (
        "## 研究问题与期限\n本次关键数据缺失，先给出定性框架与可执行的补证计划。\n"
        "## 下一验证动作\n跟踪国家邮政局月度运行情况公告与航运指数周度发布。\n"
    )
    payload = _payload(report=report)
    stage = dr.validate_direction(
        payload, mode="standard", research_mode="knowledge", evidence_ids=set(),
        form=dr.DIRECTION_FORM_STAGE,
    )
    assert stage["quality_blockers"] == [], stage["quality_blockers"]
    assert stage["missing_sections"] == []
    assert len(stage["missing_optional_sections"]) == len(dr.REQUIRED_DIRECTION_SECTIONS) - 2
    assert stage["report_form"] == dr.DIRECTION_FORM_STAGE
    assert stage["report_form_label"] == "阶段性研究"
    # 缺省形态（完整研判）下同一稿件必须阻断——形态是唯一变量。
    full = dr.validate_direction(
        payload, mode="standard", research_mode="knowledge", evidence_ids=set()
    )
    assert full["quality_blockers"], "完整研判下缺小节必须阻断"
    assert full["report_form"] == dr.DIRECTION_FORM_FULL


def test_d01_partial_form_requires_four_sections_only():
    report = (
        "## 研究问题与期限\n本次只覆盖有证据的行业，研究期限为未来 6-12 个月。\n"
        "## 需求供给与价格\n快递件量与收入同比数据可得，航运运价指数周度可得。\n"
        "## 催化与反证\n主要催化是运价环比回升，主要反证是需求被前置透支的风险。\n"
        "## 下一验证动作\n跟踪国家邮政局月度公告与上海航运交易所周度指数发布。\n"
    )
    quality = dr.validate_direction(
        _payload(report=report), mode="standard", research_mode="evidence",
        evidence_ids=set(), form=dr.DIRECTION_FORM_PARTIAL,
    )
    assert quality["quality_blockers"] == []
    assert quality["form_required_sections"] == list(dr.FORM_REQUIRED_SECTIONS[dr.DIRECTION_FORM_PARTIAL])


def test_d01_prompt_sections_follow_form():
    _s, stage_user = dr.build_messages(
        topic="快递", question="", mode="standard", research_mode="knowledge",
        evidence_block="", form=dr.DIRECTION_FORM_STAGE,
    )
    assert "阶段性研究" in stage_user
    assert f"{dr.DIRECTION_STAGE_CHAR_MIN}-{dr.DIRECTION_STAGE_CHAR_MAX} 字" in stage_user
    assert "## 产业链与利润分配" not in stage_user      # 十节模板已按形态收窄
    assert "## 下一验证动作" in stage_user
    _s2, full_user = dr.build_messages(
        topic="快递", question="", mode="standard", research_mode="knowledge",
        evidence_block="", form=dr.DIRECTION_FORM_FULL,
    )
    for name in dr.REQUIRED_DIRECTION_SECTIONS:
        assert f"## {name}" in full_user
    # 缺省（不传 form）与完整研判逐字一致，保证历史调用方零影响。
    _s3, default_user = dr.build_messages(
        topic="快递", question="", mode="standard", research_mode="knowledge", evidence_block="",
    )
    assert default_user == full_user


# ---------------------------------------------------------------------------
# D02：前置行业比较表
# ---------------------------------------------------------------------------

def test_d02_normalize_comparison_keeps_priority_whitelist_only():
    rows = dr.normalize_industry_comparison([
        {"industry": "快递", "research_priority": "高", "priority_basis": "估值分位低"},
        {"industry": "航运", "research_priority": "9.5 分", "priority_basis": ""},
        {"industry": "", "research_priority": "高"},
        "not-a-dict",
    ])
    assert len(rows) == 2
    assert rows[0]["research_priority"] == "高" and rows[0]["priority_basis_missing"] is False
    # 数值评分不属于白名单 → 置空（不强制编造数值评分）
    assert rows[1]["research_priority"] == "" and rows[1]["priority_basis_missing"] is True


def test_d02_validate_warns_when_comparison_missing_or_baseless():
    quality = dr.validate_direction(
        _payload(industry_comparison=[]), mode="standard", research_mode="evidence",
        evidence_ids=set(), form=dr.DIRECTION_FORM_FULL,
    )
    assert any("行业比较表" in w for w in quality["quality_warnings"])
    quality2 = dr.validate_direction(
        _payload(industry_comparison=[{"industry": "快递", "research_priority": "高", "priority_basis": ""}]),
        mode="standard", research_mode="evidence", evidence_ids=set(),
        form=dr.DIRECTION_FORM_FULL,
    )
    assert any("优先级" in w for w in quality2["quality_warnings"])
    assert any("优先级依据" in item for item in quality2["missing_information"])
    assert quality2["industry_comparison"][0]["industry"] == "快递"


# ---------------------------------------------------------------------------
# D03：跨层因果
# ---------------------------------------------------------------------------

def test_d03_cross_layer_claim_detected_without_bridge():
    report = _full_report({
        "需求供给与价格": "低 CPI 将推动快递公司利润上行。",  # 宏观 → 盈利，无中间环节
    })
    claims = dr.find_cross_layer_claims(report)
    assert len(claims) == 1
    assert "CPI" in claims[0]["macro_terms"] and "利润" in claims[0]["profit_terms"]


def test_d03_bridge_evidence_clears_claim():
    report = _full_report({
        "需求供给与价格": "CPI 走低，但快递件量同比+4.1%、单票收入 7.63 元，收入端不支撑利润上行。",
    })
    assert dr.find_cross_layer_claims(report) == []


def test_d03_validate_downgrades_on_multiple_cross_layer_claims():
    report = _full_report({
        "需求供给与价格": "本节说明：低 CPI 将推动快递公司利润上行，因此看好盈利。",
        "景气阶段": "本节说明：美国 CPI 走高将推动航运业绩改善，景气上行。",
    })
    quality = dr.validate_direction(
        _payload(report=report), mode="standard", research_mode="knowledge", evidence_ids=set(),
    )
    assert len(quality["cross_layer_claims"]) >= dr.CROSS_LAYER_WARN_THRESHOLD
    assert quality["quality_status"] == "needs_review"


# ---------------------------------------------------------------------------
# D04：周期正常化审查
# ---------------------------------------------------------------------------

def test_d04_cycle_board_low_judgment_needs_normalization_disclosure():
    gaps = dr.cycle_normalization_gaps(_full_report(), {"煤炭行业": dr.VALUATION_JUDGMENT_LOW})
    assert len(gaps) == 1 and gaps[0]["board"] == "煤炭行业"
    disclosed = _full_report({"景气阶段": "以周期均值的正常化盈利为口径，样本区间 2015-2026。"})
    assert dr.cycle_normalization_gaps(disclosed, {"煤炭行业": dr.VALUATION_JUDGMENT_LOW}) == []
    # 非周期板块不适用
    assert dr.cycle_normalization_gaps(_full_report(), {"白酒Ⅱ": dr.VALUATION_JUDGMENT_LOW}) == []


def test_d04_validate_flags_cycle_gap():
    quality = dr.validate_direction(
        _payload(), mode="standard", research_mode="knowledge", evidence_ids=set(),
        board_judgments={"煤炭行业": dr.VALUATION_JUDGMENT_LOW},
    )
    assert quality["cycle_normalization_gaps"]
    assert quality["quality_status"] == "needs_review"


# ---------------------------------------------------------------------------
# D05：反方审查进入定稿
# ---------------------------------------------------------------------------

def _counter(**overrides) -> dict:
    base = {
        "ok": True, "requested": True, "verdict_robustness": "mixed",
        "strongest_counter": "需求前置透支，件量增速与收入增速背离",
        "affected_conclusions": [{"id": "J1", "effect": "weakens", "reason": "中间数据缺失"}],
        "pool_challenges": [{"symbol": "600233", "challenge": "单票收入口径未说明"}],
    }
    base.update(overrides)
    return base


def _report_with_judgment_text() -> str:
    """正文含 J1 原文（"产业景气回升"）——用于验证「正文行内标注」确实落到句子上。"""
    return _full_report({"需求供给与价格": "产业景气回升需要中间证据支撑，目前只有宏观数据可用。"})


def test_d05_apply_removes_when_fragile_and_annotates_body():
    parsed = _payload(report=_report_with_judgment_text())
    out, record = dr.apply_counter_check(parsed, _counter(verdict_robustness="fragile"))
    assert record["applied"] is True
    assert record["removed_judgment_ids"] == ["J1"]
    assert all(item["id"] != "J1" for item in out["core_judgments"])
    # 正文必须带行内标注（不得正文保留强结论而文末承认依据不足）
    assert dr.COUNTER_ANNOTATION_REMOVED in out["report"]
    assert dr.COUNTER_SUMMARY_PREFIX in out["executive_summary"]
    assert any("需求前置透支" in x for x in out["risks"])
    assert any("600233" in x for x in out["risks"])
    assert record["annotated_sentences"] == 1
    assert record["catalysts_untouched_reason"]


def test_d05_apply_downgrades_when_not_fragile():
    out, record = dr.apply_counter_check(
        _payload(report=_report_with_judgment_text()), _counter(verdict_robustness="mixed")
    )
    assert record["downgraded_judgment_ids"] == ["J1"]
    kept = next(item for item in out["core_judgments"] if item["id"] == "J1")
    assert kept["confidence"] == "low" and kept["counter_effect"] == "downgraded"
    assert dr.COUNTER_ANNOTATION_DOWNGRADED in out["report"]


def test_d05_not_applied_when_counter_incomplete():
    parsed = _payload()
    out, record = dr.apply_counter_check(parsed, {"ok": False, "detail": "解析失败"})
    assert record["applied"] is False and record["reason"]
    assert out == parsed  # 未修订：原样返回，不假装已落实


def test_d05_fallback_match_annotates_paraphrased_body_sentence():
    """v2 回退匹配：判断文本与正文措辞不同，但存在 ≥8 字公共片段时仍把标注落到句子上。

    真实报告（E02 实测）里 `core_judgments[].text` 是模型压缩过的结论，前 14 字探针
    3/3 全部落空；若没有这一级回退，正文就会保留强结论而摘要却写着「已标注」。
    """
    body = "综合来看，快递行业单票收入增速高于件量增速，盈利改善的持续性需中间证据。"
    parsed = _payload(
        report=_full_report({"需求供给与价格": body}),
        core_judgments=[{
            "id": "J1", "text": "快递单票收入增速高于件量增速，盈利改善可持续",
            "kind": "industry", "support_refs": [], "confidence": "medium",
        }],
    )
    out, record = dr.apply_counter_check(parsed, _counter(verdict_robustness="fragile"))
    assert record["annotated_sentences"] == 1
    assert record["annotation_fallback_hits"] == 1, "本例前 14 字探针必然落空，只能靠回退命中"
    assert record["body_block_entries"] == 0
    assert dr.COUNTER_ANNOTATION_REMOVED in out["report"]
    assert dr.COUNTER_BODY_BLOCK_HEADER not in out["report"]


def test_d05_unlocated_judgment_is_listed_in_body_and_summary_is_honest():
    """v2：判断原文无法在正文定位时，正文开头集中列出；摘要不得声称「已逐句标注」。"""
    parsed = _payload(report=_full_report({"景气阶段": "本节不含判断原文。"}))
    out, record = dr.apply_counter_check(parsed, _counter(verdict_robustness="fragile"))

    assert record["annotation_misses"], "找不到原句必须留痕，不得静默"
    assert record["annotated_sentences"] == 0
    assert record["body_block_entries"] == 1
    # 正文必须让读者看见被撤下的判断（否则「正文保留强结论、文末承认依据不足」依旧成立）
    assert dr.COUNTER_BODY_BLOCK_HEADER in out["report"]
    assert dr.COUNTER_ANNOTATION_REMOVED in out["report"]
    # 摘要文案按实际标注条数生成，不得出现「已标注」而实际为 0
    assert "正文已逐句标注" not in out["executive_summary"]
    assert "已在正文开头集中列出" in out["executive_summary"]


# ---------------------------------------------------------------------------
# D06：研究对象池 / 低估候选池分离
# ---------------------------------------------------------------------------

def test_d06_split_pools_by_qualification():
    low, research = dr.split_pools([
        _candidate(),
        _candidate(symbol="002352", valuation_ref="", valuation_status="unavailable"),
        _candidate(symbol="600603", identity_status=dr.IDENTITY_UNVERIFIED),
        _candidate(symbol="601006", sector="煤炭行业"),
    ], {"煤炭行业": dr.VALUATION_JUDGMENT_HIGH})
    assert [item["symbol"] for item in low] == ["600233"]
    assert low[0]["pool_kind"] == dr.POOL_KIND_LOW
    by_symbol = {item["symbol"]: item for item in research}
    assert "估值证据缺失" in by_symbol["002352"]["pool_exclusion_reasons"]
    assert any("补齐估值" in action for action in by_symbol["002352"]["verification_actions"])
    assert "证券身份未核验" in by_symbol["600603"]["pool_exclusion_reasons"]
    assert "非低估象限" in by_symbol["601006"]["pool_exclusion_reasons"]
    assert all(item["pool_kind"] == dr.POOL_KIND_RESEARCH for item in research)


def test_d06_validate_flags_research_pool_in_low_context():
    report = _full_report({"需求供给与价格": "低估候选：600233 圆通速递估值低，可直接研究。"})
    quality = dr.validate_direction(
        _payload(report=report, stock_pool=[
            _candidate(valuation_ref="", valuation_status="unavailable"),
        ]),
        mode="standard", research_mode="knowledge", evidence_ids=set(),
    )
    assert quality["research_pool_mislabeled"]
    assert [item["symbol"] for item in quality["research_pool"]] == ["600233"]
    assert quality["low_valuation_pool"] == []
    assert quality["quality_status"] == "needs_review"


# ---------------------------------------------------------------------------
# D07：引用支持性
# ---------------------------------------------------------------------------

def test_d07_reference_support_detects_mismatched_number():
    report = _full_report({"需求供给与价格": "快递业务量同比 4.1% [E1]。"})
    idx = {"E1": "快递业务收入 1303.7亿元，同比 8.1%"}
    problems = dr.check_reference_support(report, idx)
    assert len(problems) == 1
    assert problems[0]["refs"] == ["E1"] and "4.1" in problems[0]["numbers"]


def test_d07_reference_support_passes_when_number_present_or_derived():
    report = _full_report({"需求供给与价格": "快递业务量同比 4.1% [E1]。"})
    assert dr.check_reference_support(report, {"E1": "快递业务量同比 4.1%"}) == []
    derived = _full_report({"需求供给与价格": "单票收入推算 7.633 元/件 [E1]。"})
    assert dr.check_reference_support(derived, {"E1": "收入 1303.7亿元 / 件量 170.8亿件"}) == []


def test_d07_validate_downgrades_on_multiple_reference_problems():
    sections = {}
    for name in ("需求供给与价格", "景气阶段", "竞争与替代"):
        sections[name] = "本节口径：该指标同比 9.9% [E1]，据此判断景气回升。"
    quality = dr.validate_direction(
        _payload(report=_full_report(sections)), mode="standard", research_mode="evidence",
        evidence_ids={"E1"}, evidence_index={"E1": "内容里只有 1.1%"},
    )
    assert len(quality["reference_support_problems"]) >= dr.REFERENCE_SUPPORT_WARN_THRESHOLD
    assert quality["quality_status"] == "needs_review"


# ---------------------------------------------------------------------------
# D08：缺口集中与去重
# ---------------------------------------------------------------------------

def test_d08_repeated_gap_detected_across_sections():
    gap = "上市公司分企量价未接入"
    sections = {name: f"缺口：{gap}，因此结论受限。" for name in (
        "需求供给与价格", "景气阶段", "竞争与替代",
    )}
    repeated = dr.find_repeated_gaps(_full_report(sections), [gap])
    assert len(repeated) == 1 and len(repeated[0]["sections"]) >= dr.GAP_REPEAT_SECTION_THRESHOLD


def test_d08_data_gaps_deduped_in_result():
    quality = dr.validate_direction(
        _payload(data_gaps=["分企量价未接入", "分企量价未接入", "运力订单未接入"]),
        mode="standard", research_mode="evidence", evidence_ids=set(),
    )
    assert quality["data_gaps"] == ["分企量价未接入", "运力订单未接入"]
