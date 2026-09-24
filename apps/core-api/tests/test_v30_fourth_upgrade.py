"""v30 四次升级（研报整体能力与阅读体验）P0 测试。

覆盖五块：
A. 预期区间五态（report_quality.derive_range_status / normalize_scenarios）——数值校验不信任模型自报；
B. 告警四级分组（categorize_warnings）——确定性关键词分组，每条带影响与建议动作；
C. claims 三层拆分（aspects：事实/比较/判断）——事实层低等级来源即可成立，比较层诚实封顶「部分支持」；
D. 驾驶舱三行分项（pillar_rows）——按 requires 字段确定性归类；
E. 摘要预算 + 端点「仅重写摘要」——超 180 字触发一次重写调用，正文不动；季度派生公式留痕。
"""

from __future__ import annotations

import json

import pytest

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import financial_evidence, report_quality
from investment_steward_core.financial_evidence import FinancialStatementRow


def _fin_row(statement_type: str, period: str, published: str, items: dict[str, str]) -> FinancialStatementRow:
    return FinancialStatementRow(
        symbol="603328", statement_type=statement_type, period_end=period,
        published_raw=published, items=items, identity=f"test-{statement_type}-{period}",
    )


# ---------------------------------------------------------------------------
# A. 预期区间五态（§4）：数值校验不信任模型自报
# ---------------------------------------------------------------------------


def test_range_status_five_states():
    derive = report_quality.derive_range_status
    # 双端有效 → estimated
    assert derive(10.0, 12.0)[0] == "estimated"
    # 倒挂 → invalid（带原因）
    status, note = derive(12.0, 10.0)
    assert status == "invalid" and "不小于上界" in note
    # 含负值 → invalid
    assert derive(-1.0, 5.0)[0] == "invalid"
    # 单端 + 技术位依据 → technical_range
    assert derive(10.0, None, basis="MA20 支撑位")[0] == "technical_range"
    # 单端 + 无技术依据 → estimated（单边，note 说明）
    status, note = derive(10.0, None)
    assert status == "estimated" and "单边" in note
    # 双端缺失 + 事件词 → not_applicable（不强行补价格）
    status, note = derive(None, None, basis="由并购公告验证")
    assert status == "not_applicable" and "事件" in note
    # 双端缺失 + 技术词 → technical_range
    assert derive(None, None, basis="压力位")[0] == "technical_range"
    # 双端缺失 + 无任何依据 → unavailable（缺可靠基准，不冒充）
    status, note = derive(None, None)
    assert status == "unavailable" and "未给出" in note
    # 模型声明 unavailable 且无数值 → 采信声明
    assert derive(None, None, declared="unavailable")[0] == "unavailable"


def test_normalize_scenarios_attaches_range_status():
    out = report_quality.normalize_scenarios([
        {"name": "多头", "trigger": "放量", "expected_range": {"low": 10.0, "high": 12.0}, "range_basis": "历史统计"},
        {"name": "事件", "trigger": "并购", "invalidates": "终止", "range_basis": "由并购公告落地与否验证"},
        {"name": "坏值", "trigger": "x", "expected_range": {"low": 15.6, "high": 14.8}},
    ])
    by_name = {item["name"]: item for item in out}
    assert by_name["多头"]["range_status"] == "estimated"
    assert by_name["事件"]["range_status"] == "not_applicable"
    assert by_name["坏值"]["range_status"] == "invalid"
    assert "不小于上界" in by_name["坏值"]["range_status_note"]


# ---------------------------------------------------------------------------
# B. 告警四级分组（§2.2）：确定性关键词分组，每条带影响与建议动作
# ---------------------------------------------------------------------------


