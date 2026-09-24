"""v27「可信交付」测试（2026-09-12 方案第一期）。

覆盖四条红线：
1. **引用真实 ≠ 引用支撑**：每条核心判断必须声明它依赖哪些字段（claims.requires），
   闸门校验所引来源是否覆盖该字段；只有 C 级证据的判断降级为「公司口径，未独立验证」；
   不满足只**标注降级**，绝不改写模型原话、也不阻断交付；
2. **情景可被证伪**：每个情景必须有 `invalidates`（失效条件）与 `horizon`（有效窗口）；
   验证点带 `due_on` 与 `pending_review` 状态，并进入待复盘队列（G01（2026-09-18 桌面端路线图）
   起，队列会把带确切日期的验证点幂等落成可回填判断；回填契约见
   `test_desktop_g01_watchpoint_judgment.py`）；
3. **反方检查独立且不致命**：普通研报默认追加一次轻量反方检查（单次短 JSON 调用），
   失败一律如实标注，报告本身照常交付；
4. **排序口径公开**：战法信号带触发价/年龄/失效条件/冷却窗口，结果并列给出
   `signal_score` 与 `data_quality`，且**不编造**尚未计算的后验指标。

行情/新闻/财报/估值取数一律 monkeypatch，绝不真实联网。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import counter_check, report_quality, tactics, tactics_score


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


# ---------------------------------------------------------------------------
# A. 证据质量分级与覆盖词表（纯函数）
# ---------------------------------------------------------------------------


def test_evidence_quality_tiers_are_honest_about_no_grade_a():
    """★ 诚实守卫：当前没有任何来源达到 A 级，不得为了好看把数据商接口写成 A。"""
    report = report_quality.evidence_quality_report({"S1", "S2", "S3", "S4", "S5"})
    by_id = {item["id"]: item["quality"] for item in report["sources"]}
    assert by_id == {"S1": "B", "S2": "C", "S3": "B", "S4": "B", "S5": "C"}
    assert report["tiers"]["A"] == []
    assert "尚未接入" in report["note"]
    assert report["core_support_qualities"] == ["A", "B"]


def test_source_profiles_only_cover_declared_vocab():
    """覆盖字段必须是词表子集，且词表里的「刻意空缺项」不得被任何来源覆盖。"""
    for source_id, profile in report_quality.SOURCE_PROFILES.items():
        for field in profile["coverage"]:
            assert field in report_quality.COVERAGE_VOCAB_SET, (source_id, field)
    covered = {f for p in report_quality.SOURCE_PROFILES.values() for f in p["coverage"]}
    # 分部利润 / 扣非净利：任何来源都覆盖不到 → 要求它们的判断必然被降级
    assert "segment_profit" not in covered
    assert "net_income_deducted" not in covered


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-10-31前", "2026-10-31"),
        ("2026年10月31日", "2026-10-31"),
        ("2026/10/31 收盘后", "2026-10-31"),
        ("2026-10 前", ""),            # 只到月份 → 不猜日期
        ("三季报披露后", ""),           # 锚定事件 → 不猜日期
        ("2026-13-45", ""),            # 非法日期 → 空串，不抛异常
        ("", ""),
    ],
)
def test_extract_due_date(value, expected):
    assert report_quality.extract_due_date(value) == expected


# ---------------------------------------------------------------------------
# B. claim → source 覆盖闸门（纯函数）
# ---------------------------------------------------------------------------


def test_claim_supported_when_cited_source_covers_required_fields():
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "营收与现金流改善", "sources": ["S4"],
         "requires": ["revenue", "cash_flow"]},
    ])
    result = report_quality.check_claims(claims, allowed={"S1", "S4"})
    item = result["claims"][0]
    assert item["status"] == "supported"
    assert item["missing_coverage"] == []
    assert item["evidence_quality"] == "B"
    assert result["core_conclusion_supported"] is True
    assert result["warnings"] == []


def test_claim_downgraded_when_source_does_not_cover_required_field():
    """★ 方案 §3.1 的原例：只有公司新闻稿却下「主业减亏」的结论 → 降级。"""
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "主业减亏", "sources": ["S2"],
         "requires": ["net_income", "segment_profit"]},
    ])
    result = report_quality.check_claims(claims, allowed={"S1", "S2", "S4"})
    item = result["claims"][0]
    assert item["status"] == "downgraded"
    assert set(item["missing_coverage"]) == {"net_income", "segment_profit"}
    assert "未独立验证" in item["note"]
    assert result["downgraded"] == ["C1"]
    assert any("无法覆盖的字段" in w for w in result["warnings"]) or any(
        "降级" in w for w in result["warnings"]
    )
    # 原始表述必须原样保留（只降级、不改写）
    assert item["text"] == "主业减亏"


def test_claim_downgraded_when_only_low_grade_evidence():
    """只有 C 级证据（媒体摘要）支撑的核心结论 → 降级，即使字段覆盖齐全。"""
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "获大额订单", "sources": ["S2"], "requires": ["news_event"]},
    ])
    result = report_quality.check_claims(claims, allowed={"S1", "S2"})
    item = result["claims"][0]
    assert item["status"] == "downgraded"
    assert item["missing_coverage"] == []
    assert item["evidence_quality"] == "C"
    assert "公司口径" in item["note"]


def test_claim_with_missing_source_is_flagged_not_published_as_supported():
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "某判断", "sources": ["S9"], "requires": ["revenue"]},
    ])
    result = report_quality.check_claims(claims, allowed={"S1", "S4"})
    item = result["claims"][0]
    assert item["status"] == "no_source" and item["dropped_sources"] == ["S9"]
    assert result["no_source"] == ["C1"]
    assert result["core_conclusion_supported"] is False
    assert any("不存在于本次证据包" in w for w in result["warnings"])


def test_claim_unknown_requires_is_reported_not_judged():
    """不在覆盖词表里的字段需求无法校验 → 如实标注，不据它判定通过/不通过。"""
    claims = report_quality.normalize_claims([
        {"claim_id": "C1", "text": "趋势判断", "sources": ["S1"], "requires": ["magic_field"]},
    ])
    result = report_quality.check_claims(claims, allowed={"S1"})
    item = result["claims"][0]
    assert item["status"] == "supported"
    assert item["unknown_requires"] == ["magic_field"]
    assert item["requires"] == []
    assert any("不在覆盖词表内" in w for w in result["warnings"])


def test_claim_check_is_unknown_without_claims():
    """没有输出 claim 时不得假装「核心结论已被支撑」。"""
    result = report_quality.check_claims([], allowed={"S1"})
    assert result["core_conclusion_supported"] is None
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# C. 情景 / 验证点字段（纯函数）
# ---------------------------------------------------------------------------


def test_normalize_scenarios_carries_invalidation_probability_and_horizon():
    out = report_quality.normalize_scenarios([
        {"name": "乐观", "trigger": "站稳 4.12", "invalidates": "收盘跌破 3.99",
         "horizon": "10 个交易日", "probability": 0.35,
         "expected_range": {"low": "4.12", "high": "4.80"}, "source": "S1"},
        {"name": "悲观", "trigger": "跌破 3.99", "invalidates": "重回 4.12 上方",
         "horizon": "至三季报披露", "probability": 35},
    ])
    first, second = out
    assert first["invalidates"] == "收盘跌破 3.99"
    assert first["horizon"] == "10 个交易日"
    # v28（二次方案 §2.4）：无历史基准 → 数值概率一律置 None，只用等级表达 + 原值留档。
    assert first["probability"] is None
    assert first["probability_raw"] == pytest.approx(0.35)
    assert first["probability_level"] == "中"
    assert first["probability_basis"] == report_quality.PROBABILITY_BASIS_NONE
    assert first["expected_range"] == {"low": 4.12, "high": 4.80}
    # 百分数写法如实归一（35 → 0.35）进 probability_raw，非法值置 None 而不是硬凑
    assert second["probability"] is None
    assert second["probability_raw"] == pytest.approx(0.35)
    assert second["expected_range"] == {"low": None, "high": None}


def test_normalize_watchpoints_enters_pending_review_state():
    out = report_quality.normalize_watchpoints([
        {"signal": "量能回升", "verify_by": "2026-10-31", "expected_if_true": "倾向乐观"},
        {"signal": "事故影响", "verify_by": "下次董事会决议公告后", "expected_if_true": "偏空"},
    ])
    assert out[0]["due_on"] == "2026-10-31"
    assert out[1]["due_on"] == ""
    for item in out:
        assert item["status"] == report_quality.WATCHPOINT_STATUS_PENDING == "pending_review"


def test_validate_report_bounds_summary_and_limitations():
    payload = {
        "executive_summary": "长" * 181,
        "report": "### 技术面\nx\n### 消息面\ny\n### 基本面\nz\n### 综合判断\nw",
        "citations": ["S1"],
        "limitations": ["a", "b"],
        "valuation": {"verdict": "合理"},
    }
    warnings = "\n".join(report_quality.validate_report(payload, allowed={"S1"})["warnings"])
    # v30：超硬上限的告警文案改为「只重写摘要、不重跑全文」口径（181/180）。
    assert "执行摘要 181/180 字" in warnings
    assert "少于约定的 3 条下限" in warnings
    assert "未输出结构化核心判断清单" in warnings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("下月再看量能", True),
        ("后续观察公告", True),
        ("择机确认", True),
        ("合适时机复核", True),
        ("2026-10-31", False),
        ("三季报披露后", False),          # 明确事件：不误伤（它进 event_anchored 档）
        ("下次董事会决议公告后", False),
    ],
)
def test_vague_verify_by_only_flags_relative_time_words(value, expected):
    """方案 §3.5：验证点必须绑定日期或明确事件。相对时间词两头不靠，必须被识别出来。"""
    assert report_quality.vague_verify_by(value) is expected


def test_validate_report_warns_on_vague_verify_time_without_rewriting():
    """相对时间词只告警、不改写：原话必须原样保留在 watchpoints 里（软校验铁律）。"""
    payload = {
        "executive_summary": "摘要",
        "report": "### 技术面\nx\n### 消息面\ny\n### 基本面\nz\n### 综合判断\nw",
        "citations": ["S1"],
        "limitations": ["a", "b", "c"],
        "valuation": {"verdict": "合理"},
        "watchpoints": [
            {"signal": "量能回升", "verify_by": "下月", "expected_if_true": "倾向乐观"},
            {"signal": "事故影响", "verify_by": "后续观察", "expected_if_true": "偏空"},
        ],
    }
    result = report_quality.validate_report(payload, allowed={"S1"})
    warnings = "\n".join(result["warnings"])
    assert "相对时间词" in warnings
    assert "待复盘队列" in warnings
    # 原话未被改写，且解析不出到期日（因此只会落 event_anchored，不会假装已排期）
    assert [item["verify_by"] for item in report_quality.normalize_watchpoints(payload["watchpoints"])] == ["下月", "后续观察"]


# ---------------------------------------------------------------------------
# D. 端点：研报接入 claims 闸门 + 反方检查
# ---------------------------------------------------------------------------


def _two_call_model(report: str, counter: str | None):
    """第一次返回报告 JSON，第二次返回反方检查 JSON（counter=None 时两次都返回报告）。"""
    calls: list[dict] = []

    def _fake(base_url, json_payload, api_key, timeout):
        calls.append(json_payload)
        content = report if len(calls) == 1 else (counter if counter is not None else report)
        return {"choices": [{"message": {"content": content}}]}

    return _fake, calls


def test_stock_report_exposes_claim_findings_and_evidence_quality(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report = _report_json(
        claims=[
            {"claim_id": "C1", "text": "营收与现金流改善", "sources": ["S4"],
             "requires": ["revenue", "cash_flow"]},
            {"claim_id": "C2", "text": "主业减亏", "sources": ["S2"],
             "requires": ["net_income", "segment_profit"]},
        ],
        scenarios=[{"name": "乐观", "trigger": "站稳", "invalidates": "跌破", "horizon": "10 个交易日",
                    "probability": 0.4, "source": "S1"}],
        watchpoints=[{"signal": "量能回升", "verify_by": "2026-10-31", "expected_if_true": "倾向乐观"}],
    )
    fake, calls = _two_call_model(report, None)
    monkeypatch.setattr(mc, "_post_json", fake)

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body

    findings = {item["claim_id"]: item for item in body["claims"]}
    assert findings["C1"]["status"] == "supported"
    assert findings["C2"]["status"] == "downgraded"
    assert set(findings["C2"]["missing_coverage"]) == {"net_income", "segment_profit"}
    assert body["claim_findings"]["supported"] == 1
    assert body["evidence_quality"]["sources"][0]["id"] == "S1"
    assert body["scenarios"][0]["invalidates"] == "跌破"
    assert body["watchpoints"][0]["due_on"] == "2026-10-31"
    assert body["watchpoints"][0]["status"] == "pending_review"
    # 第二次调用确实是反方检查（即便模型没按 schema 回答，也必须发起过）
    assert len(calls) == 2


def test_counter_check_success_is_attached_alongside_rule_findings(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    counter = json.dumps({
        "strongest_support": "现金流改善 [S4]",
        "strongest_counter": "新闻仅标题口径，无法支撑订单金额 [S2]",
        "necessary_conditions": ["下季度经营现金流不转负"],
        "most_misread_sentence": {"quote": "缓涨", "why": "易被读成趋势确认"},
        "verdict_robustness": "mixed",
    }, ensure_ascii=False)
    fake, _ = _two_call_model(_report_json(), counter)
    monkeypatch.setattr(mc, "_post_json", fake)

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    check = body["counter_check"]
    assert check["ok"] is True and check["requested"] is True
    assert "标题口径" in check["strongest_counter"]
    assert check["necessary_conditions"] == ["下季度经营现金流不转负"]
    assert check["verdict_robustness"] == "mixed"
    assert check["prompt_version"] == counter_check.COUNTER_CHECK_PROMPT_VERSION


def test_counter_check_failure_never_blocks_the_report(client, tmp_path, monkeypatch):
    """★ 红线：反方检查只是自检层，失败必须如实标注但绝不影响报告交付。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    # 第二次调用返回一份没有 strongest_counter 的内容 → 归一化判定为「未完成」
    fake, calls = _two_call_model(_report_json(), json.dumps({"summary": "看起来还行"}))
    monkeypatch.setattr(mc, "_post_json", fake)

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    assert len(calls) == 2
    assert body["counter_check"]["ok"] is False
    assert "strongest_counter" in body["counter_check"]["detail"]


