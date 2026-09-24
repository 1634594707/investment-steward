"""M5.8 发言人信号流测试（§3.5 段一/段二，D-13）。

红线锁定：
- 沟通证据 = 官方原文粘贴（官方优先、转载明示），仅本机；
- AI 解读必须逐字引用原文句子，无引用不回填方向（ADR-0006）；
- 解读只提供依据，权重调整仍走 M5.5 提议卡/手动滑杆（不在本模块改权重）。
"""

from __future__ import annotations

import json

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用
from investment_steward_core.macro_ai import verify_speaker_citations

EXCERPT = (
    "通胀进展是不平衡的，服务通胀仍然黏着。"
    "与此同时，就业市场的下行风险有所上升，我们将密切关注。"
    "政策没有预设路径，将依赖数据逐会决策。"
)

BODY = {
    "speaker": "鲍威尔",
    "event_type": "FOMC 记者会",
    "event_date": "2026-08-12",
    "source_name": "官方",
    "source_url": "https://www.federalreserve.gov/transcripts.htm",
    "excerpt": EXCERPT,
}


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
    test_client.put(
        "/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers,
    )
    created = test_client.post(
        "/model-profiles",
        json={"name": "Deepseek官方", "base_url": "https://api.deepseek.com",
              "model": "deepseek-v4-flash", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    test_client.post(f"/model-profiles/{created['profile_id']}/activate", headers=headers)


# ---- 纯函数：原文引用校验 ----

def test_speaker_citation_verifier_accepts_verbatim_and_rejects_paraphrase():
    rationale = f"「{EXCERPT.split('。')[0]}。」——据此判断方向为鹰派。"
    assert verify_speaker_citations(rationale, EXCERPT)
    # 转述（意思相近但非逐字）→ 拒绝
    paraphrase = "通胀进展不平衡且服务通胀黏着，因此方向为鹰派。"
    assert verify_speaker_citations(paraphrase, EXCERPT) == []
    # 完全无关 → 拒绝
    assert verify_speaker_citations("政策将大幅转向宽松。", EXCERPT) == []


# ---- 端点：录入 / 列表 / 门禁 ----

def test_signal_requires_enabled_plugin(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/evidence/macro/us/signals", json=BODY, headers=headers).status_code == 409
    assert test_client.get("/evidence/macro/us/signals", headers=headers).status_code == 409


def test_signal_region_whitelist_and_create_list(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    assert test_client.post("/evidence/macro/xx/signals", json=BODY, headers=headers).status_code == 422

    created = test_client.post("/evidence/macro/us/signals", json=BODY, headers=headers)
    assert created.status_code == 201, created.text
    signal = created.json()
    assert signal["direction"] is None  # 录入时方向留空（段二由 AI 解读回填）
    assert signal["signal_id"]

    listed = test_client.get("/evidence/macro/us/signals", headers=headers).json()
    assert len(listed) == 1 and listed[0]["signal_id"] == signal["signal_id"]
    assert listed[0]["source_name"] == "官方"  # D-13：官方优先

    # 转载来源可录入（降级但明示）
    repost = {**BODY, "source_name": "转载", "speaker": "中国央行"}
    assert test_client.post("/evidence/macro/cn/signals", json=repost, headers=headers).status_code == 201


# ---- 端点：AI 解读（引用校验硬门禁） ----

def _mock_model(monkeypatch, content: str) -> None:
    from investment_steward_core import model_client as mc

    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": content}}]})


def test_interpret_requires_active_profile(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    signal_id = test_client.post("/evidence/macro/us/signals", json=BODY, headers=headers).json()["signal_id"]
    response = test_client.post(f"/evidence/macro/us/signals/{signal_id}/interpret", headers=headers)
    assert response.status_code == 409  # 无「使用中」模型方案


def test_interpret_success_backfills_direction_with_citation(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    signal_id = test_client.post("/evidence/macro/us/signals", json=BODY, headers=headers).json()["signal_id"]

    quoted = EXCERPT.split("。")[1]  # 「与此同时，就业市场的下行风险有所上升，我们将密切关注」
    content = json.dumps({
        "direction": "鸽派",
        "rationale": f"原文称「{quoted}」，关注点从通胀转向就业下行风险，方向为鸽派。",
        "focus_shift": "从通胀转向就业下行风险",
    }, ensure_ascii=False)
    _mock_model(monkeypatch, content)

    response = test_client.post(f"/evidence/macro/us/signals/{signal_id}/interpret", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["signal"]["direction"] == "鸽派"
    assert body["signal"]["model"] == "deepseek-v4-flash"
    assert body["citations"]  # 回填依据 = 原文引用句
    # 列表里也能读到方向
    listed = test_client.get("/evidence/macro/us/signals", headers=headers).json()
    assert listed[0]["direction"] == "鸽派"


def test_interpret_rejects_uncited_rationale_and_keeps_signal(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    signal_id = test_client.post("/evidence/macro/us/signals", json=BODY, headers=headers).json()["signal_id"]

    content = json.dumps({
        "direction": "鹰派",
        "rationale": "发言人强烈暗示将继续大幅加息以抑制通胀。（纯属转述+编造，无原文引用）",
        "focus_shift": "无",
    }, ensure_ascii=False)
    _mock_model(monkeypatch, content)

    response = test_client.post(f"/evidence/macro/us/signals/{signal_id}/interpret", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False and body["stage"] == "citation_check"
    assert body["signal"]["direction"] is None  # 信号保持原样，方向未回填

    # JSON 解析失败同样拒绝
    _mock_model(monkeypatch, "我觉得是鹰派。")
    body2 = test_client.post(f"/evidence/macro/us/signals/{signal_id}/interpret", headers=headers).json()
    assert body2["ok"] is False and body2["stage"] == "json_parse"

    # 缺字段同样拒绝
    _mock_model(monkeypatch, json.dumps({"direction": "鹰派"}, ensure_ascii=False))
    body3 = test_client.post(f"/evidence/macro/us/signals/{signal_id}/interpret", headers=headers).json()
    assert body3["ok"] is False and body3["stage"] == "format_check"


def test_interpret_unknown_signal_404(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    response = test_client.post("/evidence/macro/us/signals/deadbeefdeadbeef/interpret", headers=headers)
    assert response.status_code == 404
