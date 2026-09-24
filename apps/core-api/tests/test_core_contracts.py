from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def test_health_does_not_require_session(client):
    test_client, _ = client
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_protected_routes_require_session(client):
    test_client, _ = client
    assert test_client.get("/session").status_code == 401


def test_policy_requires_confirmation_and_preserves_previous_version(client):
    test_client, headers = client
    draft = test_client.post(
        "/investment-policies",
        headers=headers,
        json={"investment_goal": "长期建立可复盘的 ETF 组合", "horizon_years": 10},
    )
    assert draft.status_code == 201
    policy = draft.json()
    assert policy["status"] == "draft"

    blocked = test_client.post(
        f"/investment-policies/{policy['policy_id']}/confirm",
        headers=headers,
        json={"confirmation_summary": "确认长期目标"},
    )
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "active"
    assert blocked.json()["confirmation_method"] == "explicit_ui"

    second = test_client.post(
        "/investment-policies",
        headers=headers,
        json={"investment_goal": "长期建立可复盘的 A 股与 ETF 组合", "change_reason": "review"},
    ).json()
    confirmed = test_client.post(
        f"/investment-policies/{second['policy_id']}/confirm",
        headers=headers,
        json={"confirmation_summary": "复核后确认"},
    )
    assert confirmed.status_code == 200
    policies = test_client.get("/investment-policies", headers=headers).json()
    assert [item["status"] for item in policies] == ["active", "superseded"]


