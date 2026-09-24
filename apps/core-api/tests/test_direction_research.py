"""AI 方向研判测试（POST /evidence/direction-research；v33 路线图更新契约）。

v33 起模型输出 `report`（十小节正文）+ 结构化判断/候选池；`direction_summary` 作为
兼容字段保留同一正文。本文件保留旧链路行为断言：无「使用中」方案 409（_resolve_model_profile
同口径）、非法 pool 行剔除、无法解析输出按 parse 失败如实标注。
"""

from __future__ import annotations

import json

from conftest import client as client_fixture  # noqa: F401


def _file_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _setup_active_profile(test_client, headers) -> None:
    test_client.put("/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers)
    created = test_client.post(
        "/model-profiles",
        json={"name": "Deepseek官方", "base_url": "https://api.deepseek.com",
              "model": "deepseek-v4-flash", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    assert test_client.post(f"/model-profiles/{created['profile_id']}/activate", headers=headers).status_code == 200


def test_direction_research_requires_active_profile(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    response = test_client.post("/evidence/direction-research", json={"topic": "AI 芯片"}, headers=headers)
    assert response.status_code == 409
    assert "使用中" in response.json()["detail"]


def test_direction_research_success_with_pool(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    sections = "".join(
        f"## {name}\n本节给出可核对的数据与推理边界，避免空泛表述。\n"
        for name in __import__("investment_steward_core").direction_research.REQUIRED_DIRECTION_SECTIONS
    )
    payload = json.dumps({
        "title": "AI 芯片：国产替代加速",
        "executive_summary": "方向判断：国产替代加速。最强证据：代工订单饱满。最大不确定性：设备良率爬坡节奏。",
        "report": sections,
        "core_judgments": [
            {"id": "J1", "text": "国产替代加速", "kind": "industry", "confidence": "medium"},
            {"id": "J2", "text": "代工环节格局优于设计", "kind": "competitiveness", "confidence": "low"},
            {"id": "J3", "text": "主题热度高位", "kind": "sentiment", "confidence": "low"},
        ],
        "catalysts": ["算力需求扩张", "国产替代政策"],
        "risks": ["技术迭代不及预期", "产能过剩"],
        "stock_pool": [
            {"symbol_raw": "600001", "name": "甲股", "business_link": "算力龙头", "profit_path": "订单转化收入"},
            {"symbol_raw": "600002", "name": "乙股", "business_link": "封测环节", "profit_path": "产能利用率提升"},
            {"invalid": "row"},
        ],
        "data_gaps": ["行业设备招标数据未接入"],
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": payload}}]})
    response = test_client.post(
        "/evidence/direction-research",
        json={"topic": "AI 芯片", "question": "国产替代进度"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["topic"] == "AI 芯片"
    assert "国产替代加速" in body["executive_summary"]  # v33：摘要单独成字段
    assert body["report"] == body["direction_summary"]
    assert len(body["catalysts"]) == 2 and len(body["risks"]) == 2
    # 非法 pool 行被如实剔除；v33：候选带证券身份归一
    assert [item["symbol"] for item in body["stock_pool"]] == ["600001", "600002"]
    assert body["stock_pool"][0]["identity_status"] == "verified"
    # v33：质量三态与调用身份随响应
    assert body["quality_status"] in ("complete", "needs_review", "incomplete")
    assert body["generation_trace"]["prompt_version"]
    assert body["model"] == "deepseek-v4-flash"


def test_direction_research_rejects_unparseable_output(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": "抱歉，我无法输出 JSON。"}}]},
    )
    body = test_client.post("/evidence/direction-research", json={"topic": "AI 芯片"}, headers=headers).json()
    assert body["ok"] is False and body["stage"] == "parse"
