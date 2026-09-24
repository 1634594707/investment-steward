"""v24 质量与评分测试：战法质量分 + 研报引用真实性与形式质量。

红线：
- 战法排序主键是战法质量分；**反向信号只能对冲，不得作为数量加分**；
- 命中窗口按 **K 线日期下界** 过滤（修正 v23「最近 N 条信号」的语义偏差）；
- 研报/协同的每条引用必须属于本次**真实取到**的来源；虚构引用一律剔除，全部虚构则按
  「无引用不发布」不交付（ADR-0006）；
- 形式质量（长度/小节/confidence）只产出 warnings，不阻断交付、不改写模型原值。
行情/新闻/财报取数一律 monkeypatch，绝不真实联网。
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import tactics, tactics_score

_NAME_BY_ID = {item["id"]: item["name"] for item in tactics.catalog()}




def _post_scan_sync(test_client, headers, payload):
    """D01 适配：POST 只建任务；这里轮询到 done 并返回 summary（模拟旧同步响应形状）。"""
    import time as _time

    response = test_client.post("/tactics/scan-market", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    started = response.json()
    job_id = started["job_id"]
    for _ in range(200):
        _time.sleep(0.05)
        status = test_client.get(f"/tactics/scan-market/{job_id}", headers=headers).json()
        if status["state"] == "done":
            return status["summary"]
    raise AssertionError("扫描任务 10s 内未完成")

def _bars(count: int, start: str = "2026-01-01") -> list[dict[str, object]]:
    """**日期严格升序**的合成日线（真实行情口径；用于验证按日期过滤与新鲜度衰减）。"""
    base = date.fromisoformat(start)
    return [
        {
            "timestamp": (base + timedelta(days=index)).isoformat(),
            "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1_000_000,
        }
        for index in range(count)
    ]


def _signal(tactic_id: str, day: str, direction: str) -> dict[str, object]:
    return {
        "tactic_id": tactic_id,
        "tactic_name": _NAME_BY_ID[tactic_id],
        "direction": direction,
        "detail": "test",
        "date": day,
    }


# ---- A. 引擎级（纯函数） ----


def test_signal_window_uses_bar_date_lower_bound():
    bars = _bars(80)
    signals = [
        _signal("doji", str(bars[5]["timestamp"])[:10], "neutral"),
        _signal("doji", str(bars[79]["timestamp"])[:10], "neutral"),
    ]
    windowed, from_date = tactics_score.signal_window(signals, bars, 10)
    # 下界 = 倒数第 10 根 K 线的日期；第 5 根那天的信号必须被排除（v23 会把它当成「最近一条」漏算进来）
    assert from_date == str(bars[-10]["timestamp"])[:10]
    assert [item["date"] for item in windowed] == [str(bars[79]["timestamp"])[:10]]


def test_score_conflict_does_not_reward_opposite_signals():
    bars = _bars(80)
    day = str(bars[-1]["timestamp"])[:10]
    bull_only = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], bars)
    both = tactics_score.score(
        [_signal("ma_golden_cross", day, "bullish"), _signal("ma_death_cross", day, "bearish")],
        bars,
    )
    assert bull_only["direction_bias"] == "bullish"
    assert both["direction_bias"] == "conflict"
    # 反向信号必须拉低分数，而不是像 v23 计数口径那样让排名上升
    assert both["tactic_score"] < bull_only["tactic_score"]
    assert both["score_parts"]["conflict"] > 0


def test_score_decays_with_signal_age():
    bars = _bars(80)
    fresh = tactics_score.score([_signal("ma_golden_cross", str(bars[-1]["timestamp"])[:10], "bullish")], bars)
    stale = tactics_score.score([_signal("ma_golden_cross", str(bars[0]["timestamp"])[:10], "bullish")], bars)
    assert fresh["tactic_score"] > stale["tactic_score"]
    assert fresh["hit_detail"][0]["freshness"] == 1.0
    # 80 根前的老信号：0.5 ^ (79/10) ≈ 0.004，趋近 0
    assert stale["hit_detail"][0]["freshness"] < 0.05


def test_score_freshness_uses_fixed_half_life():
    """新鲜度 = 0.5 ^ (age / 10)，与窗口/历史长度无关（长窗口不会把分数顶到满分）。"""
    bars = _bars(250)
    day = str(bars[-3]["timestamp"])[:10]
    result = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], bars)
    item = result["hit_detail"][0]
    assert item["age_bars"] == 2
    assert item["freshness"] == pytest.approx(round(0.5 ** (2 / tactics_score.FRESHNESS_HALF_LIFE_BARS), 3))
    # 单一高权重形态（权重 7.0）在 2 根前命中：v1 线性口径 ≈ 15 分，v2 饱和曲线 + 量价/位置
    # bonus（随新鲜度缩放）≈ 25 分级别——「极强共振」才可能上 80+，绝不满分。
    assert 15 < result["tactic_score"] < 35


def test_score_does_not_saturate_on_long_history():
    """回归守卫：旧口径（新鲜度按窗口线性衰减）在 60 根以上窗口恒为 100，必须不再发生。"""
    bars = _bars(250)
    stale_day = str(bars[10]["timestamp"])[:10]
    fresh = tactics_score.score([_signal("ma_golden_cross", str(bars[-1]["timestamp"])[:10], "bullish")], bars)
    stale = tactics_score.score([_signal("ma_golden_cross", stale_day, "bullish")], bars)
    assert fresh["tactic_score"] > stale["tactic_score"]
    assert stale["tactic_score"] < 1.0


def test_score_for_bars_window_metadata_matches_direct_score():
    """score_for_bars 的窗口元数据与 signal_window 一致，分数等于对窗口内信号直接打分。"""
    bars = _bars(120)
    signals = [
        _signal("doji", str(bars[100]["timestamp"])[:10], "neutral"),   # 窗口外
        _signal("ma_golden_cross", str(bars[-1]["timestamp"])[:10], "bullish"),
    ]
    result = tactics_score.score_for_bars({"signals": signals}, bars, 5)
    windowed, from_date = tactics_score.signal_window(signals, bars, 5)
    assert result["signal_window_bars"] == 5
    assert result["signal_window_from"] == from_date
    assert [item["tactic_id"] for item in result["window_signals"]] == [item["tactic_id"] for item in windowed]
    assert result["window_signals"] == [windowed[-1]]        # 100 号那根在窗口外，不参与
    assert result["tactic_score"] == tactics_score.score(windowed, bars)["tactic_score"]


def test_score_resonance_rewards_cross_category_agreement():
    bars = _bars(80)
    day = str(bars[-1]["timestamp"])[:10]
    same_category = tactics_score.score(
        [_signal("ma_golden_cross", day, "bullish"), _signal("ma_bullish_alignment", day, "bullish")],
        bars,
    )
    cross_category = tactics_score.score(
        [_signal("ma_golden_cross", day, "bullish"), _signal("volume_breakout", day, "bullish")],
        bars,
    )
    assert same_category["score_parts"]["resonance"] == 0.0
    assert cross_category["score_parts"]["resonance"] > 0.0


def test_score_keeps_latest_signal_per_tactic():
    bars = _bars(80)
    old, new = str(bars[10]["timestamp"])[:10], str(bars[-1]["timestamp"])[:10]
    result = tactics_score.score(
        [_signal("ma_golden_cross", old, "bullish"), _signal("ma_golden_cross", new, "bullish")],
        bars,
    )
    assert len(result["hit_detail"]) == 1
    assert result["hit_detail"][0]["date"] == new


def test_score_reports_insufficient_tactics_below_own_min_bars():
    bars = _bars(20)
    result = tactics_score.score([_signal("macd_golden_cross", str(bars[-1]["timestamp"])[:10], "bullish")], bars)
    # MACD 需要 35 根才可靠；20 根时如实标注该战法样本不足，而不是当作有效信号
    assert result["insufficient_tactics"] == ["macd_golden_cross"]


# ---- B. 端点级：战法扫描 ----


def test_scan_ranks_by_tactic_score_with_explainable_parts(client, monkeypatch):
    from investment_steward_core.api import app as app_module

    test_client, headers = client
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(80), "test-provider")
    )
    response = test_client.post(
        "/tactics/scan", headers=headers,
        json={"sources": ["manual"], "symbols": ["600001", "600002"], "recent_bars": 60},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recent_bars"] == 60
    assert body["weight_version"] == tactics.TACTIC_WEIGHT_VERSION

    scores = [row["tactic_score"] for row in body["results"]]
    assert scores == sorted(scores, reverse=True)  # 排序主键 = 质量分
    for row in body["results"]:
        assert row["ok"] is True
        for key in ("tactic_score", "score_parts", "direction_bias", "signal_window_from",
                    "hit_tactics", "hit_detail", "insufficient_tactics", "weight_version"):
            assert key in row, key
        assert set(row["score_parts"]) >= {"bullish", "bearish", "neutral", "resonance", "conflict", "raw"}
        # 命中集合与逐条明细必须一致（明细保留日期/权重/新鲜度/贡献，可复算）
        assert row["hit_tactics"] == [item["tactic_id"] for item in row["hit_detail"]]


def test_signals_endpoint_returns_windowed_score(client, monkeypatch):
    """单票详情的质量分必须走**窗口**口径（默认 DETAIL_WINDOW_BARS），不得用「全部 K 线」虚高。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = client
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(250), "test-provider")
    )
    body = test_client.get("/tactics/signals/600001", headers=headers).json()
    score = body["score"]
    assert score["signal_window_bars"] == tactics_score.DETAIL_WINDOW_BARS
    assert score["weight_version"] == tactics.TACTIC_WEIGHT_VERSION
    assert score["freshness_half_life_bars"] == tactics_score.FRESHNESS_HALF_LIFE_BARS
    assert score["tactic_score"] < 100          # 旧口径（全段 250 根线性衰减）会恒为满分
    assert "window_signals" not in score        # 原始信号只经 signals 字段返回，不在 score 里重复


