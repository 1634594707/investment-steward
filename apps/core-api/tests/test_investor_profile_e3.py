"""E3 · InvestorProfile 消费契约测试。

覆盖三端：
1. 首次访问：未建立画像时 GET /investor/profile 返回 `null`（供前端识别「首次引导」）；
2. PUT 建立/更新画像：持久化后可回读，user_id 与会话本地用户一致；
3. 幂等更新：再度 PUT 保留原 created_at、仅刷新 updated_at 与内容。
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


def test_first_visit_returns_null_for_first_run_guide(client):
    test_client, headers = client
    response = test_client.get("/investor/profile", headers=headers)
    assert response.status_code == 200
    assert response.json() is None


def test_upsert_persists_profile_and_matches_user(client):
    test_client, headers = client
    user_id = str(test_client.get("/session", headers=headers).json()["user_id"])

    response = test_client.put(
        "/investor/profile",
        headers=headers,
        json={
            "investment_goal": "稳健增值",
            "horizon_years": 5.0,
            "markets_and_assets": ["A股ETF", "低波动"],
            "consent": {"data_local": True, "share_aggregated": False},
        },
    )
    assert response.status_code == 200
    profile = response.json()
    assert profile["user_id"] == user_id
    assert profile["investment_goal"] == "稳健增值"
    assert profile["markets_and_assets"] == ["A股ETF", "低波动"]

    fetched = test_client.get("/investor/profile", headers=headers).json()
    assert fetched == profile


def test_repeated_upsert_preserves_created_at_and_updates_content(client):
    test_client, headers = client
    first = test_client.put(
        "/investor/profile",
        headers=headers,
        json={"investment_goal": "激进成长", "horizon_years": 10.0},
    ).json()

    second = test_client.put(
        "/investor/profile",
        headers=headers,
        json={"investment_goal": "均衡配置", "horizon_years": 8.0},
    ).json()

    assert second["created_at"] == first["created_at"]
    assert second["investment_goal"] == "均衡配置"
    assert second["horizon_years"] == 8.0


def test_research_consumes_profile_without_profile_local_path(client):
    """未建画像时本地确定性研究引擎如实标注「未提供个性化视角」，不编造。"""
    test_client, headers = client
    run = test_client.post(
        "/research/runs", headers=headers, json={"user_question": "复盘 510300"}
    ).json()
    r = test_client.post(f"/research/runs/{run['run_id']}/run", headers=headers)
    assert r.status_code == 200
    answer = r.json()
    assert any("未建立投资者画像" in line for line in answer["limitations"])


def test_research_consumes_profile_with_goal_reflected(client):
    """建画像后本地引擎在局限说明中如实复述投资目标，作为参考视角而非买卖建议。"""
    test_client, headers = client
    test_client.put(
        "/investor/profile",
        headers=headers,
        json={"investment_goal": "稳健增值", "horizon_years": 5.0},
    )
    run = test_client.post(
        "/research/runs", headers=headers, json={"user_question": "复盘 510300"}
    ).json()
    r = test_client.post(f"/research/runs/{run['run_id']}/run", headers=headers)
    assert r.status_code == 200
    answer = r.json()
    assert any("稳健增值" in line for line in answer["limitations"])
    assert any("5 年" in line for line in answer["limitations"])