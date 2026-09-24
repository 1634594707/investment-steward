"""v39 P2（Q16/Q17/Q18）：深研三块拆解的确定性归一与端点落地。

对应路线图 §5 三条验收：
- **Q16** 关键增长结论要有来源与计算过程，**无法拆解的因素不伪造贡献比例**；
- **Q17** 不再只凭「经营现金流 ÷ 净利 = 2.63」认定盈利质量，缺字段就降低结论强度；
- **Q18** 低 PE 不自动等同安全边际；正常化依据不足时不给敏感性/目标价，可比样本要交代剔除规则。

红线：算不出就留 null，服务端不替模型编数字；正文已经说了「盈利质量较好」而口径不足时，
只加降档说明、不改写模型正文。模型调用与取数一律 mock，不联网。
"""

from __future__ import annotations

from typing import Any

from investment_steward_core import report_quality as rq
from test_v32_followup_and_cockpit import _CannedModel, _setup

ALLOWED = {"S1", "S2", "S3", "S4", "S5"}


# ---------------------------------------------------------------------------
# Q16：盈利增长拆解
# ---------------------------------------------------------------------------

def test_q16_keeps_contribution_only_when_it_is_recomputable() -> None:
    rows = rq.normalize_earnings_growth(
        [
            {"factor": "volume", "effect": "业务量上升带动收入", "contribution_pct": None,
             "basis": "8 月业务量同比 +18%（月度经营简报）", "sources": ["S2"]},
            {"factor": "price", "effect": "单票收入企稳", "contribution_pct": 35, "basis": "", "sources": ["S4"]},
            {"factor": "cost_efficiency", "effect": "单位成本下降", "contribution_pct": 20,
             "basis": "单票成本同比 -4%（中报口径）", "formula": "(1 - 4%)", "sources": ["S9"]},
            {"factor": "made_up_factor", "effect": "越界因素不猜归属"},
        ],
        allowed_sources=ALLOWED,
    )
    assert [row["factor"] for row in rows] == ["volume", "price", "cost_efficiency", ""]
    assert rows[3]["factor_raw"] == "made_up_factor"
    # 只给数字、不给计算依据 → 按未量化处理（不保留那个 35%）。
    assert rows[1]["contribution_pct"] is None and "缺少计算依据" in rows[1]["note"]
    # 有依据有算式 → 保留比例；但引用了本次没取到的来源，必须留痕。
    assert rows[2]["contribution_pct"] == 20.0
    assert "不属于本次真实取到的来源" in rows[2]["note"] and rows[2]["sources"] == []
    assert rows[2]["source_note"] == "无来源"
    # 没给比例的条目如实说明「服务端不代填」。
    assert rows[0]["contribution_pct"] is None and "不代填" in rows[0]["note"]
    assert rows[0]["factor_label"] == "业务量"


def test_q16_block_flips_the_completeness_item() -> None:
    payload = {
        "report": "### 基本面\n盈利同比增长。",
        "earnings_growth": [
            {"factor": "volume", "effect": "业务量上升", "basis": "8 月业务量 +18%", "sources": ["S2"]},
            {"factor": "low_base", "effect": "上年同期基数低", "basis": "2025H1 净利 4.53 亿", "sources": ["S4"]},
            {"factor": "cost_efficiency", "effect": "单位成本下降", "basis": "单票成本 -4%", "sources": ["S4"]},
        ],
    }
    item = next(row for row in rq.research_completeness(payload)["items"] if row["key"] == "earnings_decomposition")
    assert item["state"] == "done"
    assert set(item["covered"]) >= {"业务量", "低基数", "成本效率"}
    assert "价格" in item["gaps"] and "非经常性损益" in item["gaps"]


# ---------------------------------------------------------------------------
# Q17：现金流质量
# ---------------------------------------------------------------------------

