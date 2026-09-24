"""存储位置选择与迁移测试（路线图 3.1 第 3 步）。

覆盖：指针读写与优先级、迁移全流程（临时复制/哈希校验/落位/指针切换）、
失败语义（同目录/无库/哈希篡改）、端点契约（GET layout / preview / migrate 409）。
所有测试都必须 patch `STEWARD_LAYOUT_POINTER`，绝不触碰真实 %LOCALAPPDATA%。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from investment_steward_core.storage import migration
from investment_steward_core.storage.paths import (
    StorageLayout,
    clear_pointer,
    load_pointer,
    pointer_file,
    write_pointer,
)


def _patch_pointer(monkeypatch, tmp_path: Path) -> Path:
    pointer = tmp_path / "anchor" / "storage-layout.json"
    monkeypatch.setenv("STEWARD_LAYOUT_POINTER", str(pointer))
    return pointer


def _make_source(root: Path) -> StorageLayout:
    """构造带真实内容（SQLite + 量化 JSON）的源布局。"""
    layout = StorageLayout.from_user_data(root)
    layout.ensure()
    conn = sqlite3.connect(layout.database_file)
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO demo (value) VALUES ('迁移不丢数据')")
    conn.commit()
    conn.close()
    layout.quant_parameter_sets_file.write_text(
        json.dumps({"sets": [{"artifact_id": "ps-demo"}]}, ensure_ascii=False), encoding="utf-8"
    )
    return layout


def test_pointer_roundtrip_and_priority(monkeypatch, tmp_path):
    pointer = _patch_pointer(monkeypatch, tmp_path)
    assert load_pointer() is None and not pointer.exists()

    target = tmp_path / "moved"
    write_pointer(target, artifacts=tmp_path / "moved-artifacts")
    loaded = load_pointer()
    assert loaded is not None
    assert loaded["user_data"] == str(target)
    assert loaded["artifacts"] == str(tmp_path / "moved-artifacts")

    # from_environment 优先级:显式 env > 指针 > 默认
    monkeypatch.delenv("STEWARD_DATA_DIR", raising=False)
    layout = StorageLayout.from_environment()
    assert Path(layout.user_data) == target
    monkeypatch.setenv("STEWARD_DATA_DIR", str(tmp_path / "env-wins"))
    layout_env = StorageLayout.from_environment()
    assert Path(layout_env.user_data) == tmp_path / "env-wins"

    assert clear_pointer() is True
    assert load_pointer() is None
    assert clear_pointer() is False


def test_migrate_full_flow_verifies_and_keeps_source(monkeypatch, tmp_path):
    _patch_pointer(monkeypatch, tmp_path)
    source = _make_source(tmp_path / "source")
    target_root = tmp_path / "target"

    result = migration.migrate(source, str(target_root))
    assert result["ok"] is True and result["restart_required"] is True
    assert result["old_directory_removed"] is False
    assert result["copied_files"] >= 2  # sqlite + quant json (+ 指针不会出现在源里)

    target = StorageLayout.from_user_data(target_root)
    assert target.database_file.exists()
    with sqlite3.connect(target.database_file) as conn:
        assert conn.execute("SELECT value FROM demo").fetchone()[0] == "迁移不丢数据"
    assert json.loads(target.quant_parameter_sets_file.read_text(encoding="utf-8"))["sets"][0]["artifact_id"] == "ps-demo"

    # 指针已切换到目标;源目录原样保留(用户确认前不删除)
    assert load_pointer()["user_data"] == str(target_root)
    assert source.database_file.exists()
    assert (source.user_data / "quant_parameter_sets.json").exists()

    # 再次校验:清单逐文件复核 + quick_check
    report = migration.verify(target)
    assert report["ok"] is True and report["database"]["ok"] is True
    assert report["checked_files"] >= 2


def test_migrate_rejects_same_dir_and_missing_db(monkeypatch, tmp_path):
    _patch_pointer(monkeypatch, tmp_path)
    empty = StorageLayout.from_user_data(tmp_path / "empty")
    empty.ensure()
    try:
        migration.migrate(empty, str(tmp_path / "target"))
        raise AssertionError("无数据库时应拒绝迁移")
    except migration.MigrationError as error:
        assert error.code == "no_database"

    source = _make_source(tmp_path / "source")
    try:
        migration.migrate(source, str(source.user_data))
        raise AssertionError("同目录应拒绝迁移")
    except migration.MigrationError as error:
        assert error.code == "same_directory"


def test_verify_detects_tampered_file(monkeypatch, tmp_path):
    _patch_pointer(monkeypatch, tmp_path)
    source = _make_source(tmp_path / "source")
    target_root = tmp_path / "target"
    migration.migrate(source, str(target_root))

    target = StorageLayout.from_user_data(target_root)
    target.quant_parameter_sets_file.write_text('{"sets": []}', encoding="utf-8")  # 篡改
    report = migration.verify(target)
    assert report["ok"] is False
    assert any("哈希不一致" in item for item in report["mismatches"])


def test_storage_endpoints_contract(client):
    http, headers = client
    resp = http.get("/storage/layout", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert body["database_file"].endswith("steward.sqlite3")
    assert body["usage"]["database_bytes"] >= 0

    # 预览:目标=当前目录 → 不可行结论
    layout_body = http.get("/storage/layout", headers=headers).json()
    resp = http.post(
        "/storage/layout/migrate/preview",
        headers=headers,
        json={"target_user_data": layout_body["user_data"]},
    )
    assert resp.status_code == 200
    preview = resp.json()
    assert preview["feasible"] is False and preview["problems"]

    # 迁移:同目录 → 409
    resp = http.post(
        "/storage/layout/migrate",
        headers=headers,
        json={"target_user_data": layout_body["user_data"]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"].startswith("same_directory")


def test_storage_lifecycle_reports_backups_and_integrity(client):
    """8.6 数据生命周期:备份清单 + PRAGMA 完整性 + 保留规则,全部本机可复算。"""
    http, headers = client
    resp = http.get("/storage/lifecycle", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["backups"]["retention_keep"] == 7
    assert isinstance(body["backups"]["count"], int)
    assert body["integrity"]["result"] == "ok"
    assert body["integrity"]["method"].startswith("PRAGMA")
    assert isinstance(body["retention_rules"], list) and body["retention_rules"]
    assert "usage" in body and "data_snapshots" in body["artifacts_breakdown"]


# ---------------------------------------------------------------------------
# 宿主兜底目录（2026-09-10 桌面端「存储位置改不动」缺陷回归守卫）
#
# 真机缺陷：桌面壳硬编码 --data-dir=userData/data 启动 Core，而 CoreSettings 对
# 显式 data_dir 一律重派生 layout → 位置指针在每次重启都被覆盖，用户迁移永远白做。
# 修复：壳改传 --default-data-dir（宿主兜底），优先级 env > 指针 > 兜底 > 编译默认。
# ---------------------------------------------------------------------------

def test_from_environment_fallback_loses_to_pointer_and_env(monkeypatch, tmp_path):
    _patch_pointer(monkeypatch, tmp_path)
    monkeypatch.delenv("STEWARD_DATA_DIR", raising=False)
    fallback = tmp_path / "host-default"

    # 1) 无指针 → 宿主兜底生效（桌面壳首次启动）
    layout = StorageLayout.from_environment(fallback_data_dir=fallback)
    assert Path(layout.user_data) == fallback

    # 2) 指针存在 → 指针赢回兜底（用户迁移后重启仍生效的核心语义）
    target = tmp_path / "moved"
    write_pointer(target)
    layout = StorageLayout.from_environment(fallback_data_dir=fallback)
    assert Path(layout.user_data) == target

    # 3) env 最高优先（dev / 测试隔离）
    monkeypatch.setenv("STEWARD_DATA_DIR", str(tmp_path / "env-wins"))
    assert Path(StorageLayout.from_environment(fallback_data_dir=fallback).user_data) == tmp_path / "env-wins"

    # 4) 不传 fallback 时行为与旧版一致（env > 指针 > 编译期默认）
    monkeypatch.delenv("STEWARD_DATA_DIR", raising=False)
    assert Path(StorageLayout.from_environment().user_data) == target


def test_reset_default_migrates_to_host_fallback_dir(tmp_path, monkeypatch):
    """桌面壳场景：「恢复默认」应迁回宿主兜底目录，而不是编译期 %LOCALAPPDATA% 默认。"""
    _patch_pointer(monkeypatch, tmp_path)
    monkeypatch.delenv("STEWARD_DATA_DIR", raising=False)

    from fastapi.testclient import TestClient

    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings

    host_default = tmp_path / "host-default"
    host_default.mkdir()
    moved = _make_source(tmp_path / "moved")  # 用户已迁移到的位置（带真实库）
    write_pointer(moved.user_data)  # 指针指向已迁移位置

    token = "t"
    app = create_app(
        CoreSettings(session_token=token, data_dir=moved.user_data, default_data_dir=host_default)
    )
    http = TestClient(app)
    headers = {"X-Core-Session-Token": token}
    resp = http.post("/storage/layout/reset-default", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["migrated"] is True
    assert Path(body["target_user_data"]) == host_default
    assert load_pointer() is None  # 指针已清（迁移过程中写过，端点最后清除）
    assert (host_default / "steward.sqlite3").exists()
    # 源目录保留待用户确认
    assert moved.database_file.exists()