def test_catalog_exposes_weight_and_min_bars(client):
    test_client, headers = client
    catalog = test_client.get("/tactics/catalog", headers=headers).json()
    assert catalog, "目录不应为空"
    for item in catalog:
        assert isinstance(item["weight"], (int, float)) and item["weight"] > 0
        assert isinstance(item["min_bars"], int) and item["min_bars"] >= 1
    by_id = {item["id"]: item for item in catalog}
    # 恒命中的观察形态权重必须显著低于量价共振形态（否则计数膨胀会污染排序）
    assert by_id["doji"]["weight"] < by_id["volume_breakout"]["weight"]
    assert by_id["weak_to_strong"]["weight"] < by_id["ma_golden_cross"]["weight"]


# ---- C. 研报：引用真实性 ----


def test_stock_report_drops_fabricated_citations(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report_json = json.dumps({
        "title": "甲股：技术面缓涨配合基本面稳健",
        "report": "### 技术面\n缓涨 [S1]\n### 消息面\n中标 [S2]\n### 基本面\n无数据\n### 综合判断\n见 [S9]",
        "citations": ["S1", "S2", "S9"],
        "limitations": ["公开来源摘要口径"],
        "confidence": "medium",
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": report_json}}]})

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001", "question": "趋势"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    assert body["citations"] == ["S1", "S2"]          # S9 不属于本次来源，被剔除
    assert body["citation_dropped"] == ["S9"]
    assert body["available_citations"] == ["S1", "S2", "S3", "S4", "S5"]
    assert any("S9" in item for item in body["quality_warnings"])
    # v27：提示词版本随 claims 覆盖声明递增；v28→2.5，v29→2.6，v30 随摘要四句结构/aspects 三层到 2.7。
    assert body["prompt_policy_version"] == "2.11"


def test_stock_report_rejects_all_fabricated_citations(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": json.dumps({
            "title": "假引用报告", "report": "### 技术面\n内容 [S9]", "citations": ["S9"]})}}]},
    )
    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is False and body["stage"] == "citation"
    assert body["dropped_citations"] == ["S9"]