def test_q17_coverage_ratio_alone_does_not_prove_quality() -> None:
    block = rq.cash_flow_quality(operating_cash_flow=51.23, net_income=19.50, claimed_quality="盈利质量较好")
    assert block["coverage_ratio"] == 2.63
    assert block["verdict"] == "不足以判断盈利质量"
    assert block["strength"] == "insufficient"
    assert "折旧摊销" in block["note"] and "资本开支" in block["note"]
    # 正文已下结论 → 给降档说明（不改写正文，由调用方作为证据告警）。
    assert "正文出现「盈利质量较好」" in block["downgrade_note"]


def test_q17_two_extras_unlock_verdict_and_capex_can_flip_it_weak() -> None:
    strong = rq.cash_flow_quality(
        operating_cash_flow=51.23, net_income=19.50, depreciation_amortization=12.0, capex=20.0,
        operating_cash_flow_period="2026H1", capex_period="2026H1",
    )
    assert strong["free_cash_flow"] == 31.23  # 服务端复算，不采信模型自报
    assert strong["verdict"] == "盈利质量较好"
    assert any("服务端复算" in item for item in strong["extras_present"])  # 同期才允许相减

    weak = rq.cash_flow_quality(
        operating_cash_flow=51.23, net_income=19.50, capex=60.0, working_capital_change=5.0,
        operating_cash_flow_period="2026H1", capex_period="2026H1",
    )
    assert weak["free_cash_flow"] == -8.77
    assert weak["verdict"] == "盈利质量偏弱" and "吞掉经营现金流" in weak["note"]

    no_data = rq.cash_flow_quality(operating_cash_flow=None, net_income=19.5)
    assert no_data["coverage_ratio"] is None and no_data["verdict"] == "无现金流口径"


# ---------------------------------------------------------------------------
# Q18：估值分化与可比性
# ---------------------------------------------------------------------------

def _valuation(**overrides: Any) -> dict[str, Any]:
    base = {
        "pe_ttm": "11.70",
        "pe_percentile": "近1250个交易日 1.7% 分位",
        "pb_percentile": "66.9% 分位",
        "peer_position": "物流同业中位 12.22，本股第4/7",
        "valuation_status": "undetermined",
        "normalized_earnings": {"value": None, "basis": "", "confidence": ""},
        "valuation_cases": [],
        "margin_of_safety": None,
    }
    base.update(overrides)
    return base


def test_q18_low_pe_never_equals_margin_of_safety_and_sensitivity_is_gated() -> None:
    block = rq.valuation_divergence(
        valuation=_valuation(),
        financials={"roe": 9.4, "cycle_position": "利润处于近三年高位"},
        comparables=[
            {"name": "公司A", "business_model": "加盟制快递"},
            {"name": "公司B", "business_model": "加盟制快递"},
            {"name": "公司C", "business_model": "供应链物流", "excluded": True, "exclude_reason": "亏损，PE 无意义"},
        ],
        subject_model="加盟制快递",
    )
    assert block["low_pe_is_not_margin_of_safety"] is True
    assert "安全边际必须建立在正常化盈利" in block["guardrail"]
    assert block["valuation_evidence_state"] == "normalization_unverified"
    # 正常化盈利未验证 → 不给敏感性/目标价。
    assert block["sensitivity_allowed"] is False
    assert "不给敏感性或目标价" in block["sensitivity_note"]
    review = block["comparable_review"]
    assert review["sample_count"] == 2 and review["comparability"] == "thin"
    assert "亏损，PE 无意义" in review["exclusion_rule"]
    assert set(block["explained_axes"]) == {"roe", "cycle"}

    assessed = rq.valuation_divergence(
        valuation=_valuation(
            valuation_status="undervalued",
            normalized_earnings={"value": 19.5, "basis": "剔除一次性收益后 TTM", "confidence": "medium"},
            valuation_cases=[{"name": "base", "earnings": 19.5, "multiple": 12, "fair_value": 17.8}],
            margin_of_safety=0.16,
        ),
        financials={"roe": 9.4, "equity_basis": "归母净资产，不含少数股东权益"},
        comparables=[{"name": f"公司{i}", "business_model": "加盟制快递"} for i in range(5)],
        subject_model="加盟制快递",
    )
    assert assessed["sensitivity_allowed"] is True
    assert assessed["comparable_review"]["comparability"] == "ok"
    assert assessed["explanation_state"] == "done"


