"""v21 追加两能力：战法雷达全市场趋势扫描（mode=all）+ AI 个股研究报告。

红线：
- 全市场遍历结果缓存 5 分钟，complete=False 如实标注不完整遍历；
- 研究报告：四类输入各自独立取数、失败降级为「来源不可用」，全部失败不调模型不编造；
- 报告输出无 citations 不发布（ADR-0006 同口径）。
行情/新闻/财报取数一律 monkeypatch，绝不真实联网。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用




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

def _file_client(tmp_path, monkeypatch):
    """强制文件凭据后端的 client（同 test_m52 的本地重建，避免循环 import）。"""
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
    """合成缓涨日线（战法引擎 MIN_BARS=35，须超过阈值）。"""
    rows = []
    price = 10.0
    for index in range(count):
        price = round(price * 1.01, 2)
        rows.append({
            "timestamp": f"2026-06-{(index % 28) + 1:02d}",
            "open": price * 0.99,
            "close": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "volume": 1_000_000 + index * 1000,
        })
    return rows


def _market_rows() -> list[dict[str, object]]:
    return [
        {"symbol": "600001", "name": "甲股", "price": 12.0, "change_pct": 3.0, "volume": 1000, "turnover": 5.0e8, "turnover_rate": 5.0},
        {"symbol": "600002", "name": "乙股", "price": 8.0, "change_pct": 8.0, "volume": 2000, "turnover": 9.0e8, "turnover_rate": 8.0},
        {"symbol": "600003", "name": "丙股", "price": 6.0, "change_pct": -1.0, "volume": 100, "turnover": 1.0e7, "turnover_rate": 0.5},
        {"symbol": "688001", "name": "科创股", "price": 30.0, "change_pct": 6.0, "volume": 1500, "turnover": 7.0e8, "turnover_rate": 6.0},
    ]


# ---- 全市场趋势扫描（mode=all）----


def test_scan_market_all_mode_runs_engine_on_top_candidates(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_market_full_a",
        lambda max_pages=40: (_market_rows(), True),
    )
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_kline",
        lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider"),
    )
    body = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 3, "recent_bars": 5})
    assert body["mode"] == "all" and body["complete"] is True
    assert body["market_rows"] == 4 and body["scanned"] == 3
    assert all(row["ok"] for row in body["results"])
    # 趋势预筛评分输出 + 评分降序主排序：乙股（8% 涨 + 8% 换手 + 高成交额分位）第一
    top = body["results"][0]
    assert top["symbol"] == "600002"
    assert top["trend_score"] is not None and top["trend_score"] > 0
    assert set(top["trend_parts"].keys()) == {"cp", "tr", "amt"}
    scores = [row.get("trend_score", -1) for row in body["results"]]
    assert scores == sorted(scores, reverse=True)


def test_scan_market_all_incomplete_traversal_is_flagged(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_market_full_a",
        lambda max_pages=40: (_market_rows(), False),
    )
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_kline",
        lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider"),
    )
    body = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 2})
    assert body["complete"] is False and body["scanned"] == 2


def test_scan_market_all_segment_filter(client, tmp_path, monkeypatch):
    """板块过滤：只选主板时 688 开头的科创板行在预筛前被剔除。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_market_full_a",
        lambda max_pages=40: (_market_rows(), True),
    )
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_kline",
        lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider"),
    )
    body = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10, "segments": ["main"]})
    assert body["segments"] == ["main"]
    symbols = {row["symbol"] for row in body["results"]}
    assert "688001" not in symbols and symbols <= {"600001", "600002", "600003"}
    # 默认全板块：科创板在列
    body_all = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10})
    assert set(body_all["segments"]) == {"main", "chinext", "star"}
    assert "688001" in {row["symbol"] for row in body_all["results"]}


def test_scan_market_all_price_range_filter(client, tmp_path, monkeypatch):
    """价格区间：10 元以下只留丙股（6.0）；5~9 元留乙股+丙股；min>max 视为不限。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_market_full_a",
        lambda max_pages=40: (_market_rows(), True),
    )
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_kline",
        lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider"),
    )
    low = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10, "price_max": 10})
    # ≤10 元：乙股（8.0）+ 丙股（6.0）；高价股（甲 12.0 / 科创 30.0）剔除
    assert {row["symbol"] for row in low["results"]} == {"600002", "600003"}
    band = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10, "price_min": 5, "price_max": 9})
    assert {row["symbol"] for row in band["results"]} == {"600002", "600003"}
    band_high = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10, "price_min": 10})
    assert {row["symbol"] for row in band_high["results"]} == {"600001", "688001"}
    # min>max 无效区间 → 如实按不限处理（全部候选都在）
    inverted = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10, "price_min": 100, "price_max": 1})
    assert inverted["scanned"] == 4


def test_scan_market_tactic_subset(client, tmp_path, monkeypatch):
    """战法选择：tactic_ids 非空时信号只来自所选战法（跨 mode 生效）。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_market_full_a",
        lambda max_pages=40: (_market_rows(), True),
    )
    monkeypatch.setattr(
        "investment_steward_core.api.app.fetch_cn_kline",
        lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider"),
    )
    body = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10, "tactic_ids": ["platform_breakout"]})
    for row in body["results"]:
        if row["ok"]:
            assert set(row["hit_tactics"]) <= {"platform_breakout"}
    # 空 tactic_ids = 全部战法（与默认行为一致）
    body_all = _post_scan_sync(test_client, headers, {"mode": "all", "top_symbols": 10, "tactic_ids": []})
    assert body_all["scanned"] == body["scanned"]


