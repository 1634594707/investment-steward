"""恢复失败不能把当前用户数据换成不兼容或部分迁移的库。"""
import sqlite3
from hashlib import sha256

import pytest
from investment_steward_core.storage.database import SCHEMA_VERSION, Database, rotate_backup
from test_desktop_e03_backups import _client, _seed_reports


@pytest.mark.parametrize("kind", ["corrupt", "future", "empty", "migration", "missing_table", "missing_column"])
def test_invalid_or_unmigratable_backup_keeps_current_data(tmp_path, monkeypatch, kind):
    client, headers = _client(tmp_path)
    db = client.app.state.core.database
    backup = rotate_backup(db.path)
    _seed_reports(db, 1)
    original = db.get_ai_research_report("rep-000")
    if kind == "corrupt":
        backup.write_bytes(b"not a SQLite database")
    elif kind in {"future", "empty"}:
        with sqlite3.connect(backup) as raw:
            raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1 if kind == 'future' else 0}")
    elif kind in {"missing_table", "missing_column"}:
        with sqlite3.connect(backup) as raw:
            if kind == "missing_table":
                raw.execute("DROP TABLE evidence")
            else:
                raw.execute("ALTER TABLE evidence DROP COLUMN payload")
    else:
        migrate = Database._migrate

        def broken(self):
            if self.path.resolve() != db.path.resolve():
                raise RuntimeError("simulated migration failure")
            return migrate(self)

        monkeypatch.setattr(Database, "_migrate", broken)
    response = client.post(f"/storage/backups/{backup.name}/restore", headers=headers)
    assert response.status_code == (503 if kind == "migration" else 422), response.text
    assert db.get_ai_research_report("rep-000") == original
    with sqlite3.connect(db.path) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize("backup_failure", [False, True])
def test_restore_failure_preserves_current_data_and_reports_failure(tmp_path, monkeypatch, backup_failure):
    from investment_steward_core.storage import backup_restore

    client, headers = _client(tmp_path)
    db = client.app.state.core.database
    backup = rotate_backup(db.path)
    _seed_reports(db, 1)
    original = db.get_ai_research_report("rep-000")
    copy = backup_restore._copy
    writes = []

    def fail_after_copy(source, target):
        copy(source, target)
        if target.resolve() == db.path.resolve():
            writes.append(source)
            if len(writes) == 1:
                raise OSError("simulated failure after target changed")

    if backup_failure:
        monkeypatch.setattr(backup_restore, "rotate_backup", lambda path: None)
    else:
        monkeypatch.setattr(backup_restore, "_copy", fail_after_copy)
    result = client.post(f"/storage/backups/{backup.name}/restore", headers=headers)
    assert result.status_code == 503, result.text
    assert ("回退点" if backup_failure else "已还原恢复前的数据") in result.json()["detail"]
    assert db.get_ai_research_report("rep-000") == original
    assert len(writes) == (0 if backup_failure else 2), "写入后失败必须实际从回退点复制回来"
    assert not list(tmp_path.glob("steward-restore-*")), "失败后临时库应释放并清理"


@pytest.mark.parametrize("version", [36, 39])
def test_old_backup_migrates_without_rewriting_source_and_preserves_legacy_calls(tmp_path, version):
    """重建v36/v39结构：缺列/缺collab表须迁移，旧用量未知不能编造成零。"""
    client, headers = _client(tmp_path)
    db = client.app.state.core.database
    _seed_reports(db, 1)
    db.record_model_call(profile_id=None, model="legacy", purpose=None, prompt_tokens=None,
                         completion_tokens=None, total_tokens=None, latency_ms=12, retried=False, outcome="error")
    backup = rotate_backup(db.path)
    with sqlite3.connect(backup) as raw:
        raw.execute("DROP TABLE collab_runs")
        if version == 36:
            for column in ("returned_model", "attempt_count", "error_kind", "http_status", "usage_complete", "attempts"):
                raw.execute(f"ALTER TABLE model_calls DROP COLUMN {column}")
        raw.execute(f"PRAGMA user_version={version}")
        old_call = raw.execute("SELECT * FROM model_calls").fetchone()
    original_hash = sha256(backup.read_bytes()).hexdigest()
    # 恢复前另有当前产出，恢复后应仅留备份时点数据；回退点须保留当前产出。
    _seed_reports(db, 2)
    result = client.post(f"/storage/backups/{backup.name}/restore", headers=headers)
    assert result.status_code == 200, result.text
    with sqlite3.connect(db.path) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        restored = raw.execute("SELECT * FROM model_calls").fetchone()
        assert restored[:len(old_call)] == old_call
        assert restored[len(old_call):] == ((None,) * 6 if version == 36 else ())
        assert raw.execute("SELECT COUNT(*) FROM collab_runs").fetchone()[0] == 0
    assert db.get_ai_research_report("rep-000") is not None
    assert db.get_ai_research_report("rep-001") is None
    with sqlite3.connect(result.json()["rollback_point"]) as raw:
        assert raw.execute("SELECT COUNT(*) FROM ai_research_reports").fetchone()[0] == 2
    assert sha256(backup.read_bytes()).hexdigest() == original_hash, "迁移只发生在临时副本"
    assert not list(tmp_path.glob("steward-restore-*"))
    # 重开数据库不得重复补列或丢失迁移后的记录。
    reopened = Database(db.path)
    assert reopened.get_ai_research_report("rep-000") == db.get_ai_research_report("rep-000")
    reopened.close()
