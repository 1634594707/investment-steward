"""File-backed restore rehearsal for scan archives; uses pytest temporary storage only."""
import sqlite3
from pathlib import Path

import pytest

from test_desktop_e02_import import _client


TABLE_KEYS = {"scan_job_history": "job_id", "scan_observations": "snapshot_id"}


def _rows(tables):
    return {table: {row[key]: row for row in tables[table]} for table, key in TABLE_KEYS.items()}


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_scan_archives_file_restore_preserves_payloads_and_real_backup(tmp_path, mode):
    http, headers = _client(tmp_path)
    database = http.app.state.core.database
    try:
        for job_id, state in [("complete", "done"), ("cancelled", "cancelled")]:
            database.create_scan_job(job_id, job_id, {"symbols": ["600618"]})
            database.insert_scan_observation(job_id, {
                "result": {"symbol": "600618", "ok": True, "eligible": False},
                "bars": [{"timestamp": "2026-10-23", "close": 10.2}], "adjust": None,
            })
            database.update_scan_job_progress(job_id, 1, 2)
            if state == "cancelled":
                assert database.cancel_scan_job(job_id)
            else:
                database.finish_scan_job(job_id, state, {"scanned": 1})
        exported = http.get("/export/all", headers=headers).json()
        expected = _rows(exported)
        database.create_scan_job("extra", "extra", {})
        database.finish_scan_job("extra", "error", {"detail": "offline"})
        extra_snapshot = database.insert_scan_observation("extra", {"result": {"ok": False}, "bars": []})
        lost_snapshot = next(iter(expected["scan_observations"]))
        with sqlite3.connect(database.path) as raw:
            raw.execute("DELETE FROM scan_job_history WHERE job_id = ?", ("complete",))
            raw.execute("DELETE FROM scan_observations WHERE snapshot_id = ?", (lost_snapshot,))
        before = _rows(http.get("/export/all", headers=headers).json())
        response = http.post("/import/apply", headers=headers, json={
            "data": exported, "tables": list(TABLE_KEYS), "mode": mode, "confirm": True,
        })
        assert response.status_code == 200, response.text
        result = response.json()
        backup = Path(result["backup_path"])
        assert backup.is_file(), "The restore must create an actual file backup"
        with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as raw:
            raw.row_factory = sqlite3.Row
            assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            backed_up = {table: [dict(row) for row in raw.execute(f"SELECT * FROM {table}")]
                         for table in TABLE_KEYS}
        assert _rows(backed_up) == before, "Backup must contain the state before restoration"
        restored = _rows(http.get("/export/all", headers=headers).json())
        for table, rows in expected.items():
            assert all(restored[table][key] == row for key, row in rows.items()), "Original payloads, batch links and timestamps must survive"
        if mode == "replace":
            assert restored == expected, "Replace must remove observations outside the exported snapshot"
        else:
            assert "extra" in restored["scan_job_history"]
            assert extra_snapshot in restored["scan_observations"]
        assert {row["job_id"] for row in restored["scan_observations"].values()} <= set(restored["scan_job_history"])
    finally:
        database.close()