def test_categorize_warnings_four_levels():
    grouped = report_quality.categorize_warnings([
        "剔除 1 条模型引用的、本次未真实取到的来源 id（S9）——防虚构引用。",
        "C2 所引来源未覆盖 市盈率，该判断降级为「未独立验证」（原始表述保留）",
        "情景 1 的预期区间下界不小于上界，字段校验失败。",
        "验证点清单有 2 条的验证时点只写到月份。",
        "正文中未识别到「综合判断」小节。",
    ])
    by_category: dict[str, list[dict]] = {}
    for item in grouped:
        by_category.setdefault(item["category"], []).append(item)
    assert set(by_category) == {"blocking", "evidence", "computation", "format"}
    # 每条都带影响与建议动作；handled 恒 False（处理留痕属后续闭环，本期只读）
    for item in grouped:
        assert item["impact"] and item["action"]
        assert item["handled"] is False
        assert item["category_label"]
    # blocking 级：虚假引用
    assert "S9" in by_category["blocking"][0]["message"]
    # 空列表 → 空输出
    assert report_quality.categorize_warnings([]) == []


# ---------------------------------------------------------------------------
# C. claims 三层拆分（§4）：事实层低等级来源即可成立；比较层诚实封顶「部分支持」
# ---------------------------------------------------------------------------


def test_claim_aspects_three_layer_support_rules():
    claims = report_quality.normalize_claims([
        {
            "claim_id": "C2", "text": "整体偏便宜", "sources": ["S5"],
            "requires": ["pe", "peer_comparison"],
            "aspects": [
                {"kind": "fact", "text": "PE-TTM 19.7", "sources": ["S5"], "requires": ["pe"]},
                {"kind": "comparison", "text": "低于同业中位", "sources": ["S5"], "requires": ["peer_comparison"]},
                {"kind": "judgement", "text": "整体偏便宜", "sources": ["S5"], "requires": ["pe", "peer_comparison"]},
                {"kind": "fact", "text": "引用不存在的来源", "sources": ["S9"], "requires": ["pe"]},
            ],
        },
    ])
    findings = report_quality.check_claims(claims, allowed={"S5"})
    aspects = findings["claims"][0]["aspects"]
    by_kind: dict[str, dict] = {}
    for item in aspects:
        # 同 kind 可能有多条（如一条真来源、一条幽灵来源）：按来源区分取用
        by_kind.setdefault(f"{item['kind']}@{'/'.join(item['sources'])}", item)
    # 事实层：C 级来源真实即可 full（低等级来源不应让基础数值事实整体失败）
    assert by_kind["fact@S5"]["support"] == "full"
    # 比较层：同业样本明细回链尚未接入 → 诚实封顶 partial，note 说明升级条件
    assert by_kind["comparison@S5"]["support"] == "partial"
    assert "回链" in by_kind["comparison@S5"]["note"]
    # 判断层：requires 覆盖齐全（S5 覆盖 pe/pb/ps/peer_comparison）→ 与比较层同口径
    assert by_kind["judgement@S5"]["support"] in ("partial", "full")
    # 引用不存在来源的层 → none（虚构来源不因为「层」而洗白；S9 被剔进 dropped_sources）
    ghost = by_kind["fact@"]
    assert ghost["support"] == "none" and ghost["dropped_sources"] == ["S9"]
    # 每层都带 kind_label 与 support_label（展示层直接可用）
    for item in aspects:
        assert item["kind_label"] and item["support_label"]


# ---------------------------------------------------------------------------
# D. 驾驶舱三行分项（§3）：按 requires 字段确定性归类
# ---------------------------------------------------------------------------


def test_pillar_rows_classified_by_requires():
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "盈利改善", "sources": ["S4"], "requires": ["net_income", "cash_flow"], "importance": "high"},
        {"claim_id": "C2", "text": "估值偏便宜", "sources": ["S5"], "requires": ["pe", "peer_comparison"], "importance": "high"},
        {"claim_id": "C3", "text": "趋势缓涨", "sources": ["S1"], "requires": ["trend", "volume"], "importance": "medium"},
        {"claim_id": "C4", "text": "与柱无关的判断", "sources": [], "requires": [], "importance": "low"},
    ])
    findings = report_quality.check_claims(claims, allowed={"S1", "S4", "S5"})
    rows = report_quality.pillar_rows(findings)
    by_pillar = {row["pillar"]: row for row in rows}
    assert set(by_pillar) == {"fundamental", "valuation", "technical"}          # 无字段归类的 C4 不进分项
    assert by_pillar["fundamental"]["total"] == 1
    assert by_pillar["fundamental"]["label"] == "基本面"
    assert by_pillar["valuation"]["representative"].startswith("估值偏便宜")
    assert by_pillar["technical"]["full"] == 1
    # 模型未输出 claims → 空列表（前端分项区整体不渲染，不冒充有分项）
    assert report_quality.pillar_rows({"claims": []}) == []


