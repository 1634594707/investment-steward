"""个人中心通知偏好 · 生效链路测试（2026-09-07 接入通知管道）。

覆盖：
1. 免打扰时段内：POST /notifications/evaluate 跳过外部投递（保持 pending、不计退避），
   GET /notifications/pending 不外露（落库未读保留，窗口结束后恢复可见）；
2. 免打扰窗口外：投递照常尝试（未配置通道按 E4 语义 failed），站内可见；
3. in_app_enabled=false：站内列表恒为空，外部投递不受影响；
4. external_enabled=false：不投递、不退避，last_delivery_error 如实标注原因。
免打扰按本机时区 HH:MM 判断；测试窗口由「当前本机时刻 ± 分钟」构造，兼容跨零点窗口。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _session_user(test_client, headers) -> object:
    return test_client.get("/session", headers=headers).json()["user_id"]


def _quiet_window_covering_now() -> tuple[str, str]:
    now = datetime.now().astimezone()
    start = (now - timedelta(minutes=1)).strftime("%H:%M")
    end = (now + timedelta(minutes=1)).strftime("%H:%M")
    return start, end


def _window_not_covering_now() -> tuple[str, str]:
    now = datetime.now().astimezone()
    minute_of_day = now.hour * 60 + now.minute
    if minute_of_day <= 1137:
        start, end = minute_of_day + 2, minute_of_day + 3
    else:
        start, end = minute_of_day - 120, minute_of_day - 60
    fmt = lambda value: f"{value // 60:02d}:{value % 60:02d}"
    return fmt(start), fmt(end)


def test_quiet_hours_holds_delivery_and_hides_in_app(client, monkeypatch):
    test_client, headers = client
    start, end = _quiet_window_covering_now()
    saved = test_client.put(
        "/personal/settings", headers=headers,
        json={"display_name": "甲", "notify": {"quiet_hours_enabled": True, "quiet_start": start, "quiet_end": end}},
    )
    assert saved.status_code == 200

    user_id = _session_user(test_client, headers)
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [_notification(user)])
    monkeypatch.setattr(
        app_module, "deliver_notification",
        lambda n, store: (_ for _ in ()).throw(AssertionError("免打扰时段不应尝试投递")),
    )

    evaluated = test_client.post("/notifications/evaluate", headers=headers)
    assert evaluated.status_code == 200
    item = evaluated.json()[0]
    assert item["delivery_status"] == "pending"
    assert item["next_retry_at"] is None
    assert "免打扰" in (item["last_delivery_error"] or "")

    assert test_client.get("/notifications/pending", headers=headers).json() == []


def test_outside_quiet_hours_delivery_attempted_and_in_app_visible(client, monkeypatch):
    test_client, headers = client
    start, end = _window_not_covering_now()
    saved = test_client.put(
        "/personal/settings", headers=headers,
        json={"notify": {"quiet_hours_enabled": True, "quiet_start": start, "quiet_end": end}},
    )
    assert saved.status_code == 200

    user_id = _session_user(test_client, headers)
    stable = _notification(user_id)  # evaluate 与 GET 两次调用返回同一实例，保证 dedup 一致
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [stable])
    monkeypatch.setattr(app_module, "deliver_notification", lambda n, store: [
        {"channel": "notify_webhook", "kind": "dingtalk", "delivered": False, "detail": "未配置投递通道，保持站内 pending"}
    ])

    evaluated = test_client.post("/notifications/evaluate", headers=headers).json()
    assert evaluated[0]["delivery_status"] == "failed"  # 未配置通道按 E4 原语义，不受免打扰影响

    pending = test_client.get("/notifications/pending", headers=headers).json()
    assert len(pending) == 1


def test_in_app_disabled_hides_pending_but_external_still_tried(client, monkeypatch):
    test_client, headers = client
    saved = test_client.put(
        "/personal/settings", headers=headers,
        json={"notify": {"in_app_enabled": False}},
    )
    assert saved.status_code == 200

    user_id = _session_user(test_client, headers)
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [_notification(user)])
    monkeypatch.setattr(app_module, "deliver_notification", lambda n, store: [
        {"channel": "notify_webhook", "kind": "dingtalk", "delivered": False, "detail": "未配置投递通道，保持站内 pending"}
    ])

    evaluated = test_client.post("/notifications/evaluate", headers=headers).json()
    assert evaluated[0]["delivery_status"] == "failed"  # 站内开关不影响外部投递
    assert test_client.get("/notifications/pending", headers=headers).json() == []


def test_external_disabled_skips_delivery_without_backoff(client, monkeypatch):
    test_client, headers = client
    saved = test_client.put(
        "/personal/settings", headers=headers,
        json={"notify": {"external_enabled": False}},
    )
    assert saved.status_code == 200

    user_id = _session_user(test_client, headers)
    monkeypatch.setattr(app_module, "evaluate_notifications", lambda db, user: [_notification(user)])
    monkeypatch.setattr(
        app_module, "deliver_notification",
        lambda n, store: (_ for _ in ()).throw(AssertionError("外部通道关闭时不应尝试投递")),
    )

    evaluated = test_client.post("/notifications/evaluate", headers=headers).json()
    assert evaluated[0]["delivery_status"] == "pending"
    assert evaluated[0]["next_retry_at"] is None
    assert "外部通道" in (evaluated[0]["last_delivery_error"] or "")
