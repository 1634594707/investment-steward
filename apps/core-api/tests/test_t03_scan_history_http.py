"""Exercise HTTP serialization/authentication with isolated, shared in-memory storage."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import app as app_module
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import PluginInstallationState
from investment_steward_core.market_feed import FeedError
from investment_steward_core.storage import database as database_module
from sqlalchemy.pool import StaticPool


@pytest.fixture
def scan_http(monkeypatch):
    create_engine = database_module.create_engine
    monkeypatch.setattr(database_module, "create_engine", lambda *args, **kwargs:
                        create_engine(*args, **kwargs, poolclass=StaticPool))
    database = database_module.Database(Path(":memory:"))
    settings = CoreSettings(session_token="t03-http-test-token")
    core = SimpleNamespace(database=database, settings=settings,
                           local_user_id=UUID("00000000-0000-0000-0000-000000000003"))
    monkeypatch.setattr(app_module, "CoreState", lambda settings: core)
    monkeypatch.setattr(app_module.model_client, "set_model_call_recorder", lambda recorder: None)
    monkeypatch.setattr(app_module.feed_health, "set_feed_attempt_recorder", lambda recorder: None)
    monkeypatch.setattr(app_module, "resolve_store", lambda database: None)
    monkeypatch.setattr(app_module.jev_client, "build_shard_reviewer", lambda *args, **kwargs: None)
    monkeypatch.setattr(database, "get_plugin_installation",
                        lambda plugin_id: SimpleNamespace(state=PluginInstallationState.ENABLED))
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars = [{"timestamp": (start + timedelta(days=i)).isoformat(), "open": 10.1,
             "high": 10.4, "low": 10.0, "close": 10.2, "volume": 100000,
             "adjust": "qfq", "requested_adjust": "qfq", "source_series": "qfqday"}
            for i in range(40)]
    monkeypatch.setattr(app_module, "fetch_cn_kline", lambda *args, **kwargs: (bars, "tencent"))
    app = app_module.create_app(settings)
    try:
        with TestClient(app) as http:
            yield http, {"X-Core-Session-Token": settings.session_token}, database
    finally:
        database.close()


def test_daily_http_export_and_preview_preserve_batch_and_snapshot(scan_http):
    http, headers, database = scan_http
    request = {"sources": ["manual"], "symbols": ["600618"]}
    assert http.post("/tactics/scan", json=request).status_code == 401
    assert database.export_payload_tables()["scan_job_history"] == []
    response = http.post("/tactics/scan", json=request, headers=headers)
    assert response.status_code == 200
    exported = http.get("/export/all", headers=headers)
    assert exported.status_code == 200
    body = exported.json()
    history = body["scan_job_history"]
    observations = body["scan_observations"]
    assert len(history) == len(observations) == 1
    assert observations[0]["job_id"] == history[0]["job_id"]
    assert json.loads(json.loads(history[0]["payload"])["summary"]) == response.json()
    assert json.loads(observations[0]["payload"])["adjust"] == "qfq"
    preview = http.post("/import/preview", json=body, headers=headers)
    assert preview.status_code == 200
    tables = {row["table"]: row for row in preview.json()["tables"]}
    for name in ("scan_job_history", "scan_observations"):
        assert tables[name]["incoming"] == tables[name]["conflicts"] == 1
    assert http.get("/export/all", headers=headers).json()["scan_job_history"] == history


@pytest.mark.parametrize("offline", [False, True])
def test_market_http_worker_archives_success_and_fetch_failure(scan_http, monkeypatch, offline):
    http, headers, _database = scan_http
    monkeypatch.setattr(app_module, "fetch_cn_market_board", lambda *args, **kwargs:
                        [{"symbol": "600618", "name": "氯碱化工"}])
    if offline:
        def fail(*args, **kwargs):
            raise FeedError("offline")
        monkeypatch.setattr(app_module, "fetch_cn_kline", fail)
    # Keep the actual worker thread; wait for its termination before reading shared memory storage.
    thread_type = app_module.threading.Thread
    workers = []

    def track_thread(*args, **kwargs):
        worker = thread_type(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(app_module.threading, "Thread", track_thread)
    response = http.post("/tactics/scan-market", headers=headers,
                         json={"boards": ["turnover"], "concurrency": 1, "interval_secs": 0})
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive(), "The scan worker must terminate before archive verification"
    status = http.get(f"/tactics/scan-market/{job_id}", headers=headers)
    assert status.status_code == 200
    assert status.json()["state"] == "done"
    assert status.json()["summary"]["results"][0]["ok"] is (not offline)
    body = http.get("/export/all", headers=headers).json()
    assert body["scan_observations"][0]["job_id"] == job_id
    assert json.loads(body["scan_job_history"][0]["payload"])["state"] == "done"
    saved = json.loads(body["scan_observations"][0]["payload"])
    assert saved["result"]["ok"] is (not offline)
    assert saved["adjust"] == (None if offline else "qfq")


def test_cancel_http_preserves_cancelled_archive(scan_http):
    http, headers, database = scan_http
    database.create_scan_job("cancel-http", "fp", {"mode": "boards"})
    response = http.post("/tactics/scan-market/cancel-http/cancel", headers=headers)
    assert response.status_code == 200 and response.json()["ok"] is True
    database.finish_scan_job("cancel-http", "done", {})
    assert http.get("/tactics/scan-market/cancel-http", headers=headers).json()["state"] == "cancelled"
    archived = http.get("/export/all", headers=headers).json()["scan_job_history"]
    assert json.loads(archived[0]["payload"])["state"] == "cancelled"


def test_cancel_during_rate_wait_stops_next_fetch_and_keeps_completed_snapshot(scan_http, monkeypatch):
    http, headers, _database = scan_http
    monkeypatch.setattr(app_module, "fetch_cn_market_board", lambda *args, **kwargs: [
        {"symbol": "600618", "name": "氯碱化工"}, {"symbol": "000060", "name": "中金岭南"},
    ])
    fetch = app_module.fetch_cn_kline
    fetched = []

    def track_fetch(symbol, **kwargs):
        fetched.append(symbol)
        return fetch(symbol, **kwargs)

    monkeypatch.setattr(app_module, "fetch_cn_kline", track_fetch)
    waiting = Event()
    release = Event()
    pace_wait = app_module._RequestPacer.wait
    waits = 0

    def wait_between_fetches(pacer):
        nonlocal waits
        pace_wait(pacer)
        waits += 1
        if waits == 2:
            waiting.set()
            assert release.wait(timeout=5), "Cancellation test did not release the waiting worker"

    monkeypatch.setattr(app_module._RequestPacer, "wait", wait_between_fetches)
    thread_type = app_module.threading.Thread
    workers = []

    def track_thread(*args, **kwargs):
        worker = thread_type(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(app_module.threading, "Thread", track_thread)
    try:
        started = http.post("/tactics/scan-market", headers=headers,
                            json={"boards": ["turnover"], "concurrency": 1, "interval_secs": 0.01})
        assert started.status_code == 200
        job_id = started.json()["job_id"]
        assert waiting.wait(timeout=5), "Worker did not reach the second request's rate wait"
        cancelled = http.post(f"/tactics/scan-market/{job_id}/cancel", headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()["ok"] is True
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive()
    assert fetched == ["600618"], "A cancellation acknowledged during rate waiting must stop the next fetch"
    status = http.get(f"/tactics/scan-market/{job_id}", headers=headers).json()
    assert status["state"] == "cancelled" and status["done"] == 1 and status["total"] == 2
    exported = http.get("/export/all", headers=headers).json()
    assert len(exported["scan_observations"]) == 1
    saved = exported["scan_observations"][0]
    assert saved["job_id"] == job_id
    assert json.loads(saved["payload"])["result"]["symbol"] == "600618"
    archived = json.loads(exported["scan_job_history"][0]["payload"])
    assert archived["state"] == "cancelled" and archived["done"] == 1


def test_restore_refuses_missing_backup_before_changing_scan_history(scan_http, monkeypatch):
    http, headers, database = scan_http
    database.insert_scan_observation(None, {"marker": "original snapshot"})
    original = http.get("/export/all", headers=headers).json()["scan_observations"]
    incoming = [dict(original[0], payload=json.dumps({"marker": "incoming replacement"}))]
    # rotate_backup reports ordinary filesystem failures with None, rather than raising.
    monkeypatch.setattr(database_module, "rotate_backup", lambda *args, **kwargs: None)
    response = http.post("/import/apply", headers=headers, json={
        "data": {"scan_observations": incoming}, "tables": ["scan_observations"],
        "mode": "replace", "confirm": True,
    })
    assert response.status_code == 503, "A failed backup must stop replacement of irreplaceable history"
    assert http.get("/export/all", headers=headers).json()["scan_observations"] == original


@pytest.mark.parametrize("mode,remaining", [("merge", 1), ("replace", 0)])
def test_empty_export_restores_selected_scan_table_without_clearing_omitted_table(scan_http, monkeypatch, mode, remaining):
    http, headers, database = scan_http
    database.create_scan_job("empty-restore", "fp", {})
    database.finish_scan_job("empty-restore", "done", {"results": []})
    database.insert_scan_observation("empty-restore", {"result": {"ok": True}, "bars": []})
    original_history = database.export_payload_tables()["scan_job_history"]
    monkeypatch.setattr(database_module, "rotate_backup", lambda *args, **kwargs: Path("memory-test-backup"))
    response = http.post("/import/apply", headers=headers, json={
        "data": {"scan_observations": []}, "tables": ["scan_observations", "scan_job_history"],
        "mode": mode, "confirm": True,
    })
    assert response.status_code == 200, response.text
    restored = http.get("/export/all", headers=headers).json()
    assert len(restored["scan_observations"]) == remaining, "Replace must return to an explicitly empty snapshot; merge retains existing observations"
    assert restored["scan_job_history"] == original_history, "An omitted table is not an instruction to erase it"
