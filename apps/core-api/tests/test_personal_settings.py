"""个人中心设置（PersonalSettings）消费契约测试。

覆盖四件事：
1. 首次访问：从未保存时 GET /personal/settings 返回 `null`（前端据此回退本机默认值）；
2. PUT 建立/更新：持久化后可原样回读，user_id 与会话本地用户一致；
3. 幂等更新：再度 PUT 保留原 created_at，仅刷新 updated_at 与内容；
4. 入参边界：落地页白名单、头像底色、免打扰时段格式非法一律 422；未带会话令牌 401。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def test_first_visit_returns_null_before_any_save(client):
    test_client, headers = client
    response = test_client.get("/personal/settings", headers=headers)
    assert response.status_code == 200
    assert response.json() is None


def test_upsert_persists_settings_and_matches_user(client):
    test_client, headers = client
    user_id = str(test_client.get("/session", headers=headers).json()["user_id"])

    response = test_client.put(
        "/personal/settings",
        headers=headers,
        json={
            "display_name": "aplicity",
            "avatar_color": "#1d9e75",
            "default_view": "macro",
            "notify": {"quiet_hours_enabled": True, "quiet_start": "23:00", "quiet_end": "07:30"},
            "risk_profile": "balanced",
            "style_tags": ["长期持有", "指数定投"],
        },
    )
    assert response.status_code == 200
    settings = response.json()
    assert settings["user_id"] == user_id
    assert settings["display_name"] == "aplicity"
    assert settings["default_view"] == "macro"
    assert settings["notify"]["quiet_start"] == "23:00"
    assert settings["notify"]["in_app_enabled"] is True  # 未传字段走默认
    assert settings["style_tags"] == ["长期持有", "指数定投"]

    assert test_client.get("/personal/settings", headers=headers).json() == settings


def test_repeated_upsert_preserves_created_at_and_updates_content(client):
    test_client, headers = client
    first = test_client.put(
        "/personal/settings", headers=headers, json={"display_name": "甲", "default_view": "today"}
    ).json()

    second = test_client.put(
        "/personal/settings", headers=headers, json={"display_name": "乙", "default_view": "library"}
    ).json()

    assert second["created_at"] == first["created_at"]
    assert second["display_name"] == "乙"
    assert second["default_view"] == "library"


@pytest.mark.parametrize(
    "payload",
    [
        {"default_view": "not-a-view"},
        {"avatar_color": "blue"},
        {"notify": {"quiet_start": "25:00"}},
        {"notify": {"frequency": "hourly"}},
    ],
)
def test_invalid_payload_rejected_with_422(client, payload):
    test_client, headers = client
    response = test_client.put("/personal/settings", headers=headers, json=payload)
    assert response.status_code == 422


def test_endpoint_requires_session_token(client):
    test_client, _headers = client
    assert test_client.get("/personal/settings").status_code == 401
    assert test_client.put("/personal/settings", json={"display_name": "甲"}).status_code == 401


def test_blank_display_name_falls_back_to_default(client):
    """空名不落库为空白身份：服务端归一化为默认值，避免头像与问候语出现空串。"""
    test_client, headers = client
    saved = test_client.put(
        "/personal/settings", headers=headers, json={"display_name": "   "}
    ).json()
    assert saved["display_name"] == "投资人"