def test_stock_report_emits_form_warnings_without_rewriting_model_values(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report_json = json.dumps({
        "title": "短报告", "report": "### 技术面\n短 [S1]", "citations": ["S1"],
        "limitations": ["a", "b", "c", "d", "e", "f"], "confidence": "very high",
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": report_json}}]})

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    warnings = " ".join(body["quality_warnings"])
    assert "字" in warnings                 # 长度不足
    assert "消息面" in warnings             # 缺必需小节
    assert "信度" in warnings               # confidence 取值越界
    # v27：局限条数口径改为 3～5 条（方案 §2），6 条越上限
    assert "6 条" in warnings
    # 形式问题只告警：不阻断交付，也不改写模型给出的原值
    assert body["confidence"] == "very high"
    assert len(body["limitations"]) == 6


def test_stock_report_normalizes_structured_judgements(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report_json = json.dumps({
        "title": "结构化报告", "report": "### 技术面\n内容 [S1]", "citations": ["S1"],
        "confidence": "medium",
        "scenarios": [{"name": "乐观", "trigger": "站上 MA20", "source": "S1"}],
        "levels": {
            "support": [{"price": "9.80", "basis": "MA20"}],
            "resistance": [{"price": "无法解析", "basis": "前高"}],
        },
        "watchpoints": [{"signal": "量能回升", "verify_by": "2026-10 前", "expected_if_true": "倾向乐观"}],
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": report_json}}]})

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    assert body["scenarios"][0]["name"] == "乐观"
    assert body["levels"]["support"][0]["price"] == 9.8      # 字符串数字归一为数值
    assert body["levels"]["resistance"][0]["price"] is None  # 非数值如实置 None，不猜测
    assert body["watchpoints"][0]["verify_by"] == "2026-10 前"


# ---- D. 协同流水线：引用真实性 ----


def _collab_factory(citations: list[str]):
    """按调用序返回四角色产出；draft 与 revise 使用给定 citations。"""
    calls = {"count": 0}

    def _fake(*_args, **_kwargs):
        calls["count"] += 1
        index = calls["count"]
        report = json.dumps({
            "title": f"报告 {index}",
            "executive_summary": "结论先行。",
            "report": "### 技术面\n缓涨 [S1]\n### 消息面\n中标 [S2]\n### 基本面\n无数据\n### 综合判断\n见 [S1]",
            "citations": citations,
            "limitations": ["口径限制"],
            "confidence": "medium",
        }, ensure_ascii=False)
        payloads = [
            json.dumps({"observations": ["S1 为日线"], "gaps": [], "readiness": "high"}, ensure_ascii=False),
            report,
            json.dumps({"risks": ["量能判断证据不足"], "verdict": "partial"}, ensure_ascii=False),
            json.dumps({**json.loads(report), "revision_notes": "采纳：下调量能判断。"}, ensure_ascii=False),
        ]
        return {"choices": [{"message": {"content": payloads[index - 1]}}]}

    return _fake


def _start_collab(test_client, headers) -> str:
    body = test_client.post(
        "/evidence/collab-run", json={"symbol": "600001", "question": "趋势"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    return body["run"]["run_id"]


def test_collab_drops_fabricated_citations_and_notes_them(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(mc, "_post_json", _collab_factory(["S1", "S9"]))

    run_id = _start_collab(test_client, headers)
    test_client.post("/evidence/collab-next", json={"run_id": run_id}, headers=headers)  # check
    draft = test_client.post("/evidence/collab-next", json={"run_id": run_id}, headers=headers).json()
    assert draft["executed_stage"] == "draft" and draft["executed_status"] == "done"
    draft_stage = draft["run"]["stages"][1]
    assert draft_stage["result"]["citations"] == ["S1"]     # S9 被剔除
    assert draft_stage["citation_dropped"] == ["S9"]
    assert any("S9" in item for item in draft_stage["quality_warnings"])


def test_collab_all_fabricated_citations_fails_stage_without_skipping(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(mc, "_post_json", _collab_factory(["S9"]))

    run_id = _start_collab(test_client, headers)
    test_client.post("/evidence/collab-next", json={"run_id": run_id}, headers=headers)  # check
    draft = test_client.post("/evidence/collab-next", json={"run_id": run_id}, headers=headers).json()
    assert draft["executed_stage"] == "draft" and draft["executed_status"] == "failed"
    assert "无引用不发布" in draft["run"]["stages"][1]["error"]
    assert draft["done"] is False                            # 失败不跳过，流水线停在原阶段
    assert draft["run"]["stages"][2]["status"] == "pending"


def test_collab_prompt_shares_the_single_report_template():
    """两条链路必须共用 prompting 里的同一份模板（防止再次出现措辞漂移）。"""
    from investment_steward_core import collab
    from investment_steward_core.prompting import (
        STOCK_REPORT_RULES_TEXT,
        STOCK_REPORT_SCHEMA_TEXT,
    )

    assert collab._REPORT_SCHEMA_TEXT is STOCK_REPORT_SCHEMA_TEXT
    assert "scenarios" in STOCK_REPORT_SCHEMA_TEXT and "levels" in STOCK_REPORT_SCHEMA_TEXT
    assert "watchpoints" in STOCK_REPORT_SCHEMA_TEXT
    assert "逐位一致" in STOCK_REPORT_RULES_TEXT


def test_scan_market_ranking_ignores_snapshot_trend_score(client, monkeypatch, tmp_path):
    """全市场模式最终排名以战法质量分为主键，快照 trend_score 只作参考展示。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = client
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(80), "test-provider")
    )
    # 甲乙两只票：甲快照分高但 K 线同源（质量分相同）→ 不得因快照分改变主排序口径
    monkeypatch.setattr(
        app_module, "fetch_cn_market_full_a",
        lambda max_pages=40: ([
            {"symbol": "600001", "name": "甲股", "price": 12.0, "change_pct": 9.0,
             "volume": 1000.0, "turnover": 9.0e8, "turnover_rate": 10.0},
            {"symbol": "600002", "name": "乙股", "price": 12.0, "change_pct": 0.5,
             "volume": 1000.0, "turnover": 1.0e7, "turnover_rate": 1.0},
        ], True),
    )
    body = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10, "recent_bars": 60, "max_pages": 5})
    assert body["mode"] == "all" and body["weight_version"] == tactics.TACTIC_WEIGHT_VERSION
    rows = [row for row in body["results"] if row["ok"]]
    assert rows and all("trend_score" in row for row in rows)
    scores = [row["tactic_score"] for row in rows]
    assert scores == sorted(scores, reverse=True)
