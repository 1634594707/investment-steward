"""v39 P2（Q19/Q20）：深研「研究完成度」与「反方检查影响最终摘要」。

对应路线图 `docs/研报与追问质量提升任务路线图-2026-09-19.zh-CN.md` §5：

- **Q19** `report_quality.research_completeness(payload)`：四项关键研究项
  （盈利增长拆解 / 现金流与资本开支 / 估值分化解释 / 情景与反证）各 `done|partial|missing`，
  `score = done/4`；**长而缺关键论证的报告不得显示研究完整，短而四项齐备的报告不因字数被判无效**；
  缺失项必须进 `quality_warnings`（逐条写清缺什么 + 下一步补什么），不再只有一条字数告警。
- **Q20** `report_quality.summary_overreach_review(...)`：反方检查末尾否认了安全边际 / 正常化 / 趋势确认，
  执行摘要里替它背书的句子**就地**加「（反方检查：此说不成立，…）」标注，模型原话整份留痕；
  判据与正文 D01/D02 同源（`undoubted_ban_phrases` / `is_valuation_denial` / `align_valuation_language`）。

红线：完成度与摘要标注都由 payload 字段**确定性复算**，不采信模型自评、不代填数字；
反方检查未执行时如实说明「未运行」，不假装已修订。模型调用与取数一律 mock，绝不联网。
"""

from __future__ import annotations

import json
import re

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import report_quality

_MARKER_RE = re.compile(r"（反方检查：此说不成立，[^）]*）")

# 探针安全填充句：不含任何研究项探针词（业务量/单价/成本率/现金流/资本开支/样本/分位…均不出现）。
_FILLER = "本节按来源给出的数字与日期逐条陈列，未作额外推算，也不补充来源之外的说法。"


def _deep_body(repeats: int) -> str:
    """四小节齐备的长正文（只用不含研究项探针的句子填充）——模拟「长但没拆解」的样报。"""
    block = _FILLER * repeats
    return (
        "### 技术面\n" + block
        + "\n### 消息面\n" + block
        + "\n### 基本面\n" + block
        + "盈利 10.35 亿、上年同期 4.53 亿——只此一句带过。"
        + "TTM经营现金流 51.23 亿 ÷ TTM归母净利 19.50 亿 = 2.63 倍，故现金流健康。"
        + "PE(TTM) 11.70 处于 1.7% 分位；PB 处于 66.9% 分位。\n"
        + "\n### 综合判断\n" + block
        + "总体中性看待，矛盾集中在规模扩张与单价走弱之间，需持续跟踪后续披露。\n"
    )


def _shentong_payload() -> dict:
    """缺陷样本复刻：约 5000 字，但四项关键研究项全缺（Q19 的原始症状）。"""
    return {
        "title": "申通快递：规模扩张与单价走弱",
        "executive_summary": (
            "方向偏多：规模扩张抵消了单价走弱，份额回升。"
            "最强依据是 TTM 经营现金流对净利覆盖 2.63 倍且 PE 历史分位仅 1.7%，估值提供安全边际。"
            "技术面均线多头排列。"
            "最大风险是价格竞争延续。"
        ),
        "report": _deep_body(34),
        "citations": ["S1", "S2", "S3", "S4", "S5"],
        "limitations": ["同业口径以数据商为准", "行情为日线粒度", "财务摘要为累计口径"],
        "confidence": "medium",
        "valuation": {
            "pe_ttm": "11.70",
            "pe_percentile": "近5年 1.7% 分位",
            "peer_position": "PE 高于同业中位数",
            "verdict": "偏便宜",
            "basis": "分位低 [S5]",
            "valuation_status": "undervalued",
        },
    }