def test_research_run_state_machine_lifecycle(client):
    test_client, headers = client
    created = test_client.post(
        "/research/runs", headers=headers, json={"user_question": "组合是否过度集中？"}
    )
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == "created"
    assert run["user_question"] == "组合是否过度集中？"

    advanced = test_client.post(
        f"/research/runs/{run['run_id']}/transition", headers=headers, json={"target": "planning"}
    )
    assert advanced.status_code == 200
    assert advanced.json()["status"] == "planning"

    collecting = test_client.post(
        f"/research/runs/{run['run_id']}/transition",
        headers=headers,
        json={"target": "collecting_evidence"},
    )
    assert collecting.status_code == 200
    assert collecting.json()["status"] == "collecting_evidence"

    analyzing = test_client.post(
        f"/research/runs/{run['run_id']}/transition", headers=headers, json={"target": "analyzing"}
    )
    assert analyzing.status_code == 200
    assert analyzing.json()["status"] == "analyzing"

    composing = test_client.post(
        f"/research/runs/{run['run_id']}/transition", headers=headers, json={"target": "composing"}
    )
    assert composing.status_code == 200

    completed = test_client.post(
        f"/research/runs/{run['run_id']}/transition", headers=headers, json={"target": "completed"}
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    listed = test_client.get("/research/runs", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["run_id"] == run["run_id"]


def test_research_run_rejects_invalid_transition(client):
    test_client, headers = client
    created = test_client.post(
        "/research/runs", headers=headers, json={"user_question": "追涨是否违背原则？"}
    ).json()
    # created -> completed 非法中间态，应返回 409。
    invalid = test_client.post(
        f"/research/runs/{created['run_id']}/transition", headers=headers, json={"target": "completed"}
    )
    assert invalid.status_code == 409
    assert "invalid" in invalid.json()["detail"]


def test_research_run_answers_from_evidence_no_fabrication(client):
    test_client, headers = client
    session = test_client.get("/session", headers=headers).json()
    tenant_id = session["user_id"]

    evidence = {
        "evidence_id": str(uuid4()),
        "tenant_id": tenant_id,
        "subject_refs": ["instrument:CN:ETF:510300 ETF"],
        "evidence_type": "quote",
        "source_name": "fixture-market",
        "license_status": "test-fixture",
        "summary": "沪深300ETF 估值处于历史中位数",
        "content_hash": "fedcba9876543210fedcba9876543210",
        "relation": "supporting",
        "freshness": "as_of_fixture",
        "status": "active",
    }
    assert test_client.post("/evidence", headers=headers, json=evidence).status_code == 201

    created = test_client.post(
        "/research/runs", headers=headers, json={"user_question": "沪深300ETF 逻辑是否仍成立？"}
    ).json()

    answer = test_client.post(
        f"/research/runs/{created['run_id']}/run", headers=headers
    )
    assert answer.status_code == 200
    payload = answer.json()
    assert payload["supporting_refs"] == [evidence["evidence_id"]]
    assert payload["contradicting_refs"] == []
    assert payload["model_provider"] == "local-deterministic"
    assert "买卖建议" in payload["limitations"][0]

    run = test_client.get(f"/research/runs/{created['run_id']}", headers=headers).json()
    assert run["status"] == "completed"
    assert run["response_id"] == payload["response_id"]

    latest = test_client.get("/research/questions/latest", headers=headers)
    assert latest.status_code == 200
    assert latest.json()["response_id"] == payload["response_id"]


def test_research_run_missing_evidence_explicitly_admits_uncertainty(client):
    test_client, headers = client
    created = test_client.post(
        "/research/runs", headers=headers, json={"user_question": "某冷门标的基本面是否恶化？"}
    ).json()

    answer = test_client.post(f"/research/runs/{created['run_id']}/run", headers=headers)
    assert answer.status_code == 200
    payload = answer.json()
    assert payload["supporting_refs"] == []
    assert payload["missing_information"]
    assert "无法形成有据结论" in payload["summary"]

    run = test_client.get(f"/research/runs/{created['run_id']}", headers=headers).json()
    assert run["status"] == "completed"


def test_research_run_cannot_be_rerun(client):
    test_client, headers = client
    created = test_client.post(
        "/research/runs", headers=headers, json={"user_question": "组合集中度如何？"}
    ).json()
    first = test_client.post(f"/research/runs/{created['run_id']}/run", headers=headers)
    assert first.status_code == 200
    second = test_client.post(f"/research/runs/{created['run_id']}/run", headers=headers)
    assert second.status_code == 409


def test_research_run_can_be_deleted_with_its_response(client):
    test_client, headers = client
    created = test_client.post(
        "/research/runs", headers=headers, json={"user_question": "组合过度集中怎么判断？"}
    ).json()
    answered = test_client.post(f"/research/runs/{created['run_id']}/run", headers=headers)
    assert answered.status_code == 200

    assert test_client.get(f"/research/runs/{created['run_id']}/response", headers=headers).status_code == 200

    deleted = test_client.delete(f"/research/runs/{created['run_id']}", headers=headers)
    assert deleted.status_code == 204

    assert test_client.get(f"/research/runs/{created['run_id']}", headers=headers).status_code == 404
    # /response 为可选契约：删除后返回 200 且正文为 null（与未作答一致），不残留已删回答。
    assert test_client.get(f"/research/runs/{created['run_id']}/response", headers=headers).json() is None
    listed = test_client.get("/research/runs", headers=headers).json()
    assert created["run_id"] not in {item["run_id"] for item in listed}


def test_research_run_delete_missing_returns_404(client):
    test_client, headers = client
    assert test_client.delete(f"/research/runs/{uuid4()}", headers=headers).status_code == 404


def test_today_brief_without_holdings_does_not_fabricate_personalization(client):
    test_client, headers = client
    brief = test_client.get("/brief/today", headers=headers)
    assert brief.status_code == 200
    payload = brief.json()
    assert payload["has_personalization"] is False
    assert payload["items"] == []
    assert payload["empty_reason"]


def test_today_brief_links_holdings_evidence(client):
    test_client, headers = client
    session = test_client.get("/session", headers=headers).json()
    tenant = session["user_id"]
    holding = test_client.post(
        "/holdings",
        headers=headers,
        json={"instrument": "CN:ETF:510300", "label": "沪深 300 ETF", "status": "holding"},
    )
    assert holding.status_code == 201
    evidence = test_client.post(
        "/evidence",
        headers=headers,
        json={
            "evidence_id": str(uuid4()),
            "tenant_id": tenant,
            "subject_refs": ["instrument:CN:ETF:510300"],
            "evidence_type": "quote",
            "source_name": "fixture-market",
            "license_status": "test-fixture",
            "summary": "510300 走势平稳，未触发失效条件",
            "content_hash": "beef" + "0" * 28,
            "relation": "supporting",
            "freshness": "as_of_fixture",
            "status": "active",
        },
    )
    assert evidence.status_code == 201

    brief = test_client.get("/brief/today", headers=headers).json()
    assert brief["has_personalization"] is True
    assert len(brief["items"]) == 1
    item = brief["items"][0]
    assert item["related_instrument"] == "CN:ETF:510300"
    assert item["signal"] == "observe"
    assert evidence.json()["evidence_id"] in item["evidence_refs"]


def test_weekly_review_summarizes_state(client):
    test_client, headers = client
    test_client.post(
        "/research/runs", headers=headers, json={"user_question": "仍待解决的集中度问题"}
    )
    review = test_client.get("/review/weekly", headers=headers)
    assert review.status_code == 200
    payload = review.json()
    assert payload["review_id"]
    assert payload["sections"]
    keys = {s["key"] for s in payload["sections"]}
    assert {"thesis", "research", "decisions", "next"} <= keys
    assert any("集中度" in line for line in payload["open_research"])


def test_notifications_empty_without_matching_evidence(client):
    test_client, headers = client
    unanswered = test_client.get("/notifications/pending", headers=headers)
    assert unanswered.status_code == 200
    assert unanswered.json() == []


def test_notifications_fire_on_confirmed_condition_match(client):
    test_client, headers = client
    session = test_client.get("/session", headers=headers).json()
    tenant = session["user_id"]
    thesis = test_client.post(
        "/thesis",
        headers=headers,
        json={
            "instrument": "CN:ETF:510300",
            "original_statement": "以低成本分散方式参与核心资产，验证估值与盈利是否支持长期持有。",
            "core_assumptions": ["指数整体 ROE 保持向上趋势"],
            "supporting_conditions": ["波动率收窄且成分股基本面未见恶化"],
            "invalidation_conditions": ["指数估值分位持续处于历史极高分位"],
            "observation_metrics": ["沪深 300 市盈率历史分位"],
        },
    )
    assert thesis.status_code == 201
    thesis_id = thesis.json()["thesis_id"]

    evidence = test_client.post(
        "/evidence",
        headers=headers,
        json={
            "evidence_id": str(uuid4()),
            "tenant_id": tenant,
            "subject_refs": ["instrument:CN:ETF:510300"],
            "evidence_type": "analysis",
            "source_name": "fixture-valuation",
            "license_status": "test-fixture",
            "summary": "沪深 300 估值分位已升至历史高位，接近极端区间",
            "content_hash": "cafe" + "0" * 28,
            "relation": "contradicting",
            "freshness": "as_of_fixture",
            "status": "active",
        },
    )
    assert evidence.status_code == 201

    pending = test_client.get("/notifications/pending", headers=headers)
    assert pending.status_code == 200
    payload = pending.json()
    assert len(payload) == 1
    notification = payload[0]
    assert notification["condition_kind"] == "invalidation_condition"
    assert notification["action_mode"] == "review_plan"
    assert notification["thesis_id"] == thesis_id
    assert notification["evidence_refs"] == [evidence.json()["evidence_id"]]
    assert "极高分位" in notification["triggered_by"]


def test_evidence_is_tenant_scoped_and_immutable(client):
    test_client, headers = client
    session = test_client.get("/session", headers=headers).json()
    body = {
        "evidence_id": str(uuid4()),
        "tenant_id": session["user_id"],
        "subject_refs": ["instrument:CN:ETF:510300"],
        "evidence_type": "quote",
        "source_name": "fixture-market",
        "license_status": "test-fixture",
        "summary": "测试行情证据",
        "content_hash": "0123456789abcdef0123456789abcdef",
        "relation": "unknown",
        "freshness": "as_of_fixture",
        "status": "active",
    }
    assert test_client.post("/evidence", headers=headers, json=body).status_code == 201
    assert test_client.post("/evidence", headers=headers, json=body).status_code == 409
    foreign = {**body, "evidence_id": str(uuid4()), "tenant_id": str(uuid4())}
    assert test_client.post("/evidence", headers=headers, json=foreign).status_code == 403


def test_plugin_catalog_installation_and_market_data_lifecycle(client):
    test_client, headers = client

    catalog = test_client.get("/plugins/catalog", headers=headers)
    assert catalog.status_code == 200
    entries = {entry["manifest"]["plugin_id"]: entry for entry in catalog.json()}
    assert {
        "official.cn-market-data",
        "official.portfolio-health",
        "official.investing-learning",
    } <= entries.keys()
    assert entries["official.cn-market-data"]["installation"]["state"] == "enabled"

    disabled = test_client.post("/plugins/official.cn-market-data/disable", headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["state"] == "disabled"
    assert test_client.get("/market/candles/510300", headers=headers).status_code == 409

    installed = test_client.post("/plugins/official.portfolio-health/install", headers=headers)
    assert installed.status_code == 200
    assert installed.json()["state"] == "enabled"
    assert installed.json()["release_version"] == "0.1.0"
    assert installed.json()["granted_capabilities"] == ["read_portfolio", "read_thesis", "run_analysis"]

    enabled = test_client.post("/plugins/official.cn-market-data/install", headers=headers)
    assert enabled.status_code == 200
    candles = test_client.get("/market/candles/510300", headers=headers)
    assert candles.status_code == 200
    payload = candles.json()
    assert payload["instrument"] == "CN:ETF:510300"
    assert payload["source_plugin_id"] == "official.cn-market-data"
    # 网络可达返回真实行情（is_demo=False）；两个行情源都不可达才降级演示（is_demo=True）。
    # 脱机降级路径由 test_quality_gates 显式打桩覆盖；这里只校验两种形态共有的 OHLC 不变量，
    # 不绑定本机网络可达性。
    assert isinstance(payload["is_demo"], bool)
    assert len(payload["bars"]) >= 20
    assert payload["bars"][0]["low"] <= min(payload["bars"][0]["open"], payload["bars"][0]["close"])


def _create_holding(test_client, headers, instrument="CN:ETF:510300", label="沪深 300 ETF"):
    return test_client.post(
        "/holdings",
        headers=headers,
        json={"instrument": instrument, "label": label, "status": "watchlist"},
    )


def test_learning_unit_empty_without_holdings_does_not_fabricate(client):
    test_client, headers = client
    unit = test_client.get("/learning/unit/today", headers=headers)
    assert unit.status_code == 200
    assert unit.json() is None  # 空态：无持仓/自选时不伪造学习内容
    goal = test_client.get("/learning/goals/current", headers=headers)
    assert goal.status_code == 200
    assert goal.json()["completed_count"] == 0
    assert goal.json()["target_count"] >= 1


def test_learning_exercise_binds_own_holding(client):
    test_client, headers = client
    _create_holding(test_client, headers)

    thesis = test_client.post(
        "/thesis",
        headers=headers,
        json={
            "instrument": "CN:ETF:510300",
            "original_statement": "低成本分散参与核心资产",
            "supporting_conditions": ["波动率收窄"],
            "invalidation_conditions": ["估值分位持续极高位"],
        },
    )
    assert thesis.status_code == 201

    unit = test_client.get("/learning/unit/today", headers=headers)
    assert unit.status_code == 200
    payload = unit.json()
    assert payload is not None
    assert payload["bound_instrument"] == "CN:ETF:510300"
    assert payload["bound_instrument_label"] == "沪深 300 ETF"
    assert payload["source_plugin_id"] == "official.investing-learning"
    assert set(payload["related_conditions"]) == {"波动率收窄", "估值分位持续极高位"}
    combined = (payload["content"] or "") + (payload["guidance"] or "")
    assert "建议买入" not in combined and "建议卖出" not in combined


def test_learning_activity_save_delete_export(client):
    test_client, headers = client
    _create_holding(test_client, headers)
    unit = test_client.get("/learning/unit/today", headers=headers).json()

    created = test_client.post(
        "/learning/activities",
        headers=headers,
        json={
            "unit_id": unit["unit_id"],
            "objective": unit["objective"],
            "bound_instrument": unit["bound_instrument"],
            "bound_instrument_label": unit["bound_instrument_label"],
            "user_answer": "我重述了失效条件。",
            "reflection": "我发现应改用可比估值分位而非名义分位。",
        },
    ).json()
    assert created["bound_instrument"] == unit["bound_instrument"]
    assert created["reflection"] == "我发现应改用可比估值分位而非名义分位。"

    listed = test_client.get("/learning/activities", headers=headers).json()
    assert listed[0]["activity_id"] == created["activity_id"]

    export = test_client.get("/learning/reflections/export", headers=headers)
    assert export.status_code == 200
    assert export.json()[0]["reflection"].startswith("我发现")

    goal = test_client.get("/learning/goals/current", headers=headers).json()
    assert goal["completed_count"] == 1

    deleted = test_client.delete(f"/learning/activities/{created['activity_id']}", headers=headers)
    assert deleted.status_code == 204
    assert test_client.get("/learning/activities", headers=headers).json() == []


def test_learning_reflection_proposes_draft_but_never_auto_activates(client):
    test_client, headers = client
    _create_holding(test_client, headers)

    base = test_client.post(
        "/investment-policies",
        headers=headers,
        json={"investment_goal": "长期可复盘的 ETF 组合"},
    ).json()
    test_client.post(
        f"/investment-policies/{base['policy_id']}/confirm",
        headers=headers,
        json={"confirmation_summary": "确认"},
    )

    unit = test_client.get("/learning/unit/today", headers=headers).json()

    empty = test_client.post(
        "/learning/activities",
        headers=headers,
        json={"unit_id": unit["unit_id"], "objective": unit["objective"], "reflection": "  "},
    ).json()
    assert (
        test_client.post(
            f"/learning/activities/{empty['activity_id']}/propose-policy-change", headers=headers
        ).status_code
        == 409
    )

    with_refl = test_client.post(
        "/learning/activities",
        headers=headers,
        json={
            "unit_id": unit["unit_id"],
            "objective": unit["objective"],
            "reflection": "观察框架需加上流动性失效条件。",
        },
    ).json()

    draft = test_client.post(
        f"/learning/activities/{with_refl['activity_id']}/propose-policy-change", headers=headers
    )
    assert draft.status_code == 201
    draft_payload = draft.json()
    assert draft_payload["status"] == "draft"
    assert draft_payload["source_learning_activity_ids"] == [with_refl["activity_id"]]
    assert draft_payload["investment_goal"] == "长期可复盘的 ETF 组合"

    policies = test_client.get("/investment-policies", headers=headers).json()
    assert [p["status"] for p in policies if p["policy_id"] == draft_payload["policy_id"]] == [
        "draft"
    ]

    confirmed = test_client.post(
        f"/investment-policies/{draft_payload['policy_id']}/confirm",
        headers=headers,
        json={"confirmation_summary": "经学习后显式确认修订"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "active"
