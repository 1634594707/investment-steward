"""G6-2 量化分享池骨架端点：阶段未开放时返回空态可用性，不编造制品、不按收益率排序。"""

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


def test_quant_artifacts_empty_available(client):
    """阶段 A 未开放：GET /quant/artifacts 返回 available=False 空态，绝不返回虚构制品。"""
    test_client, headers = client
    res = test_client.get("/quant/artifacts", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["stage"] == "A"
    assert body["stage_label"]
    assert body["artifacts"] == []
    assert body["degraded_reason"]
    assert "口径" in body["notice"] or "不出现" in body["notice"]


def test_quant_create_run_rejected(client):
    """契约未冻结：POST /quant/runs 拒绝创建回测运行（409），不产生任何制品。"""
    test_client, headers = client
    res = test_client.post("/quant/runs", headers=headers, json={})
    assert res.status_code == 409
    assert "未开放" in res.json()["detail"]


def test_quant_lineage_empty(client):
    """无制品：GET /quant/lineage/{artifact_id} 返回空谱系，不编造上游关系。"""
    test_client, headers = client
    res = test_client.get("/quant/lineage/steward/cn-equity-mr-baseline", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["artifact_id"] == "steward/cn-equity-mr-baseline"
    assert body["lineage"] == []