"""Historical samples must survive cancellation/pruning without rewriting raw inputs."""
import json
from pathlib import Path

import pytest

from investment_steward_core.storage.database import Database


def test_raw_observations_survive_job_cancellation_and_pruning():
    database = Database(Path(":memory:"))
    try:
        database.create_scan_job("original", "fp", {"mode": "boards"})
        payload = {"result": {"symbol": "600618", "ok": True, "eligible": False,
                              "primary_signal_state": {"state": "invalidated"}},
                   "bars": [{"timestamp": "2026-10-23T00:00:00", "close": 9.85}],
                   "adjust": None}
        first = database.insert_scan_observation("original", payload)
        failed = {"result": {"symbol": "600618", "ok": False, "error": "feed offline"},
                  "bars": None, "adjust": None}
        second = database.insert_scan_observation("original", failed)
        # Subsequent annotation must not mutate the historical input/result.
        payload["result"]["eligible"] = True
        database.cancel_scan_job("original")
        database.finish_scan_job("original", "cancelled", None)
        with database._connection() as connection:
            connection.exec_driver_sql(
                "UPDATE scan_jobs SET updated_at = ? WHERE job_id = ?",
                ("2026-01-01T00:00:00+00:00", "original"),
            )
        for index in range(22):
            job_id = f"later-{index}"
            database.create_scan_job(job_id, job_id, {})
            database.finish_scan_job(job_id, "done", {})
        assert database.get_scan_job("original") is None
        with database._connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT snapshot_id, payload FROM scan_observations"
            ).all()
        stored = {row[0]: json.loads(row[1]) for row in rows}
        assert len(stored) == 2, "Failed and invalidated observations both belong in the archive"
        assert stored[first]["result"]["eligible"] is False
        assert stored[first]["result"]["primary_signal_state"]["state"] == "invalidated"
        assert stored[first]["bars"] == payload["bars"]
        assert stored[first]["adjust"] is None, "Unknown adjustment must stay unknown"
        assert stored[second] == failed
    finally:
        database.close()


def test_repeat_observations_are_preserved_for_later_explicit_sample_selection():
    database = Database(Path(":memory:"))
    try:
        payload = {"result": {"symbol": "600618", "ok": True}, "bars": [], "adjust": None}
        first = database.insert_scan_observation(None, payload)
        second = database.insert_scan_observation(None, payload)
        assert first != second, "Repeated observations must not overwrite earlier scan snapshots"
    finally:
        database.close()


def test_snapshots_round_trip_through_existing_export_and_restore():
    source = Database(Path(":memory:"))
    restored = Database(Path(":memory:"))
    try:
        source.insert_scan_observation(None, {
            "result": {"symbol": "600618", "ok": False, "error": "offline"},
            "bars": None, "adjust": None,
        })
        exported = source.export_payload_tables()
        assert "scan_observations" in exported, "Full backup must include irreplaceable scan history"
        rows = exported["scan_observations"]
        preview = restored.import_table_preview("scan_observations", rows)
        assert preview["incoming"] == 1 and preview["existing"] == 0
        applied = restored.import_table_apply("scan_observations", rows, "merge")
        assert applied["inserted"] == 1 and applied["skipped"] == 0
        assert restored.export_payload_tables()["scan_observations"] == rows
        repeated = restored.import_table_apply("scan_observations", rows, "merge")
        assert repeated["inserted"] == 0 and repeated["skipped"] == 1
        replaced = restored.import_table_apply("scan_observations", rows, "replace")
        assert replaced["inserted"] == 1 and replaced["skipped"] == 0
        assert restored.export_payload_tables()["scan_observations"] == rows
    finally:
        source.close()
        restored.close()


