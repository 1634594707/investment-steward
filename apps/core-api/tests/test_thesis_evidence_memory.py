"""证据关联保存历史判断，不把跨租户/跨标的材料混入投资逻辑。"""
from uuid import uuid4

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Evidence, Thesis


def test_thesis_links_preserve_user_text_and_reject_foreign_evidence(tmp_path):
    app = create_app(CoreSettings(session_token="memory-test", data_dir=tmp_path))
    http, headers = TestClient(app), {"X-Core-Session-Token": "memory-test"}
    core = app.state.core
    ids = []
    for tenant, subject in [(core.local_user_id, "600001"), (uuid4(), "600001"), (core.local_user_id, "600002")]:
        item = Evidence(evidence_id=uuid4(), tenant_id=tenant, subject_refs=[subject],
            evidence_type="announcement", source_name="synthetic", summary="客户取消订单，挑战增长假设",
            content_hash="synthetic-memory-123456", relation="contradicting", status="retracted")
        core.database.insert_evidence(item)
        ids.append(str(item.evidence_id))
    body = {"instrument": "600001", "original_statement": "订单增长假设", "core_assumptions": ["订单持续"],
            "invalidation_conditions": ["主要客户取消订单"], "evidence_refs": [ids[0], ids[0]]}
    created = http.post("/thesis", headers=headers, json=body)
    assert created.status_code == 201, created.text
    saved = created.json()
    assert saved["evidence_refs"] == [ids[0]]
    for bad in [ids[1], ids[2], str(uuid4())]:
        rejected = http.put(f"/thesis/{saved['thesis_id']}", headers=headers, json={**body, "evidence_refs": [bad]})
        assert rejected.status_code == 422
        assert http.get("/thesis", headers=headers).json() == [saved]
    legacy = {key: value for key, value in body.items() if key != "evidence_refs"}
    updated = http.put(f"/thesis/{saved['thesis_id']}", headers=headers, json=legacy)
    assert updated.json()["evidence_refs"] == [ids[0]], "未提交新字段的旧客户端不能清空历史关联"
    assert updated.json()["original_statement"] == body["original_statement"]
    assert updated.json()["invalidation_conditions"] == body["invalidation_conditions"]
    cleared = http.put(f"/thesis/{saved['thesis_id']}", headers=headers, json={**legacy, "evidence_refs": []})
    assert cleared.json()["evidence_refs"] == []


def test_scoped_research_returns_local_memory_snapshot_without_mutating_thesis(tmp_path):
    app = create_app(CoreSettings(session_token="memory-test", data_dir=tmp_path))
    http, headers = TestClient(app), {"X-Core-Session-Token": "memory-test"}
    core = app.state.core
    old, new = uuid4(), uuid4()
    for eid, state in [(old, "retracted"), (new, "active")]:
        core.database.insert_evidence(Evidence(evidence_id=eid, tenant_id=core.local_user_id,
            subject_refs=["600001"], evidence_type="announcement", source_name="synthetic",
            summary="Operating cash flow declined", content_hash="synthetic-memory-123456",
            relation="contradicting", status=state))
    theses = []
    for user, symbol, state in [(core.local_user_id, "600001", "active"),
                               (core.local_user_id, "600002", "active"),
                               (uuid4(), "600001", "active"), (core.local_user_id, "600001", "archived")]:
        thesis = Thesis(thesis_id=uuid4(), user_id=user, instrument=symbol, status=state,
            original_statement="订单增长是用户假设", core_assumptions=["订单增长"],
            invalidation_conditions=["客户取消订单"], evidence_refs=[old])
        core.database.upsert_thesis(thesis)
        theses.append(thesis)
    before = core.database.list_theses(core.local_user_id)
    created = http.post("/research/runs", headers=headers, json={"user_question": "cash flow",
        "evidence_scope": {"subject_refs": ["600001"]}}).json()
    response = http.post(f"/research/runs/{created['run_id']}/run", headers=headers).json()
    memory = response["research_memory"]
    assert len(memory) == 1 and memory[0]["thesis_id"] == str(theses[0].thesis_id)
    assert memory[0]["linked_evidence_refs"] == [str(old)]
    assert memory[0]["candidate_contradicting_refs"] == [str(new)]
    assert memory[0]["invalidation_conditions"] == ["客户取消订单"]
    assert response["evidence_refs"] == [str(new)], "用户假设和撤回引用不能升级为当前事实"
    assert core.database.list_theses(core.local_user_id) == before
    stored = http.get(f"/research/runs/{created['run_id']}/response", headers=headers).json()
    assert stored["research_memory"] == memory
    core.database.upsert_thesis(theses[0].model_copy(update={"original_statement": "用户后来修订了假设"}))
    assert http.get(f"/research/runs/{created['run_id']}/response", headers=headers).json()["research_memory"] == memory
    unscoped = http.post("/research/runs", headers=headers, json={"user_question": "cash flow"}).json()
    assert http.post(f"/research/runs/{unscoped['run_id']}/run", headers=headers).json()["research_memory"] == []
