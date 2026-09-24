from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Evidence, EvidenceRelation, EvidenceStatus, EvidenceType
from investment_steward_core.response_gate import ResponseGateError, build_gate_payload
from investment_steward_core.storage import Database


def _client(tmp_path: Path) -> tuple[TestClient, dict[str, str]]:
    token = "p0-token"
    return TestClient(create_app(CoreSettings(session_token=token, data_dir=tmp_path))), {
        "X-Core-Session-Token": token
    }


def _evidence(tenant_id, *, valid_until=None, subject="instrument:CN:ETF:510300") -> Evidence:
    return Evidence(
        evidence_id=uuid4(),
        tenant_id=tenant_id,
        subject_refs=[subject],
        evidence_type=EvidenceType.ANALYSIS,
        source_name="fixture",
        summary="沪深300估值分位持续处于历史极高分位",
        content_hash=uuid4().hex,
        relation=EvidenceRelation.SUPPORTING,
        status=EvidenceStatus.ACTIVE,
        valid_until=valid_until,
    )


def test_response_gate_balances_nested_json_and_rejects_unknown_action():
    run_id, user_id = uuid4(), uuid4()
    raw = '{"user_question":"问","summary":"答","confidence_description":"中","action_mode":"observe","supported_actions":["open_evidence"],"meta":{"nested":true}} trailing text'
    payload = build_gate_payload(raw, [], run_id=run_id, user_id=user_id)
    assert payload["action_mode"].value == "observe"
    assert payload["supported_actions"] == ["open_evidence"]

    bad = '{"user_question":"问","summary":"答","confidence_description":"中","action_mode":"delete_all"}'
    try:
        build_gate_payload(bad, [], run_id=run_id, user_id=user_id)
    except ResponseGateError as exc:
        assert "action_mode" in str(exc)
    else:
        raise AssertionError("unknown action mode must be rejected")


def test_stale_and_expired_evidence_are_not_research_basis(tmp_path):
    client, headers = _client(tmp_path)
    tenant_id = client.get("/session", headers=headers).json()["user_id"]
    expired = _evidence(tenant_id, valid_until=datetime.now(UTC) - timedelta(minutes=1))
    assert client.post("/evidence", headers=headers, json=expired.model_dump(mode="json")).status_code == 201
    run = client.post("/research/runs", headers=headers, json={"user_question": "沪深300估值分位如何？"}).json()
    answer = client.post(f"/research/runs/{run['run_id']}/run", headers=headers).json()
    assert answer["evidence_refs"] == []
    assert "无法形成有据结论" in answer["summary"]


def test_editing_thesis_keeps_one_active_record_and_get_pending_is_read_only(tmp_path):
    client, headers = _client(tmp_path)
    tenant_id = client.get("/session", headers=headers).json()["user_id"]
    first = client.post(
        "/thesis", headers=headers,
        json={"instrument": "sh510300", "original_statement": "第一版"},
    ).json()
    edited = client.put(
        f"/thesis/{first['thesis_id']}", headers=headers,
        json={"instrument": "510300", "original_statement": "第二版"},
    )
    assert edited.status_code == 200
    assert len([item for item in client.get("/thesis", headers=headers).json() if item["status"] == "active"]) == 1

    db = Database(tmp_path / "steward.sqlite3")
    before = db.list_notifications(tenant_id)
    assert client.get("/notifications/pending", headers=headers).status_code == 200
    assert db.list_notifications(tenant_id) == before == []


def test_plan_terminal_states_cannot_reopen(tmp_path):
    client, headers = _client(tmp_path)
    plan = client.post("/plans", headers=headers, json={"title": "验证"}).json()
    active = client.post(
        f"/plans/{plan['plan_id']}/transition",
        headers=headers,
        json={"target": "active", "confirmation_summary": "开始执行本周验证计划"},
    )
    assert active.status_code == 200
    audit = client.get("/audit", headers=headers).json()
    transition = next(item for item in audit if item["action"] == "plan.transition")
    assert transition["payload"]["confirmation_summary"] == "开始执行本周验证计划"
    completed = client.post(f"/plans/{plan['plan_id']}/transition", headers=headers, json={"target": "completed"})
    assert completed.status_code == 200
    reopened = client.post(f"/plans/{plan['plan_id']}/transition", headers=headers, json={"target": "active"})
    assert reopened.status_code == 409