def _complete_payload() -> dict:
    """一千出头字、四项关键研究项齐备（Q19 另一半验收：短而不因字数被判无效）。"""
    extra = (
        "以上各项数字都取自本次真实来源，累计报告期与 TTM 两种口径分开列示、不混用；"
        "来源未给出的明细一律留空并写明缺什么，不做推算，也不借其他口径代替。"
    )
    report = (
        "### 技术面\n"
        "股价在 MA20 上方缩量整理，量能较 20 日均值萎缩，短期方向未明，"
        "等待放量或跌破前低给出信号，均线与量能的变化逐日如实记录在快照里。[S1]\n"
        + extra + "\n"
        "### 消息面\n"
        "公司公告月度经营数据已披露，市场情绪偏谨慎，暂无新的产能或并购信息落地，"
        "过往传闻与已落地事实分别标注，不以标题代替正文。[S3]\n"
        + extra + "\n"
        "### 基本面\n"
        "上半年归母净利 10.35 亿、上年同期 4.53 亿，增长必须拆开看："
        "业务量同比增长带动收入扩张；单票收入同比下降，价格端是主要拖累；"
        "单票成本随分拣自动化与产能利用率提升而下降，成本效率贡献第三块增量；"
        "上年同期基数偏低放大了同比读数，属低基数效应；"
        "非经常性损益（政府补助与资产处置收益）本期未披露明细，暂不计入贡献。\n"
        "TTM 经营现金流 51.23 亿对归母净利 19.50 亿为 2.63 倍；"
        "本期折旧摊销规模较大、营运资本（应收账款）季节性收敛、资本开支仍高，"
        "因此覆盖倍数高不等于盈利质量高，自由现金流口径待补。[S4]\n"
        "估值端 PE(TTM) 11.70 处于近5年 1.7% 分位，而 PB 处于 66.9% 分位，两者分化："
        "ROE 处于周期偏低位置、净资产口径含定增与商誉，故低 PE 只说明价格相对历史利润便宜；"
        "可比对象取商业模式相近的三家，亏损标的已排除在样本外，口径差异需人工复核。[S5]\n"
        + extra + "\n"
        "### 综合判断\n"
        "多空矛盾在规模扩张与单价走弱之间，低分位不作为安全边际依据，需等正常化盈利验证。\n"
        + extra + "\n"
    )
    return {
        "title": "把增长拆开看：规模、价格与成本",
        "executive_summary": (
            "方向中性：规模扩张被单价走弱部分抵消。"
            "最强依据是成本效率改善与现金流覆盖倍数。"
            "技术面缩量整理，趋势未确认。"
            "最大反证是低 PE 分位不等于安全边际。"
        ),
        "report": report,
        "citations": ["S1", "S3", "S4", "S5"],
        "limitations": ["同业口径差异", "资本开支明细未逐项披露", "非经常性损益未拆分"],
        "confidence": "medium",
        "valuation": {
            "pe_ttm": "11.70",
            "pe_percentile": "近5年 1.7% 分位",
            "peer_position": "低于同业中位数",
            "verdict": "无数据",
            "basis": "正常化盈利未验证 [S5]",
            "valuation_status": "undetermined",
        },
        "scenarios": [
            {"name": "中性", "trigger": "单票收入环比转正",
             "invalidates": "单票收入继续下降且规模增速回落", "horizon": "至下月经营数据披露"},
            {"name": "悲观", "trigger": "价格竞争重启",
             "invalidates": "单票成本降幅继续扩大且份额稳定", "horizon": "10 个交易日"},
        ],
    }


def _item(view: dict, key: str) -> dict:
    return next(item for item in view["items"] if item["key"] == key)


def _completeness_view(container: dict) -> dict:
    """完成度视图的响应键名（Q19 与 Q16–Q18 的 quality_gaps 撞名后改成 deep_research_completeness；
    两个名字都读，避免同一份契约在两次落地之间断言失败）。"""
    view = container.get("research_completeness")
    if view is None:
        view = container.get("deep_research_completeness")
    assert view is not None, sorted(container)
    return view


# ---------------------------------------------------------------------------
# Q19 研究完成度
# ---------------------------------------------------------------------------


