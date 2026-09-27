"""S3（用户视角路线图 2026-09-26）：研报落库失败必须如实告知，不得静默吞掉。

改造前三个落库点（个股 `api/app.py`、方向研判、协同修订稿）都是
`except Exception: pass`。落库失败时端点仍返回 **200 + 完整已付费的报告**，但响应里
没有 `report_id` 键——用户读完关掉，报告从未写库、无 id 可回取、不会出现在
`GET /ai-research/reports`，界面零提示。按 S7 口径，单份研报最坏可达 8 分钟付费调用，
一次磁盘满/锁超时就会凭空蒸发。

本文件锁定：落库失败时 `persisted=False`、**没有** `report_id`、日志留痕；
落库成功时 `persisted=True` 且有 `report_id`。
"""

from __future__ import annotations

import json
import logging

from conftest import client as client_fixture, run_stock_report  # noqa: F401
from investment_steward_core.storage import database as db_module


def _file_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    return TestClient(create_app(CoreSettings(session_token=token, data_dir=tmp_path))), {
        "X-Core-Session-Token": token
    }


def _setup_active_profile(test_client, headers) -> None:
    test_client.put("/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers)
    created = test_client.post(
        "/model-profiles",
        json={"name": "Deepseek官方", "base_url": "https://api.deepseek.com",
              "model": "deepseek-v4-flash", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    test_client.post(f"/model-profiles/{created['profile_id']}/activate", headers=headers)


def _synth_bars():
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


def _prepare(monkeypatch) -> None:
    """把取数与模型调用全部 mock 掉，测试不真实联网、不花钱。"""
    import investment_steward_core.api.app as app_module
    from investment_steward_core import model_client as mc
    from investment_steward_core.valuation_evidence import ValuationSnapshot

    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_synth_bars(), "test")
    )
    monkeypatch.setattr(app_module, "fetch_cn_news", lambda symbol, limit=8: [])
    monkeypatch.setattr(app_module, "fetch_cn_announcements", lambda symbol, limit=6: [])
    monkeypatch.setattr(app_module, "fetch_financials", lambda symbol, limit_per_type=8: [])
    monkeypatch.setattr(
        app_module, "fetch_valuation",
        lambda symbol, **kwargs: ValuationSnapshot(
            symbol=symbol, name="测试标的", board_code="B1", board_name="测试行业",
            trade_date="2026-09-10", close_price=10.0, pe_ttm=19.73, pb_mrq=6.39,
            ps_ttm=9.27, total_market_cap=1e11, float_market_cap=1e11, total_shares=1e10,
        ),
    )
    payload = json.dumps({
        "title": "测试标的研报", "report": "## 技术面\n- 判断 [S1]",
        "citations": ["S1"], "limitations": ["测试口径"], "confidence": "low",
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": payload}}]})


def test_success_path_reports_persisted_true(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    _prepare(monkeypatch)

    body = run_stock_report(test_client, headers, **{"symbol": "000060"})
    assert body["ok"] is True
    assert body.get("persisted") is True, "成功落库时应显式告知 persisted=True"
    assert body.get("report_id")


def test_persist_failure_is_reported_not_silent(tmp_path, monkeypatch, caplog):
    """核心回归：落库失败必须以 persisted=False 如实暴露。"""

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    _prepare(monkeypatch)

    def _boom(record):
        raise RuntimeError("模拟磁盘满 / 锁超时 / schema 漂移")

    # CoreState.database 是实例属性，patch Database 类本身
    monkeypatch.setattr(db_module.Database, "insert_ai_research_report", _boom)

    with caplog.at_level(logging.ERROR):
        body = run_stock_report(test_client, headers, **{"symbol": "000060"})

    # 仍交付报告（不阻断），但必须诚实标注未保存
    assert body["ok"] is True
    assert body.get("persisted") is False, "落库失败时必须显式 persisted=False，不能靠缺 report_id 让前端猜"
    assert not body.get("report_id"), "未落库就不应有 report_id"
    # 报告正文本身仍完整交付，用户至少还能导出
    assert body.get("report")

    # 有留痕，支持可反查
    assert any("落库失败" in record.getMessage() for record in caplog.records)


def test_failed_persist_does_not_appear_in_history(tmp_path, monkeypatch):
    """未落库的报告不应出现在历史列表里（与 persisted=False 自洽）。"""

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    _prepare(monkeypatch)

    def _boom(record):
        raise RuntimeError("模拟写入失败")

    monkeypatch.setattr(db_module.Database, "insert_ai_research_report", _boom)
    test_client.post("/evidence/stock-research-report", json={"symbol": "000060"}, headers=headers)

    listed = test_client.get("/ai-research/reports", params={"kind": "stock"}, headers=headers).json()
    assert listed["items"] == []
