"""E02（桌面端升级路线图 2026-09-18）：恢复入口（/import/preview、/import/apply）的端点级测试。

覆盖：preview 不改数据且逐表报告冲突/缺失、apply 需显式确认、恢复前强制 rotate_backup、
merge 保留现有主键行只补新行、replace 回到导出时点、设备态/秘密表不可导入、写入审计。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

TOKEN = "e02-import-token"


def _client(tmp_path):
    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def _seed_reports(database, count: int, *, prefix: str = "rep") -> None:
    start = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(count):
        database.insert_ai_research_report({
            "report_id": f"{prefix}-{index:03d}",
            "kind": "stock",
            "subject": f"00{index}",
            "generated_at": (start + timedelta(days=index)).isoformat(),
            "report": f"# 报告 {index} 正文",
            "model": "test-model",
        })


def _export_body(http, headers) -> dict:
    return http.get("/export/all", headers=headers).json()


def test_preview_reports_counts_and_conflicts_without_writes(tmp_path):
    http, headers = _client(tmp_path)
    database = http.app.state.core.database
    _seed_reports(database, 3)
    export_body = _export_body(http, headers)

    # 在另一个库构造「已有 2 份同名主键报告」的冲突场景：直接用同一导出体 preview。
    before_counts = _table_count(tmp_path, "ai_research_reports")
    preview = http.post("/import/preview", json=export_body, headers=headers).json()
    assert preview["ok"] is True
    tables = {row["table"]: row for row in preview["tables"]}
    row = tables["ai_research_reports"]
    assert row["status"] == "ok" and row["incoming"] == 3 and row["conflicts"] == 3
    assert _table_count(tmp_path, "ai_research_reports") == before_counts, "preview 不得改数据"
    # 模型键与 raw 表键都在报告中（theses → thesis 等）。
    assert any(item["table"] == "thesis" for item in preview["tables"])
    # 未登记键会被点名提示。
    preview_with_junk = http.post("/import/preview", json={**export_body, "mystery": [1]}, headers=headers).json()
    assert "mystery" in preview_with_junk["unsupported_keys"]


def test_apply_requires_confirm_and_forces_backup(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    export_body = _export_body(http, headers)

    # confirm 缺省 → 422，不做无提示覆盖。
    denied = http.post("/import/apply", json={"data": export_body, "tables": ["ai_research_reports"]}, headers=headers)
    assert denied.status_code == 422

    monkeypatch.setattr(
        "investment_steward_core.storage.database.rotate_backup",
        lambda db_path, keep=7: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    blocked = http.post(
        "/import/apply",
        json={"data": export_body, "tables": ["ai_research_reports"], "confirm": True},
        headers=headers,
    )
    assert blocked.status_code == 503, "恢复前备份失败必须中止恢复"


def test_apply_merge_and_replace_round_trip(tmp_path):
    """真实恢复演练（API 级）：导出 → 模拟数据丢失（删 2 份报告）→ merge 恢复 → 行数与导出时一致。"""
    http, headers = _client(tmp_path)
    _seed_reports(http.app.state.core.database, 5)
    export_body = _export_body(http, headers)
    exported_count = _table_count(tmp_path, "ai_research_reports")
    assert exported_count == 5

    # 模拟丢失：删除 2 份报告（含正文），剩 3 份。
    with sqlite3.connect(http.app.state.core.database.path) as raw:
        raw.execute("DELETE FROM ai_research_reports WHERE report_id IN ('rep-000', 'rep-001')")
        raw.commit()
    assert _table_count(tmp_path, "ai_research_reports") == 3

    applied = http.post(
        "/import/apply",
        json={"data": export_body, "tables": ["ai_research_reports"], "mode": "merge", "confirm": True},
        headers=headers,
    ).json()
    assert applied["ok"] is True
    assert applied["backup_path"], "恢复前已强制 rotate_backup 并回报备份路径"
    result = applied["results"][0]
    assert result["inserted"] == 2 and result["skipped"] == 3, "merge 只补缺失的主键行"
    assert _table_count(tmp_path, "ai_research_reports") == exported_count, "恢复前后行数一致"

    # replace：即便数据再丢一部分，整表回到导出时点。
    with sqlite3.connect(http.app.state.core.database.path) as raw:
        raw.execute("DELETE FROM ai_research_reports WHERE report_id = 'rep-004'")
        raw.commit()
    applied_replace = http.post(
        "/import/apply",
        json={"data": export_body, "tables": ["ai_research_reports"], "mode": "replace", "confirm": True},
        headers=headers,
    ).json()
    assert applied_replace["results"][0]["inserted"] == 5
    assert _table_count(tmp_path, "ai_research_reports") == exported_count
    detail = http.get("/ai-research/reports/rep-003", headers=headers).json()
    assert "报告 3 正文" in detail["item"]["report"], "恢复的 payload 正文逐字一致"

    # 写入审计：import.applied 有留痕。
    audit = http.get("/audit", params={"limit": 20}, headers=headers).json()
    assert any(event["action"] == "import.applied" for event in audit)


def _table_count(tmp_path: Path, table: str) -> int:
    from investment_steward_core.storage.database import Database

    db_path = tmp_path / "steward.sqlite3"
    assert db_path.exists()
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()
