"""JV03 契约测试：战法 AI 复核的「Jev 判定 + chat 叙述」两层分工。

覆盖：
1. 纯函数：三题结构（score 5 级描述性 rubric / choice 3 选项 / noul 带 true-false 描述）；
2. 纯函数：state 只含白名单字段，且与 chat 链路同源（同一批摘要函数）；
3. 纯函数：判定归一化（score→0–100、noul→三路风险、缺题回退中性**不抛错**）；
4. 纯函数：两层合并优先级（判定字段 Jev 优先；叙述字段只能来自 chat）；
5. 端点：Jev 启用且可用 → `engine=jev`，判定来自 Jev、叙述来自 chat，原始分与 confidence 落库，
   `purpose="jev:tactics-review"` 在 `model_calls` 可查；
6. 端点：Jev 启用但不可用 → `engine=chat`，如实标注回落（不假装 Jev 参与过）；
7. 端点：chat 吐不出 JSON 但 Jev 可用 → **仍交付判定**，`narrative_status=unparsable`
   （这就是「解析失败类 502 消失」的落点）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from investment_steward_core import credential_store, jev_client, tactics_ai

# —— 合成证据包（与 test_stock_research_tools._synth_bars 同口径，缓涨日线）——


def _bars(count: int = 120) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    price = 10.0
    for index in range(count):
        price = round(price * 1.01, 2)
        rows.append({
            "timestamp": f"2026-06-{(index % 28) + 1:02d}",
            "open": price * 0.99,
            "close": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "volume": 1_000_000 + index * 1_000,
        })
    return rows


_RULE_SCORE = {
    "tactic_score": 58,
    "score_formula_version": "v2",
    "weight_version": "w1",
    "direction_bias": "bullish",
    "signal_window_bars": 40,
    "signal_window_from": "2026-05-01",
    "volume_ratio": 1.4,
    "volume_lookback_bars": 20,
    "position": 0.72,
    "position_window_bars": 60,
    "merged_families": ["均线族"],
    "score_parts": {"form": 30, "resonance": 12},
    "hit_detail": [
        {
            "tactic_name": "放量突破",
            "direction": "bullish",
            "family": "形态族",
            "date": "2026-06-10",
            "age_bars": 3,
            "freshness": 0.9,
            "weight": 12,
            "contribution": 10.8,
            "trigger_price": 11.2,
            "invalidation_rule": "跌破 10.8",
            "counted": True,
        }
    ],
}


def _jev_systemone_response(
    *, score: float = 3.0, choice: str = "partial", noul: float = 0.92, model: str = "jev-1.13.0"
) -> dict[str, object]:
    """官方 `/systemone` 应答形状（`answers` 是 id→题答的映射）。"""
    return {
        "model": model,
        "answers": {
            tactics_ai.JEV_QUESTION_RELIABILITY: {
                "type": "score",
                "score": score,
                "probabilities": {"3": 0.7, "2": 0.2, "4": 0.1},
            },
            tactics_ai.JEV_QUESTION_AGREEMENT: {
                "type": "choice",
                "choice": choice,
                "probabilities": {choice: 0.71},
                "confidence": 0.71,
            },
            tactics_ai.JEV_QUESTION_FAKE_BREAKOUT: {"type": "noul", "noul": noul},
        },
        "usage": {"input_tokens": 1500, "output_tokens": 12},
    }


# chat 叙述层的标准应答（与 test_v26_sector_scan 同口径）。
_CHAT_REVIEW = {
    "ai_score": 72,
    "verdict": "bullish",
    "agreement": "partial",
    "summary": "突破形态成立但量能未同步放大",
    "strengths": ["均线多头排列"],
    "concerns": ["量能背离"],
    "fake_breakout_risk": "low",
    "key_levels": {"support": 10.5, "resistance": 12.8},
    "watch_points": ["回踩 10.5 是否守住"],
    "rule_score_comment": "规则分略高估",
}


def _chat_reply(*args: object, **kwargs: object) -> dict[str, object]:
    """打桩 chat 的 HTTP 层（`model_client._post_json`）。"""
    content = json.dumps(_CHAT_REVIEW, ensure_ascii=False)
    return {"choices": [{"message": {"content": content}}]}


# ——————————————————————————————————————————————————————————————
# 1–2. 题目与 state
# ——————————————————————————————————————————————————————————————


def test_jev_questions_are_three_typed_questions():
    questions = tactics_ai.jev_questions()
    assert set(questions) == {
        tactics_ai.JEV_QUESTION_RELIABILITY,
        tactics_ai.JEV_QUESTION_AGREEMENT,
        tactics_ai.JEV_QUESTION_FAKE_BREAKOUT,
    }
    # 可靠度：score，5 级**描述性**等级（官方：只给数字等级会让概率摊平、confidence 崩到 0.33）
    reliability = questions[tactics_ai.JEV_QUESTION_RELIABILITY]
    assert reliability["type"] == "score"
    assert len(reliability["criteria"]) == len(tactics_ai.JEV_RELIABILITY_LEVELS) == 5
    assert all(len(level) > 8 for level in reliability["criteria"]), "等级必须是描述情境而不是程度"
    # 认同度：choice，三个选项与 normalize_review 的合法枚举一致
    agreement = questions[tactics_ai.JEV_QUESTION_AGREEMENT]
    assert agreement["type"] == "choice"
    assert set(agreement["criteria"]) == {"agree", "partial", "disagree"}
    # 假突破：noul，高值代表「存在风险」（官方建议措辞让高值 = 是）
    breakout = questions[tactics_ai.JEV_QUESTION_FAKE_BREAKOUT]
    assert breakout["type"] == "noul"
    assert set(breakout["criteria"]) == {"true", "false"}


def test_jev_state_carries_only_whitelisted_evidence():
    state = tactics_ai.jev_state(
        symbol="600176",
        name="中国巨石",
        rule_score=_RULE_SCORE,
        indicators={"ma20": 10.5, "rsi14": 62},
        bars=_bars(30),
        sector={"name": "玻璃行业", "change_pct": 2.1, "member_count": 30},
    )
    # 白名单内的证据必须在场（否则规则分与 AI 分的分歧就不可比了）
    for marker in ("规则分项", "命中明细", "指标现值", "近期 K 线", "所属板块", "触发价 11.2"):
        assert marker in state, marker
    # state 出网到第三方，不得夹带账户/持仓/备注这类与判定无关的东西
    for forbidden in ("持仓", "成本价", "账户", "用户备注"):
        assert forbidden not in state


# ——————————————————————————————————————————————————————————————
# 3–4. 归一化与合并
# ——————————————————————————————————————————————————————————————


def test_jev_judgement_normalizes_score_to_percent_and_risk_to_three_way():
    answers = {
        tactics_ai.JEV_QUESTION_RELIABILITY: jev_client.JevAnswer(
            question_id="reliability", type="score", score=3.0, levels=5
        ),
        tactics_ai.JEV_QUESTION_AGREEMENT: jev_client.JevAnswer(
            question_id="agreement", type="choice", choice="disagree", confidence=0.71
        ),
        # 0.92 ≥ 0.85（R6 中文标定 YES 阈值，非官方英文范例 0.8）→ 高
        tactics_ai.JEV_QUESTION_FAKE_BREAKOUT: jev_client.JevAnswer(
            question_id="fake_breakout", type="noul", noul=0.92
        ),
    }
    judgement = tactics_ai.jev_judgement(answers)

    # 3.0 在 5 级轴（0…4）上 → 75 分；原始值与等级数都要留痕（否则事后无法复算）
    assert judgement["ai_score"] == 75.0
    assert judgement["ai_score_raw"] == 3.0
    assert judgement["ai_score_levels"] == 5
    assert judgement["agreement"] == "disagree"
    assert judgement["fake_breakout_risk"] == "high"
    assert judgement["confidence"] == 0.71
    assert judgement["noul_value"] == 0.92
    assert judgement["noul_verdict"] == "yes"


def test_jev_judgement_maps_noul_middle_band_to_medium_not_binary():
    """noul 无 confidence（C1），中间地带必须单独标「中」而不是强行二值化。"""

    def risk_for(noul: float) -> str:
        return tactics_ai.jev_judgement({
            tactics_ai.JEV_QUESTION_FAKE_BREAKOUT: jev_client.JevAnswer(
                question_id="fake_breakout", type="noul", noul=noul
            )
        })["fake_breakout_risk"]

    assert risk_for(0.9) == "high"
    assert risk_for(0.05) == "low"
    assert risk_for(0.5) == "medium"  # 0.2–0.8 之间 = 存疑，不猜


def test_jev_judgement_tolerates_missing_answers_without_raising():
    """判定层是软校验：单题不可用不该让整次复核失败（那正是 502 的成因）。"""
    judgement = tactics_ai.jev_judgement({})
    assert judgement["ai_score"] is None  # 不编分，前端显示「—」
    assert judgement["agreement"] == "partial"
    assert judgement["fake_breakout_risk"] == "medium"
    assert judgement["confidence"] is None


def test_review_view_prefers_jev_judgement_but_keeps_chat_narrative():
    narrative = tactics_ai.normalize_review({
        "ai_score": 72, "verdict": "bullish", "agreement": "agree",
        "summary": "突破形态成立但量能未同步放大",
        "strengths": ["均线多头排列"], "concerns": ["量能背离"],
        "fake_breakout_risk": "low", "key_levels": {"support": 10.5, "resistance": 12.8},
        "watch_points": ["回踩 10.5 是否守住"], "rule_score_comment": "规则分略高估",
    })
    judgement = {"ai_score": 75.0, "agreement": "disagree", "fake_breakout_risk": "high"}

    merged = tactics_ai.review_view(judgement=judgement, narrative=narrative)
    assert merged is not None
    # 判定三字段：Jev 赢（专职判定，且可复算）
    assert merged["ai_score"] == 75.0
    assert merged["agreement"] == "disagree"
    assert merged["fake_breakout_risk"] == "high"
    # 叙述字段：只能来自 chat，Jev 生成不了文本
    assert merged["summary"] == "突破形态成立但量能未同步放大"
    assert merged["verdict"] == "bullish"
    assert merged["concerns"] == ["量能背离"]
    assert merged["key_levels"] == {"support": 10.5, "resistance": 12.8}


def test_review_view_with_narrative_only_matches_pre_jv03_behaviour():
    """Jev 关掉时行为必须与今天完全一致（含 chat 自己的 ai_score）。"""
    narrative = tactics_ai.normalize_review({"ai_score": 72, "summary": "可以", "agreement": "agree"})
    merged = tactics_ai.review_view(judgement=None, narrative=narrative)
    assert merged is not None
    assert merged["ai_score"] == 72
    assert merged["agreement"] == "agree"


def test_review_view_returns_none_when_both_layers_absent():
    assert tactics_ai.review_view(judgement=None, narrative=None) is None


# ——————————————————————————————————————————————————————————————
# 5–7. 端点
# ——————————————————————————————————————————————————————————————


def _client(tmp_path: Path, monkeypatch, *, jev_enabled: bool, with_jev_key: bool = True):
    from investment_steward_core import model_client as mc
    from investment_steward_core.api import app as api_app
    from test_stock_research_tools import _file_client, _setup_active_profile

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(
        api_app, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(120), "test-provider")
    )
    monkeypatch.setattr(api_app, "fetch_cn_sector_list", list)
    monkeypatch.setattr(mc, "_post_json", _chat_reply)
    if jev_enabled:
        if with_jev_key:
            test_client.put("/credentials/jev_api_key", json={"secret": "sk-jev-secret"}, headers=headers)
        saved = test_client.put("/jev/config", headers=headers, json={
            "enabled": True,
            "base_url": jev_client.JEV_DEFAULT_BASE_URL,
            "model": jev_client.JEV_MODEL_ALIAS,
            "credential_ref": credential_store.JEV_API_KEY if with_jev_key else "",
            "timeout_secs": 60,
        })
        assert saved.status_code == 200, saved.text
    return test_client, headers


def test_jev_engine_delivers_judgement_and_records_raw_values(tmp_path, monkeypatch):
    """Jev 启用且可用：判定来自 Jev、叙述来自 chat，原始分/confidence/来源全部落库。"""
    test_client, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    calls: list[dict[str, object]] = []

    def _fake(base_url, payload, api_key, timeout):
        calls.append({"base_url": base_url, "payload": payload, "api_key": api_key})
        return _jev_systemone_response(score=3.0, choice="partial", noul=0.92)

    monkeypatch.setattr(jev_client, "_post_endpoint", _fake)

    response = test_client.post("/tactics/ai-review", headers=headers, json={"symbol": "600176"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["engine"] == "jev"
    assert body["narrative_status"] == "ok"

    review = body["review"]
    # 判定来自 Jev（score 3.0/5 级 → 75 分），而不是 chat 的 72
    assert review["ai_score"] == 75.0
    assert review["ai_score_raw"] == 3.0
    assert review["confidence"] == 0.71
    assert review["agreement"] == "partial"
    # 叙述来自 chat
    assert "突破" in review["summary"]
    assert review["verdict"] == "bullish"
    assert review["payload"]["judgement"]["noul_verdict"] == "yes"
    # model 列 = 判定层来源（响应版本号，不是请求别名）
    assert review["model"] == "Jev／jev-1.13.0"
    assert review["payload"]["jev"]["requested_model"] == "jev-latest"
    assert review["payload"]["narrative_model"].startswith("Deepseek官方")

    # 只发了一次 Jev 请求，且 state 里带的是证据而不是指令
    assert len(calls) == 1
    sent = calls[0]["payload"]
    assert sent["model"] == "jev-latest"
    assert set(sent["questions"]) == set(tactics_ai.jev_questions())
    assert "命中明细" in sent["state"]

    # 落库：engine/confidence/ai_score_raw 三列可查（JV10 对账要按它们聚合）
    listed = test_client.get("/tactics/ai-reviews", headers=headers, params={"symbol": "600176"}).json()
    assert listed[0]["engine"] == "jev"
    assert listed[0]["confidence"] == 0.71
    assert listed[0]["ai_score_raw"] == 3.0

    # purpose 必须能按场景归集（否则 JV10 分不出是哪个场景花的额度）
    db = test_client.app.state.core.database
    with sqlite3.connect(db.path) as raw:
        purposes = [row[0] for row in raw.execute("SELECT purpose FROM model_calls ORDER BY rowid")]
    assert "jev:tactics-review" in purposes
    assert "战法复核" in purposes


def test_jev_unavailable_falls_back_to_chat_and_says_so(tmp_path, monkeypatch):
    """Jev 启用但凭据缺失：判定层回落 chat，并在留痕里如实标注（不假装 Jev 参与过）。"""
    test_client, headers = _client(tmp_path, monkeypatch, jev_enabled=True, with_jev_key=False)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("凭据缺失时不应发出 Jev 请求")

    monkeypatch.setattr(jev_client, "_post_endpoint", _must_not_be_called)

    response = test_client.post("/tactics/ai-review", headers=headers, json={"symbol": "600176"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["engine"] == "chat"
    review = body["review"]
    assert review["ai_score"] == 72  # chat 的分
    assert review["payload"]["jev"] is None
    assert "回落 chat 判定" in review["payload"]["jev_note"]


def test_chat_unparsable_still_delivers_jev_judgement(tmp_path, monkeypatch):
    """JV03 的核心收益：chat 吐不出 JSON 时**不再 502**，判定层照常交付、叙述缺失如实标注。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": "抱歉，我无法回答这个问题。"}}]},
    )
    monkeypatch.setattr(
        jev_client, "_post_endpoint",
        lambda base_url, payload, api_key, timeout: _jev_systemone_response(score=1.0, noul=0.1),
    )

    response = test_client.post("/tactics/ai-review", headers=headers, json={"symbol": "600176"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["engine"] == "jev"
    assert body["narrative_status"] == "unparsable"

    review = body["review"]
    # 判定层在：1.0/4 → 25 分；noul 0.1 → 低风险（判定三字段落在 payload.review 里）
    assert review["ai_score"] == 25.0
    assert review["payload"]["review"]["fake_breakout_risk"] == "low"
    # 叙述层缺席：字段是空值而不是编造内容
    assert review["summary"] == ""
    assert review["payload"]["review"]["concerns"] == []
    assert review["verdict"] == "neutral"
    # 失败原因照旧留痕，便于判断是提示词问题还是模型问题
    assert "无法解析" in review["payload"]["narrative_error"]