def test_q18_mixed_business_models_are_flagged_as_only_partially_comparable() -> None:
    block = rq.valuation_divergence(
        valuation=_valuation(),
        comparables=[
            {"name": "公司A", "business_model": "加盟制快递"},
            {"name": "公司B", "business_model": "合同物流"},
            {"name": "公司C", "business_model": "跨境专线"},
            {"name": "公司D", "business_model": "合同物流"},
        ],
        subject_model="加盟制快递",
    )
    review = block["comparable_review"]
    assert review["comparability"] == "mixed" and review["comparability_label"] == "模式混杂"
    assert "仅 1 只标注与标的同模式" in review["note"] and "按部分可比对待" in review["note"]
    assert rq.comparable_review([], subject_model="加盟制快递")["comparability"] == "unavailable"


# ---------------------------------------------------------------------------
# 端点：三块拆解真的随报告下发，且告警口径接上
# ---------------------------------------------------------------------------

def _canned_report() -> dict[str, Any]:
    from test_v32_followup_and_cockpit import _v28_report_payload

    payload = _v28_report_payload()
    payload["report"] = str(payload.get("report", "")) + "\n盈利质量较好，现金流覆盖充足。"
    payload["cash_flow_quality"] = {"operating_cash_flow": 51.23, "net_income": 19.5}
    payload["earnings_growth"] = [
        {"factor": "volume", "effect": "业务量上升", "basis": "8 月业务量 +18%", "sources": ["S2"]},
        {"factor": "low_base", "effect": "低基数", "contribution_pct": 40, "sources": ["S4"]},
    ]
    payload["valuation_divergence"] = {
        "comparables": [{"name": "A", "business_model": "快递"}, {"name": "B", "business_model": "快递"}],
    }
    payload["business_model"] = "加盟制快递"
    return payload


def test_endpoint_ships_the_three_deep_research_blocks(client, tmp_path, monkeypatch) -> None:
    _CannedModel(monkeypatch, report=_canned_report())
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    cash = body["cash_flow_quality"]
    assert (cash["coverage_ratio"], cash["verdict"]) == (2.63, "不足以判断盈利质量")
    assert any("现金流口径不足" in item for item in body["quality_warnings"]), body["quality_warnings"]
    earnings = body["earnings_growth"]
    assert [row["factor"] for row in earnings] == ["volume", "low_base"]
    assert earnings[1]["contribution_pct"] is None  # 40% 没有计算依据 → 不采信
    divergence = body["valuation_divergence"]
    assert divergence["low_pe_is_not_margin_of_safety"] is True
    assert divergence["comparable_review"]["comparability"] == "thin"
    # 完成度看见了三块（盈利拆解 partial→有结构化块；现金流仍缺两项）。
    items = {row["key"]: row["state"] for row in body["research_completeness"]["items"]}
    assert items["earnings_decomposition"] in ("partial", "done")
    assert items["cash_flow_and_capex"] == "partial"


# ---------------------------------------------------------------------------
# v39 真机回归（2026-09-20 首份 2.9 报告暴露的缺陷）
# 报告：002468 申通快递 · deepseek-v4.1-flash · 深研版 3274 字。
# 这一组只锁「系统不能把没拿到的东西说成拿到了」：编号、覆盖、样本数、结论强度。
# ---------------------------------------------------------------------------

def test_claim_ids_are_numbered_by_the_server_when_the_model_omits_them() -> None:
    """真机：7 条 claims 全部没有 claim_id → 「部分支撑（2/3）」点不出数的是谁，
    追问闸门也失去剔除自造编号的锚点。编号是服务端职责。"""
    rows = rq.normalize_claims([
        {"text": "A", "sources": ["S4"], "importance": "high"},
        {"text": "B", "sources": ["S4"], "importance": "high"},
        {"claim_id": "C7", "text": "C", "sources": ["S5"], "importance": "medium"},
    ])
    assert [row["claim_id"] for row in rows] == ["C1", "C2", "C7"]


