"""未判定关系的材料可供核查，不能被本地或降级回答当成方向性证据。"""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import model_client
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Evidence


@pytest.mark.parametrize("fallback", [False, True], ids=["local", "model-unavailable"])
@pytest.mark.parametrize("relations, expected_mode", [
    (["unknown"], "research"), (["unknown", "unknown"], "research"),
    (["supporting", "unknown"], "observe"),
    (["contradicting", "unknown"], "research"),
    (["supporting", "contradicting", "unknown"], "research"), ([], "research"),
])
def test_unknown_evidence_never_becomes_directional_support_in_saved_answer(
    tmp_path, monkeypatch, fallback, relations, expected_mode,
):
    attempts = []

    def unavailable(*args, **kwargs):
        attempts.append(True)
        raise model_client.ModelUnavailable("synthetic offline")

    monkeypatch.setattr(model_client, "call_active_model", unavailable)
    app = create_app(CoreSettings(data_dir=tmp_path, session_token="unknown", model_access_enabled=fallback))
    core, headers = app.state.core, {"X-Core-Session-Token": "unknown"}
    try:
        with TestClient(app) as http:
            if fallback:
                # _try_model 获取方案后即进入受控失败；绝不调用外部网关。
                from investment_steward_core.domain import ModelProfile
                core.database.upsert_model_profile(ModelProfile(profile_id=uuid4(), name="synthetic",
                    base_url="https://example.invalid", model="synthetic", credential_ref="unused", status="使用中"))
            items = []
            for index, relation in enumerate(relations):
                item = Evidence(evidence_id=uuid4(), tenant_id=core.local_user_id, subject_refs=["TEST"],
                    evidence_type="announcement", source_name="synthetic", summary="cash flow evidence",
                    content_hash=f"unknown-relation-{index:016d}", relation=relation)
                core.database.insert_evidence(item)
                items.append(item)
            run = http.post("/research/runs", headers=headers, json={"user_question": "cash flow"})
            assert run.status_code == 201
            run_id = run.json()["run_id"]
            answer = http.post(f"/research/runs/{run_id}/run", headers=headers)
            assert answer.status_code == 200, answer.text
            result = answer.json()
            assert result["action_mode"] == expected_mode
            assert result["model_provider"] == "local-deterministic"
            assert set(result["evidence_refs"]) == {str(item.evidence_id) for item in items}
            for field, relation in [("supporting_refs", "supporting"), ("contradicting_refs", "contradicting")]:
                assert set(result[field]) == {str(item.evidence_id) for item in items if item.relation == relation}
            if relations and set(relations) == {"unknown"}:
                assert "不能形成方向性判断" in result["summary"]
                assert "中等" not in result["confidence_description"]
                assert result["supported_actions"] == ["create_plan"]
                assert any("关系" in line for line in result["missing_information"])
            assert http.get(f"/research/runs/{run_id}/response", headers=headers).json() == result
            assert http.get(f"/research/runs/{run_id}", headers=headers).json()["status"] == "completed"
            assert {item.evidence_id: item.relation for item in core.database.list_evidence(core.local_user_id)} == {
                item.evidence_id: item.relation for item in items}
            assert bool(attempts) == fallback
    finally:
        core.database.close()
