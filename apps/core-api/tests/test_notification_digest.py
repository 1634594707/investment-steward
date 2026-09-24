"""v21 M2-02：通知 digest 每日/每周汇总节奏测试。

覆盖：
1. daily 节奏、窗口未到：外部投递逐条保持 pending（标注汇总原因），不产出 digest；
2. daily 节奏、窗口已到：evaluate 聚合一条 digest（read=True 不进站内 pending），
   外发成功后被聚合提醒标记 delivered（已并入汇总）；
3. 周期去重：同窗口第二次 evaluate 不重复外发 digest（存储侧周期 dedup 键守卫）；
4. weekly 节奏：digest 标题/键锚定每周汇总语义；
5. realtime（默认）：不产生 digest，行为与既有语义一致。
时间控制：monkeypatch app_module.digest_window（_persist_evaluated 经模块全局引用）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.api import app as app_module
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Notification


def _notification(user_id) -> Notification:
    return Notification(
        notification_id=uuid4(),
        user_id=user_id,
        thesis_id=uuid4(),
        instrument="510300",
        triggered_by="跌破 MA60",
        condition_kind="invalidation_condition",
        title="510300 命中已确认失效条件",
        summary="新证据与已确认条件匹配，建议复核。",
        action_mode="review_plan",
        evidence_refs=[],
        created_at=datetime.now(UTC),
        data_time=datetime.now(UTC),
    )


@pytest.fixture()
def client(tmp_path: Path):
    token = "digest-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _is_digest(item: dict) -> bool:
    return str(item.get("triggered_by", "")).startswith("notify.digest")


_DELIVERED_RECEIPT = [{"channel": "notify_webhook", "kind": "dingtalk", "delivered": True, "detail": "已投递钉钉 Webhook"}]


def test_daily_before_window_holds_without_digest(client, monkeypatch):
    test_client, headers = client
    assert test_client.put(
        "/personal/settings", headers=headers, json={"notify": {"frequency": "daily"}}
    ).status_code == 200

    user_id = test_client.get("/session", headers=headers).json()["user_id"]
    stable = _notification(user_id)  # evaluate 与 GET 两次调用同一实例，保证 dedup 一致
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [stable])
    monkeypatch.setattr(app_module, "digest_window", lambda prefs, local_now: (False, "digest:daily:2099-01-01"))
    monkeypatch.setattr(
        app_module, "deliver_notification",
        lambda n, store: (_ for _ in ()).throw(AssertionError("窗口未到不应尝试外部投递")),
    )

    evaluated = test_client.post("/notifications/evaluate", headers=headers).json()
    assert all(not _is_digest(item) for item in evaluated), "窗口未到不应产出 digest"
    assert evaluated[0]["delivery_status"] == "pending"
    assert "汇总" in (evaluated[0]["last_delivery_error"] or "")
    assert evaluated[0]["next_retry_at"] is None, "汇总持有不计退避"
    # digest 只约束外部投递：站内呈现不受影响
    assert len(test_client.get("/notifications/pending", headers=headers).json()) == 1


def test_daily_at_window_aggregates_and_flushes(client, monkeypatch):
    test_client, headers = client
    assert test_client.put(
        "/personal/settings", headers=headers, json={"notify": {"frequency": "daily"}}
    ).status_code == 200

    user_id = test_client.get("/session", headers=headers).json()["user_id"]
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [_notification(user)])
    monkeypatch.setattr(app_module, "digest_window", lambda prefs, local_now: (True, "digest:daily:2099-01-01"))
    monkeypatch.setattr(app_module, "deliver_notification", lambda n, store: _DELIVERED_RECEIPT)

    evaluated = test_client.post("/notifications/evaluate", headers=headers).json()
    digests = [item for item in evaluated if _is_digest(item)]
    assert len(digests) == 1
    assert "每日通知汇总（1 条）" == digests[0]["title"]
    assert digests[0]["delivery_status"] == "delivered"
    assert digests[0]["read"] is True

    individuals = [item for item in evaluated if not _is_digest(item)]
    assert individuals[0]["delivery_status"] == "delivered"
    assert "汇总" in (individuals[0]["last_delivery_error"] or "")

    # digest 是外部投递载体，不在站内 pending 重复打扰
    pending = test_client.get("/notifications/pending", headers=headers).json()
    assert all(not _is_digest(item) for item in pending)


def test_daily_same_window_no_duplicate_digest(client, monkeypatch):
    test_client, headers = client
    assert test_client.put(
        "/personal/settings", headers=headers, json={"notify": {"frequency": "daily"}}
    ).status_code == 200

    user_id = test_client.get("/session", headers=headers).json()["user_id"]
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [_notification(user)])
    monkeypatch.setattr(app_module, "digest_window", lambda prefs, local_now: (True, "digest:daily:2099-01-01"))
    deliver_calls: list[Notification] = []

    def fake_deliver(n: Notification, store) -> list[dict]:
        deliver_calls.append(n)
        return _DELIVERED_RECEIPT

    monkeypatch.setattr(app_module, "deliver_notification", fake_deliver)
    test_client.post("/notifications/evaluate", headers=headers)
    assert len([n for n in deliver_calls if str(n.triggered_by).startswith("notify.digest")]) == 1

    # 同周期第二次评估：周期 dedup 键已存在且 delivered → 不再外发 digest
    test_client.post("/notifications/evaluate", headers=headers)
    digests_sent = [n for n in deliver_calls if str(n.triggered_by).startswith("notify.digest")]
    assert len(digests_sent) == 1, "同周期重复 evaluate 不应重复外发 digest"


def test_weekly_digest_uses_weekly_semantics(client, monkeypatch):
    test_client, headers = client
    assert test_client.put(
        "/personal/settings", headers=headers, json={"notify": {"frequency": "weekly"}}
    ).status_code == 200

    user_id = test_client.get("/session", headers=headers).json()["user_id"]
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [_notification(user)])
    monkeypatch.setattr(app_module, "digest_window", lambda prefs, local_now: (True, "digest:weekly:2099-01-05"))
    monkeypatch.setattr(app_module, "deliver_notification", lambda n, store: _DELIVERED_RECEIPT)

    evaluated = test_client.post("/notifications/evaluate", headers=headers).json()
    digests = [item for item in evaluated if _is_digest(item)]
    assert len(digests) == 1
    assert digests[0]["title"].startswith("每周通知汇总")


def test_realtime_frequency_never_produces_digest(client, monkeypatch):
    test_client, headers = client
    user_id = test_client.get("/session", headers=headers).json()["user_id"]
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [_notification(user)])
    monkeypatch.setattr(app_module, "digest_window", lambda prefs, local_now: (True, ""))
    monkeypatch.setattr(app_module, "deliver_notification", lambda n, store: [
        {"channel": "notify_webhook", "kind": "dingtalk", "delivered": False, "detail": "未配置投递通道，保持站内 pending"}
    ])

    evaluated = test_client.post("/notifications/evaluate", headers=headers).json()
    assert all(not _is_digest(item) for item in evaluated)
    # realtime 保持既有 E4 语义：未配置通道 → failed（可重试），不是汇总持有
    assert evaluated[0]["delivery_status"] == "failed"