def test_q19_long_report_without_key_analysis_is_not_complete():
    """验收：5000 字级报告缺四项关键研究项 → complete=False，headline 直说「关键研究项缺失」。"""
    view = report_quality.research_completeness({**_shentong_payload(), "report_mode": "deep"})
    assert view["word_count"] > 3500, view["word_count"]              # 篇幅够长
    assert view["complete"] is False
    assert view["score"] == 0.0
    assert view["headline"].startswith("关键研究项缺失")
    for key in report_quality.COMPLETENESS_ITEM_KEYS:                 # 没有一项能被字数撑成完成
        assert _item(view, key)["state"] != report_quality.COMPLETENESS_DONE, key
    assert view["word_count_advisory"] is True
    assert view["word_count_mode"]["label"] == "深研版"
    # 现金流只给覆盖倍数即判质量 → 至多部分完成，且必须写明覆盖倍数不等于盈利质量
    cash = _item(view, "cash_flow_and_capex")
    assert cash["state"] == report_quality.COMPLETENESS_PARTIAL
    assert any("覆盖倍数本身不等于盈利质量" in text for text in cash["evidence"])
    assert set(cash["gaps"]) >= {"折旧摊销", "资本开支"}
    # 盈利拆解只有一句「本期 vs 上年同期」→ 维度覆盖不足
    earnings = _item(view, "earnings_decomposition")
    assert earnings["state"] == report_quality.COMPLETENESS_PARTIAL
    # 「单价走弱」「上年同期」被探针看见，但业务量/成本效率/非经常性损益确实没拆
    assert earnings["covered"] == ["价格", "低基数"], earnings["covered"]
    assert set(earnings["gaps"]) >= {"业务量", "成本效率", "非经常性损益"}


def test_q19_short_report_with_four_items_is_complete():
    """验收：一千字出头的报告四项齐备 → complete=True，字数只作辅助提示。"""
    view = report_quality.research_completeness({**_complete_payload(), "report_mode": "standard"})
    assert 600 < view["word_count"] < report_quality.REPORT_MODES["standard"]["min"], view["word_count"]
    assert view["complete"] is True
    assert view["score"] == 1.0
    assert view["warnings"] == []                                      # 不再有关键研究项缺失告警
    assert view["word_count_advisory"] is True
    assert "低于标准版建议区间" in view["word_count_note"]              # 短这件事照实说
    assert "字数只作辅助提示" in view["word_count_note"]
    assert _item(view, "earnings_decomposition")["state"] == report_quality.COMPLETENESS_DONE
    assert _item(view, "valuation_explanation")["state"] == report_quality.COMPLETENESS_DONE
    assert _item(view, "scenarios_and_counter_evidence")["state"] == report_quality.COMPLETENESS_DONE


def test_q19_states_come_from_payload_and_do_not_invent_ratios():
    """完成度只看 payload 实有内容：结构化拆解给不出贡献比例时如实说明，不伪造比例。"""
    payload = {
        "report": "### 基本面\n增长来自规模与价格。",
        "limitations": ["a"],
        "earnings_growth": [
            {"factor": "volume", "effect": "正贡献", "basis": "H1 业务量同比 +30%", "source": "S4"},
            {"factor": "price", "effect": "负贡献", "basis": "", "source": "",
             "contribution_pct": None, "note": "单票收入未披露同比口径，无法拆解贡献比例"},
            {"factor": "cost_efficiency", "effect": "正贡献", "basis": "单票成本下降",
             "source": "S4", "contribution_pct": 25.0},
        ],
    }
    item = _item(report_quality.research_completeness(payload), "earnings_decomposition")
    assert item["state"] == report_quality.COMPLETENESS_DONE            # 三个维度都有结论文字
    assert item["covered"] == ["业务量", "价格", "成本效率"]
    assert "低基数" in item["gaps"] and "非经常性损益" in item["gaps"]
    joined = " ".join(item["evidence"])
    assert "无法拆解贡献比例" in joined                                   # 缺什么照实说，不补数
    assert "无来源支撑的因素：价格" in joined
    # 结构化块为空 → 按缺失处理，不猜模型「大概写了」
    # 路线图书面键名同样可读（完成度不绑定某一份命名）
    alias = _item(report_quality.research_completeness({
        "earnings_decomposition": [{"factor": "low_base", "effect": "正贡献", "basis": "上年同期亏损", "source": "S4"}]
    }), "earnings_decomposition")
    assert alias["covered"] == ["低基数"]
    empty = _item(report_quality.research_completeness({"earnings_growth": []}), "earnings_decomposition")
    assert empty["state"] == report_quality.COMPLETENESS_MISSING


