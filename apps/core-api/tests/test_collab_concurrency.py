"""不同进程共享数据目录时，恢复读取不能覆盖正在执行的任务。"""
import subprocess
import sys
from types import SimpleNamespace

import pytest
from investment_steward_core import collab
from investment_steward_core.storage.database import Database


def test_live_process_blocks_recovery_and_crash_releases_guard(tmp_path):
    db = Database(tmp_path / "runs.sqlite3")
    run = collab.create_run("600001", "q", "tech", {"S1": "source"}, {}, None, "m")
    run["stages"][0]["status"] = "running"
    db.save_collab_run(run)
    script = """
import sys
from pathlib import Path
from investment_steward_core.collab import run_guard
from investment_steward_core.storage.database import Database
with run_guard(Database(Path(sys.argv[1])), sys.argv[2]):
    print('locked', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(db.path), run["run_id"]],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        collab._runs.clear()
        with pytest.raises(collab.CollabBusyError):
            collab.get_run(run["run_id"], db)
        with pytest.raises(collab.CollabBusyError):
            collab.execute_next(SimpleNamespace(database=db), run, None, None)
        assert db.get_collab_run(run["run_id"])["stages"][0]["status"] == "running"
    finally:
        process.kill()
        process.communicate(timeout=10)
    restored = collab.get_run(run["run_id"], db)
    assert restored["stages"][0]["status"] == "failed"


def test_stale_snapshot_cannot_overwrite_another_completed_stage(tmp_path):
    from copy import deepcopy

    db = Database(tmp_path / "runs.sqlite3")
    stale = collab.create_run("600001", "q", "tech", {"S1": "source"}, {}, None, "m")
    advanced = deepcopy(stale)
    advanced["next_index"] = 1
    advanced["stages"][0].update(status="done", result={"observations": ["original"]})
    db.save_collab_run(advanced)
    with pytest.raises(collab.CollabBusyError):
        collab.execute_next(SimpleNamespace(database=db), stale, None, None)
    assert db.get_collab_run(stale["run_id"])["stages"][0]["result"] == {"observations": ["original"]}
    assert collab.get_run(stale["run_id"], db)["next_index"] == 1