def test_duplicate_claim_ids_are_renumbered_not_reused() -> None:
    rows = rq.normalize_claims([{"claim_id": "C1", "text": "A"}, {"claim_id": "C1", "text": "B"}])
    assert rows[0]["claim_id"] == "C1"
    assert rows[1]["claim_id"] != "C1" and rows[1]["claim_id"].startswith("C")


def test_cash_flow_derived_items_alone_do_not_support_a_positive_verdict() -> None:
    """真机：只有资本开支 + 自由现金流（两者都由经营现金流减出来）就被判「盈利质量较好」，
    而正文同时写「利润质量的解释只能停留在覆盖倍数层面，属证据不足」。"""
    block = rq.cash_flow_quality(
        operating_cash_flow=51.23, net_income=19.5, capex=31.52, claimed_quality="盈利质量较好",
        operating_cash_flow_period="TTM-2026H1", capex_period="2026H1",
    )
    assert block["verdict"] == rq.CASH_FLOW_VERDICT_INSUFFICIENT
    assert block["explanatory_present"] == []
    assert block["derived_present"] == ["资本开支"]  # 跨期不相减 → 连自由现金流都不该出现
    assert "派生" in block["note"]
    assert "按证据不足对待" in block["downgrade_note"]


def test_cash_flow_positive_verdict_needs_an_explanatory_item() -> None:
    positive = rq.cash_flow_quality(
        operating_cash_flow=51.23, net_income=19.5, depreciation_amortization=18.4, capex=31.52,
        operating_cash_flow_period="2026H1", capex_period="2026H1",
    )
    assert positive["verdict"] == rq.CASH_FLOW_VERDICT_STRONG
    # 负向结论是算术事实，不需要解释项也成立。
    negative = rq.cash_flow_quality(
        operating_cash_flow=20.0, net_income=19.5, capex=31.52,
        operating_cash_flow_period="2026H1", capex_period="2026H1",
    )
    assert negative["verdict"] == rq.CASH_FLOW_VERDICT_WEAK


def test_comparable_sample_count_follows_the_server_snapshot_not_the_model() -> None:
    """真机：模型自述「5 只样本，剔除 1 只」，同业快照实为 20 只池、PE 口径 7 只可比。"""
    review = rq.comparable_review(
        [{"name": f"公司{i}", "business_model": "快递"} for i in range(5)]
        + [{"name": "公司C", "excluded": True, "exclude_reason": "亏损，PE 无意义"}],
        server_peer_count=20,
        server_comparable_basis={"pe_ttm": 7, "pb_mrq": 20},
    )
    assert review["sample_count"] == 7
    assert review["model_claimed_sample_count"] == 5
    assert "模型自述可比样本 5 只，服务端同业快照为 7 只" in review["count_conflict"]


def test_valuation_divergence_reads_numbers_from_the_server_snapshot() -> None:
    """真机：块里 pe_ttm 存成模型转述的字符串，PB 1.94 / 66.9% 分位明明取得到却显示「未取到」。"""
    block = rq.valuation_divergence(
        valuation={"pe_ttm": "PE(TTM) 11.70", "pe_percentile": "近1250日 1.7% 分位"},
        server_metrics={
            "pe_ttm": 11.69555428, "pb_mrq": 1.93994513,
            "percentiles": {"pe_ttm": 1.7, "pb_mrq": 66.9},
            "peer_median": {"pe_ttm": 12.22, "pb_mrq": 1.52},
            "peer_count": 20, "peer_median_basis": {"pe_ttm": 7, "pb_mrq": 20},
            "percentile_window_bars": 1250, "percentile_window_from": "2021-07-27",
        },
    )
    assert block["pe_ttm"] == 11.69555428
    assert block["pb_mrq"] == 1.93994513
    assert block["pe_percentile"] == "1.7%，近1250个交易日自2021-07-27"
    assert block["pb_percentile"] == "66.9%，近1250个交易日自2021-07-27"
    assert block["metrics_source"] == "服务端估值快照"
    assert block["peer_median"] == {"pe_ttm": 12.22, "pb_mrq": 1.52}
    assert block["comparable_review"]["sample_count"] == 7


