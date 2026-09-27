"""第四轮审计的回归（web-1 / web-8 / web-3 / web-2 / web-5 / api-1 / domain-8）。

前两轮改动的教训在这一轮反复出现：**测试必须打穿到调用点**。
本文件里 Q1、T8 都先栽在「只测被测单元、没测调用点」上，所以这里的用例一律
沿真实链路断言（端点 / hook 的公开行为），而不是内部实现。
"""

from __future__ import annotations

from investment_steward_core import tactics_ai
from investment_steward_core.api import app as api_app

# ------------------------------------------------------ domain-8：提示词不写死上限


def test_review_prompt_states_actual_requested_limit():
    """bars_limit 可取 60-500，提示词不许再写死「250 根上限」。

    这段文本会**出网**（chat 的 user 消息 + Jev 的 state），错误的标尺会让模型
    按错的位置/新鲜度判断，属实的错误证据。
    """
    bars = [{"timestamp": f"2026-01-{i + 1:02d}", "open": 10, "high": 11, "low": 9,
             "close": 10 + i * 0.01, "volume": 1000} for i in range(60)]
    common = {"symbol": "600000", "name": "浦发银行", "rule_score": {}, "indicators": {}, "bars": bars}

    _, user_500 = tactics_ai.review_messages(**common, bars_limit=500)
    assert "本次请求上限 500 根" in user_500
    assert "回看 250 根上限" not in user_500

    _, user_60 = tactics_ai.review_messages(**common, bars_limit=60)
    assert "本次请求上限 60 根" in user_60
    assert "250 根上限" not in user_60

    state_500 = tactics_ai.jev_state(**common, bars_limit=500)
    assert "本次请求上限 500 根" in state_500
    assert "回看 250 根上限" not in state_500


def test_review_prompt_default_limit_is_250():
    """不传时保持 250（与端点 `TacticsReviewRequest.bars_limit` 的默认值一致）。"""
    bars = [{"timestamp": "2026-01-01", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1}]
    _, user = tactics_ai.review_messages(symbol="6", name="x", rule_score={}, indicators={}, bars=bars)
    assert "本次请求上限 250 根" in user


def test_review_endpoint_threads_bars_limit_into_prompt():
    """回归点：端点必须把 `body.bars_limit` 真的传下去（不是只改提示词模板）。"""
    import inspect

    source = inspect.getsource(api_app)
    # 两处调用点都要带 bars_limit
    assert source.count("bars_limit=body.bars_limit") >= 2, "端点没有把 bars_limit 传进提示词"


# ------------------------------------------------------ api-1：追问落库失败要如实说


def test_followup_endpoint_reports_persisted_flag_when_insert_fails(tmp_path, monkeypatch):
    """**核心回归**：追问落库失败必须 `persisted: false`，而不是静默 pass 掉。

    改造前是 `except Exception: pass`——S3 只修了个股研报那一处，漏了追问。
    后果：这一轮追问是真实的付费模型调用，答案正常渲染，但从未进库；刷新/重启后
    消失，「连续追问承接」也断了，而界面零提示。

    走真实链路：先跑出��份已落库研报 → 注入写失败 → 提交追问 → 断言响应带
    `persisted: false` 且答案仍照常交付。
    """
    from conftest import run_stock_report
    from investment_steward_core.storage import database as db_module
    from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile

    http, headers = _file_client(tmp_path, monkeypatch)
    assert http.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(http, headers)
    _mock_sources(monkeypatch)

    import json as _json

    from investment_steward_core import model_client as mc
    from test_jev06_followup_triage import _FOLLOWUP_JSON, _REPORT_JSON

    def _chat(_base_url, json_payload, _api_key, _timeout):
        blob = _json.dumps(json_payload, ensure_ascii=False)
        content = _FOLLOWUP_JSON if "追加的分析附录" in blob else _REPORT_JSON
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(mc, "_post_json", _chat)

    report = run_stock_report(http, headers, symbol="600001")
    report_id = report["report_id"]
    assert report_id, "前置：先要有一份已落库研报"

    def _boom(*_a, **_k):
        raise RuntimeError("模拟磁盘写满")

    original = db_module.Database.insert_ai_analysis_turn
    monkeypatch.setattr(db_module.Database, "insert_ai_analysis_turn", _boom)
    response = http.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={"question": "为什么这里？"}, headers=headers,
    )
    monkeypatch.setattr(db_module.Database, "insert_ai_analysis_turn", original)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("persisted") is False, f"落库失败却没如实告知：{body.keys()}"
    # 答案仍照常交付（S3 口径：交付优先，但必须说清没留档）
    assert body.get("turn") is not None
    # 清单里不应出现这条（确实没写进去）
    listed = http.get(f"/ai-research/reports/{report_id}/follow-ups", headers=headers).json()
    assert listed["count"] == 0, "写失败了却出现在附录里"


# ------------------------------------------------------ 前端侧：见 vitest 用例
# （web-1 / web-2 / web-3 / web-5 / web-8 的断言在 apps/web-shell 侧的
#   useResearch.fourth.test.ts 与 ResearchWorkbenchPage.fourth.test.tsx）
