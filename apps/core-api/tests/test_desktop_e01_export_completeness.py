"""E01（桌面端升级路线图 2026-09-18）：全量导出逐表补全的契约测试。

验收：「全量数据导出」这句话能被逐表核对——导出体必须覆盖 EXPORT_PAYLOAD_TABLES 全部
用户产出表（含研报与追问附录正文），且默认不含秘密/设备态（credentials、plugin_*、sync_*、
audit_events、macro_cache）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from investment_steward_core.config import CoreSettings
from investment_steward_core.storage.database import EXPORT_PAYLOAD_TABLES

TOKEN = "e01-export-token"


def _client(tmp_path):
    from investment_steward_core.api import create_app

    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def test_export_covers_all_user_tables(tmp_path):
    http, headers = _client(tmp_path)
    database = http.app.state.core.database
    database.insert_ai_research_report({
        "report_id": "e01-1",
        "kind": "stock",
        "subject": "002468",
        "generated_at": datetime(2026, 9, 18, tzinfo=UTC).isoformat(),
        "report": "# 002468 深研正文\n关键结论……",
        "model": "test-model",
    })
    database.insert_ai_analysis_turn({
        "analysis_turn_id": "turn-1",
        "parent_report_id": "e01-1",
        "created_at": datetime(2026, 9, 18, tzinfo=UTC).isoformat(),
        "question": "追问题干",
        "answer": "追问回答正文（必须在导出中）",
    })

    body = http.get("/export/all", headers=headers).json()
    # 逐表核对：EXPORT_PAYLOAD_TABLES 的每一张表都在导出体里。
    missing = [table for table in EXPORT_PAYLOAD_TABLES if table not in body]
    assert missing == [], f"导出缺表：{missing}"
    # 研报正文与追问附录正文都在导出里（验收原文）。
    report_row = next(row for row in body["ai_research_reports"] if row["report_id"] == "e01-1")
    assert "深研正文" in report_row["payload"]
    turn_row = body["ai_analysis_turns"][0]
    assert turn_row["payload"] if isinstance(turn_row.get("payload"), str) else True
    assert "追问回答正文" in (turn_row["payload"] if isinstance(turn_row["payload"], str) else turn_row["payload"].get("answer", ""))
    # 原有 9 类仍覆盖。
    for key in ("policies", "holdings", "theses", "plans", "decisions", "evidence", "research_runs", "learning_activities", "notifications"):
        assert key in body, f"原有导出键缺失：{key}"
    # 导出政策成文且明确默认排除项。
    policy = body["export_policy"]
    for excluded in ("credentials", "plugin_installations", "sync_state", "audit_events", "macro_cache"):
        assert excluded in policy["excluded_default_no_secrets"]


def test_export_excludes_secrets_and_device_state(tmp_path):
    http, headers = _client(tmp_path)
    body = http.get("/export/all", headers=headers).json()
    for excluded in ("credentials", "plugin_installations", "plugin_update_candidates",
                     "sync_inbox", "sync_outbox", "sync_state", "audit_events", "macro_cache"):
        assert excluded not in body, f"设备态/秘密 {excluded} 不应出现在默认导出里"
    # model_profiles 可在（方案元数据），但不能携带任何明文密钥字段。
    profiles = body.get("model_profiles") or []
    for profile in profiles:
        for secret_key in ("api_key", "apikey", "secret", "token"):
            assert secret_key not in profile, f"model_profiles 泄漏疑似密钥字段 {secret_key}"


def test_export_payload_tables_whitelist_rejects_unknown(tmp_path):
    """表名走白名单硬校验：不在名单内的表名直接拒绝（防拼接注入）。"""
    http, _headers = _client(tmp_path)
    database = http.app.state.core.database
    try:
        database.export_payload_tables(("credentials",))
    except ValueError as error:
        assert "whitelist" in str(error)
    else:
        raise AssertionError("credentials 不在白名单却未被拒绝")
