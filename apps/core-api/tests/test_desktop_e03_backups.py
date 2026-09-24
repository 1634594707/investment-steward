"""E03（桌面端升级路线图 2026-09-18）：备份清单 / 从备份恢复 / StorageLayout 纳管。

覆盖：/storage/layout 暴露备份目录、/storage/backups 列出时间点与大小、
从备份恢复（rotate 先行 + SQLite backup API 还原 + 审计）、路径穿越与非法名拒绝、
恢复把整库回到备份时点（ai_research_reports 行数与正文一致）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

TOKEN = "e03-backups-token"


def _client(tmp_path):
    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def _seed_reports(database, count: int) -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(count):
        database.insert_ai_research_report({
            "report_id": f"rep-{index:03d}",
            "kind": "stock",
            "subject": f"00{index}",
            "generated_at": (start + timedelta(days=index)).isoformat(),
            "report": f"# 报告 {index} 正文",
            "model": "test-model",
        })


def test_layout_exposes_backups_directory(tmp_path):
    http, headers = _client(tmp_path)
    layout = http.get("/storage/layout", headers=headers).json()
    assert layout["backups"].endswith("backups"), "备份目录纳入 StorageLayout 显式管理"
    lifecycle = http.get("/storage/lifecycle", headers=headers).json()
    # E05 落地后：说明更新为「启动时与每日首次写入时轮换 + 按时间跨度保留」。
    assert lifecycle["backups"]["note"].startswith("启动时与每日首次写入时在线轮换备份"), "对外说明与实现保持一致"
    assert "最近 7 天日份" in lifecycle["backups"]["note"]
    assert any("恢复前强制回退点" in rule for rule in lifecycle["retention_rules"])


def test_backup_list_and_restore_round_trip(tmp_path):
    http, headers = _client(tmp_path)
    database = http.app.state.core.database
    _seed_reports(database, 4)

    # 用 rotate_backup 产生一份真实备份（与启动期同一机制）。
    from investment_steward_core.storage.database import rotate_backup

    rotate_backup(database.path)
    listing = http.get("/storage/backups", headers=headers).json()
    assert listing["ok"] is True and len(listing["items"]) == 1
    item = listing["items"][0]
    assert item["name"].startswith("steward-") and item["bytes"] > 0
    assert item["table_counts"]["ai_research_reports"] == 4, "备份内行数可感知（那一天能回到哪）"
    assert "整库快照" in listing["rollback_scope"]

    # 模拟丢失：删 2 份 → 从备份恢复 → 行数与正文回到备份时点。
    with sqlite3.connect(database.path) as raw:
        raw.execute("DELETE FROM ai_research_reports WHERE report_id IN ('rep-000', 'rep-001')")
        raw.commit()
    assert http.get("/storage/backups", headers=headers).json()["items"][0]["table_counts"]["ai_research_reports"] == 4
    restored = http.post(f"/storage/backups/{item['name']}/restore", headers=headers).json()
    assert restored["ok"] is True and restored["restored_from"] == item["name"]
    assert restored["rollback_point"], "恢复前已强制 rotate_backup 产生回退点"
    conn = sqlite3.connect(database.path)
    try:
        count = int(conn.execute("SELECT COUNT(*) FROM ai_research_reports").fetchone()[0])
        payload = conn.execute(
            "SELECT payload FROM ai_research_reports WHERE report_id = 'rep-000'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 4, "恢复后行数与备份时点一致"
    assert "报告 0 正文" in payload
    # 审计留痕。
    audit = http.get("/audit", params={"limit": 20}, headers=headers).json()
    assert any(event["action"] == "storage.backup.restored" for event in audit)


def test_restore_rejects_illegal_names(tmp_path):
    http, headers = _client(tmp_path)
    for name in ("../steward-20260918-000000.sqlite3", "sub/dir.sqlite3", "credentials.sqlite3", "steward-not-a-date.sqlite3"):
        response = http.post(f"/storage/backups/{name}/restore", headers=headers)
        assert response.status_code in (404, 422), f"{name} 应被拒绝"
    # 不存在的合法名 → 404。
    assert http.post("/storage/backups/steward-20260101-000000.sqlite3/restore", headers=headers).status_code == 404