# ---------------------------------------------------------------------------
# E. 摘要预算 + 季度派生公式留痕
# ---------------------------------------------------------------------------


def test_validate_report_summary_budget_zones():
    payload = {
        "title": "甲股",
        "report": "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大，结构完好 [S1]\n### 消息面\n公司公告中标大单，市场关注度明显上升 [S2]\n### 基本面\n营收净利稳健增长，经营现金流为正 [S4]\n### 综合判断\n趋势向好但估值已不便宜，总体中性看待，跟踪量能确认。",
        "citations": ["S1", "S2", "S4"],
        "confidence": "medium",
        "limitations": ["a", "b", "c"],
    }
    def _summary(n: int) -> str:
        # 「四句结构。」= 5 字/组，切片后恰为 n 字（report_char_count 只去空白）
        return ("四句结构。" * 40)[:n]
    # 目标区间内（150-165）→ 无摘要告警
    ok_body = {**payload, "executive_summary": _summary(155)}
    ok_quality = report_quality.validate_report(ok_body, allowed={"S1", "S2", "S4"})
    assert not [w for w in ok_quality["warnings"] if "摘要" in w]
    assert ok_quality["summary_chars"] == report_quality.report_char_count(_summary(155))
    # 超 165 未超 180 → 目标区间告警；超 180 → 硬上限告警（触发端点重写）
    over_target = report_quality.validate_report({**payload, "executive_summary": _summary(172)}, allowed={"S1", "S2", "S4"})
    assert any("目标区间" in w for w in over_target["warnings"])
    over_hard = report_quality.validate_report({**payload, "executive_summary": _summary(200)}, allowed={"S1", "S2", "S4"})
    assert any("绝对软上限" in w for w in over_hard["warnings"])
    # 低于 150 → 偏短提示
    short = report_quality.validate_report({**payload, "executive_summary": "太短。"}, allowed={"S1", "S2", "S4"})
    assert any("低于目标区间" in w for w in short["warnings"])


def test_quarterly_series_records_formula_and_inputs():
    rows = [
        _fin_row("income", "2026-06-30", "2026-08-20", {"revenue": "200"}),
        _fin_row("income", "2026-03-31", "2026-04-25", {"revenue": "90"}),
    ]
    series = financial_evidence.derive_quarterly_series(rows, statement_type="income", item_key="revenue")
    by_label = {item["label"]: item for item in series}
    # 差分期：公式写明两个输入报告期（可复算、可回链）
    q2 = by_label["2026Q2"]
    assert q2["is_derived"] is True
    assert "2026-03-31" in q2["formula"] and "2026-06-30" in q2["formula"]
    assert q2["inputs"] == ["2026-03-31", "2026-06-30"]
    # Q1 原值：公式注明不差分
    q1 = by_label["2026Q1"]
    assert q1["is_derived"] is False
    assert "原值" in q1["formula"] and q1["inputs"] == ["2026-03-31"]


# ---------------------------------------------------------------------------
# F. 端点：摘要超限仅重写摘要（正文不动）+ 告警分组透传
# ---------------------------------------------------------------------------