def test_completeness_never_counts_a_declared_but_missing_field_as_covered() -> None:
    """真机：完成度写「已完成（已覆盖 折旧摊销、营运资本变动）」，同一份导出里这两项却是「未取到」——
    因为正文那句「来源未提供折旧摊销与营运资本变动明细」被关键词探针当成了覆盖。"""
    view = rq.research_completeness({
        "cash_flow_quality": {
            "operating_cash_flow": 51.23, "net_income": 19.5, "coverage_ratio": 2.63,
            "depreciation_amortization": None, "working_capital": None,
            "capex": 31.52, "free_cash_flow": 19.71,
            "verdict": rq.CASH_FLOW_VERDICT_INSUFFICIENT,
        },
        "valuation_divergence": {
            "roe": None, "equity_basis": "", "cycle_note": "",
            "comparable_review": {"sample_count": 7},
        },
        "earnings_growth": [
            {"factor": "volume", "effect": "业务量上升带动收入"},
            {"factor": "low_base", "effect": "上年同期基数低"},
            {"factor": "cost_efficiency", "effect": "单票成本下降"},
        ],
        "report": "来源未提供折旧摊销与营运资本变动明细；ROE 17.95%（S4派生），净资产口径未说明；"
                  "价格竞争若重启将压制单票收入。",
        "scenarios": [{"name": "乐观"}, {"name": "中性"}, {"name": "悲观"}],
        "limitations": ["a", "b", "c", "d", "e"],
        "counter_check": {"strongest_counter": "PB 分位偏高"},
    })
    items = {item["key"]: item for item in view["items"]}
    cash = items["cash_flow_and_capex"]
    assert "折旧摊销" not in cash["covered"] and "折旧摊销" in cash["gaps"]
    assert cash["state"] == rq.COMPLETENESS_PARTIAL
    assert any("按缺口计" in line for line in cash["evidence"])
    valuation = items["valuation_explanation"]
    assert "ROE/净资产回报口径" in valuation["gaps"]
    assert any("正文提到但估值块里未取到" in line for line in valuation["evidence"])
    earnings = items["earnings_decomposition"]
    assert "价格" in earnings["gaps"]  # 正文写了「价格竞争」，但没拆进拆解块
    assert view["complete"] is False


# ---------------------------------------------------------------------------
# v39 第二轮真机回归（2026-09-20 首份 2.10 报告）：三处「把没做的说成做了」
# ---------------------------------------------------------------------------

def test_percentile_is_never_shown_without_its_window() -> None:
    """真机：块里只剩「1.7%」，丢掉了「近1250个交易日（2021-07-27 起）」——
    而反方检查点名的正是「分位基于样本起点、易被读成绝对低估」。"""
    block = rq.valuation_divergence(
        valuation={},
        server_metrics={
            "pe_ttm": 11.69555428, "pb_mrq": 1.93994513,
            "percentiles": {"pe_ttm": 1.7, "pb_mrq": 66.9},
            "percentile_window_bars": 1250, "percentile_window_from": "2021-07-27",
        },
    )
    assert block["pe_percentile"] == "1.7%，近1250个交易日自2021-07-27"
    assert block["pb_percentile"] == "66.9%，近1250个交易日自2021-07-27"


def test_comparability_does_not_claim_business_model_was_checked_without_one() -> None:
    """真机：模型从未被要求给出标的商业模式，`subject_model` 恒为空，
    代码却跳过比对直接判 ok 并写「商业模式口径已核对」。"""
    rows = [{"name": f"公司{i}", "business_model": "加盟制快递"} for i in range(7)]
    unverified = rq.comparable_review(rows, server_peer_count=20, server_comparable_basis={"pe_ttm": 7})
    assert unverified["comparability"] == "unverified"
    assert unverified["comparability_label"] == "模式未核对"
    assert "未核对" in unverified["note"] and "已核对。" not in unverified["note"]
    checked = rq.comparable_review(
        rows, subject_model="加盟制快递", server_peer_count=20, server_comparable_basis={"pe_ttm": 7},
    )
    assert checked["comparability"] == "ok" and "已核对" in checked["note"]