def test_q19_cash_flow_item_reads_structured_block():
    """现金流项由结构化字段判定（折旧摊销/营运资本/资本开支），与正文探针同一判据可复算。"""
    base = {"report": "TTM 经营现金流 51.23 亿，覆盖净利 2.63 倍。"}
    assert _item(report_quality.research_completeness(base), "cash_flow_and_capex")["state"] == \
        report_quality.COMPLETENESS_PARTIAL
    filled = {**base, "cash_flow_quality": {
        "coverage": 2.63,
        "depreciation_amortization": 18.4,
        "working_capital_change": -3.1,
        "capex": 42.0,
        "free_cash_flow": 9.1,
        "verdict": "盈利质量较好",
    }}
    item = _item(report_quality.research_completeness(filled), "cash_flow_and_capex")
    assert item["state"] == report_quality.COMPLETENESS_DONE
    assert item["gaps"] == []
    # 结构化块自己降了调（缺字段），完成度里也必须看得见这条说明
    weak = {**base, "cash_flow_quality": {
        "coverage": 2.63,
        "verdict": "不足以判断盈利质量",
        "downgrade_note": "缺折旧摊销、营运资本变动、资本开支",
    }}
    downgraded = _item(report_quality.research_completeness(weak), "cash_flow_and_capex")
    assert downgraded["state"] == report_quality.COMPLETENESS_PARTIAL
    assert any("不足以判断盈利质量" in text for text in downgraded["evidence"])


def test_q19_scenarios_item_needs_falsifiable_scenarios_and_counter_evidence():
    """情景与反证：只有触发条件没有失效条件 → 部分完成；补上失效条件与反方反证即完成。"""
    partial_payload = {
        "limitations": ["a", "b", "c"],
        "scenarios": [{"name": "中性", "trigger": "放量突破", "invalidates": ""}],
    }
    item = _item(report_quality.research_completeness(partial_payload), "scenarios_and_counter_evidence")
    assert item["state"] == report_quality.COMPLETENESS_PARTIAL
    assert "情景失效条件（不可证伪）" in item["gaps"]
    done_payload = {
        **partial_payload,
        "scenarios": [{"name": "中性", "trigger": "放量突破", "invalidates": "缩量回踩前低"}],
        "counter_check": {"ok": True, "strongest_counter": "低 PE 分位不等于安全边际，利润含一次性成分。"},
    }
    done = _item(report_quality.research_completeness(done_payload), "scenarios_and_counter_evidence")
    assert done["state"] == report_quality.COMPLETENESS_DONE
    assert any("反方检查已执行并给出反证" in text for text in done["evidence"])


def test_q19_is_pure_and_deterministic():
    """同一 payload 两次调用结果完全一致（纯函数：无 IO、不读时钟、不采信模型自评）。"""
    payload = {**_complete_payload(), "report_mode": "deep"}
    first = report_quality.research_completeness(payload)
    second = report_quality.research_completeness(payload)
    assert first == second
    assert first["version"] == report_quality.RESEARCH_COMPLETENESS_VERSION
    assert first["done_count"] == sum(1 for item in first["items"] if item["state"] == "done")
    assert first["score"] == round(first["done_count"] / 4, 2)
    # 模式标注异常不静默崩：按标准口径给出辅助提示并写明异常
    odd = report_quality.research_completeness({**payload, "report_mode": "ultra"})
    assert "研报模式标注异常" in odd["word_count_note"]


