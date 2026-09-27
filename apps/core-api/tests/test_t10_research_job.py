"""T10（用户视角路线图 2026-09-26）：个股深研改后台任务。

改造前 POST /evidence/stock-research-report 是一个**同步**请求：单份最坏 6 次模型调用
× 2 次重试 × 120s ≈ 24 分钟，全程占住一个请求线程。用户切页面、崩溃、重启，已付费的
调用凭空蒸发，服务端没有任何可重挂的凭据；界面只能提示「请勿关闭窗口」。

本文件锁定任务语义：建任务即返回、指纹去重、进度可查、取消、终态报告形状不变，
以及**业务 stage 不被任务生命周期 stage 覆盖**这一易踩的坑。
"""

from __future__ import annotations

import json
import threading
import time

from conftest import await_stock_report_job, run_stock_report, start_stock_report_job
from investment_steward_core import model_client as mc


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


def _mock_everything(monkeypatch, report_payload: dict | None = None) -> None:
    import investment_steward_core.api.app as app_module
    from investment_steward_core.valuation_evidence import ValuationSnapshot

    bars = [
        {"date": f"2026-07-{i + 10:02d}" if i < 21 else f"2026-08-{i - 20:02d}",
         "open": 6.0, "close": 6.0 + i * 0.02, "high": 6.1, "low": 5.9, "volume": 1_000_000}
        for i in range(40)
    ]
    monkeypatch.setattr(app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (bars, "test"))
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
    payload = json.dumps(
        report_payload
        or {
            "title": "测试标的研报", "report": "## 技术面\n- 判断 [S1]",
            "citations": ["S1"], "limitations": ["测试口径"], "confidence": "low",
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": payload}}]})


def test_post_returns_job_id_immediately(tmp_path, monkeypatch):
    """核心行为：POST 不再同步跑完，而是立即返回 job_id。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    _mock_everything(monkeypatch)

    started_at = time.monotonic()
    started = start_stock_report_job(test_client, headers, symbol="000060")
    elapsed = time.monotonic() - started_at

    assert started["ok"] is True
    assert started["job_id"]
    assert started["state"] == "running"
    # 建任务必须是毫秒级返回，而不是等模型跑完（模型被 mock 时是快的，但这里断言的是
    # 「返回了 job_id 且任务仍在 running」这一形态本身）。
    assert elapsed < 5.0

    final = await_stock_report_job(test_client, headers, started["job_id"])
    assert final["state"] == "done"
    assert final["ok"] is True
    assert final["symbol"] == "000060"


def test_final_payload_keeps_report_shape(tmp_path, monkeypatch):
    """终态响应体与改造前的同步 POST 同形状（前端只多一次轮询）。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    _mock_everything(monkeypatch)

    body = run_stock_report(test_client, headers, symbol="000060")
    for key in ("title", "report", "citations", "model", "source_keys", "source_errors", "generated_at"):
        assert key in body, f"终态报告缺字段 {key}"
    assert body["ok"] is True
    # 任务字段是额外附加的，不影响报告体
    assert body["job_id"] and body["state"] == "done"


def test_business_stage_not_overwritten_by_job_stage(tmp_path, monkeypatch):
    """回归：业务失败阶段（parse）不得被任务生命周期阶段（saving）覆盖。

    两者同名极易踩坑——第一次实现时 `stage` 被 `saving` 盖掉，前端拿不到真实失败原因。
    """
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    _mock_everything(monkeypatch)
    # 返回无法解析的模型输出 → 业务失败 stage=parse
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": "不是 JSON"}}]})

    body = run_stock_report(test_client, headers, symbol="000060")
    assert body["ok"] is False
    assert body["state"] == "error"
    assert body["stage"] == "parse", f"业务阶段被任务阶段覆盖了：{body.get('stage')}"


def test_same_request_does_not_start_two_jobs(tmp_path, monkeypatch):
    """同输入指纹的运行中任务直接命中，不重复烧模型。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    _mock_everything(monkeypatch)

    gate = threading.Event()

    def _slow(*_a, **_k):
        gate.wait(timeout=5)
        return {"choices": [{"message": {"content": json.dumps({
            "title": "T", "report": "## 技术面\n- 判断 [S1]", "citations": ["S1"],
            "limitations": [], "confidence": "low"}, ensure_ascii=False)}}]}

    monkeypatch.setattr(mc, "_post_json", _slow)
    first = start_stock_report_job(test_client, headers, symbol="000060")
    second = start_stock_report_job(test_client, headers, symbol="000060")
    gate.set()

    assert second["job_id"] == first["job_id"], "同输入应复用同一个 job"
    assert second.get("reused") is True
    await_stock_report_job(test_client, headers, first["job_id"])


def test_cancel_marks_job_cancelled(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    _mock_everything(monkeypatch)

    gate = threading.Event()
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: (gate.wait(timeout=5), {"choices": [{"message": {"content": "{}"}}]})[1],
    )
    started = start_stock_report_job(test_client, headers, symbol="000060")
    cancelled = test_client.post(
        f"/evidence/stock-research-report/{started['job_id']}/cancel", headers=headers
    ).json()
    gate.set()
    assert cancelled["ok"] is True

    # 取消后不再落库新报告
    time.sleep(0.3)
    listed = test_client.get("/ai-research/reports", params={"kind": "stock"}, headers=headers).json()
    assert listed["items"] == []


def test_model_access_disabled_does_not_create_job(tmp_path, monkeypatch):
    """出网总闸关闭：不建任务（否则留下注定失败的任务行）。"""
    import investment_steward_core.api.app as api_app
    from fastapi.testclient import TestClient
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    settings = CoreSettings(session_token="t", data_dir=tmp_path, model_access_enabled=False)
    test_client = TestClient(create_app(settings))
    headers = {"X-Core-Session-Token": "t"}
    _mock_everything(monkeypatch)

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "000060"}, headers=headers
    ).json()
    assert body["ok"] is False
    assert body["stage"] == "model_access_disabled"
    assert "job_id" not in body


def test_missing_active_profile_rejected_before_creating_job(tmp_path, monkeypatch):
    """无「使用中」方案：仍同步 409，不建任务。"""
    import investment_steward_core.api.app as api_app
    from fastapi.testclient import TestClient
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    test_client = TestClient(create_app(CoreSettings(session_token="t", data_dir=tmp_path)))
    headers = {"X-Core-Session-Token": "t"}
    test_client.post("/plugins/official.cn-market-data/install", headers=headers)
    _mock_everything(monkeypatch)

    response = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "000060"}, headers=headers
    )
    assert response.status_code == 409
    assert "使用中" in response.json()["detail"]


def test_unknown_job_id_is_404(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    response = test_client.get("/evidence/stock-research-report/does-not-exist", headers=headers)
    assert response.status_code == 404