def test_counter_check_can_be_disabled_to_save_one_call(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    fake, calls = _two_call_model(_report_json(), None)
    monkeypatch.setattr(mc, "_post_json", fake)

    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True, body
    assert len(calls) == 1                     # 只调用一次模型
    assert body["counter_check"]["ok"] is False
    assert body["counter_check"]["requested"] is False


def test_normalize_counter_check_requires_a_real_counter_argument():
    assert counter_check.normalize_counter_check(None) is None
    assert counter_check.normalize_counter_check({"summary": "x"}) is None
    out = counter_check.normalize_counter_check({
        "strongest_counter": "反证", "necessary_conditions": ["a", "b", "c", "d"],
        "verdict_robustness": "nonsense",
    })
    assert out is not None
    assert len(out["necessary_conditions"]) == 3          # 列表截断
    assert out["verdict_robustness"] == "mixed"           # 非法枚举回退


# ---------------------------------------------------------------------------
# E. 端点：待复盘队列（派生；G01 起带确切日期的验证点会幂等落成可回填判断）
# ---------------------------------------------------------------------------


def test_review_queue_buckets_overdue_scheduled_and_event_anchored(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    # 与端点同一口径取本机当天（tz-aware 写法，避免 DTZ011）。
    today = datetime.now().astimezone().date()
    report = _report_json(
        watchpoints=[
            {"signal": "已到期的信号", "verify_by": (today - timedelta(days=3)).isoformat(),
             "expected_if_true": "倾向乐观"},
            {"signal": "未来的信号", "verify_by": (today + timedelta(days=30)).isoformat(),
             "expected_if_true": "倾向乐观"},
            {"signal": "事件锚定信号", "verify_by": "三季报披露后", "expected_if_true": "偏空"},
        ],
        scenarios=[{"name": "中性", "trigger": "x", "invalidates": "跌破 3.99", "horizon": "10 个交易日"}],
    )
    fake, _ = _two_call_model(report, None)
    monkeypatch.setattr(mc, "_post_json", fake)
    assert test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()["ok"] is True

    body = test_client.get("/ai-research/review-queue", headers=headers).json()
    assert body["ok"] is True and body["today"] == today.isoformat()
    assert body["counts"] == {"overdue": 1, "scheduled": 1, "event_anchored": 1}
    assert [item["bucket"] for item in body["items"]] == ["overdue", "scheduled", "event_anchored"]
    # G01：带确切日期的两条已幂等落成可回填判断（status=pending）；锚定事件没有日期、不落库，
    # 状态保留研报内嵌口径 pending_review、judgment_id 为空（详细契约见
    # tests/test_desktop_g01_watchpoint_judgment.py）。
    assert [item["status"] for item in body["items"]] == ["pending", "pending", "pending_review"]
    assert [item["judgment_id"] is not None for item in body["items"]] == [True, True, False]
    assert body["closure"] == {
        "materialized": 2, "filled": 0, "overdue_unfilled": 1, "not_materialized": 1,
    }
    overdue = body["items"][0]
    assert overdue["days_until"] == -3
    assert overdue["symbol"] == "600001"
    assert overdue["scenarios"][0]["invalidates"] == "跌破 3.99"
    # 回填入口写在 note 里（端点已不是只读，回填复用既有 verify 通道）。
    assert "POST /judgments/{id}/verify" in body["note"]


def test_review_queue_is_empty_on_a_fresh_database(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    body = test_client.get("/ai-research/review-queue", headers=headers).json()
    assert body["items"] == [] and body["item_count"] == 0
    assert body["counts"] == {"overdue": 0, "scheduled": 0, "event_anchored": 0}


# ---------------------------------------------------------------------------
# F. 战法雷达：信号语义 + 数据质量（排序口径公开）
# ---------------------------------------------------------------------------


def test_catalog_exposes_signal_semantics_for_every_tactic(client):
    test_client, headers = client
    catalog = test_client.get("/tactics/catalog", headers=headers).json()
    assert catalog
    for item in catalog:
        # family 是 category 的对等别名（方案 §4.2 术语），显式回传
        assert item["family"] == item["category"]
        assert isinstance(item["cooldown_bars"], int) and item["cooldown_bars"] >= 1
        assert item["confirmation_rule"].strip()
        assert item["invalidation_rule"].strip()


def test_registry_semantics_version_is_published():
    assert tactics.TACTIC_SEMANTICS_VERSION == "v1"
    for item in tactics.catalog():
        assert item["cooldown_bars"] >= 1


def test_snapshot_signals_carry_trigger_price_and_age():
    bars = _bars(60)
    snap = tactics.snapshot(bars)
    # 平盘合成线必然有命中（十字星等）；逐条校验新增语义字段
    assert snap["signals"], "合成数据应至少命中一个战法"
    for signal in snap["signals"]:
        assert signal["trigger_price"] == 10.0          # 展示精度 2 位，不带裸浮点
        assert isinstance(signal["age_bars"], int) and signal["age_bars"] >= 0
        assert signal["family"] == signal.get("family")
        assert signal["cooldown_bars"] >= 1


def test_hit_detail_carries_semantics_and_score_parts_are_named():
    bars = _bars(80)
    day = str(bars[-1]["timestamp"])[:10]
    signals = [{
        "tactic_id": "ma_golden_cross", "tactic_name": "均线金叉", "direction": "bullish",
        "detail": "test", "date": day, "trigger_price": 10.0,
    }]
    result = tactics_score.score(signals, bars)
    item = result["hit_detail"][0]
    assert item["trigger_price"] == 10.0
    assert item["cooldown_bars"] == tactics._TACTIC_BY_ID["ma_golden_cross"]["cooldown_bars"]
    assert item["invalidation_rule"] == tactics._TACTIC_BY_ID["ma_golden_cross"]["invalidation_rule"]
    assert item["confirmation_rule"] == tactics._TACTIC_BY_ID["ma_golden_cross"]["confirmation_rule"]

    # 方案 §4.1：图形分与后验分并列命名，后验未计算时如实置 None（不用 0 冒充）
    assert result["signal_score"] == result["tactic_score"]
    assert result["final_rank_score"] == result["tactic_score"]
    assert result["historical_edge"] is None
    assert "第二期" in result["historical_edge_note"]
    assert result["tactic_semantics_version"] == tactics.TACTIC_SEMANTICS_VERSION
    assert result["latest_signal_date"] == day
    assert result["latest_signal_age_bars"] == 0


def test_data_quality_flags_insufficient_sample_and_missing_fields():
    short = tactics_score.data_quality(
        _bars(20), hit_tactic_ids=["macd_golden_cross"], insufficient_tactics=["macd_golden_cross"]
    )
    assert short["required_bars"] == 35
    assert short["sample_sufficient"] is False
    assert short["level"] == "insufficient"
    assert short["latest_bar_date"] == (date.fromisoformat("2026-01-01") + timedelta(days=19)).isoformat()

    thin = tactics_score.data_quality(
        _bars(40), hit_tactic_ids=["macd_golden_cross"], insufficient_tactics=[]
    )
    assert thin["sample_sufficient"] is True
    assert thin["level"] == "thin"                      # 40 < 35×1.5

    ok = tactics_score.data_quality(
        _bars(120), hit_tactic_ids=["macd_golden_cross"], insufficient_tactics=[]
    )
    assert ok["level"] == "ok"

    broken = _bars(40)
    broken[-1]["volume"] = None
    missing = tactics_score.data_quality(
        broken, hit_tactic_ids=["ma_golden_cross"], insufficient_tactics=[]
    )
    assert "volume" in missing["missing_fields"]


def test_scan_rows_expose_data_quality_and_signal_semantics(client, monkeypatch):
    from investment_steward_core.api import app as app_module

    test_client, headers = client
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(80), "test-provider")
    )
    body = test_client.post(
        "/tactics/scan", headers=headers,
        json={"sources": ["manual"], "symbols": ["600001"], "recent_bars": 60},
    ).json()
    row = body["results"][0]
    assert row["ok"] is True
    assert row["signal_score"] == row["tactic_score"]
    assert row["final_rank_score"] == row["tactic_score"]
    assert row["historical_edge"] is None
    assert row["latest_signal_date"]                      # 平盘线必有命中 → 有日期
    assert row["data_quality"]["bars_count"] == 80
    assert row["data_quality"]["sample_sufficient"] is True
    assert row["data_quality"]["latest_bar_date"]
    for item in row["hit_detail"]:
        assert "invalidation_rule" in item and "cooldown_bars" in item