def test_q19_missing_items_surface_in_quality_warnings():
    """验收（Q19）：关键拆解缺失必须在质量告警里看得见，逐条写明缺什么、下一步补什么。"""
    quality = report_quality.validate_report(
        _shentong_payload(), allowed={"S1", "S2", "S3", "S4", "S5"}, mode="deep"
    )
    gaps = [warning for warning in quality["warnings"] if "关键研究项" in warning]
    assert gaps, quality["warnings"]
    assert any("盈利增长拆解" in warning for warning in gaps)
    assert any("现金流与资本开支" in warning for warning in gaps)
    assert any("下一步" in warning and "缺 " in warning for warning in gaps)
    assert any("篇幅不等于研究完整" in warning for warning in quality["warnings"])
    # 归到「证据」类（论证问题），且不得被当成阻断项
    categories = {
        entry["category"] for entry in report_quality.categorize_warnings(quality["warnings"])
        if "关键研究项" in entry["message"] or "篇幅不等于研究完整" in entry["message"]
    }
    assert categories == {"evidence"}
    view = _completeness_view(quality)
    assert view["complete"] is False
    assert view["headline"].startswith("关键研究项缺失")
    assert not [blocker for blocker in quality["quality_blockers"] if "研究项" in blocker["message"]]


def test_q19_surfaces_in_decision_card_for_frontend():
    """首屏结论卡带完成度视图；调用方未提供时如实为 None + 说明，不冒充有该项。"""
    view = report_quality.research_completeness(_complete_payload())
    common = {
        "symbol": "002468",
        "conclusion": {"statement": "中性", "direction": "中性", "core_conflict": "规模与单价", "confidence": "medium"},
        "evidence_quality": report_quality.evidence_quality_report({"S1"}),
        "claim_findings": {"claims": [], "support_counts": {}},
        "counter_check": None,
        "watchpoints": [],
        "valuation": {},
    }
    card = report_quality.decision_card(**common, research_completeness=view)
    assert _completeness_view(card) == view
    assert card.get("research_completeness_note", card.get("deep_research_completeness_note")) == ""
    bare = report_quality.decision_card(**common)
    assert bare.get("research_completeness", bare.get("deep_research_completeness")) is None
    note = bare.get("research_completeness_note", bare.get("deep_research_completeness_note"))
    assert "未提供研究完成度视图" in note


# ---------------------------------------------------------------------------
# Q20 反方检查影响最终摘要
# ---------------------------------------------------------------------------


def _margin_denial_counter() -> dict:
    """样报式反方检查：末尾否认「低 PE/PS = 安全边际」，并指出正常化盈利未验证。"""
    return {
        "ok": True,
        "strongest_support": "上半年规模扩张 [S4]",
        "strongest_support_source": "S4",
        "strongest_counter": (
            "多方把低 PE 与低 PS 当作下跌保护，但低PE/PS 不等于安全边际："
            "TTM 利润含低基数与一次性成分，正常化盈利未经验证。"
        ),
        "strongest_counter_source": "S5",
        "most_misread_sentence": {"quote": "PE 历史分位仅 1.7%", "why": "分位低不等于便宜，需利润口径验证"},
        "necessary_conditions": ["单票收入不再大幅下降", "正常化盈利可复算"],
        "verdict_robustness": "fragile",
    }


def test_q20_counter_denial_of_margin_denies_safety():
    """验收①：反方检查说「低 PE/PS 不等于安全边际」时，摘要不得继续宣称安全边际充分。"""
    summary = (
        "方向偏多：规模扩张抵消单价走弱，份额回升。"
        "最强依据是现金流覆盖 2.63 倍且 PE 历史分位仅 1.7%，估值提供安全边际。"
        "技术面缩量整理。"
        "最大风险是价格竞争延续。"
    )
    review = report_quality.summary_overreach_review(
        executive_summary=summary,
        counter_check=_margin_denial_counter(),
        valuation=report_quality.normalize_valuation({"valuation_status": "undervalued", "pe_percentile": "1.7%"}),
        claim_findings={"claims": [], "support_counts": {}},
    )
    assert review["reviewed"] is True
    assert "margin_of_safety" in review["counter_denials"]
    assert "不等于安全边际" in review["counter_denials"]["margin_of_safety"]     # 反方原话可回查
    assert review["changed"] is True
    revised = review["revised_summary"]
    assert "（反方检查：此说不成立，" in revised
    trace = review["revision_trace"][0]
    assert trace["kinds"] == ["margin_of_safety"]
    assert "不等于安全边际" in trace["counter_quotes"][0]
    assert review["overreach"][0]["phrases"]                       # 命中的具体断言词（可复算）
    # 留痕：模型原话一字未改；去掉服务端标注即还原原文（不删句、不改事实句）
    assert review["summary_original"] == summary
    assert _MARKER_RE.sub("", revised) == summary
    assert any("执行摘要过度断言" in warning for warning in review["warnings"])