@pytest.mark.parametrize("feed_state", ["ok", "adjusted", "offline", "disabled", "empty", "crash", "partial_crash"])
def test_daily_scan_archives_original_input_or_fetch_failure(monkeypatch, feed_state):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from fastapi.routing import APIRoute

    from investment_steward_core.api import app as app_module
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.domain import PluginInstallationState
    from investment_steward_core.market_feed import FeedError

    database = Database(Path(":memory:"))
    core = SimpleNamespace(database=database)
    monkeypatch.setattr(app_module, "CoreState", lambda settings: core)
    monkeypatch.setattr(app_module.model_client, "set_model_call_recorder", lambda recorder: None)
    monkeypatch.setattr(app_module.feed_health, "set_feed_attempt_recorder", lambda recorder: None)
    monkeypatch.setattr(app_module, "resolve_store", lambda database: None)
    monkeypatch.setattr(app_module.jev_client, "build_shard_reviewer", lambda *args, **kwargs: None)
    monkeypatch.setattr(database, "get_plugin_installation", lambda plugin_id:
                        None if feed_state == "disabled" else SimpleNamespace(state=PluginInstallationState.ENABLED))
    bars = [{"timestamp": (datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=i)).isoformat(),
             "open": 10 + i / 100, "high": 11 + i / 100, "low": 9 + i / 100,
             "close": 10.1 + i / 100, "volume": 100000} for i in range(40)]
    if feed_state == "adjusted":
        for bar in bars:
            bar.update(adjust="qfq", requested_adjust="qfq", source_series="qfqday")

    def fetch(symbol, limit, period):
        if feed_state == "offline":
            raise FeedError("offline")
        if feed_state == "crash" or (feed_state == "partial_crash" and symbol == "000060"):
            raise RuntimeError("unexpected feed failure")
        return bars, "unit-test"

    monkeypatch.setattr(app_module, "fetch_cn_kline", fetch)
    try:
        app = app_module.create_app(CoreSettings(session_token="t03-test-token"))
        endpoint = next(route.endpoint for route in app.routes
                        if isinstance(route, APIRoute) and route.path == "/tactics/scan")
        symbols = [] if feed_state == "empty" else ["600618"]
        if feed_state == "partial_crash":
            symbols.append("000060")
        request = app_module.TacticsScanRequest(sources=["manual"], symbols=symbols)
        if feed_state in ("crash", "partial_crash"):
            with pytest.raises(RuntimeError, match="unexpected feed failure"):
                endpoint(request, core)
            result = None
        else:
            result = endpoint(request, core)
        exported = database.export_payload_tables()
        history = exported["scan_job_history"]
        assert len(history) == 1, "Empty and interrupted daily batches also need traceable terminal records"
        archived = json.loads(history[0]["payload"])
        assert json.loads(archived["request_body"]) == request.model_dump(mode="json")
        assert archived["state"] == ("error" if feed_state in ("crash", "partial_crash") else "done")
        if result is not None:
            assert json.loads(archived["summary"]) == result
            assert archived["done"] == archived["total"] == result["scanned"]
        else:
            assert "unexpected feed failure" in json.loads(archived["summary"])["detail"]
        if feed_state == "partial_crash":
            assert archived["done"] == 1 and archived["total"] == 2
            assert len(exported["scan_observations"]) == 1, "Completed rows must survive a later scan failure"
            saved_row = exported["scan_observations"][0]
            assert saved_row["job_id"] == history[0]["job_id"]
            assert json.loads(saved_row["payload"])["result"]["symbol"] == "600618"
            return
        if feed_state in ("empty", "crash"):
            assert exported["scan_observations"] == [], "No completed per-symbol result may be invented"
            return
        assert len(exported["scan_observations"]) == 1
        assert exported["scan_observations"][0]["job_id"] == history[0]["job_id"]
        with database._connection() as connection:
            rows = connection.exec_driver_sql("SELECT payload FROM scan_observations").scalars().all()
        assert len(rows) == 1, "Daily scans need history for both successful and failed fetches"
        saved = json.loads(rows[0])
        assert saved["request_body"] == request.model_dump(mode="json")
        assert saved["result"]["symbol"] == "600618"
        successful = feed_state in ("ok", "adjusted")
        assert saved["result"]["ok"] == result["results"][0]["ok"] == successful
        assert saved["bars"] == (bars if successful else None)
        assert saved["adjust"] == ("qfq" if feed_state == "adjusted" else None)
        if feed_state == "ok":
            endpoint(request, core)
            repeated = database.export_payload_tables()
            run_ids = {row["job_id"] for row in repeated["scan_job_history"]}
            assert len(run_ids) == 2, "Repeated scans must not merge distinct historical batches"
            assert {row["job_id"] for row in repeated["scan_observations"]} == run_ids
    finally:
        database.close()
