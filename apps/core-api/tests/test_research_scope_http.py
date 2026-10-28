"""显式范围须落库并限制真实研究答案，不能混入跨标的/日期/来源证据。"""
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Evidence


def test_scope_survives_storage_and_keeps_counter_evidence(tmp_path):
    app = create_app(CoreSettings(session_token="scope-test", data_dir=tmp_path))
    http = TestClient(app)
    headers = {"X-Core-Session-Token": "scope-test"}
    core = app.state.core
    ids = []
    for subject, source, published, relation in [
        ("600001", "filing", "2026-06-01", "supporting"),
        ("600001", "filing", "2026-06-30", "contradicting"),
        ("600002", "filing", "2026-06-20", "supporting"),
        ("600001", "rumour", "2026-06-20", "supporting"),
        ("600001", "filing", "2026-05-31", "supporting"),
        ("600001", "filing", None, "supporting"),
    ]:
        item = Evidence(evidence_id=uuid4(), tenant_id=core.local_user_id, subject_refs=[subject],
            evidence_type="announcement", source_name=source, summary="Operating cash flow changed",
            published_at=datetime.fromisoformat(published).replace(tzinfo=UTC) if published else None,
            content_hash="synthetic-hash-123456", relation=relation)
        core.database.insert_evidence(item)
        ids.append(str(item.evidence_id))
    scope = {"subject_refs": ["600001"], "source_names": ["filing"],
             "published_from": "2026-06-01T00:00:00Z", "published_to": "2026-06-30T00:00:00Z"}
    created = http.post("/research/runs", headers=headers,
        json={"user_question": "operating cash flow", "evidence_scope": scope})
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    saved = http.get(f"/research/runs/{run_id}", headers=headers).json()
    assert saved["evidence_scope"] == {**scope, "financial_period_ends": [], "financial_period_basis": None}
    answer = http.post(f"/research/runs/{run_id}/run", headers=headers)
    assert answer.status_code == 200, answer.text
    assert answer.json()["supporting_refs"] == [ids[0]]
    assert answer.json()["contradicting_refs"] == [ids[1]]
    for invalid in [
        {"published_from": "2026-07-01T00:00:00Z", "published_to": "2026-06-01T00:00:00Z"},
        {"published_from": "2026-06-01T00:00:00"}, {"subject_refs": [""]},
    ]:
        assert http.post("/research/runs", headers=headers,
            json={"user_question": "cash flow", "evidence_scope": invalid}).status_code == 422


def test_financial_period_is_explicit_and_independent_of_publication(tmp_path):
    app = create_app(CoreSettings(session_token="period-test", data_dir=tmp_path, model_access_enabled=False))
    http, headers = TestClient(app), {"X-Core-Session-Token": "period-test"}
    core = app.state.core
    ids = []
    for period, relation in [("2026-06-30", "supporting"), ("2026-06-30", "contradicting"),
                             ("2026-03-31", "supporting"), (None, "supporting")]:
        item = Evidence(evidence_id=uuid4(), tenant_id=core.local_user_id, subject_refs=["600001"],
            evidence_type="financial", source_name="synthetic", summary="cash flow 2026-06-30",
            published_at=datetime(2026, 8, 1, tzinfo=UTC), financial_period_end=period,
            content_hash="synthetic-period-123456", relation=relation)
        core.database.insert_evidence(item)
        ids.append(str(item.evidence_id))
    scope = {"subject_refs": ["600001"], "financial_period_ends": ["2026-06-30"],
             "published_from": "2026-08-01T00:00:00Z", "published_to": "2026-08-01T00:00:00Z"}
    created = http.post("/research/runs", headers=headers,
                        json={"user_question": "cash flow", "evidence_scope": scope})
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    assert http.get(f"/research/runs/{run_id}", headers=headers).json()["evidence_scope"]["financial_period_ends"] == ["2026-06-30"]
    answer = http.post(f"/research/runs/{run_id}/run", headers=headers)
    assert answer.status_code == 200, answer.text
    assert set(answer.json()["evidence_refs"]) == set(ids[:2]), "同发布日期的别期和缺期不能靠摘要日期混入"
    assert answer.json()["contradicting_refs"] == [ids[1]]
    invalid = http.post("/research/runs", headers=headers,
                        json={"user_question": "cash flow", "evidence_scope": {"financial_period_ends": ["2026-02-30"]}})
    assert invalid.status_code == 422
    unscoped = http.post("/research/runs", headers=headers, json={"user_question": "cash flow"}).json()
    assert set(http.post(f"/research/runs/{unscoped['run_id']}/run", headers=headers).json()["evidence_refs"]) == set(ids)


def test_same_period_end_different_bases_are_not_interchangeable(tmp_path):
    app = create_app(CoreSettings(session_token="basis", data_dir=tmp_path, model_access_enabled=False))
    http, headers, core = TestClient(app), {"X-Core-Session-Token": "basis"}, app.state.core
    ids = {}
    for basis in ["cumulative", "single_quarter", "ttm", "instant", "unknown"]:
        item = Evidence(evidence_id=uuid4(), tenant_id=core.local_user_id, subject_refs=["600001"],
            evidence_type="financial", source_name="synthetic", summary="cash flow", content_hash="synthetic-basis-123456",
            relation="contradicting", financial_period_end="2026-06-30", financial_period_basis=basis)
        core.database.insert_evidence(item)
        ids[basis] = str(item.evidence_id)
    for basis, eid in ids.items():
        created = http.post("/research/runs", headers=headers, json={"user_question": "cash flow",
            "evidence_scope": {"financial_period_ends": ["2026-06-30"], "financial_period_basis": basis}}).json()
        answer = http.post(f"/research/runs/{created['run_id']}/run", headers=headers).json()
        assert answer["contradicting_refs"] == [eid], "相同期末的不同期间不能混成同口径事实"
    assert http.post("/research/runs", headers=headers, json={"user_question": "cash flow",
        "evidence_scope": {"financial_period_basis": "guessed"}}).status_code == 422