def test_q20_normalization_and_confirmation_denials_are_revised_too():
    """三类否认（安全边际 / 正常化 / 趋势确认）各自的支撑句都要被就地标注。"""
    counter = {
        "ok": True,
        "strongest_counter": "覆盖倍数只是口径表象，正常化盈利未经证实；单日信号尚未确认。",
        "necessary_conditions": [],
    }
    summary = "方向偏多：现金流含金量高，盈利质量较好。技术面放量，趋势已确认，突破有效。"
    review = report_quality.summary_overreach_review(
        executive_summary=summary,
        counter_check=counter,
        valuation=report_quality.normalize_valuation({"valuation_status": "undetermined"}),
        claim_findings={"claims": [], "support_counts": {}},
    )
    kinds = {kind for entry in review["revision_trace"] for kind in entry["kinds"]}
    assert {"normalization", "confirmation"} <= kinds, review["revision_trace"]
    assert review["revised_summary"].count("（反方检查：此说不成立，") == len(review["revision_trace"])


def test_q20_reuses_d01_d02_alignment_on_the_summary():
    """复用 align_valuation_language：摘要里的「安全垫」与驾驶舱口径相反时按 D01/D02 加注。"""
    valuation = report_quality.normalize_valuation({
        "pe_ttm": "11.70",
        "peer_position": "PE 高于同业中位数 8.76",
        "verdict": "偏便宜",
        "valuation_status": "undervalued",       # 拿不出正常化盈利 → 服务端降为 undetermined
    })
    assert valuation["valuation_status"] == "undetermined"
    review = report_quality.summary_overreach_review(
        executive_summary="结论偏多：PB 处于历史低位提供安全垫。",
        counter_check={"ok": False, "detail": "本次未执行反方检查。"},
        valuation=valuation,
        claim_findings={"claims": [], "support_counts": {}},
    )
    kinds = [item["kind"] for item in review["overreach"]]
    assert "valuation_language_conflict" in kinds                 # 由复用的 D01/D02 产生
    assert report_quality.VALUATION_ALIGN_NOTE in review["revised_summary"]
    assert review["changed"] is True
    assert review["reviewed"] is False                            # 反方检查未执行，照实说


def test_q20_self_denying_or_hedged_summary_is_left_alone():
    """摘要句自己已否认（「不等于低估」「不构成安全垫」）时不加注；重复执行幂等。"""
    summary = "方向中性：低 PE 分位不等于低估，也不构成安全垫。"
    review = report_quality.summary_overreach_review(
        executive_summary=summary,
        counter_check=_margin_denial_counter(),
        valuation=report_quality.normalize_valuation({"valuation_status": "undervalued"}),
        claim_findings={"claims": [], "support_counts": {}},
    )
    assert review["changed"] is False
    assert review["revised_summary"] == ""
    assert review["overreach"] == []
    assert "无需修订" in review["revision_note"]

    # 幂等：把已标注的摘要再喂一次，不会再加一层
    claimed = "方向偏多：PE 历史分位仅 1.7%，估值提供安全边际。"
    first = report_quality.summary_overreach_review(
        executive_summary=claimed,
        counter_check=_margin_denial_counter(),
        valuation=report_quality.normalize_valuation({}),
        claim_findings={"claims": [], "support_counts": {}},
    )
    assert first["changed"] is True
    second = report_quality.summary_overreach_review(
        executive_summary=first["revised_summary"],
        counter_check=_margin_denial_counter(),
        valuation=report_quality.normalize_valuation({}),
        claim_findings={"claims": [], "support_counts": {}},
    )
    assert second["changed"] is False and second["revised_summary"] == ""