def test_endpoint_rewrites_overlong_summary_only(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    long_summary = (
        "申通快递当前处于行业价格战缓和与公司产能爬坡的关键窗口期，快递行业整体单票收入同比降幅收窄，"
        "公司通过产能投放与数字化改造推动件量增速高于行业平均水平，市场份额呈现稳中有升的态势，"
        "但是单票成本改善幅度仍落后于头部两家同业公司，资产负债率有所抬升，经营现金流对资本开支的覆盖能力偏弱，"
        "叠加加盟网络管理半径扩大带来的服务质量和罚款风险，短期业绩弹性与长期竞争力之间存在明显的时间错配矛盾。"
    )
    report = json.dumps({
        "title": "甲股：缓涨",
        "executive_summary": long_summary,
        "report": "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大，结构完好 [S1]\n### 消息面\n公司公告中标大单，市场关注度明显上升 [S2]\n### 基本面\n营收净利稳健增长，经营现金流为正 [S4]\n### 综合判断\n趋势向好但估值已不便宜，总体中性看待，跟踪量能确认。",
        "citations": ["S1", "S2", "S4"],
        "confidence": "medium",
    }, ensure_ascii=False)
    rewritten_summary = "技术面缓涨，量能配合；基本面稳健。最大风险是行业价格战反复。方向中性偏多。"

    calls: list[dict] = []

    def _fake(_base_url, payload, _key, _timeout):
        calls.append(payload)
        blob = json.dumps(payload, ensure_ascii=False)
        if "改写" in blob and "四句话" in blob:
            return {"choices": [{"message": {"content": rewritten_summary}}]}
        return {"choices": [{"message": {"content": report}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)

    body = test_client.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()
    assert body["ok"] is True, body
    # 摘要被重写：正文未被改写
    assert body["executive_summary"] == rewritten_summary
    assert body["report"] == "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大，结构完好 [S1]\n### 消息面\n公司公告中标大单，市场关注度明显上升 [S2]\n### 基本面\n营收净利稳健增长，经营现金流为正 [S4]\n### 综合判断\n趋势向好但估值已不便宜，总体中性看待，跟踪量能确认。"
    assert body["summary_rewrite"]["needed"] is True
    assert body["summary_rewrite"]["rewritten"] is True
    assert body["summary_rewrite"]["rewritten_chars"] <= report_quality.SUMMARY_HARD_MAX
    assert body["summary_chars"] == report_quality.report_char_count(rewritten_summary)
    # 全程恰好三次模型调用：1 报告 + 1 摘要重写 + 1 反方检查（默认开启；重写绝不二次自动重试）
    assert len(calls) == 3
    assert "改写" in json.dumps(calls[1], ensure_ascii=False)      # 第 2 次调用就是摘要重写
    # 重写后的摘要不再触发硬上限告警 → 无 blocking 级告警
    assert not [w for w in body["categorized_warnings"] if w["category"] == "blocking"]


def test_endpoint_keeps_summary_when_rewrite_still_over_limit(client, tmp_path, monkeypatch):
    """重写仍超限 → 保留原文 + 告警人工复核；绝不静默截断，也绝不二次重试。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    long_summary = "长" * 300
    report = json.dumps({
        "title": "甲股",
        "executive_summary": long_summary,
        "report": "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大 [S1]\n### 消息面\n公司公告中标大单，市场关注度明显上升 [S2]\n### 基本面\n营收净利稳健增长，经营现金流为正 [S4]\n### 综合判断\n趋势向好但估值已不便宜，总体中性看待，跟踪量能确认。",
        "citations": ["S1", "S2", "S4"], "confidence": "low",
    }, ensure_ascii=False)

    calls: list[dict] = []

    def _fake(_base_url, payload, _key, _timeout):
        calls.append(payload)
        # 重写调用也返回超长文本 → 模拟「重写失败」
        return {"choices": [{"message": {"content": report}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)

    body = test_client.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()
    assert body["ok"] is True
    assert body["executive_summary"] == long_summary                       # 原文保留
    assert body["summary_rewrite"]["needed"] is True
    assert body["summary_rewrite"]["rewritten"] is False
    assert "保留原文" in (body["summary_rewrite"]["detail"] or "")
    assert len(calls) == 3                                                  # 报告 + 1 次重写尝试 + 反方检查；不二刷
    assert "改写" in json.dumps(calls[1], ensure_ascii=False)
    # 摘要超限告警仍然在（人工复核入口）
    assert any("摘要" in w["message"] for w in body["categorized_warnings"])
