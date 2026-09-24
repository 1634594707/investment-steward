"""v22 AI 研究产出持久化测试（方向研判 / 个股研报落库 + 列表/详情/删除）。

红线：生成成功才落库；落库失败不阻断返回（尽力而为）；
kind 仅 direction|stock；删除不存在 404；重启不丢由 SQLite 表承载。
"""

from __future__ import annotations

import json

from conftest import client as client_fixture  # noqa: F401


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


def test_direction_report_persisted_and_listed(tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    from investment_steward_core.direction_research import REQUIRED_DIRECTION_SECTIONS
    sections = "".join(f"## {name}\n可核对的数据与推理边界。\n" for name in REQUIRED_DIRECTION_SECTIONS)
    payload = json.dumps({
        "report": sections,
        "executive_summary": "国产替代加速，但设备良率爬坡节奏是最大不确定性。",
        "core_judgments": [
            {"id": "J1", "text": "国产替代加速", "kind": "industry", "confidence": "medium"},
            {"id": "J2", "text": "代工格局占优", "kind": "competitiveness", "confidence": "low"},
            {"id": "J3", "text": "热度高位", "kind": "sentiment", "confidence": "low"},
        ],
        "catalysts": ["算力需求"],
        "risks": ["产能过剩"],
        "stock_pool": [{"symbol_raw": "600001", "name": "甲股", "business_link": "龙头", "profit_path": "订单转化"}],
        "data_gaps": ["招标数据未接入"],
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": payload}}]})

    generated = test_client.post(
        "/evidence/direction-research", json={"topic": "AI 芯片"}, headers=headers
    ).json()
    assert generated["ok"] is True and generated.get("report_id")

    listed = test_client.get("/ai-research/reports", headers=headers).json()
    assert listed["ok"] is True
    assert [item["report_id"] for item in listed["items"]] == [generated["report_id"]]
    item = listed["items"][0]
    assert item["kind"] == "direction" and item["subject"] == "AI 芯片"
    # B01（桌面端升级路线图 2026-09-18）改口径：列表项为轻量投影（不带 payload），
    # executive_summary 等全文内容改从详情端点断言；理由见 B01 验证记录。
    assert "executive_summary" not in item

    detail = test_client.get(f"/ai-research/reports/{generated['report_id']}", headers=headers).json()
    assert detail["ok"] is True and detail["item"]["topic"] == "AI 芯片"
    assert "国产替代加速" in detail["item"]["executive_summary"]  # v33：摘要单独成字段；direction_summary = 正文 markdown

    kind_filter = test_client.get("/ai-research/reports", params={"kind": "stock"}, headers=headers).json()
    assert kind_filter["items"] == []


def test_stock_report_persisted(tmp_path, monkeypatch):
    """个股研报成功路径同样落库（证据来源 mock，行情测试不真实联网）。"""
    import investment_steward_core.api.app as app_module
    from investment_steward_core import model_client as mc

    def _synth_bars():
        # 40 根合成日线（战法引擎 MIN_BARS=35），价格缓涨便于生成指标
        bars = []
        for i in range(40):
            close = round(6.0 + i * 0.02, 3)
            bars.append({
                "date": f"2026-07-{i + 10:02d}" if i < 21 else f"2026-08-{i - 20:02d}",
                "open": round(close - 0.02, 3), "close": close,
                "high": round(close + 0.03, 3), "low": round(close - 0.04, 3),
                "volume": 1_000_000 + i * 1000,
            })
        return bars

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    # conftest client 会自动安装 official.cn-market-data（ENABLED）；行情/资讯一律 mock。
    # _tactic_bars 是 create_app 闭包内函数，patch 其底层的模块级 fetch_cn_kline。
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_synth_bars(), "test")
    )
    monkeypatch.setattr(app_module, "fetch_cn_news", lambda symbol, limit=8: [])
    monkeypatch.setattr(app_module, "fetch_cn_announcements", lambda symbol, limit=6: [])
    monkeypatch.setattr(app_module, "fetch_financials", lambda symbol, limit_per_type=8: [])
    # v24：估值证据源（S5）独立取数，须 mock 以免测试真实联网。
    from investment_steward_core.valuation_evidence import ValuationSnapshot

    monkeypatch.setattr(
        app_module, "fetch_valuation",
        lambda symbol, **kwargs: ValuationSnapshot(
            symbol=symbol, name="测试标的", board_code="B1", board_name="测试行业",
            trade_date="2026-09-10", close_price=10.0, pe_ttm=19.73, pb_mrq=6.39,
            ps_ttm=9.27, total_market_cap=1e11, float_market_cap=1e11, total_shares=1e10,
        ),
    )
    report_payload = json.dumps({
        "title": "测试标的研报",
        "report": "## 技术面\n- 判断 [S1]",
        "citations": ["S1"],
        "limitations": ["测试口径"],
        "confidence": "low",
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": report_payload}}]})

    generated = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "000060"}, headers=headers
    ).json()
    assert generated["ok"] is True and generated.get("report_id"), generated

    listed = test_client.get("/ai-research/reports", params={"kind": "stock"}, headers=headers).json()
    assert [item["report_id"] for item in listed["items"]] == [generated["report_id"]]
    assert listed["items"][0]["title"] == "测试标的研报"


def test_delete_missing_report_404(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    response = test_client.delete("/ai-research/reports/no-such-id", headers=headers)
    assert response.status_code == 404


def test_list_rejects_bad_kind(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    response = test_client.get("/ai-research/reports", params={"kind": "bogus"}, headers=headers)
    assert response.status_code == 422


def test_reports_survive_reopen(tmp_path, monkeypatch):
    """持久化核心承诺：同一 data_dir 重开 Core（新 Database 实例）后报告仍在。"""
    from fastapi.testclient import TestClient
    from investment_steward_core import model_client as mc
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"

    def make_client():
        return TestClient(create_app(CoreSettings(session_token=token, data_dir=tmp_path)))

    first, headers = make_client(), {"X-Core-Session-Token": token}
    test_client, headers = first, headers
    _setup_active_profile(test_client, headers)
    payload = json.dumps({
        "report": "".join(f"## {n}\n正文。\n" for n in __import__("investment_steward_core").direction_research.REQUIRED_DIRECTION_SECTIONS),
        "executive_summary": "重启不丢验证。",
        "core_judgments": [
            {"id": "J1", "text": "判断一", "kind": "industry", "confidence": "low"},
            {"id": "J2", "text": "判断二", "kind": "competitiveness", "confidence": "low"},
            {"id": "J3", "text": "判断三", "kind": "sentiment", "confidence": "low"},
        ],
        "catalysts": [], "risks": [],
        "stock_pool": [{"symbol_raw": "600001", "name": "甲股", "business_link": "龙头", "profit_path": "订单转化"}],
        "data_gaps": ["缺数据"],
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": payload}}]})
    generated = test_client.post("/evidence/direction-research", json={"topic": "重启验证"}, headers=headers).json()
    assert generated["ok"] is True

    reopened = make_client()
    listed = reopened.get("/ai-research/reports", headers=headers).json()
    assert [item["report_id"] for item in listed["items"]] == [generated["report_id"]]
    assert listed["items"][0]["subject"] == "重启验证"
