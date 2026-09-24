"""v32 价值投资升级（2026-09-13 方案）：驾驶舱三行判断 + 正常化估值 + 反方检查章节覆盖 + 研报追问。

- §二.1 首屏驾驶舱：pillar_verdicts 归一（结论词词表 / 适用期限归一 / 越界告警）；
- §二.3 正常化盈利与估值安全边际：valuation 扩展五组字段；「低 PE 分位 ≠ 低估」——
  valuation_status 不信任模型自报，拿不出正常化盈利值/依据时服务端确定性降级 undetermined；
- §四.2 反方检查按章节/claim 覆盖：长报告后半段（估值/情景/风险）必须进入送审文本；
- §三 追问链路：原报告不可变（附录 append-only）、用户补充材料默认未独立验证、
  事实归一化、受影响 claim 闸门（自造 claim_id 剔除）、结论变化五态。
模型输出全部打桩，绝不联网。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用
from investment_steward_core import analysis_followup, counter_check, report_quality

# ---------------------------------------------------------------------------
# §二.1 驾驶舱三行判断（pillar_verdicts）
# ---------------------------------------------------------------------------

def test_normalize_pillar_verdicts_vocab_and_horizon():
    raw = {
        "fundamental": "偏强",
        "valuation": "低估",
        "technical": "待确认",
        "horizon": ["3～12个月", "3-5年", "短线"],
    }
    cockpit = report_quality.normalize_pillar_verdicts(raw)
    assert cockpit["fundamental"] == "偏强"
    assert cockpit["valuation"] == "低估"
    assert cockpit["technical"] == "待确认"
    # 全角波浪线 / 连字符写法归一到统一词表
    assert cockpit["horizon"] == ["3~12个月", "3~5年", "短线"]


def test_normalize_pillar_verdicts_missing_stays_empty():
    cockpit = report_quality.normalize_pillar_verdicts(None)
    assert cockpit == {"fundamental": "", "valuation": "", "technical": "", "horizon": []}


def test_pillar_verdicts_valid_reports_out_of_vocab():
    cockpit = report_quality.normalize_pillar_verdicts({"fundamental": "买入", "technical": "确认"})
    problems = report_quality.pillar_verdicts_valid(cockpit)
    assert problems == ["基本面=买入"]


# ---------------------------------------------------------------------------
# §二.3 正常化盈利与估值安全边际
# ---------------------------------------------------------------------------

def test_valuation_status_with_normalized_earnings_is_trusted():
    valuation = report_quality.normalize_valuation({
        "verdict": "偏便宜",
        "pe_ttm": "19.73",
        "normalized_earnings": {"value": 1.1, "basis": "剔除政府补助后的年化盈利", "confidence": "medium"},
        "valuation_cases": [
            {"name": "bear", "earnings": 0.9, "multiple": 12, "fair_value": 10.8},
            {"name": "base", "earnings": 1.1, "multiple": 15, "fair_value": 16.5},
            {"name": "bull", "earnings": 1.3, "multiple": 18, "fair_value": 23.4},
        ],
        "margin_of_safety": -0.05,
        "valuation_status": "undervalued",
        "limitations": ["正常化未剔除一次性收益"],
    })
    assert valuation["valuation_status"] == "undervalued"
    assert valuation["valuation_status_note"] == ""
    assert valuation["normalized_earnings"]["value"] == 1.1
    assert [case["name_label"] for case in valuation["valuation_cases"]] == ["悲观", "基准", "乐观"]
    assert valuation["margin_of_safety"] == -0.05
    assert valuation["valuation_limitations"] == ["正常化未剔除一次性收益"]


def test_valuation_status_low_pe_without_normalization_downgraded():
    """「低 PE 分位 ≠ 低估」：声称低估但无正常化盈利值/依据 → 确定性降级 undetermined（原声明留痕）。"""
    valuation = report_quality.normalize_valuation({
        "verdict": "偏便宜",
        "pe_percentile": "近5年 6.7% 分位",
        "valuation_status": "undervalued",
    })
    assert valuation["valuation_status"] == "undetermined"
    assert valuation["valuation_status_raw"] == "undervalued"
    assert "低 PE 分位不能排除" in valuation["valuation_status_note"]


def test_valuation_legacy_payload_reports_unjudged():
    """旧版报告（无 valuation_status 字段）→ 按未判定处理并说明原因，不冒充有结论。"""
    valuation = report_quality.normalize_valuation({"verdict": "偏便宜", "pe_ttm": "19.73"})
    assert valuation["valuation_status"] == "undetermined"
    assert "未输出估值状态" in valuation["valuation_status_note"]


def test_validate_report_flags_low_pe_undervalued_and_missing_cockpit():
    payload = {
        "report": "### 技术面\n趋势向上，量能配合，结构完好，关键支撑有效，指标中性。[S1]\n\n"
                  "### 消息面\n公司公告回购，市场情绪稳定，无其他重大事项，关注后续进展。[S3]\n\n"
                  "### 基本面\n营收净利稳健增长，现金流为正，负债平稳，盈利质量健康无恶化。[S4]\n\n"
                  "### 综合判断\n多空矛盾在估值与趋势，总体中性偏多，核心跟踪回购落地节奏与量能。[S1]\n",
        "citations": ["S1", "S3", "S4"],
        "executive_summary": "结论中性偏多：趋势完好但赔率有限。基本面营收净利稳健现金流为正。技术面量价配合支撑有效。反证是估值分位低但可能为周期高点。",
        "claims": [{"claim_id": "C1", "text": "营收增长", "sources": ["S4"], "requires": ["revenue"], "importance": "high"}],
        "valuation": {"verdict": "偏便宜", "pe_percentile": "6.7%", "valuation_status": "undervalued"},
    }
    result = report_quality.validate_report(payload, allowed={"S1", "S3", "S4"})
    # 低 PE 未验证 → 降级 + 告警（计算告警口径）
    assert result["valuation"]["valuation_status"] == "undetermined"
    assert any("估值状态降级" in warning for warning in result["warnings"])
    assert any("正常化盈利数值" in warning for warning in result["warnings"])
    # 旧版提示词无驾驶舱 → 如实告警，不编造
    assert any("pillar_verdicts" in warning for warning in result["warnings"])
    assert result["pillar_verdicts"]["fundamental"] == ""


# ---------------------------------------------------------------------------
# §四.2 反方检查按章节/claim 覆盖
# ---------------------------------------------------------------------------

def test_counter_check_covers_tail_sections_beyond_old_excerpt():
    """长报告：估值/情景小节位于前 2500 字之外，仍必须出现在送审文本里（不再一刀切截断）。"""
    filler = "技术面趋势延续，量价配合良好，短期动能中性，等待确认信号，注意仓位控制，保持观察纪律。" * 60
    tail = "### 估值与情景\nPE 处于历史低分位但利润处于周期高点，情景推演需排除一次性收益后再谈低估。[S5]"
    long_report = f"### 技术面\n{filler}\n\n{tail}"
    system, user = counter_check.counter_check_messages(
        symbol="600001",
        title="长报告",
        executive_summary="摘要",
        report=long_report,
        citations=["S1", "S5"],
        claims=[{"claim_id": "C1", "text": "低估判断", "sources": ["S5"]}],
        valuation={"verdict": "偏便宜", "pe_ttm": "19.73"},
        limitations=["口径限制"],
    )
    assert "估值与情景" in user  # 后半段章节进入送审
    assert "PE 处于历史低分位" in user
    assert "verdict=偏便宜" in user  # 估值结构化块并列送审
    assert "口径限制" in user
    assert system.startswith("你是研究结论的「反方审查员」")


def test_counter_check_per_section_truncation_is_labelled():
    """单节超长截断处如实标注（不静默截断）。"""
    filler = "技术面延续。" * 600  # 远超单节截断线
    _, user = counter_check.counter_check_messages(
        symbol="600001", title="t", executive_summary="s", report=f"### 技术面\n{filler}",
        citations=["S1"],
    )
    assert counter_check.SECTION_EXCERPT_CHARS < len(filler)
    assert "已截断" in user


# ---------------------------------------------------------------------------
# §3.2/§3.3 追问契约：补充材料 / 事实归一化 / 归一化闸门
# ---------------------------------------------------------------------------

def test_supplements_default_unverified_with_trust_label():
    supplements = analysis_followup.normalize_supplements(
        [
            {"text": "美联储加息 50bp", "source_name": "用户自述新闻", "event_date": "2026-09-10"},
            {"text": "行业价格战重启", "origin": "system_event", "event_id": "EVT-1", "source_provider": "macro_feed"},
            {"text": ""},
        ],
        input_at="2026-09-13T10:00:00+00:00",
    )
    assert len(supplements) == 2
    user_item = supplements[0]
    assert user_item["verified"] is False
    assert user_item["trust_label"] == "用户补充材料/未独立验证"
    system_item = supplements[1]
    assert system_item["origin"] == "system_event"
    assert system_item["verified"] is False  # 系统事件也不是已验证的公司事实
    assert system_item["trust_label"].startswith("系统事件快照")


def test_fact_normalization_outputs_contract_fields():
    supplements = analysis_followup.normalize_supplements(
        [{"text": "美联储宣布加息 50bp，美元指数走强，新兴市场资金流出压力明显上升", "source_name": "财经网站", "event_date": "2026-09-10"}],
        input_at="2026-09-13T10:00:00+00:00",
    )
    facts = analysis_followup.normalize_facts(supplements, subject="600001")
    fact = facts[0]
    assert fact["subject"] == "600001"
    assert fact["date"] == "2026-09-10"
    assert fact["date_is_input_time"] is False
    assert fact["complete"] is True  # 含数字且长度足够
    assert fact["source_credibility"] == "用户声明来源"
    assert fact["relation"] == "unknown"  # 未出现标的代码


def test_normalize_followup_gates_claim_ids_and_vocabs():
    parsed = {
        "affected_claims": [
            {"claim_id": "C1", "effect": "weakens", "reason": "成本上行压制毛利"},
            {"claim_id": "CX", "effect": "supports", "reason": "自造编号应被剔除"},
            {"claim_id": "C2", "effect": "未知词", "reason": "越界词归 cannot_judge"},
        ],
        "conclusion_change": "修改了",  # 越界 → undetermined
        "scenario_changes": [{"name": "中性", "change": "概率下修"}],
        "new_watchpoints": [
            # v39 Q07：日期必须带依据（`disclosed_schedule`＝已披露的披露日程）才会落成到期日。
            {"signal": "单票价格", "verify_by": "2026-10-31", "date_basis": "disclosed_schedule",
             "expected_if_true": "继续下行则削弱"},
            {"signal": "双十一量价", "verify_by": "2026-11-30", "expected_if_true": "上行则强化"},
        ],
        "answer": "回答正文",
        "limitations": ["口径"],
    }
    normalized = analysis_followup.normalize_followup(parsed, allowed_claim_ids={"C1", "C2"})
    assert normalized is not None
    assert [item["claim_id"] for item in normalized["affected_claims"]] == ["C1", "C2"]
    assert normalized["dropped_claim_ids"] == ["CX"]
    assert normalized["affected_claims"][1]["effect"] == "cannot_judge"
    assert normalized["conclusion_change"] == "undetermined"
    assert normalized["conclusion_change_raw"] == "修改了"
    assert normalized["new_watchpoints"][0]["due_on"] == "2026-10-31"
    assert normalized["new_watchpoints"][0]["status"] == "pending_review"
    # 没有依据的截止日降级为事件锚定，且不得留在 due_on 上（否则凭空日期又活了）。
    assert normalized["new_watchpoints"][1]["due_on"] == ""
    assert normalized["new_watchpoints"][1]["date_demoted_from"] == "2026-11-30"
    assert "2026-11-30" not in normalized["new_watchpoints"][1]["verify_by"]


def test_normalize_followup_requires_answer():
    assert analysis_followup.normalize_followup({"affected_claims": []}, allowed_claim_ids={"C1"}) is None
    assert analysis_followup.normalize_followup("不是字典", allowed_claim_ids={"C1"}) is None


# ---------------------------------------------------------------------------
# 端点级：研报驾驶舱字段下发 + 追问链路（不可变附录）
# ---------------------------------------------------------------------------

def _file_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _setup_active_profile(test_client, headers) -> None:
    test_client.put("/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers)
    created = test_client.post(
        "/model-profiles",
        json={"name": "Deepseek官方", "base_url": "https://api.deepseek.com",
              "model": "deepseek-v4-flash", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    assert test_client.post(f"/model-profiles/{created['profile_id']}/activate", headers=headers).status_code == 200


def _synth_bars(count: int = 60) -> list[dict[str, object]]:
    rows = []
    price = 10.0
    for index in range(count):
        price = round(price * 1.01, 2)
        rows.append({
            "timestamp": f"2026-06-{(index % 28) + 1:02d}",
            "open": price * 0.99, "close": price, "high": price * 1.02, "low": price * 0.98,
            "volume": 1_000_000 + index * 1000,
        })
    return rows


def _mock_sources(monkeypatch) -> None:
    import investment_steward_core.api.app as app_module
    from investment_steward_core.valuation_evidence import ValuationSnapshot

    monkeypatch.setattr(app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider"))
    monkeypatch.setattr(
        app_module, "fetch_cn_news",
        lambda symbol, limit=20: [SimpleNamespace(published_raw="2026-09-01", title="甲股中标大单", content="内容正文")],
    )
    monkeypatch.setattr(
        app_module, "fetch_cn_announcements",
        lambda symbol, limit=20: [SimpleNamespace(notice_date_raw="2026-09-02", title="关于回购的公告", ann_type="回购", url="http://x", art_code="A1")],
    )
    monkeypatch.setattr(app_module, "fetch_financials", lambda symbol, limit_per_type=8: [
        SimpleNamespace(statement_type="income", period_end="2026-06-30", published_raw="2026-08-30",
                        items={"revenue": "1000000000", "net_profit": "120000000"}, identity="h-income-2606"),
    ])
    monkeypatch.setattr(
        app_module, "fetch_valuation",
        lambda symbol, **kwargs: ValuationSnapshot(
            symbol=symbol, name="甲股", board_code="016165", board_name="测试行业",
            trade_date="2026-09-10", close_price=10.0, pe_ttm=19.73, pe_static=19.52,
            pb_mrq=6.39, ps_ttm=9.27, peg=1.0, pcf_ocf_ttm=13.49,
            total_market_cap=1e11, float_market_cap=1e11, total_shares=1e10,
            percentiles={"pe_ttm": 6.7, "pb_mrq": 4.2, "ps_ttm": 3.6},
            percentile_window_bars=1250, percentile_window_from="2021-07-19",
        ),
    )


class _CannedModel:
    """按「提示词特征」打桩：研报 JSON / 摘要重写 / 追问分析各自返回预设内容。"""

    def __init__(self, monkeypatch, *, report: dict, followup: dict | None = None, summary: str | None = None):
        from investment_steward_core import model_client as mc
        self.report_blob = json.dumps(report, ensure_ascii=False)
        self.followup_blob = json.dumps(followup, ensure_ascii=False) if followup else None
        self.summary = summary
        self.calls: list[str] = []
        monkeypatch.setattr(mc, "_post_json", self)

    def __call__(self, *a, **k):
        blob = json.dumps(a[1] if len(a) > 1 else "", ensure_ascii=False)
        self.calls.append(blob)
        if self.followup_blob is not None and "追加的分析附录" in blob:
            return {"choices": [{"message": {"content": self.followup_blob}}]}
        if self.summary is not None and "改写" in blob and "四句话" in blob:
            return {"choices": [{"message": {"content": self.summary}}]}
        return {"choices": [{"message": {"content": self.report_blob}}]}

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _v28_report_payload() -> dict:
    return {
        "title": "甲股：缓涨趋势配合基本面稳健",
        "executive_summary": (
            "方向性结论偏多：技术面缓涨趋势完好。最强基本面依据是营收净利双增且现金流为正。"
            "最重要技术面依据是量价配合与 MA20 支撑有效。最大反证是估值分位虽低但赔率有限。"
        ),
        "report": (
            "### 技术面\n股价沿 MA20 缓涨，60 日区间涨幅稳健，量能温和放大，趋势结构完好，关键支撑 6.10。[S1]\n\n"
            "### 消息面\n已落地事实：公司公告回购 [S3]；市场传闻与情绪：中标大单新闻带动关注度 [S2]。\n\n"
            "### 基本面\n营收 10.00 亿同比增长，净利 1.20 亿同比增长，经营现金流 1.50 亿为正，利润质量健康；"
            "PE(TTM) 19.73，近5年 6.7% 分位，低于同业中位，估值偏便宜。[S4][S5]\n\n"
            "### 综合判断\n多空矛盾在于趋势向好但估值分位已低、赔率有限；综合判断偏多，核心跟踪回购落地节奏。\n"
        ),
        "citations": ["S1", "S2", "S3", "S4", "S5"],
        "limitations": ["公开来源摘要口径", "行情数据为日线粒度", "同业样本口径以数据商为准"],
        "confidence": "medium",
        "conclusion": {"statement": "趋势偏多但赔率有限", "direction": "偏多", "core_conflict": "趋势与赔率", "confidence": "medium"},
        "claims": [{
            "claim_id": "C1", "text": "营收 10.00 亿同比增长",
            "sources": ["S4"], "requires": ["revenue"], "importance": "high",
            "confidence": "high", "missing": [],
        }],
        # v32 §二.1：驾驶舱三行结论词
        "pillar_verdicts": {"fundamental": "偏强", "valuation": "无法判断", "technical": "确认", "horizon": ["3～12个月", "3-5年"]},
        # v32 §二.3：正常化盈利与估值安全边际
        "valuation": {
            "pe_ttm": "19.73", "pe_percentile": "近5年 6.7% 分位", "peer_position": "低于同业中位",
            "verdict": "偏便宜", "basis": "PE 分位与同业比较 [S5]",
            "normalized_earnings": {"value": 1.1, "basis": "剔除政府补助后的年化盈利", "confidence": "medium"},
            "valuation_cases": [
                {"name": "bear", "earnings": 0.9, "multiple": 12, "fair_value": 10.8},
                {"name": "base", "earnings": 1.1, "multiple": 15, "fair_value": 16.5},
                {"name": "bull", "earnings": 1.3, "multiple": 18, "fair_value": 23.4},
            ],
            "margin_of_safety": -0.05,
            "valuation_status": "undervalued",
            "limitations": ["正常化未剔除一次性收益"],
        },
    }


def _setup(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    return test_client, headers


def test_report_exposes_cockpit_and_normalized_valuation(client, tmp_path, monkeypatch):
    """研报端点下发驾驶舱三行结论词 + 正常化估值五字段；决策卡带估值状态与期限。"""
    _CannedModel(monkeypatch, report=_v28_report_payload())
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    cockpit = body["pillar_verdicts"]
    assert cockpit["fundamental"] == "偏强"
    assert cockpit["valuation"] == "无法判断"
    assert cockpit["technical"] == "确认"
    assert cockpit["horizon"] == ["3~12个月", "3~5年"]  # 全角/连字符归一
    valuation = body["valuation"]
    assert valuation["valuation_status"] == "undervalued"  # 有正常化盈利 → 采信
    assert valuation["normalized_earnings"]["value"] == 1.1
    assert valuation["margin_of_safety"] == -0.05
    card = body["decision_card"]
    assert card["cockpit"]["fundamental"] == "偏强"
    assert card["valuation_status"] == "undervalued"
    assert card["valuation_status_label"] == "低估"
    assert card["normalized_earnings"]["value"] == 1.1


def test_report_downgrades_low_pe_undervalued_without_normalization(client, tmp_path, monkeypatch):
    """端点级「低 PE ≠ 低估」：声称低估但无正常化盈利 → 降级 undetermined 并留痕告警。"""
    payload = _v28_report_payload()
    payload["valuation"] = {
        "pe_ttm": "19.73", "pe_percentile": "近5年 6.7% 分位",
        "verdict": "偏便宜", "basis": "低分位 [S5]",
        "valuation_status": "undervalued",
    }
    _CannedModel(monkeypatch, report=payload)
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["valuation"]["valuation_status"] == "undetermined"
    assert body["valuation"]["valuation_status_raw"] == "undervalued"
    assert any("估值状态降级" in warning for warning in body["quality_warnings"])
    assert body["decision_card"]["valuation_status_label"] == "无法判断"


def _followup_payload() -> dict:
    return {
        "affected_claims": [{"claim_id": "C1", "effect": "weakens", "reason": "行业价格战或压制营收增速"}],
        "conclusion_change": "weakened",
        "scenario_changes": [{"name": "中性", "change": "盈利增速假设下修"}],
        "new_watchpoints": [{"signal": "行业单票收入同比", "verify_by": "2026-10-31", "expected_if_true": "继续下行则进一步削弱"}],
        "answer": "**先说结论**：若价格战重启属实（用户补充材料/未独立验证），营收判断削弱，结论往谨慎方向修正。[C1]",
        "limitations": ["补充材料未经独立验证"],
    }


def test_followup_appends_immutable_turn_and_gates_inputs(client, tmp_path, monkeypatch):
    """追问链路：附录 append-only、原报告 payload 逐字不变、补充材料未验证标签、claim 闸门。"""
    canned = _CannedModel(monkeypatch, report=_v28_report_payload(), followup=_followup_payload())
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    report_id = report["report_id"]
    before = test_client.get(f"/ai-research/reports/{report_id}", headers=headers).json()["item"]

    response = test_client.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={
            "question": "如果行业价格战重启，营收判断和结论会怎样？",
            "supplements": [{
                "text": "据行业交流，2026-09 起头部快递企业重启价格战，单票收入下行 5%",
                "source_name": "用户自述渠道信息",
                "event_date": "2026-09-10",
            }],
        },
        headers=headers,
    ).json()
    assert response["ok"] is True
    turn = response["turn"]
    assert turn["parent_report_id"] == report_id
    assert turn["base_report_version"] == 1
    assert turn["turn_index"] == 1
    assert turn["conclusion_change"] == "weakened"
    assert turn["affected_claims"][0]["claim_id"] == "C1"
    supplement = turn["supplement_evidence"][0]
    assert supplement["verified"] is False
    assert supplement["trust_label"] == "用户补充材料/未独立验证"
    fact = turn["fact_normalizations"][0]
    assert fact["subject"] == "600001" and fact["complete"] is True
    # v39 Q07：模型 stub 未给日期依据 → 该截止日不得落成到期日，只留降级痕迹。
    assert turn["new_watchpoints"][0]["due_on"] == ""
    assert turn["new_watchpoints"][0]["date_demoted_from"] == "2026-10-31"
    # v39 Q02/Q03/Q09/Q10：附录自带直接回答、三条状态轴、来源目录与入口口径。
    assert turn["mode"] == "interpret" and "不获取新事实" in turn["mode_label"]
    assert turn["data_basis"] == "user_supplement" and turn["revision_status"] == "revised"
    assert turn["source_catalog"] and "url_note" in turn["source_catalog"][0]
    assert turn["retrieval_status"]["state"] == "none"
    assert "未独立验证" in turn["answer"]

    # 原报告不可变：追问后 payload 与追问前逐字一致
    after = test_client.get(f"/ai-research/reports/{report_id}", headers=headers).json()["item"]
    assert after == before

    # 附录列表：append-only、按时间正序
    listing = test_client.get(f"/ai-research/reports/{report_id}/follow-ups", headers=headers).json()
    assert listing["ok"] is True and listing["count"] == 1
    assert listing["items"][0]["analysis_turn_id"] == turn["analysis_turn_id"]

    # 再追问一次 → 序号递增（2），原报告仍不变
    response2 = test_client.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={"question": "只看 3~5 年价值逻辑，该判断还成立吗？"},
        headers=headers,
    ).json()
    assert response2["ok"] is True
    assert response2["turn"]["turn_index"] == 2
    assert test_client.get(f"/ai-research/reports/{report_id}", headers=headers).json()["item"] == before
    listing2 = test_client.get(f"/ai-research/reports/{report_id}/follow-ups", headers=headers).json()
    assert listing2["count"] == 2
    assert canned.call_count == 3  # 1 报告 + 2 追问

    # 删除报告 → 附录联动清理
    assert test_client.delete(f"/ai-research/reports/{report_id}", headers=headers).status_code == 200
    empty = test_client.get(f"/ai-research/reports/{report_id}/follow-ups", headers=headers)
    assert empty.status_code == 404


def test_followup_drops_fabricated_claim_ids(client, tmp_path, monkeypatch):
    """模型自造 claim 编号 → 服务端剔除并留痕 dropped_claim_ids，不进入附录的影响清单。"""
    followup = _followup_payload()
    followup["affected_claims"] = [
        {"claim_id": "C1", "effect": "supports", "reason": "现金流同步"},
        {"claim_id": "C99", "effect": "supports", "reason": "自造编号"},
    ]
    _CannedModel(monkeypatch, report=_v28_report_payload(), followup=followup)
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    response = test_client.post(
        f"/ai-research/reports/{report['report_id']}/follow-ups",
        json={"question": "现金流是否支持营收判断？"},
        headers=headers,
    ).json()
    assert response["ok"] is True
    assert [item["claim_id"] for item in response["turn"]["affected_claims"]] == ["C1"]
    assert response["turn"]["dropped_claim_ids"] == ["C99"]


def test_followup_parse_failure_returns_honest_error(client, tmp_path, monkeypatch):
    """追问输出不可解析（缺 answer）→ 如实返回失败，不生成附录。"""
    _CannedModel(monkeypatch, report=_v28_report_payload(), followup={"affected_claims": []})
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    response = test_client.post(
        f"/ai-research/reports/{report['report_id']}/follow-ups",
        json={"question": "随便追问"},
        headers=headers,
    ).json()
    assert response["ok"] is False
    assert response["stage"] == "parse"
    listing = test_client.get(f"/ai-research/reports/{report['report_id']}/follow-ups", headers=headers).json()
    assert listing["count"] == 0


def test_followup_on_missing_report_404_and_direction_rejected(client, tmp_path, monkeypatch):
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    assert test_client.post(
        "/ai-research/reports/does-not-exist/follow-ups", json={"question": "q"}, headers=headers
    ).status_code == 404
    assert test_client.get("/ai-research/reports/does-not-exist/follow-ups", headers=headers).status_code == 404