def test_q20_unreviewed_counter_check_is_stated_not_faked():
    """反方检查缺失/未成功：如实说明检查未运行，不假装已修订。"""
    review = report_quality.summary_overreach_review(
        executive_summary="方向偏多：估值提供安全边际。",
        counter_check={"ok": False, "requested": True, "detail": "反方检查模型调用失败"},
        valuation=report_quality.normalize_valuation({}),
        claim_findings={"claims": [], "support_counts": {}},
    )
    assert review["reviewed"] is False
    assert review["changed"] is False
    assert "未运行" in review["revision_note"] and "不假装已修订" in review["revision_note"]


def test_q20_endpoint_applies_summary_revision_and_keeps_original(client, tmp_path, monkeypatch):
    """端到端：摘要被就地修订 + report_revision 升级 + 原话进 revision_history 子键留痕。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)

    payload = _shentong_payload()
    original_summary = payload["executive_summary"]
    report_json = json.dumps(payload, ensure_ascii=False)
    counter_json = json.dumps(_margin_denial_counter(), ensure_ascii=False)
    calls: list[dict] = []

    def _fake(base_url, json_body, api_key, timeout):
        calls.append(json_body)
        return {"choices": [{"message": {"content": report_json if len(calls) == 1 else counter_json}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)

    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "mode": "deep", "with_auto_repair": False},
        headers=headers,
    ).json()
    assert body["ok"] is True, body
    # 两次调用：报告 + 反方检查（反方检查的用户消息以【标的】开头）
    assert len(calls) >= 2, [str(call["messages"][-1]["content"])[:60] for call in calls]
    assert "【标的】" in str(calls[-1]["messages"][-1]["content"])

    overreach = body["summary_overreach"]
    assert overreach["reviewed"] is True and overreach["changed"] is True
    assert _MARKER_RE.search(body["executive_summary"])
    # 只加标注、不改写事实句：去掉标注即等于模型原话
    assert _MARKER_RE.sub("", body["executive_summary"]) == original_summary
    assert overreach["summary_original"] == original_summary
    # 摘要被修订就是一次真实修订：版本号 +1，留痕挂在 summary_overreach_repair 子键下
    assert body["report_revision"] == 2
    entry = body["revision_history"][-1]
    assert entry["summary_overreach_repair"]["original_summary"] == original_summary
    assert entry["summary_overreach_repair"]["kinds"] == ["margin_of_safety"]
    assert entry["summary_overreach_repair"]["annotations"] == len(overreach["revision_trace"])
    # 告警与完成度同时可见（前端与导出共用同一份视图）
    assert any("执行摘要过度断言" in warning for warning in body["quality_warnings"])
    assert any(entry["category"] == "evidence" and "执行摘要过度断言" in entry["message"]
               for entry in body["categorized_warnings"])
    assert _completeness_view(body)["headline"].startswith("关键研究项缺失")
    assert _completeness_view(body["decision_card"]) == _completeness_view(body)
    # 服务端标注不进摘要字数预算：summary_chars 仍按模型原文计量
    assert body["summary_chars"] == report_quality.report_char_count(original_summary)


def test_q20_endpoint_without_counter_check_leaves_summary_untouched(client, tmp_path, monkeypatch):
    """关闭反方检查时摘要一字不动，并如实标注检查未运行（不误伤既有交付口径）。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    payload = _complete_payload()
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
    )
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "002468", "with_counter_check": False, "with_auto_repair": False},
        headers=headers,
    ).json()
    assert body["ok"] is True, body
    assert body["executive_summary"] == payload["executive_summary"]
    assert body["summary_overreach"]["reviewed"] is False
    assert body["summary_overreach"]["changed"] is False
    assert body["report_revision"] == 1 and body["revision_history"] == []
    assert body["counter_check"]["ok"] is False
