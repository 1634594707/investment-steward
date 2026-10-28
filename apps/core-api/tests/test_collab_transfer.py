"""协同阶段属于用户研究产出，备份/导出恢复必须保留付费结果。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from investment_steward_core import collab, model_client
from investment_steward_core.storage.database import rotate_backup
from test_collab_pipeline import _stage_reply_factory, _start_run
from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile


def test_maintenance_in_another_process_blocks_collab_and_releases_on_exit(tmp_path, monkeypatch):
    import subprocess
    import sys

    client, headers = _file_client(tmp_path, monkeypatch)
    script = """
import sys
from pathlib import Path
from investment_steward_core.collab import run_guard
from investment_steward_core.storage.database import Database
with run_guard(Database(Path(sys.argv[1])), 'storage-maintenance'):
    print('locked', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(client.app.state.core.database.path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        assert client.get("/evidence/collab-state", headers=headers).status_code == 409
        assert client.post("/evidence/collab-run", json={"symbol": "600001"}, headers=headers).status_code == 409
        assert client.post("/evidence/collab-next", json={"run_id": "missing-id"}, headers=headers).status_code == 409
    finally:
        process.kill()
        process.communicate(timeout=10)
    assert client.get("/evidence/collab-state", headers=headers).status_code == 200


def test_inflight_stage_blocks_restore_and_import_but_allows_cancel(tmp_path, monkeypatch):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    run = _start_run(client, headers)
    db = client.app.state.core.database
    backup = rotate_backup(db.path)
    entered, release = Event(), Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return _stage_reply_factory()()

    monkeypatch.setattr(model_client, "_post_json", blocked)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(client.post, "/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers)
        try:
            assert entered.wait(5)
            restored = client.post(f"/storage/backups/{backup.name}/restore", headers=headers)
            assert restored.status_code == 409, restored.text
            imported = client.post("/import/apply", headers=headers,
                                   json={"data": {"collab_runs": []}, "tables": ["collab_runs"], "mode": "replace", "confirm": True})
            assert imported.status_code == 409, imported.text
            assert client.post("/evidence/collab-cancel", json={"run_id": run["run_id"]}, headers=headers).status_code == 200
        finally:
            release.set()
        assert future.result(timeout=5).json()["executed_status"] == "cancelled"


def test_restore_older_schema_migrates_before_serving_tasks(tmp_path, monkeypatch):
    import sqlite3

    from investment_steward_core.storage.database import SCHEMA_VERSION

    client, headers = _file_client(tmp_path, monkeypatch)
    backup = rotate_backup(client.app.state.core.database.path)
    with sqlite3.connect(backup) as raw:
        raw.execute("DROP TABLE collab_runs")
        raw.execute("PRAGMA user_version=39")
    restored = client.post(f"/storage/backups/{backup.name}/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    with sqlite3.connect(client.app.state.core.database.path) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert client.get("/evidence/collab-state", headers=headers).status_code == 200


def test_backup_restore_uses_saved_progress_instead_of_live_cache(tmp_path, monkeypatch):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(model_client, "_post_json", _stage_reply_factory())
    run = _start_run(client, headers)
    body = {"run_id": run["run_id"]}
    client.post("/evidence/collab-next", json=body, headers=headers)
    db = client.app.state.core.database
    snapshot = db.get_collab_run(run["run_id"])
    backup = rotate_backup(db.path)
    client.post("/evidence/collab-next", json=body, headers=headers)
    assert collab.get_run(run["run_id"], db)["next_index"] == 2
    result = client.post(f"/storage/backups/{backup.name}/restore", headers=headers)
    assert result.status_code == 200, result.text
    view = client.get("/evidence/collab-state", params=body, headers=headers).json()["run"]
    assert view["next_index"] == 1, "恢复不能显示内存里尚未备份的第二阶段"
    assert view["stages"] == snapshot["stages"]
    assert db.get_collab_run(run["run_id"])["input_hash"] == snapshot["input_hash"]
    replies = _stage_reply_factory()
    replies()  # 下一次模拟回复从初稿开始；已保存核对不得重调。
    monkeypatch.setattr(model_client, "_post_json", replies)
    resumed = client.post("/evidence/collab-next", json=body, headers=headers).json()
    assert resumed["executed_stage"] == "draft" and resumed["executed_status"] == "done"


@pytest.mark.parametrize("cancelled", [False, True])
def test_json_export_import_preserves_task_without_device_path(tmp_path, monkeypatch, cancelled):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(model_client, "_post_json", _stage_reply_factory())
    run = _start_run(client, headers)
    client.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers)
    if cancelled:
        client.post("/evidence/collab-cancel", json={"run_id": run["run_id"]}, headers=headers)
    original = client.app.state.core.database.get_collab_run(run["run_id"])
    exported = client.get("/export/all", headers=headers).json()
    assert len(exported["collab_runs"]) == 1
    assert "_database_path" not in exported["collab_runs"][0]["payload"]
    target, target_headers = _file_client(tmp_path / "target", monkeypatch)
    response = target.post("/import/apply", headers=target_headers,
                           json={"data": exported, "tables": ["collab_runs"], "mode": "merge", "confirm": True})
    assert response.status_code == 200, response.text
    saved = target.app.state.core.database.get_collab_run(run["run_id"])
    assert bool(saved.get("cancelled_at")) == cancelled
    assert saved["input_hash"] == original["input_hash"]
    assert saved["stages"] == original["stages"]
    assert saved["evidence"] == original["evidence"]
    assert target.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=target_headers).status_code == 409
