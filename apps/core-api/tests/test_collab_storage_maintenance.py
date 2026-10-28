"""整库恢复与在途协同互斥，锁必须覆盖模型返回后的归档。"""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from investment_steward_core import collab, model_client
from investment_steward_core.storage import backup_restore
from investment_steward_core.storage.database import rotate_backup
from test_collab_pipeline import _stage_reply_factory, _start_run
from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile


def prepared(tmp_path, monkeypatch):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    reply = _stage_reply_factory()
    monkeypatch.setattr(model_client, "_post_json", reply)
    run = _start_run(client, headers)
    db = client.app.state.core.database
    backup = rotate_backup(db.path)
    assert backup is not None
    return client, headers, run, db, backup, reply


@pytest.mark.parametrize("blocking", ["model", "archive"])
def test_inflight_collab_rejects_restore_and_import_until_archive_finishes(tmp_path, monkeypatch, blocking):
    client, headers, run, db, backup, reply = prepared(tmp_path, monkeypatch)
    body = {"run_id": run["run_id"]}
    for _ in range(3 if blocking == "archive" else 0):
        assert client.post("/evidence/collab-next", headers=headers, json=body).status_code == 200
    entered, release = Event(), Event()
    insert = db.insert_ai_research_report

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), "测试必须释放在途阶段"
        return (reply if blocking == "model" else insert)(*args, **kwargs)

    if blocking == "model":
        monkeypatch.setattr(model_client, "_post_json", blocked)
    else:
        monkeypatch.setattr(db, "insert_ai_research_report", blocked)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(client.post, "/evidence/collab-next", headers=headers, json=body)
        try:
            assert entered.wait(5)
            before = db.get_collab_run(run["run_id"])
            restored = client.post(f"/storage/backups/{backup.name}/restore", headers=headers)
            assert restored.status_code == 409, restored.text
            imported = client.post("/import/apply", headers=headers,
                                   json={"confirm": True, "mode": "replace", "data": {"theses": []}, "tables": ["theses"]})
            assert imported.status_code == 409, imported.text
            assert db.get_collab_run(run["run_id"]) == before
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.status_code == 200, result.text
    saved = db.get_collab_run(run["run_id"])
    assert saved["next_index"] == (4 if blocking == "archive" else 1)
    assert saved["stages"][saved["next_index"] - 1]["status"] == "done"
    if blocking == "archive":
        assert db.get_ai_research_report(run["run_id"]) is not None
    # 已释放维护锁，合法但不存在的备份应进入业务404，而不是残留409。
    assert client.post("/storage/backups/steward-20000101-000000.sqlite3/restore", headers=headers).status_code == 404


def test_restore_blocks_new_collab_work_and_releases_after_success(tmp_path, monkeypatch):
    client, headers, run, db, backup, _reply = prepared(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    copy = backup_restore._copy

    def blocked(source, target):
        if target.resolve() == db.path.resolve():
            entered.set()
            assert release.wait(5), "测试必须释放恢复写入"
        return copy(source, target)

    monkeypatch.setattr(backup_restore, "_copy", blocked)
    before = db.get_collab_run(run["run_id"])
    with ThreadPoolExecutor() as pool:
        future = pool.submit(client.post, f"/storage/backups/{backup.name}/restore", headers=headers)
        try:
            assert entered.wait(5)
            for path, body in [("/evidence/collab-next", {"run_id": run["run_id"]}),
                               ("/evidence/collab-run", {"symbol": "600001", "question": "q"})]:
                rejected = client.post(path, headers=headers, json=body)
                assert rejected.status_code == 409, rejected.text
            assert db.get_collab_run(run["run_id"]) == before
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result.status_code == 200, result.text
    collab._runs.clear()
    resumed = client.post("/evidence/collab-next", headers=headers, json={"run_id": run["run_id"]})
    assert resumed.status_code == 200, resumed.text
    assert db.get_collab_run(run["run_id"])["next_index"] == 1


def test_separate_process_maintenance_guard_blocks_http_then_releases(tmp_path, monkeypatch):
    """另一Core进程持锁也必须阻止本进程推进，正常退出后可继续。"""
    client, headers, run, db, backup, _reply = prepared(tmp_path, monkeypatch)
    script = """
import sys
from pathlib import Path
from types import SimpleNamespace
from investment_steward_core.collab import run_guard
with run_guard(SimpleNamespace(path=Path(sys.argv[1])), "storage-maintenance"):
 print("locked", flush=True)
 sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-u", "-c", script, str(db.path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        before = db.get_collab_run(run["run_id"])
        assert client.post(f"/storage/backups/{backup.name}/restore", headers=headers).status_code == 409
        assert client.post("/evidence/collab-next", headers=headers,
                           json={"run_id": run["run_id"]}).status_code == 409
        assert db.get_collab_run(run["run_id"]) == before
    finally:
        process.communicate(input="", timeout=5)
    assert process.returncode == 0
    resumed = client.post("/evidence/collab-next", headers=headers, json={"run_id": run["run_id"]})
    assert resumed.status_code == 200, resumed.text
    assert db.get_collab_run(run["run_id"])["next_index"] == 1