def test_exclusion_count_follows_the_peer_pool_not_the_model_list() -> None:
    """真机：模型名单里只列了 1 只「已剔除」，同业快照实为 20 只池里 13 只被排除。"""
    review = rq.comparable_review(
        [{"name": f"公司{i}", "business_model": "快递"} for i in range(7)]
        + [{"name": "公司C", "excluded": True, "exclude_reason": "亏损"}],
        subject_model="快递",
        server_peer_count=20,
        server_comparable_basis={"pe_ttm": 7},
    )
    assert review["sample_count"] == 7
    assert review["excluded_count"] == 13
    assert "剔除 13 只" in review["note"]


def test_free_cash_flow_is_not_derived_across_mismatched_periods() -> None:
    """真机：TTM 经营现金流 51.23 − H1 资本开支 14.47 = 36.76 被当成事实报出。"""
    mismatched = rq.cash_flow_quality(
        operating_cash_flow=51.23, net_income=19.5, capex=14.47,
        operating_cash_flow_period="TTM-2026H1", capex_period="2026H1",
    )
    assert mismatched["free_cash_flow"] is None
    assert mismatched["derived_present"] == ["资本开支"]
    assert "跨期相减" in mismatched["note"]
    aligned = rq.cash_flow_quality(
        operating_cash_flow=51.23, net_income=19.5, capex=14.47,
        operating_cash_flow_period="2026H1", capex_period="2026H1",
    )
    assert aligned["free_cash_flow"] == 36.76
    assert aligned["free_cash_flow_derived"] is True


def test_block_field_matching_is_exact_not_substring() -> None:
    """真机：`free_cash_flow: None` 却因 `free_cash_flow_derived: False` 被前缀匹配认成有值，
    于是同一份导出里「自由现金流 未取到」与「已覆盖 自由现金流」并存。"""
    view = rq.research_completeness({
        "cash_flow_quality": {
            "operating_cash_flow": 51.23, "net_income": 19.5, "coverage_ratio": 2.63,
            "operating_cash_flow_period": "TTM", "capex_period": "2026H1",
            "depreciation_amortization": None, "working_capital": None,
            "capex": 14.47, "free_cash_flow": None, "free_cash_flow_derived": False,
            "verdict": rq.CASH_FLOW_VERDICT_INSUFFICIENT,
        },
        "report": "同一统计期内自由现金流可按经营现金流减资本开支理解。",
    })
    item = {row["key"]: row for row in view["items"]}["cash_flow_and_capex"]
    assert "自由现金流" not in item["covered"]
    assert "自由现金流" in item["gaps"]


def test_sentence_like_business_model_is_unverified_not_mixed() -> None:
    """真机：标的商业模式被写成整句话，与样本的「快递服务」精确相等必然为 0，
    代码却据此断言「混入不同商业模式的同业中位数会误导」。"""
    rows = [{"name": f"公司{i}", "business_model": "快递服务"} for i in range(7)]
    sentence = "申通快递为快递服务运营商，来源显示其业务口径为快递服务业务收入，加盟或直营模式未在来源中明确说明"
    review = rq.comparable_review(rows, subject_model=sentence, server_peer_count=20,
                                  server_comparable_basis={"pe_ttm": 7})
    assert review["comparability"] == "unverified"
    assert "整句描述" in review["note"]
    short = rq.comparable_review(rows, subject_model="加盟制快递", server_peer_count=20,
                                 server_comparable_basis={"pe_ttm": 7})
    assert short["comparability"] == "mixed"
    # 对不上时只陈述事实并列出样本口径，不替读者断言中位数一定被带偏。
    assert "仅 0 只标注与标的同模式" in short["note"] and "样本口径：快递服务" in short["note"] and "会误导" not in short["note"]
    same = rq.comparable_review(
        [{"name": f"公司{i}", "business_model": "快递服务运营商"} for i in range(7)],
        subject_model="快递服务", server_peer_count=20, server_comparable_basis={"pe_ttm": 7},
    )
    assert same["comparability"] == "ok"  # 规范化后互为包含 → 算同模式
