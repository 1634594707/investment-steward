"""M5.2 AI 分析层 + M5.5 AI 提议卡 + 图书馆认知档案测试。

红线锁定：
- AI 分析不修改规则层数值（只读快照，无写回路径）；
- 无引用不发布（引用校验器硬性拦截）；
- AI 提议护栏（D-12）：单维 ≤10pp / 每维 ≥10% / 30 天 2 次；用户手动不受限。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用
from investment_steward_core.domain import MacroIndicatorReading, MacroSnapshot
from investment_steward_core.macro_ai import (
    build_analysis_context,
    check_proposal_guardrails,
    count_recent_ai_proposals,
    extract_json_object,
    parse_analysis_direction,
    verify_citations,
    verify_insight_citations,
)

# ---- 共享脚手架 ----

def _snapshot(readings: list[MacroIndicatorReading]) -> MacroSnapshot:
    return MacroSnapshot(region="cn", label="中国", indicators=readings)


def _cn_readings() -> list[MacroIndicatorReading]:
    return [
        MacroIndicatorReading(indicator="cpi_yoy", label="CPI 同比", dim="inflation", status="ok",
                              latest=2.5, unit="%YoY", obs_date="2025", source="test", dataset_version="wb:cn:cpi:2025"),
        MacroIndicatorReading(indicator="unemployment_ilo", label="失业率（ILO 估算）", dim="employment", status="ok",
                              latest=6.2, unit="%", obs_date="2025", source="test", dataset_version="wb:cn:ilo:2025"),
        MacroIndicatorReading(indicator="gdp_growth", label="GDP 增长", dim="growth", status="pending", source="test"),
    ]


def _file_client(tmp_path, monkeypatch):
    """强制文件凭据后端的 client（同 test_g3_g5 的 file_client，本地重建避免循环 import）。"""
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _setup_active_profile(test_client, headers, credential_ref: str = "model_api_key") -> None:
    """创建并激活一个模型方案，存好凭据。"""
    test_client.put(
        "/credentials/model_api_key",
        json={"secret": "sk-test-1234567890"},
        headers=headers,
    )
    created = test_client.post(
        "/model-profiles",
        json={"name": "Deepseek官方", "base_url": "https://api.deepseek.com",
              "model": "deepseek-v4-flash", "credential_ref": credential_ref},
        headers=headers,
    ).json()
    activated = test_client.post(f"/model-profiles/{created['profile_id']}/activate", headers=headers)
    assert activated.status_code == 200, activated.text


# ---- 纯函数：JSON 提取 / 引用校验 / 护栏 / 30 天计数 ----


def test_extract_json_object_tolerates_fence_and_noise():
    fenced = "好的，这是提议：\n```json\n{\"weights\": {\"growth\": 0.35}}\n```\n以上。"
    assert extract_json_object(fenced) == {"weights": {"growth": 0.35}}
    noisy = '前置废话 {"a": 1} 后置废话'
    assert extract_json_object(noisy) == {"a": 1}
    assert extract_json_object("完全没有 JSON") is None
    assert extract_json_object("[1,2,3]") is None


def test_citation_verifier_rejects_uncited_analysis():
    snapshot = _snapshot(_cn_readings())
    uncited = "【趋势叙事】通胀压力可控。【情景推演】基准情形为软着陆。"
    assert verify_citations(uncited, snapshot) == []


def test_citation_verifier_accepts_real_labels_only():
    snapshot = _snapshot(_cn_readings())
    good = "（CPI 同比 2025, 2.5%）显示通胀温和；失业率（ILO 估算）6.2% 有上行。"
    cited = verify_citations(good, snapshot)
    assert "CPI 同比" in cited and "失业率（ILO 估算）" in cited
    # 只引用 1 个 → 不达标
    assert verify_citations("仅有 CPI 同比 一处引用。", snapshot) == []
    # 引用不存在的指标（编造）→ 拒绝
    fabricated = "（非农 2026-08, 15万）与（PMI 2026-08, 49.2）都走弱。"
    assert verify_citations(fabricated, snapshot) == []


def test_proposal_guardrails_dimension_bounds():
    current = {"growth": 0.35, "employment": 0.25, "inflation": 0.20, "monetary": 0.20}
    # 合法：单维 +10pp、每维 ≥10%
    ok, reason = check_proposal_guardrails(
        {"growth": 0.45, "employment": 0.20, "inflation": 0.25, "monetary": 0.10}, current
    )
    assert ok is not None and reason == ""
    # 单维变化超 10pp → 拒绝
    ok, reason = check_proposal_guardrails(
        {"growth": 0.50, "employment": 0.20, "inflation": 0.20, "monetary": 0.10}, current
    )
    assert ok is None and "10pp" in reason
    # 低于 10% 下限 → 拒绝（合计恰为 1.0，只有 monetary 违反下限）
    ok, reason = check_proposal_guardrails(
        {"growth": 0.40, "employment": 0.25, "inflation": 0.30, "monetary": 0.05}, current
    )
    assert ok is None and "下限" in reason
    # 合计 ≠ 1.0 → 拒绝
    ok, reason = check_proposal_guardrails(
        {"growth": 0.45, "employment": 0.25, "inflation": 0.25, "monetary": 0.20}, current
    )
    assert ok is None and "1.0" in reason
    # 缺维 → 拒绝
    ok, reason = check_proposal_guardrails({"growth": 0.5, "employment": 0.5}, current)
    assert ok is None and "四维" in reason


def test_count_recent_ai_proposals_30day_window():
    now = datetime.now(UTC)
    versions = [
        {"version": "v2", "source": "ai", "created_at": (now - timedelta(days=1)).isoformat()},
        {"version": "v3", "source": "ai", "created_at": (now - timedelta(days=5)).isoformat()},
        {"version": "v4", "source": "ai", "created_at": (now - timedelta(days=40)).isoformat()},
        {"version": "v5", "source": "user", "created_at": (now - timedelta(days=2)).isoformat()},
    ]
    assert count_recent_ai_proposals(versions, now=now) == 2  # 40 天前的不计，user 不计


def test_analysis_context_hides_pending_values():
    context = build_analysis_context(_snapshot(_cn_readings()))
    assert "GDP 增长" in context and "不可引用其数值" in context
    assert "CPI 同比" in context and "规则基线定位" not in context  # 无 positioning 时不出现


def test_insight_citation_verifier_requires_three_titles():
    class _Book:
        def __init__(self, title: str):
            self.title = title

    books = [_Book("投资最重要的事"), _Book("穷查理宝典"), _Book("证券分析")]
    assert len(verify_insight_citations("读《投资最重要的事》与《穷查理宝典》后…", books)) == 0
    assert len(verify_insight_citations(
        "《投资最重要的事》《穷查理宝典》《证券分析》构成三层。", books)) == 3


# ---- 端点：M5.2 分析层 ----


def test_analysis_requires_active_profile(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    response = test_client.post("/evidence/macro/cn/analysis", headers=headers)
    assert response.status_code == 409
    assert "使用中" in response.json()["detail"]


def test_analysis_rejects_uncited_output(client, tmp_path, monkeypatch):
    from investment_steward_core import macro_feed
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(
        macro_feed, "fetch_worldbank_series",
        lambda iso3, indicator_id: [{"obs_date": "2025", "value": 2.5 if indicator_id.startswith("FP") else 6.2}],
    )
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": "【趋势叙事】整体温和。【情景推演】软着陆。"}}]})
    response = test_client.post("/evidence/macro/cn/analysis", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["stage"] == "citation_check"
    assert body["analysis"] is None


def test_analysis_success_with_citations_and_cache(client, tmp_path, monkeypatch):
    from investment_steward_core import macro_feed
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(
        macro_feed, "fetch_worldbank_series",
        lambda iso3, indicator_id: [{"obs_date": "2025", "value": 2.5 if indicator_id.startswith("FP") else 6.2}],
    )

    def fake_post(base_url, json_payload, api_key, timeout):
        # C01：CN 主口径 CPI 改为「IMF 月度」（需 FRED key，用例内不可得），
        # 因此本用例引用 WorldBank 年度背景口径——与真实 ok 指标 label 逐字一致。
        content = (
            "【趋势叙事】CPI 同比（年度，世界银行）2.5% 温和，失业率（ILO 估算）6.2% 平稳。\n"
            "【情景推演】基准：软着陆；风险：失业率（ILO 估算）破 7；反转：CPI 同比（年度，世界银行）回 3%。\n"
            "【关注清单】下期 CPI 同比（年度，世界银行）与失业率（ILO 估算）。\n"
            "【分析局限】年度数据滞后。"
        )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(mc, "_post_json", fake_post)
    response = test_client.post("/evidence/macro/cn/analysis", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert set(body["citations"]) == {"CPI 同比（年度，世界银行）", "失业率（ILO 估算）"}
    assert body["band"] in {"扩张", "放缓", "承压", "衰退风险"}
    assert body["model"] == "deepseek-v4-flash"


# ---- M5.2 v2：三层对照（AI 结构化方向 + 分歧标记数据链） ----


def test_parse_analysis_direction_accepts_four_bands_and_tolerates_noise():
    for band in ("扩张", "放缓", "承压", "衰退风险"):
        assert parse_analysis_direction(f"【AI 方向】{band}\n【趋势叙事】正文。") == band
    # 首行带杂讯 / 空格变体也能命中
    assert parse_analysis_direction("好的，以下是分析：\n【AI 方向】 承压\n【趋势叙事】正文。") == "承压"
    assert parse_analysis_direction("【AI 方向】衰退风险") == "衰退风险"


def test_parse_analysis_direction_accepts_model_direction_prefix():
    # 现行提示词输出「【模型方向】」；历史缓存「【AI 方向】」保持兼容（去 AI 化文案调整）。
    assert parse_analysis_direction("【模型方向】放缓\n【趋势叙事】正文。") == "放缓"
    assert parse_analysis_direction("【模型方向】 扩张") == "扩张"
    assert parse_analysis_direction("【模型方向】过热") is None


def test_parse_analysis_direction_rejects_missing_or_invalid():
    assert parse_analysis_direction("【趋势叙事】没有方向行的旧格式输出。") is None
    assert parse_analysis_direction("【AI 方向】过热\n【趋势叙事】非法档位不编造。") is None
    assert parse_analysis_direction("【AI方向】扩张") == "扩张"  # \s* 容忍无空格变体
    assert parse_analysis_direction("") is None


def test_analysis_direction_passthrough_and_legacy_compat(client, tmp_path, monkeypatch):
    from investment_steward_core import macro_feed
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(
        macro_feed, "fetch_worldbank_series",
        lambda iso3, indicator_id: [{"obs_date": "2025", "value": 2.5 if indicator_id.startswith("FP") else 6.2}],
    )

    def fake_post_with_direction(base_url, json_payload, api_key, timeout):
        content = (
            "【AI 方向】扩张\n"
            "【趋势叙事】CPI 同比（年度，世界银行）2.5% 温和，失业率（ILO 估算）6.2% 平稳。\n"
            "【情景推演】基准：软着陆；风险：失业率（ILO 估算）上行；反转：CPI 同比（年度，世界银行）回落。\n"
            "【关注清单】下期 CPI 同比（年度，世界银行）与失业率（ILO 估算）。\n"
            "【分析局限】年度数据滞后。"
        )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(mc, "_post_json", fake_post_with_direction)
    body = test_client.post("/evidence/macro/cn/analysis", headers=headers).json()
    assert body["ok"] is True and body["direction"] == "扩张"
    # 缓存回读同样带 direction（GET 返回 {**payload, cached: True}）
    cached = test_client.get("/evidence/macro/cn/analysis", headers=headers).json()
    assert cached["direction"] == "扩张"

    # 旧格式（无方向行）兼容：direction=None，不报错不编造
    def fake_post_legacy(base_url, json_payload, api_key, timeout):
        content = (
            "【趋势叙事】CPI 同比（年度，世界银行）2.5% 温和，失业率（ILO 估算）6.2% 平稳。\n"
            "【情景推演】基准：软着陆。\n【关注清单】CPI 同比（年度，世界银行）。\n【分析局限】年度数据。"
        )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(mc, "_post_json", fake_post_legacy)
    body2 = test_client.post("/evidence/macro/cn/analysis", headers=headers).json()
    assert body2["ok"] is True and body2["direction"] is None


# ---- 端点：M5.5 AI 提议卡 ----


def test_ai_proposal_guardrail_rejects_oversized_delta(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    # 模型给出单维 +20pp 的越界提议（inflation 0.20 → 0.40，且 monetary 压到 0.05 触发下限）
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": json.dumps({
            "weights": {"growth": 0.35, "employment": 0.20, "inflation": 0.40, "monetary": 0.05},
            "rationale": "用户说「把通胀权重调高些」", "dim_changed": "inflation", "direction": "上调"})}}]},
    )
    response = test_client.post("/evidence/macro/weights/ai-proposal", json={"intent": "把通胀权重调高些"}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["stage"] == "guardrail"
    assert "护栏" in body["detail"]


def test_ai_proposal_success_and_confirmation_flow(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": json.dumps({
            "weights": {"growth": 0.35, "employment": 0.20, "inflation": 0.30, "monetary": 0.15},
            "rationale": "用户说「通胀更重要」", "dim_changed": "inflation", "direction": "上调"})}}]},
    )
    response = test_client.post(
        "/evidence/macro/weights/ai-proposal",
        json={"intent": "通胀更重要，权重调高些"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["proposal"]["weights"] == {"growth": 0.35, "employment": 0.20, "inflation": 0.30, "monetary": 0.15}
    assert "通胀更重要" in body["proposal"]["rationale"]
    # 用户确认 → 以 source=ai 保存（roundtrip）
    confirm = test_client.post(
        "/evidence/macro/weights",
        json={"weights": body["proposal"]["weights"], "note": body["proposal"]["rationale"], "source": "ai"},
        headers=headers,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["source"] == "ai"
    listing = test_client.get("/evidence/macro/weights", headers=headers).json()
    assert listing["active_version"] == "v2"
    assert listing["versions"][0]["source"] == "ai"


def test_ai_proposal_30day_limit(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": json.dumps({
            "weights": {"growth": 0.40, "employment": 0.20, "inflation": 0.25, "monetary": 0.15},
            "rationale": "r", "dim_changed": "growth", "direction": "上调"})}}]},
    )
    for _ in range(2):
        assert test_client.post(
            "/evidence/macro/weights/ai-proposal", json={"intent": "测试提议"}, headers=headers
        ).json()["ok"] is True
        assert test_client.post(
            "/evidence/macro/weights",
            json={"weights": {"growth": 0.40, "employment": 0.20, "inflation": 0.25, "monetary": 0.15},
                  "note": "", "source": "ai"},
            headers=headers,
        ).status_code == 200
    third = test_client.post("/evidence/macro/weights/ai-proposal", json={"intent": "再提一次"}, headers=headers)
    assert third.status_code == 429
    assert "30 天" in third.json()["detail"]
    # 用户手动调整不受护栏限制（同 422/200 口径）
    manual = test_client.post(
        "/evidence/macro/weights",
        json={"weights": {"growth": 0.50, "employment": 0.20, "inflation": 0.20, "monetary": 0.10}, "note": "手动"},
        headers=headers,
    )
    assert manual.status_code == 200


def test_analysis_cache_readback(client, tmp_path, monkeypatch):
    """GET /analysis 读缓存：成功生成后回读 cached=true；无历史时 cached=false 不报错。"""
    from investment_steward_core import macro_feed
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(
        macro_feed, "fetch_worldbank_series",
        lambda iso3, indicator_id: [{"obs_date": "2025", "value": 2.5 if indicator_id.startswith("FP") else 6.2}],
    )
    empty = test_client.get("/evidence/macro/cn/analysis", headers=headers)
    assert empty.status_code == 200 and empty.json()["cached"] is False
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": (
            "【趋势叙事】CPI 同比（年度，世界银行）2.5% 温和，失业率（ILO 估算）6.2% 平稳。\n"
            "【情景推演】基准：软着陆。风险：失业率（ILO 估算）上行。反转：CPI 同比（年度，世界银行）回升。\n"
            "【关注清单】下期数据。\n【分析局限】年度口径。"
        )}}]},
    )
    assert test_client.post("/evidence/macro/cn/analysis", headers=headers).json()["ok"] is True
    readback = test_client.get("/evidence/macro/cn/analysis", headers=headers)
    assert readback.status_code == 200
    body = readback.json()
    assert body["cached"] is True and body["ok"] is True
    assert "CPI 同比（年度，世界银行）" in body["citations"]


# ---- 端点：§3.7 第三层「我的分析」（用户主权，不依赖 AI）----


def test_my_view_roundtrip_and_validation(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    # 初始为空
    empty = test_client.get("/evidence/macro/cn/my-view", headers=headers)
    assert empty.status_code == 200 and empty.json()["view"] is None
    # 保存 → 读回
    saved = test_client.put(
        "/evidence/macro/cn/my-view",
        json={"direction": "放缓", "horizon": "3 个月", "confidence": "中", "text": "我认为是软着陆前夜"},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["direction"] == "放缓" and body["confidence"] == "中"
    readback = test_client.get("/evidence/macro/cn/my-view", headers=headers).json()
    assert readback["view"]["text"] == "我认为是软着陆前夜"
    # 覆盖（每经济体一条）
    test_client.put(
        "/evidence/macro/cn/my-view",
        json={"direction": "承压", "horizon": "1 个月", "confidence": "低"},
        headers=headers,
    )
    rows = test_client.get("/evidence/macro/cn/my-view", headers=headers).json()
    assert rows["view"]["direction"] == "承压"
    # 非法 direction → 422
    bad = test_client.put(
        "/evidence/macro/cn/my-view",
        json={"direction": "暴涨", "horizon": "1 个月", "confidence": "高"},
        headers=headers,
    )
    assert bad.status_code == 422
    # 不支持的 region → 422
    bad_region = test_client.put(
        "/evidence/macro/xx/my-view",
        json={"direction": "放缓", "horizon": "1 个月", "confidence": "高"},
        headers=headers,
    )
    assert bad_region.status_code == 422


# ---- 端点：图书馆认知档案 ----


def test_insights_require_books(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    response = test_client.post("/library/insights", headers=headers)
    assert response.status_code == 409
    assert "书单为空" in response.json()["detail"]


def test_insights_rejects_uncited_profile(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    assert test_client.post(
        "/library/books", json={"title": "投资最重要的事", "author": "Howard Marks"}, headers=headers
    ).status_code == 201
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": "【当前关注主题】价值投资。【知识结构】经典著作为主。"}}]},
    )
    response = test_client.post("/library/insights", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["stage"] == "citation_check"


def test_insights_success_with_book_citations(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    for title in ("投资最重要的事", "穷查理宝典", "证券分析"):
        assert test_client.post(
            "/library/books", json={"title": title, "author": "作者"}, headers=headers
        ).status_code == 201
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": (
            "【当前关注主题】《投资最重要的事》的风险观与《穷查理宝典》的多元思维。\n"
            "【知识结构】《证券分析》提供估值底座。\n"
            "【荐读方向】周期与逆向投资主题。\n"
            "【研究与投资的连接】证据账本对照阅读。"
        )}}]},
    )
    response = test_client.post("/library/insights", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert set(body["citations"]) == {"投资最重要的事", "穷查理宝典", "证券分析"}
    assert body["book_count"] == 3
