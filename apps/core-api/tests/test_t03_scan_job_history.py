"""Terminal scan records must survive cache pruning and preserve cancellation."""
import json
from pathlib import Path

import pytest

from investment_steward_core.storage.database import Database, SCAN_JOB_STALE_REASON


@pytest.fixture
def database():
    database = Database(Path(":memory:"))
    yield database
    database.close()


def _history(database, job_id):
    rows = database.export_payload_tables()["scan_job_history"]
    return json.loads(next(row["payload"] for row in rows if row["job_id"] == job_id))


@pytest.mark.parametrize("state,summary", [("done", {"results": [], "scanned": 0}),
                                          ("error", {"detail": "board offline"})])
def test_empty_and_failed_jobs_remain_traceable_after_pruning(database, state, summary):
    request = {"mode": "boards", "boards": ["turnover"]}
    database.create_scan_job("original", "fp", request)
    database.finish_scan_job("original", state, summary)
    with database._connection() as connection:
        connection.exec_driver_sql("UPDATE scan_jobs SET updated_at = ? WHERE job_id = ?",
                                   ("2026-01-01T00:00:00+00:00", "original"))
    for index in range(22):
        job_id = f"later-{index}"
        database.create_scan_job(job_id, job_id, {})
        database.finish_scan_job(job_id, "done", {})
    assert database.get_scan_job("original") is None
    archived = _history(database, "original")
    assert archived["state"] == state
    assert json.loads(archived["request_body"]) == request
    assert json.loads(archived["summary"]) == summary


def test_cancellation_is_archived_before_worker_returns_and_cannot_become_done(database):
    database.create_scan_job("cancel", "fp", {})
    database.update_scan_job_progress("cancel", 2, 5)
    assert database.cancel_scan_job("cancel")
    original = _history(database, "cancel")
    database.finish_scan_job("cancel", "done", {"results": []})
    assert _history(database, "cancel") == original
    assert original["state"] == "cancelled"
    assert original["done"] == 2 and original["total"] == 5
    assert database.get_scan_job("cancel")["state"] == "cancelled"


@pytest.mark.parametrize("transition", ["read", "create"])
def test_stale_jobs_are_archived_at_both_existing_transition_points(database, transition):
    database.create_scan_job("stale", "fp", {})
    with database._connection() as connection:
        connection.exec_driver_sql("UPDATE scan_jobs SET updated_at = ? WHERE job_id = ?",
                                   ("2026-01-01T00:00:00+00:00", "stale"))
    if transition == "read":
        database.get_scan_job("stale")
    else:
        database.create_scan_job("new", "new", {})
    archived = _history(database, "stale")
    assert archived["state"] == "error"
    assert archived["error"] == SCAN_JOB_STALE_REASON


def test_existing_terminal_jobs_backfill_without_reconstructing_missing_data(database):
    database.create_scan_job("old", "fp", {})
    database.finish_scan_job("old", "done", {"results": []})
    with database._connection() as connection:
        connection.exec_driver_sql("DROP TABLE scan_job_history")
        connection.exec_driver_sql("PRAGMA user_version = 35")
    database._migrate()
    assert _history(database, "old")["state"] == "done"
    database._migrate()
    rows = database.export_payload_tables()["scan_job_history"]
    restored = Database(Path(":memory:"))
    try:
        outcome = restored.import_table_apply("scan_job_history", rows, "merge")
        assert outcome["inserted"] == 1 and outcome["skipped"] == 0
        assert restored.export_payload_tables()["scan_job_history"] == rows
    finally:
        restored.close()


def test_archive_failure_rolls_back_terminal_transition(database, monkeypatch):
    database.create_scan_job("atomic", "fp", {})
    archive = database._archive_scan_jobs

    def fail_after_insert(connection):
        archive(connection)
        raise RuntimeError("archive write failed")

    with monkeypatch.context() as patch:
        patch.setattr(database, "_archive_scan_jobs", fail_after_insert)
        with pytest.raises(RuntimeError, match="archive write failed"):
            database.finish_scan_job("atomic", "done", {"results": []})
    assert database.get_scan_job("atomic")["state"] == "running", (
        "Completion must not commit when the required historical record cannot be committed"
    )
    assert database.export_payload_tables()["scan_job_history"] == []
