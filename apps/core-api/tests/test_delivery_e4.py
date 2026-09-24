"""E4 · 通知投递通道契约测试。

覆盖：
1. 未配置通道 → 尽力投递返回 delivered=False，保持站内 pending（绝不报错）；
2. 配置 `notify_webhook` → 经钉钉 webhook 投递，回执 delivered=True；
3. 投递失败 → 返回 delivered=False + 错误详情，绝不向上抛出（不影响评估主流程）；
4. `channel_status` / `GET /notifications/channels` 反映配置态（可观测，非死代码）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import delivery
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Notification


class _Store:
    def __init__(self, mapping: dict[str, str] | None = None):
        self._d = mapping or {}

    def get(self, key_id: str) -> str | None:
        return self._d.get(key_id)


def _notification() -> Notification:
    return Notification(
        notification_id=uuid4(),
        user_id=uuid4(),
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
    app = create_app(CoreSettings(session_token="t", data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": "t"}


def test_unconfigured_channel_stays_in_app_pending():
    receipts = delivery.deliver_notification(_notification(), _Store())
    assert receipts == [
        {
            "channel": "notify_webhook",
            "kind": "dingtalk",
            "delivered": False,
            "detail": "未配置投递通道，保持站内 pending",
        }
    ]


def test_configured_channel_delivers(monkeypatch):
    sent: list[Notification] = []

    def _fake_post(url, notification, timeout):
        sent.append(notification)
        assert url == "https://oapi.dingtalk.com/robot/send?access_token=abc"
        return b'{"errcode":0}'

    monkeypatch.setattr(delivery, "_post_dingtalk", _fake_post)
    receipts = delivery.deliver_notification(_notification(), _Store({"notify_webhook": "https://oapi.dingtalk.com/robot/send?access_token=abc"}))
    assert receipts[0]["delivered"] is True
    assert receipts[0]["detail"] == "已投递钉钉 Webhook"
    assert len(sent) == 1


def test_delivery_failure_reports_without_raising(monkeypatch):
    def _fail_post(url, notification, timeout):
        raise OSError("connection reset")

    monkeypatch.setattr(delivery, "_post_dingtalk", _fail_post)
    receipts = delivery.deliver_notification(_notification(), _Store({"notify_webhook": "https://x"}))
    assert receipts[0]["delivered"] is False
    assert "connection reset" in receipts[0]["detail"]


def test_channel_status_reflects_configuration():
    off = delivery.channel_status(_Store())
    on = delivery.channel_status(_Store({"notify_webhook": "https://x"}))
    assert off[0]["channel"] == "notify_webhook"
    assert off[0]["configured"] is False
    assert on[0]["configured"] is True
    assert on[0]["kind"] == "dingtalk"


def test_channels_endpoint_shape(client):
    test_client, headers = client
    response = test_client.get("/notifications/channels", headers=headers)
    assert response.status_code == 200
    assert response.json()[0]["channel"] == "notify_webhook"
    assert response.json()[0]["configured"] is False