# 注：CoreState.__init__ 自动引导安装 official.cn-market-data（ENABLED），
# 「未启用插件 → 409」在纯净新库上不可构造；该守卫与 boards 模式共用，已由既有测试覆盖。


# ---- AI 个股研究报告 ----


def _mock_sources(monkeypatch) -> None:
    import investment_steward_core.api.app as app_module

    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider")
    )
    monkeypatch.setattr(
        app_module, "fetch_cn_news",
        lambda symbol, limit=20: [SimpleNamespace(published_raw="2026-09-01", title="甲股中标大单", content="内容正文")],
    )
    monkeypatch.setattr(
        app_module, "fetch_cn_announcements",
        lambda symbol, limit=20: [SimpleNamespace(notice_date_raw="2026-09-02", title="关于回购的公告", ann_type="回购", url="http://x", art_code="A1")],
    )
    # v22.1 起 S4 用「规范化英文行项目 + 累计报告期」的真实契约（派生 TTM 依赖同月日差分）。
    monkeypatch.setattr(
        app_module, "fetch_financials",
        lambda symbol, limit_per_type=8: [
            SimpleNamespace(statement_type="income", period_end="2026-06-30", published_raw="2026-08-30",
                            items={"revenue": "1000000000", "net_profit": "120000000"}, identity="h-income-2606"),
            SimpleNamespace(statement_type="income", period_end="2025-12-31", published_raw="2026-03-30",
                            items={"revenue": "1800000000", "net_profit": "200000000"}, identity="h-income-2512"),
            SimpleNamespace(statement_type="income", period_end="2025-06-30", published_raw="2025-08-30",
                            items={"revenue": "900000000", "net_profit": "100000000"}, identity="h-income-2506"),
            SimpleNamespace(statement_type="balance", period_end="2026-06-30", published_raw="2026-08-30",
                            items={"total_equity": "5000000000"}, identity="h-balance-2606"),
            SimpleNamespace(statement_type="cash_flow", period_end="2026-06-30", published_raw="2026-08-30",
                            items={"operating_cash_flow": "150000000"}, identity="h-cash-2606"),
            SimpleNamespace(statement_type="cash_flow", period_end="2025-12-31", published_raw="2026-03-30",
                            items={"operating_cash_flow": "300000000"}, identity="h-cash-2512"),
            SimpleNamespace(statement_type="cash_flow", period_end="2025-06-30", published_raw="2025-08-30",
                            items={"operating_cash_flow": "120000000"}, identity="h-cash-2506"),
        ],
    )
    # v24 档 B/C：估值证据源（S5）。用真实 `ValuationSnapshot` 契约，测试绝不联网。
    from investment_steward_core.valuation_evidence import ValuationSnapshot

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


def test_research_report_requires_active_profile(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _mock_sources(monkeypatch)
    response = test_client.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers)
    assert response.status_code == 409
    assert "使用中" in response.json()["detail"]


def test_research_report_success_with_citations(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report_json = json.dumps({
        "title": "甲股：技术面缓涨配合基本面稳健",
        "report": "【技术面】60 日缓涨 [S1]。【消息面】中标大单 [S2]；回购公告 [S3]。【基本面】总资产 100亿 [S4]。",
        "citations": ["S1", "S2", "S3", "S4"],
        "limitations": ["公开来源摘要口径"],
        "confidence": "medium",
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": report_json}}]})
    response = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "question": "趋势是否延续"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["symbol"] == "600001"
    assert set(body["citations"]) == {"S1", "S2", "S3", "S4"}
    assert body["model"] == "deepseek-v4-flash"
    assert body["source_errors"] == {}
    assert set(body["source_keys"]) == {"S2", "S3", "S4", "S5"}


def test_research_report_all_sources_fail_no_model_call(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    import investment_steward_core.api.app as app_module

    def _raise(*a, **k):
        raise RuntimeError("网络不可用")

    monkeypatch.setattr(app_module, "fetch_cn_kline", _raise)
    monkeypatch.setattr(app_module, "fetch_cn_news", _raise)
    monkeypatch.setattr(app_module, "fetch_cn_announcements", _raise)
    monkeypatch.setattr(app_module, "fetch_financials", _raise)
    monkeypatch.setattr(app_module, "fetch_valuation", _raise)
    called = {"count": 0}

    def _spy(*a, **k):
        called["count"] += 1
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(mc, "_post_json", _spy)
    response = test_client.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False and body["stage"] == "evidence_unavailable"
    assert called["count"] == 0  # 无证据不调模型（不编造）
    # v22：周/月线补充层失败也如实计入 source_errors（kline_week / kline_month）；
    # v24：估值来源（S5）独立降级，失败同样如实记录，绝不用财务数据冒充估值。
    assert set(body["source_errors"].keys()) == {
        "kline", "kline_week", "kline_month", "news", "announcements", "financials", "valuation",
    }


def test_research_report_rejects_uncited_output(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": json.dumps({
            "title": "无引用报告", "report": "全部是判断没有来源标注。", "citations": []})}}]},
    )
    body = test_client.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()
    assert body["ok"] is False and body["stage"] == "parse"
    assert "无引用不发布" in body["detail"]
