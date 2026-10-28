"""诊断必须经过已有会话鉴权，并限制时间窗口和返回数量。"""
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings


def test_recent_diagnostics_are_bounded_and_authenticated(tmp_path):
    client = TestClient(create_app(CoreSettings(session_token="session", data_dir=tmp_path)))
    db = client.app.state.core.database
    for index in range(25):
        db.record_model_call(profile_id=None, model=f"requested-{index}", purpose="研报", prompt_tokens=None,
                             completion_tokens=None, total_tokens=None, latency_ms=100, retried=False,
                             outcome="error", error_kind="protocol", http_status=200, attempt_count=1,
                             returned_model="reported", usage_complete=False)
    with sqlite3.connect(db.path) as connection:
        connection.execute("UPDATE model_calls SET created_at = ? WHERE model = 'requested-24'",
                           ((datetime.now(UTC) - timedelta(days=40)).isoformat(),))
    assert client.get("/model-usage").status_code in (401, 403)
    result = client.get("/model-usage", headers={"X-Core-Session-Token": "session"}).json()
    rows = result["recent_calls"]
    assert len(rows) == 20
    assert all(row["model"] != "requested-24" for row in rows)
    assert rows[0]["returned_model"] == "reported" and rows[0]["error_kind"] == "protocol"
    assert rows[0]["attempt_count"] == 1 and rows[0]["http_status"] == 200
    assert rows[0]["usage_complete"] == 0
    assert set(rows[0]) == {"call_id", "model", "returned_model", "purpose", "created_at", "attempt_count",
                           "outcome", "error_kind", "http_status", "latency_ms", "usage_complete", "attempts"}
