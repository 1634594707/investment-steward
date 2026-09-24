"""v28「研报与战法雷达二次优化」测试（2026-09-12 二次方案 P0 范围）。

覆盖四块 P0 能力，每条都以「可确定性复算 / 不编造 / 不改写模型原话」为前提：

A. 情景概率降级（§2.4）——无后验基准时**只给低/中/高等级**，数字概率一律置空但保留原值可审计；
B. 证据支撑链（§2.3）——支撑强度由服务端确定性判定（不采信模型自评），数据时效按来源阈值判定，
   缺口 / 段 `missing` 双口径并列，摘要降级提示**不改写 executive_summary 一个字符**；
C. 首屏结论卡（§2.1/§2.2）——纯聚合既有结构化字段，零新判断；
D. 信号状态机 + 质量闸门（§3.2/§3.6）——四态确定性判定；`rank_score = 图形分 × 数据质量因子`；
   后验因子不可用时显式标注，**不用 0 冒充**。

行情 / 新闻 / 财报 / 估值取数一律 monkeypatch，模型调用一律 mock `_post_json`，绝不真实联网。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import report_quality, tactics, tactics_score

_NAME_BY_ID = {item["id"]: item["name"] for item in tactics.catalog()}


def _bars(count: int, start: str = "2026-01-01") -> list[dict[str, object]]:
    """日期严格升序的合成日线（与 v24 测试同一口径）。"""
    base = date.fromisoformat(start)
    return [
        {
            "timestamp": (base + timedelta(days=index)).isoformat(),
            "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1_000_000,
        }
        for index in range(count)
    ]


def _signal(tactic_id: str, day: str, direction: str, trigger_price: float | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "tactic_id": tactic_id,
        "tactic_name": _NAME_BY_ID[tactic_id],
        "direction": direction,
        "detail": "test",
        "date": day,
    }
    if trigger_price is not None:
        payload["trigger_price"] = trigger_price
    return payload


def _report_json(**extra) -> str:
    base = {
        "title": "甲股：缓涨",
        "executive_summary": "结论：技术面缓涨，基本面稳健。",
        "report": "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大，结构完好 [S1]\n### 消息面\n公司公告中标大单，市场关注度明显上升 [S2]\n### 基本面\n营收净利稳健增长，经营现金流为正 [S4]\n### 综合判断\n趋势向好但估值已不便宜，总体中性看待，跟踪量能确认。",
        "citations": ["S1", "S2", "S4"],
        "confidence": "medium",
        "valuation": {"verdict": "合理"},
    }
    base.update(extra)
    return json.dumps(base, ensure_ascii=False)


def _model_with_calls(report: str, counter: str | None) -> tuple[object, list[dict]]:
    """第 1 次调用返回报告 JSON，第 2 次返回反方检查 JSON（counter=None 时两次都返回报告）。"""
    calls: list[dict] = []

    def _fake(base_url, json_payload, api_key, timeout):
        calls.append(json_payload)
        content = report if len(calls) == 1 else (counter if counter is not None else report)
        return {"choices": [{"message": {"content": content}}]}

    return _fake, calls


# ---------------------------------------------------------------------------
# A. 情景概率降级（§2.4）
# ---------------------------------------------------------------------------


def test_probability_level_boundaries():
    """等级只按固定分档换算；给不出值就是空串，不猜一个「中」出来。"""
    assert report_quality.probability_level_from_value(None) == ""
    assert report_quality.probability_level_from_value(0.2) == "低"
    assert report_quality.probability_level_from_value(0.5) == "中"
    assert report_quality.probability_level_from_value(0.9) == "高"


def test_scenario_probability_is_never_numeric_without_posterior_basis():
    """★ 本期无后验统计 → 小数概率一律置空，等级表达 + 原值留档，绝不拿 0 或假数字冒充。"""
    items = report_quality.normalize_scenarios([
        {"name": "乐观", "trigger": "放量站稳", "invalidates": "跌破 60 日线", "horizon": "10 个交易日",
         "probability": 0.4, "range_basis": "按历史同类形态估算"},
    ])
    item = items[0]
    assert item["probability"] is None
    assert item["probability_level"] == "中"          # 0.4 落在 [1/3, 2/3)
    assert item["probability_raw"] == 0.4             # 原值保留，可审计
    assert item["probability_basis"] == report_quality.PROBABILITY_BASIS_NONE
    assert "无历史基准" in item["probability_note"]
    assert item["range_basis"] == "按历史同类形态估算"


def test_scenario_declared_level_is_honored_only_when_valid():
    """模型自报的等级在词表内则采信；越界则按原值换算（只标注，不静默改写）。"""
    valid, invalid = report_quality.normalize_scenarios([
        {"name": "A", "probability": 0.1, "probability_level": "高"},
        {"name": "B", "probability": 0.1, "probability_level": "极高"},
    ])
    assert valid["probability_level"] == "高"          # 采信模型自报
    assert valid["probability"] is None
    assert invalid["probability_level"] == "低"        # 越界 → 按 0.1 换算


# ---------------------------------------------------------------------------
# B. 证据支撑链（§2.3）
# ---------------------------------------------------------------------------


def test_claim_support_is_full_partial_none_and_not_model_self_reported():
    """★ 支撑强度必须由服务端算：模型自评写 support=full 也**不采信**。"""
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "营收与现金流改善", "sources": ["S4"],
         "requires": ["revenue", "cash_flow"], "importance": "high"},
        {"claim_id": "C2", "text": "主业减亏", "sources": ["S2"],
         "requires": ["net_income_deducted"], "importance": "high"},
        {"claim_id": "C3", "text": "外部传闻", "sources": ["S9"],
         "requires": ["price_trend"], "importance": "medium"},
    ])
    result = report_quality.check_claims(claims, allowed={"S1", "S2", "S4"})
    by_id = {item["claim_id"]: item for item in result["claims"]}
    assert by_id["C1"]["support"] == report_quality.SUPPORT_FULL
    assert by_id["C1"]["support_label"] == "完整支持"
    assert by_id["C2"]["support"] == report_quality.SUPPORT_PARTIAL
    assert by_id["C3"]["support"] == report_quality.SUPPORT_NONE
    assert result["support_counts"] == {"full": 1, "partial": 1, "none": 1}
    assert result["version"] == report_quality.CLAIM_SUPPORT_VERSION
    # 核心判断 = importance=high 的两条；C1 完整支持 → 核心未降级集合里不该出现 C1
    assert result["core_claims"] == ["C1", "C2"]
    assert result["core_downgraded"] == ["C2"]
    # 旧字段保持向后兼容（v27 断言依赖）
    assert result["supported"] == 1 and result["downgraded"] == ["C2"] and result["no_source"] == ["C3"]


def test_claim_missing_has_two_parallel_calibers():
    """服务端算的 `missing`（可读缺口）与模型自述的 `model_missing` 并列，互不覆盖。"""
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "主业减亏", "sources": ["S2"],
         "requires": ["net_income_deducted"], "missing": ["缺少分部数据"]},
    ])
    item = report_quality.check_claims(claims, allowed={"S2"})["claims"][0]
    assert any("扣非净利润" in note for note in item["missing"])
    assert item["model_missing"] == ["缺少分部数据"]


def test_staleness_is_unavailable_without_both_asof_and_today():
    """★ 缺任一时**不做**时效判定并如实标注未校验——不拿「未知」当「通过」。"""
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "趋势", "sources": ["S1"], "requires": ["price_trend"]},
    ])
    no_today = report_quality.check_claims(claims, allowed={"S1"}, source_asof={"S1": "2026-01-01"})
    assert no_today["staleness_checked"] is False
    assert no_today["claims"][0]["staleness_checked"] is False
    assert no_today["claims"][0]["stale"] is False

    no_asof = report_quality.check_claims(claims, allowed={"S1"}, today=date(2026, 9, 12))
    assert no_asof["staleness_checked"] is False


def test_staleness_uses_per_source_threshold():
    """S1（行情）阈值 7 天：42 天前的行情必须被判过期；阈值随来源类别不同（S4 财报 180 天）。"""
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "趋势", "sources": ["S1"], "requires": ["price_trend"], "importance": "high"},
        {"claim_id": "C2", "text": "财报", "sources": ["S4"], "requires": ["revenue"], "importance": "high"},
    ])
    result = report_quality.check_claims(
        claims, allowed={"S1", "S4"},
        source_asof={"S1": "2026-08-01", "S4": "2026-08-30"}, today=date(2026, 9, 12),
    )
    by_id = {item["claim_id"]: item for item in result["claims"]}
    assert by_id["C1"]["stale"] is True               # 42 天 > 7 天
    assert by_id["C2"]["stale"] is False              # 13 天 < 180 天
    assert result["stale_claims"] == ["C1"]
    assert any("时效上限" in warning for warning in result["warnings"])


def test_conflict_signals_are_caliber_level_only():
    """口径信号只判等级跨度 / 单一来源 / 未登记来源，**不做内容矛盾检测**。"""
    single = report_quality.claim_conflict_signals(["S2"], requires=["revenue"])
    assert [item["kind"] for item in single] == ["single_source"]
    gap = report_quality.claim_conflict_signals(["S1", "S9"], requires=["revenue"])
    kinds = {item["kind"] for item in gap}
    assert "quality_gap" in kinds and "unknown_source" in kinds
    note = report_quality.check_claims([], allowed=set())["conflict_note"]
    assert "不做内容层面的矛盾检测" in note


# ---------------------------------------------------------------------------
# C. 摘要降级提示 + 首屏结论卡（§2.1/§2.2）
# ---------------------------------------------------------------------------


def test_summary_downgrade_notes_cover_partial_none_and_stale():
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "主业减亏", "sources": ["S2"], "requires": ["net_income_deducted"], "importance": "high"},
        {"claim_id": "C2", "text": "外部传闻", "sources": ["S9"], "requires": ["price_trend"], "importance": "high"},
        {"claim_id": "C3", "text": "趋势", "sources": ["S1"], "requires": ["price_trend"], "importance": "high"},
    ])
    findings = report_quality.check_claims(
        claims, allowed={"S1", "S2"},
        source_asof={"S1": "2026-01-01", "S2": "2026-09-01"}, today=date(2026, 9, 12),
    )
    notes = "\n".join(report_quality.summary_downgrade_notes(findings))
    assert "部分支持" in notes
    assert "无来源支撑" in notes
    assert "时效阈值" in notes


def test_no_claims_yields_honest_note_and_none_support_flags():
    """没输出 claims 时不假装「已核验」：支撑度返回 None，并给出「未经核验」提示。"""
    findings = report_quality.check_claims([], allowed={"S1"})
    assert findings["core_conclusion_supported"] is None
    assert "未经核验" in report_quality.summary_downgrade_notes(findings)[0]


def test_summary_downgrade_never_rewrites_executive_summary():
    """★ 软校验铁律：降级只以结构化字段随报告返回，executive_summary 原话一字不改。"""
    original = "结论：技术面缓涨，基本面稳健。"
    payload = {
        "executive_summary": original,
        "report": "### 技术面\nx [S1]\n### 消息面\ny\n### 基本面\nz\n### 综合判断\nw",
        "citations": ["S1"],
        "limitations": ["a", "b", "c"],
        "claims": [{"claim_id": "C1", "text": "主业减亏", "sources": ["S1"],
                    "requires": ["net_income_deducted"], "importance": "high"}],
    }
    result = report_quality.validate_report(payload, allowed={"S1"})
    assert payload["executive_summary"] == original          # 原话未被改写
    assert result["summary_downgrade_notes"]                 # 但降级提示必须给出
    assert isinstance(result["conclusion"], dict)


def test_decision_card_aggregates_without_new_judgements():
    findings = report_quality.check_claims(
        report_quality.normalize_claims([
            {"claim_id": "C1", "text": "营收改善", "sources": ["S4"], "requires": ["revenue"], "importance": "high"},
            {"claim_id": "C2", "text": "主业减亏", "sources": ["S2"], "requires": ["net_income_deducted"], "importance": "high"},
        ]),
        allowed={"S1", "S2", "S4"},
    )
    card = report_quality.decision_card(
        symbol="600001",
        conclusion={"statement": "短期偏多但量能不足", "direction": "bullish",
                    "core_conflict": "量能未跟上", "confidence": "medium"},
        evidence_quality=report_quality.evidence_quality_report({"S1", "S2", "S4"}),
        claim_findings=findings,
        counter_check={"ok": True, "strongest_support": "现金流改善 [S4]", "strongest_support_source": "S4",
                       "strongest_support_date": "2026-08-30", "strongest_counter": "新闻仅标题 [S2]",
                       "strongest_counter_source": "S2", "strongest_counter_date": "2026-09-01"},
        watchpoints=report_quality.normalize_watchpoints([
            {"signal": "量能回升", "verify_by": "2026-10-15", "expected_if_true": "站稳"},
        ]),
        valuation={"verdict": "偏贵", "basis": "PE 分位 80%", "peer_position": ""},
        peer_count=14,
    )
    assert card["version"] == report_quality.DECISION_CARD_VERSION
    assert card["conclusion"]["statement"] == "短期偏多但量能不足"
    assert card["evidence_strength"]["counts"] == {"A": 0, "B": 2, "C": 1, "D": 0}
    assert card["evidence_strength"]["support_counts"] == {"full": 1, "partial": 1, "none": 0}
    # 结论卡只搬运，不新增判断：最大支持/反证直接来自反方检查的结构化字段
    assert card["max_support"]["text"] == "现金流改善 [S4]"
    assert card["max_counter"]["source"] == "S2"
    assert "同业样本 14 只" in card["valuation_label"]
    assert card["next_action"]["due_on"] == "2026-10-15"


def test_decision_card_marks_missing_counter_check_and_peer_sample():
    """反方检查未执行 / 同业样本缺失都必须**显式说明**，不拿空白当通过。"""
    card = report_quality.decision_card(
        symbol="600001",
        conclusion={"statement": "", "direction": "", "core_conflict": "", "confidence": ""},
        evidence_quality=report_quality.evidence_quality_report({"S1"}),
        claim_findings=report_quality.check_claims([], allowed={"S1"}),
        counter_check=None,
        watchpoints=[],
        valuation={"verdict": "合理", "basis": "", "peer_position": ""},
        peer_count=None,
    )
    assert card["max_support"] is None and card["max_counter"] is None
    assert "未执行反方检查" in card["counter_check_reason"]
    assert "同业样本数未取到" in card["valuation_label_note"]
    assert "同业样本" not in card["valuation_label"]
    assert card["next_action"] is None


# ---------------------------------------------------------------------------
# D. 信号状态机（§3.2）
# ---------------------------------------------------------------------------


def test_signal_state_new_when_trigger_is_the_latest_bar():
    bars = _bars(60)
    state = tactics_score.signal_state(
        direction="bullish", signal_date=str(bars[-1]["timestamp"])[:10], trigger_price=10.0, bars=bars,
    )
    assert state["state"] == tactics_score.STATE_NEW
    assert state["observed_bars"] == 0
    assert state["version"] == tactics_score.SIGNAL_STATE_VERSION


def test_signal_state_neutral_never_confirms():
    """中性形态没有多空方向 → 只可能是 new/pending，永远不判 confirmed/invalidated。"""
    bars = _bars(60)
    state = tactics_score.signal_state(
        direction="neutral", signal_date=str(bars[10]["timestamp"])[:10], trigger_price=None, bars=bars,
    )
    assert state["state"] == tactics_score.STATE_PENDING
    assert "中性形态" in state["reason"]


def test_signal_state_confirms_on_two_holding_closes_with_volume():
    bars = _bars(60)
    state = tactics_score.signal_state(
        direction="bullish", signal_date=str(bars[10]["timestamp"])[:10], trigger_price=10.0, bars=bars,
    )
    assert state["state"] == tactics_score.STATE_CONFIRMED
    assert state["observed_bars"] == tactics_score.CONFIRM_WINDOW_BARS


def test_signal_state_fails_closed_when_volume_missing():
    """★ 量能字段缺失时确认条件按「不可判定」处理 → 保守地不给 confirmed。"""
    bars = _bars(60)
    for bar in bars:
        bar["volume"] = None
    state = tactics_score.signal_state(
        direction="bullish", signal_date=str(bars[10]["timestamp"])[:10], trigger_price=10.0, bars=bars,
    )
    assert state["state"] == tactics_score.STATE_PENDING
    assert "量能" in state["reason"]


def test_signal_state_invalidates_on_breakdown_with_volume_expansion():
    bars = _bars(60)
    for index in range(11, 14):                      # 触发日之后三根：跌回触发日平台下方并放量
        bars[index]["close"] = 9.0
        bars[index]["low"] = 9.0
        bars[index]["high"] = 9.1
        bars[index]["volume"] = 2_000_000
    state = tactics_score.signal_state(
        direction="bullish", signal_date=str(bars[10]["timestamp"])[:10], trigger_price=10.0, bars=bars,
    )
    assert state["state"] == tactics_score.STATE_INVALIDATED


def test_signal_state_unknown_when_trigger_date_not_in_bars():
    bars = _bars(60)
    state = tactics_score.signal_state(
        direction="bullish", signal_date="1999-01-01", trigger_price=10.0, bars=bars,
    )
    assert state["state"] == tactics_score.STATE_NEW
    assert "无法判定" in state["reason"]


# ---------------------------------------------------------------------------
# E. 质量闸门（§3.6）
# ---------------------------------------------------------------------------


def test_rank_score_is_graphic_score_times_quality_factor():
    bars = _bars(80)
    day = str(bars[-1]["timestamp"])[:10]
    result = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], bars)
    assert result["rank_score"] == round(result["tactic_score"] * result["quality_factor"], 1)
    assert result["rank_basis"].startswith("综合分 = 图形分")
    assert "后验因子当前不可用" in result["rank_basis"]
    # v27 并列口径保持：signal_score 与 tactic_score 同值，historical_edge 不编造
    assert result["signal_score"] == result["tactic_score"]
    assert result["historical_edge"] is None
    assert result["posterior"]["available"] is False


def test_gate_blocks_rows_with_missing_bar_fields():
    bars = _bars(80)
    for bar in bars[-5:]:
        bar["close"] = None
    day = str(bars[-1]["timestamp"])[:10]
    result = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], bars)
    assert result["hard_blocked"] is True
    assert result["eligible"] is False
    assert any("硬性不可排名" in reason for reason in result["gating_reasons"])


def test_gate_blocks_rows_with_broken_bar_dates():
    bars = _bars(80)
    bars[-1]["timestamp"] = bars[-2]["timestamp"]        # 重复日期
    day = str(bars[-3]["timestamp"])[:10]
    result = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], bars)
    assert result["hard_blocked"] is True
    assert result["eligible"] is False


def test_gate_keeps_insufficient_sample_out_of_default_board():
    bars = _bars(20)
    day = str(bars[-1]["timestamp"])[:10]
    result = tactics_score.score([_signal("macd_golden_cross", day, "bullish")], bars)
    assert result["quality_factor"] == 0.0
    assert result["eligible"] is False
    assert any("样本不足" in reason for reason in result["gating_reasons"])
    assert result["rank_score"] == 0.0                   # 图形分乘以质量因子 0 → 不进默认榜单


def test_sector_resonance_unavailable_is_not_zero_bonus():
    """单票扫描没有板块上下文 → 必须标注「不可用」，与真的没有加成（0 分）区分开。"""
    bars = _bars(80)
    day = str(bars[-1]["timestamp"])[:10]
    single = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], bars)
    assert single["sector_resonance"]["available"] is False
    assert "单票扫描" in single["sector_resonance"]["reason"]
    with_sector = tactics_score.score(
        [_signal("ma_golden_cross", day, "bullish")], bars,
        sector_context={"direction": "bullish", "ratio": 0.8},
    )
    assert with_sector["sector_resonance"]["available"] is True
    assert with_sector["sector_resonance"]["bonus"] >= 0.0


def test_hit_detail_carries_signal_state_per_item():
    bars = _bars(80)
    day = str(bars[-1]["timestamp"])[:10]
    result = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], bars)
    assert result["signal_states"]
    for item in result["hit_detail"]:
        assert item["signal_state"] in {
            tactics_score.STATE_NEW, tactics_score.STATE_PENDING,
            tactics_score.STATE_CONFIRMED, tactics_score.STATE_INVALIDATED,
        }
        assert item["signal_state_label"]
    assert result["primary_signal_state"]["state"]


def test_cooldown_merged_is_reported_not_silently_dropped():
    """同战法多信号按冷却窗口合并时要如实给出合并条数，不能悄悄丢掉。"""
    bars = _bars(80)
    day = str(bars[-1]["timestamp"])[:10]
    prev = str(bars[-2]["timestamp"])[:10]
    result = tactics_score.score(
        [_signal("ma_golden_cross", prev, "bullish"), _signal("ma_golden_cross", day, "bullish")], bars,
    )
    assert result["cooldown_merged"].get("ma_golden_cross", 0) >= 1


# ---------------------------------------------------------------------------
# F. 端点：/tactics/scan 暴露闸门字段
# ---------------------------------------------------------------------------


def test_scan_exposes_gate_fields_and_keeps_eligible_first(client, monkeypatch):
    from investment_steward_core.api import app as app_module

    test_client, headers = client
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(80), "test-provider")
    )
    body = test_client.post(
        "/tactics/scan", headers=headers,
        json={"sources": ["manual"], "symbols": ["600001", "600002"], "recent_bars": 60},
    ).json()
    assert body["gate_version"] == tactics_score.GATE_VERSION
    for row in body["results"]:
        for key in ("rank_score", "quality_factor", "eligible", "hard_blocked", "gating_reasons",
                    "direction_display", "sector_resonance", "signal_states", "primary_signal_state",
                    "posterior", "signal_state_version"):
            assert key in row, key
        assert row["signal_state_version"] == tactics_score.SIGNAL_STATE_VERSION
    # 默认榜单排序：过硬门的一方必须排在未过硬门之前
    flags = [bool(row["eligible"]) for row in body["results"]]
    assert flags == sorted(flags, reverse=True)


def test_scan_sorting_still_descends_by_score_for_clean_rows(client, monkeypatch):
    """回归守卫：数据齐全（eligible）时 rank_score == tactic_score，既有榜单次序不变。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = client
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(80), "test-provider")
    )
    body = test_client.post(
        "/tactics/scan", headers=headers,
        json={"sources": ["manual"], "symbols": ["600001", "600002"], "recent_bars": 60},
    ).json()
    scores = [row["tactic_score"] for row in body["results"] if row["ok"] and row["eligible"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# G. 端点：个股研报返回结论卡 / 支撑链 / 来源数据日期
# ---------------------------------------------------------------------------


def test_stock_report_exposes_v28_decision_card_and_support_chain(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report = _report_json(
        conclusion={"statement": "短期偏多但量能不足", "direction": "bullish",
                    "core_conflict": "量能未跟上", "confidence": "medium"},
        claims=[
            {"claim_id": "C1", "text": "营收与现金流改善", "sources": ["S4"],
             "requires": ["revenue", "cash_flow"], "importance": "high", "confidence": "medium",
             "missing": ["缺少分部数据"]},
            {"claim_id": "C2", "text": "主业减亏", "sources": ["S2"],
             "requires": ["net_income_deducted"], "importance": "high"},
        ],
        scenarios=[{"name": "乐观", "trigger": "放量", "invalidates": "跌破 60 日线",
                    "horizon": "10 个交易日", "probability": 0.4, "range_basis": "同类形态"}],
    )
    counter = json.dumps({
        "strongest_support": "现金流改善 [S4]",
        "strongest_support_source": "S4", "strongest_support_date": "2026-08-30",
        "strongest_counter": "新闻仅标题口径，无法支撑订单金额 [S2]",
        "strongest_counter_source": "S2", "strongest_counter_date": "2026-09-01",
        "necessary_conditions": ["下季度经营现金流不转负"],
        "verdict_robustness": "mixed",
    }, ensure_ascii=False)
    fake, calls = _model_with_calls(report, counter)
    monkeypatch.setattr(mc, "_post_json", fake)

    body = test_client.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()
    assert body["ok"] is True, body
    assert len(calls) == 2                                    # 报告 + 反方检查

    assert body["conclusion"]["statement"] == "短期偏多但量能不足"
    card = body["decision_card"]
    assert card["version"] == report_quality.DECISION_CARD_VERSION
    assert card["evidence_strength"]["support_counts"]["full"] == 1
    assert card["max_support"]["source"] == "S4"              # 支撑/反证带来源与日期
    assert card["max_counter"]["source"] == "S2"
    assert isinstance(body["summary_downgrade_notes"], list)
    # 来源数据日期随报告返回（时效判定与结论卡估值标签共用同一份快照）
    assert "S1" in body["source_asof"] and "S5" in body["source_asof"]
    findings = {item["claim_id"]: item for item in body["claims"]}
    assert findings["C1"]["support"] == report_quality.SUPPORT_FULL
    assert findings["C2"]["support"] == report_quality.SUPPORT_PARTIAL
    assert findings["C1"]["model_missing"] == ["缺少分部数据"]
    # 概率降级：小数概率置空、等级表达、原值留档
    scenario = body["scenarios"][0]
    assert scenario["probability"] is None and scenario["probability_level"] == "中"
    assert scenario["probability_raw"] == 0.4


def test_counter_check_drops_fabricated_source_ids():
    """★ 反方检查里的来源必须真实存在于本次证据包，虚构的一律丢弃并留痕。"""
    parsed = {
        "strongest_support": "现金流改善 [S4]", "strongest_support_source": "S4",
        "strongest_counter": "外部消息 [S9]", "strongest_counter_source": "S9",
        "necessary_conditions": [], "verdict_robustness": "mixed",
    }
    from investment_steward_core import counter_check

    result = counter_check.normalize_counter_check(parsed, allowed={"S1", "S4"})
    assert result["strongest_support_source"] == "S4"
    assert result["strongest_counter_source"] == ""           # S9 不在本次来源 → 剔除
    assert "S9" in result["dropped_sources"]


# ---------------------------------------------------------------------------
# H. 协同流水线：v28 字段随运行快照留存并落库
# ---------------------------------------------------------------------------


def _stage_reply_factory() -> object:
    calls = {"count": 0}

    def _fake(*_args, **_kwargs):
        calls["count"] += 1
        payloads = [
            json.dumps({"observations": ["S1 为日线 K 线"], "gaps": [], "readiness": "high"}, ensure_ascii=False),
            _report_json(title="初稿：甲股缓涨"),
            json.dumps({"risks": ["量能证据不足"], "verdict": "partial"}, ensure_ascii=False),
            json.dumps({
                **_json_load(_report_json(title="修订稿：甲股缓涨")),
                "conclusion": {"statement": "短期偏多但量能不足", "direction": "bullish",
                               "core_conflict": "量能未跟上", "confidence": "medium"},
                "claims": [{"claim_id": "C1", "text": "营收与现金流改善", "sources": ["S4"],
                            "requires": ["revenue", "cash_flow"], "importance": "high"}],
                "watchpoints": [{"signal": "量能回升", "verify_by": "2026-10-31", "expected_if_true": "站稳"}],
                "scenarios": [{"name": "乐观", "trigger": "放量", "invalidates": "跌破", "horizon": "10 日"}],
                "valuation": {"verdict": "合理", "basis": "PE 分位 50%"},
                "revision_notes": "采纳：下调量能判断。",
            }, ensure_ascii=False),
        ]
        return {"choices": [{"message": {"content": payloads[calls["count"] - 1]}}]}

    return _fake


def _json_load(text: str) -> dict:
    return json.loads(text)


def test_collab_persists_v28_decision_card_and_conclusion(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(mc, "_post_json", _stage_reply_factory())

    run = test_client.post(
        "/evidence/collab-run", json={"symbol": "600001", "question": "趋势是否延续"}, headers=headers,
    ).json()["run"]
    for _ in range(4):
        body = test_client.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).json()
        assert body["executed_status"] == "done", body
    assert body["done"] is True

    history = test_client.get("/ai-research/reports", params={"kind": "stock"}, headers=headers).json()
    listed = next(item for item in history["items"] if str(item["model"]).endswith("·协同流水线"))
    # B01（桌面端升级路线图 2026-09-18）：列表为轻量投影，全文断言改走详情端点。
    record = test_client.get(f"/ai-research/reports/{listed['report_id']}", headers=headers).json()["item"]
    assert record["conclusion"]["statement"] == "短期偏多但量能不足"
    assert record["decision_card"]["version"] == report_quality.DECISION_CARD_VERSION
    assert record["decision_card"]["max_support"] is None        # 协同无独立反方检查
    assert "未执行反方检查" in record["decision_card"]["counter_check_reason"]
    assert isinstance(record["summary_downgrade_notes"], list)
    assert "S5" in record["source_asof"]                         # 来源数据日期随运行快照落库
    # 概率降级同样在协同链路生效
    assert record["scenarios"][0]["probability"] is None
    assert record["scenarios"][0]["probability_basis"] == report_quality.PROBABILITY_BASIS_NONE
