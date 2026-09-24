"""阶段 D 主动落库端点契约：日报生成落库、证据新鲜度只审不改、通知评估去重落库。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.notifications import notification_dedup_key
from investment_steward_core.storage import Database


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}, Database(tmp_path / "steward.sqlite3")


def _evidence(**overrides):
    body = {
        "evidence_id": str(uuid4()),
        "subject_refs": ["instrument:CN:ETF:510300"],
        "evidence_type": "quote",
        "source_name": "fixture-worker",
        "summary": "沪深300ETF 资金净流出超阈值需警惕",
        "content_hash": str(uuid4()) + str(uuid4()),
        "relation": "supporting",
        "status": "active",
    }
    body.update(overrides)
    return body


def test_today_brief_generate_persists_and_audits(client):
    test_client, headers, _db = client
    response = test_client.post("/brief/today/generate", headers=headers)
    assert response.status_code == 200
    brief = response.json()
    assert brief["brief_id"]
    assert brief["has_personalization"] is False

    # 落库：再 GET 应返回同一份（不再重新生成）
    fetched = test_client.get("/brief/today", headers=headers).json()
    assert fetched["brief_id"] == brief["brief_id"]

    actions = {item["action"] for item in test_client.get("/audit", headers=headers).json()}
    assert "brief.today.persisted" in actions


def test_freshness_patrol_reports_expired_without_mutation(client):
    test_client, headers, _db = client
    session = test_client.get("/session", headers=headers).json()
    expired = _evidence(tenant_id=session["user_id"], valid_until=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    assert test_client.post("/evidence", headers=headers, json=expired).status_code == 201

    result = test_client.post("/evidence/freshness/patrol", headers=headers).json()
    assert result["checked"] >= 1
    assert result["stale"] >= 1
    assert expired["evidence_id"] in result["stale_ids"]

    # 只审计不改写账本：证据状态仍为 active（不可变）
    stored = next(
        item for item in test_client.get("/evidence", headers=headers).json()
        if item["evidence_id"] == expired["evidence_id"]
    )
    assert stored["status"] == "active"

    actions = {item["action"] for item in test_client.get("/audit", headers=headers).json()}
    assert "evidence.freshness.patrolled" in actions


def test_notification_evaluate_deduplicates_on_disk(client):
    test_client, headers, db = client
    session = test_client.get("/session", headers=headers).json()
    thesis = test_client.post(
        "/thesis",
        headers=headers,
        json={
            "instrument": "CN:ETF:510300",
            "original_statement": "沪深300 长期配置逻辑成立",
            "supporting_conditions": [],
            "invalidation_conditions": ["资金净流出超阈值"],
        },
    ).json()
    evidence = _evidence(tenant_id=session["user_id"])
    assert test_client.post("/evidence", headers=headers, json=evidence).status_code == 201

    first = test_client.post("/notifications/evaluate", headers=headers).json()
    assert isinstance(first, list) and len(first) >= 1
    second = test_client.post("/notifications/evaluate", headers=headers).json()
    assert len(second) == len(first)

    # 去重落在 DB：同一 thesis 在通知表只有一行（相同 dedup_key，重复评估不产生重复行）
    stored = db.list_notifications(UUID(session["user_id"]))
    matched = [n for n in stored if str(n.thesis_id) == thesis["thesis_id"]]
    assert len(matched) == 1

    actions = {item["action"] for item in test_client.get("/audit", headers=headers).json()}
    assert "notification.rules.re-evaluated" in actions


def test_notification_delivery_failure_is_visible_and_retries_after_backoff(client, monkeypatch):
    import investment_steward_core.api.app as app_module

    test_client, headers, db = client
    session = test_client.get("/session", headers=headers).json()
    test_client.post(
        "/thesis",
        headers=headers,
        json={
            "instrument": "CN:ETF:510300",
            "original_statement": "沪深300 长期配置逻辑成立",
            "supporting_conditions": [],
            "invalidation_conditions": ["资金净流出超阈值"],
        },
    )
    evidence = _evidence(tenant_id=session["user_id"])
    assert test_client.post("/evidence", headers=headers, json=evidence).status_code == 201

    calls = 0

    def fake_delivery(notification, _credentials):
        nonlocal calls
        calls += 1
        delivered = calls > 1
        return [{"channel": "notify_webhook", "delivered": delivered, "detail": "模拟投递"}]

    monkeypatch.setattr(app_module, "deliver_notification", fake_delivery)
    first = test_client.post("/notifications/evaluate", headers=headers)
    assert first.status_code == 200
    failed = first.json()[0]
    assert failed["delivery_status"] == "failed"
    assert failed["delivery_attempts"] == 1
    assert failed["next_retry_at"] is not None
    assert calls == 1

    # 退避窗口内重复评估不再次外呼，但保留失败可见状态。
    pending = test_client.post("/notifications/evaluate", headers=headers).json()[0]
    assert pending["delivery_status"] == "failed"
    assert pending["delivery_attempts"] == 1
    assert calls == 1

    stored = db.list_notifications(UUID(session["user_id"]))[0]
    retryable = stored.model_copy(update={"next_retry_at": datetime.now(UTC) - timedelta(minutes=1)})
    db.upsert_notification(retryable, notification_dedup_key(retryable))

    retried = test_client.post("/notifications/evaluate", headers=headers).json()[0]
    assert retried["delivery_status"] == "delivered"
    assert retried["delivery_attempts"] == 2
    assert retried["next_retry_at"] is None
    assert calls == 2
