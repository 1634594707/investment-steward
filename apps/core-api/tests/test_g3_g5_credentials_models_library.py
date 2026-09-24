"""G3 数据源与密钥 + G5 研读图书馆后端。

- G3-1/G3-4：凭据库端点（响应永不回传明文）+ 测试连接代理（密钥原文不出 Core、不进审计 payload）。
- G3-3：模型服务多方案，同一时刻恰好一个「使用中」。
- G5-1/G5-2/G5-3：书目 CRUD、荐读计划生成、批注转研究。
- G5-4：荐读计划只进学习流、永不写投资原则（pytest 锁定）。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.credential_store import DbCredentialStore

PLAIN_TOKEN = "a-secret-market-token-1234"


@pytest.fixture()
def file_client(tmp_path: Path, monkeypatch):
    """故意强制走文件兜底，避免在 CI/本机把测试密钥写进 OS 凭据管理器。

    app.py 以 `from ... import resolve_store` 直接绑定到 api.app 命名空间，
    故在此打补丁而非常用模块属性。
    """
    from investment_steward_core.api import app as api_app

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


# —— G3-1：凭据库，永不回传明文 ——


def test_credential_upsert_returns_meta_not_secret(file_client):
    test_client, headers = file_client
    resp = test_client.put(
        "/credentials/market_data_token",
        headers=headers,
        json={"secret": PLAIN_TOKEN},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_id"] == "market_data_token"
    assert body["last4"] == PLAIN_TOKEN[-4:]
    assert "updated_at" in body and "backend" in body
    raw = json.dumps(body)
    assert PLAIN_TOKEN not in raw  # 明文绝不回传
    assert "secret" not in body


def test_credential_get_list_delete_roundtrip(file_client):
    test_client, headers = file_client
    test_client.put("/credentials/market_data_token", headers=headers, json={"secret": PLAIN_TOKEN})
    test_client.put(
        "/credentials/notify_webhook", headers=headers, json={"secret": "https://example.com/hook"}
    )

    got = test_client.get("/credentials/market_data_token", headers=headers).json()
    assert got["last4"] == PLAIN_TOKEN[-4:]

    listing = test_client.get("/credentials", headers=headers).json()
    assert {entry["key_id"] for entry in listing} == {
        "market_data_token",
        "notify_webhook",
    }

    assert test_client.delete("/credentials/market_data_token", headers=headers).status_code == 204
    assert test_client.get("/credentials/market_data_token", headers=headers).status_code == 404


def test_credential_audit_never_contains_secret(file_client):
    test_client, headers = file_client
    test_client.put("/credentials/market_data_token", headers=headers, json={"secret": PLAIN_TOKEN})
    test_client.post("/credentials/market_data_token/test", headers=headers)
    audit = test_client.get("/audit", headers=headers).json()
    raw = json.dumps(audit)
    assert PLAIN_TOKEN not in raw
    # 审计只记事件名，不记密钥原文。
    actions = {event["action"] for event in audit}
    assert "credential.upserted" in actions
    assert "credential.tested" in actions


# —— G3-4：测试连接代理 ——


def test_credential_test_returns_ok_for_market_token(file_client):
    test_client, headers = file_client
    test_client.put("/credentials/market_data_token", headers=headers, json={"secret": PLAIN_TOKEN})
    result = test_client.post("/credentials/market_data_token/test", headers=headers).json()
    assert result["ok"] is True
    assert "latency_ms" in result and "detail" in result


def test_credential_test_rejects_missing_and_bad_webhook(file_client):
    test_client, headers = file_client
    missing = test_client.post("/credentials/market_data_token/test", headers=headers).json()
    assert missing["ok"] is False

    test_client.put(
        "/credentials/notify_webhook", headers=headers, json={"secret": "not-a-url"}
    )
    bad = test_client.post("/credentials/notify_webhook/test", headers=headers).json()
    assert bad["ok"] is False


# —— G3-3：模型服务多方案 ——


def test_model_profiles_single_active_scheme(file_client):
    test_client, headers = file_client
    a = test_client.post(
        "/model-profiles",
        headers=headers,
        json={"name": "方案A", "base_url": "http://127.0.0.1:1/v1", "model": "m-a", "credential_ref": "market_data_token"},
    )
    b = test_client.post(
        "/model-profiles",
        headers=headers,
        json={"name": "方案B", "base_url": "http://127.0.0.1:2/v1", "model": "m-b", "credential_ref": "market_data_token"},
    )
    assert a.status_code == 201 and b.status_code == 201
    id_a, id_b = a.json()["profile_id"], b.json()["profile_id"]

    # 激活 A → 恰好一个「使用中」。
    activated_a = test_client.post(f"/model-profiles/{id_a}/activate", headers=headers)
    assert activated_a.status_code == 200
    assert activated_a.json()["status"] == "使用中"
    active = [
        p for p in test_client.get("/model-profiles", headers=headers).json() if p["status"] == "使用中"
    ]
    assert [p["profile_id"] for p in active] == [id_a]

    # 切到 B → A 变未启用，B 唯一使用中。
    test_client.post(f"/model-profiles/{id_b}/activate", headers=headers)
    by_id = {p["profile_id"]: p for p in test_client.get("/model-profiles", headers=headers).json()}
    assert by_id[id_a]["status"] == "未启用"
    assert by_id[id_b]["status"] == "使用中"


def test_model_profile_test_and_unknown_activate(file_client):
    test_client, headers = file_client
    # 未启用/不存在 profile 的激活 → 404。
    assert (
        test_client.post(f"/model-profiles/{uuid4()}/activate", headers=headers).status_code == 404
    )

    created = test_client.post(
        "/model-profiles",
        headers=headers,
        json={"name": "不可达", "base_url": "http://127.0.0.1:65535/v1", "model": "m", "credential_ref": "未配置的密钥"},
    ).json()
    # 阶段 A3：测试连接现在走真实模型调用；凭据缺失先于任何外呼被拦下，ok 恒为 False，且不触网。
    result = test_client.post(f"/model-profiles/{created['profile_id']}/test", headers=headers).json()
    assert result["ok"] is False
    assert "latency_ms" in result and "detail" in result


def test_model_profile_update_roundtrip(file_client):
    """PUT 编辑方案：四字段可改，profile_id/状态保留；不存在的 id → 404。"""
    test_client, headers = file_client
    created = test_client.post(
        "/model-profiles",
        headers=headers,
        json={"name": "旧方案", "base_url": "http://127.0.0.1:1/v1", "model": "m-old", "credential_ref": "market_data_token"},
    ).json()

    updated = test_client.put(
        f"/model-profiles/{created['profile_id']}",
        headers=headers,
        json={"name": "新方案", "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "credential_ref": "model_api_key"},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["profile_id"] == created["profile_id"]
    assert body["name"] == "新方案"
    assert body["base_url"] == "https://api.deepseek.com"
    assert body["model"] == "deepseek-chat"
    assert body["credential_ref"] == "model_api_key"
    assert body["created_at"] == created["created_at"]  # 创建时间保留

    listed = {p["profile_id"]: p for p in test_client.get("/model-profiles", headers=headers).json()}
    assert listed[created["profile_id"]]["name"] == "新方案"

    missing = test_client.put(
        f"/model-profiles/{uuid4()}",
        headers=headers,
        json={"name": "x", "base_url": "http://x", "model": "m", "credential_ref": "k"},
    )
    assert missing.status_code == 404


def test_model_profile_test_hint_mentions_key_id(file_client):
    """凭据未配置时，提示应给出三种可操作填法（明文自动入库 / key_id / 尾号匹配）。"""
    test_client, headers = file_client
    created = test_client.post(
        "/model-profiles",
        headers=headers,
        json={"name": "误导配置", "base_url": "https://api.deepseek.com", "model": "deepseek-chat", "credential_ref": "sk-3uWbQv5Ph7Z1WAL8_example"},
    ).json()
    result = test_client.post(f"/model-profiles/{created['profile_id']}/test", headers=headers).json()
    assert result["ok"] is False
    assert "key_id" in result["detail"]
    assert "直接粘贴 sk- 明文密钥" in result["detail"]
    assert "尾号" in result["detail"]


def test_cred_blob_sized_by_bytes_not_chars():
    """回归：CredWrite 的 CredentialBlob 缓冲必须按字节数分配。

    历史缺陷按字符数分配却填 2 倍初始化器 → Windows 上任何密钥保存必 IndexError（用户截图反馈）。
    """
    from investment_steward_core.credential_store import _cred_blob

    for secret in ["sk-abc123", "d" * 64, "密钥测试 with spaces"]:
        blob, size = _cred_blob(secret)
        assert size == len(secret.encode("utf-16-le"))
        assert len(blob) == size


def test_windows_store_write_failure_falls_back_to_file(file_client, monkeypatch, tmp_path):
    """CredWrite 失败（OSError）时 store 自动回退文件兜底，不再 500；get 仍能读到明文。"""
    from investment_steward_core import credential_store as cs
    from investment_steward_core.storage.database import Database

    db = Database(tmp_path / "t.sqlite3")
    store = cs.WindowsCredentialManagerStore(db)

    def broken_write(target: str, secret: str) -> None:
        raise OSError("CredWriteW failed: 1317")

    def no_os_read(target: str) -> None:
        # 模拟 OS 凭据不存在：真机上 _cred_read 会读到真实系统凭据，污染断言（真机反馈）。
        return None

    monkeypatch.setattr(cs, "_cred_write", broken_write)
    monkeypatch.setattr(cs, "_cred_read", no_os_read)
    record = store.store("model_api_key", "sk-test-1234567890")
    assert record.backend == "file"
    assert record.last4 == "7890"
    assert store.get("model_api_key") == "sk-test-1234567890"


def test_windows_store_list_records_calibrated_to_live_os(file_client, monkeypatch, tmp_path):
    """列表 = DB 元数据 + OS 实时校准：OS 被覆盖后列表必须显示新尾号，不得展示过期快照。

    历史缺陷（2026-09-04）：真实 key 被覆盖后 GET /credentials 仍显示旧尾号 6366，
    而 probe 实读是示例 key → 401，严重误导诊断。
    """
    from investment_steward_core import credential_store as cs
    from investment_steward_core.storage.database import Database

    db = Database(tmp_path / "t.sqlite3")
    store = cs.WindowsCredentialManagerStore(db)

    def stale_read(target: str) -> str:
        # 模拟「OS 明文已被其他路径覆盖」：DB 元数据停在旧尾号，OS 实读是另一个值。
        return "sk-test-1234567890"

    monkeypatch.setattr(cs, "_cred_read", stale_read)
    # 先造出过期元数据：绕过 store()，直接写 meta（模拟真实 key 曾存 OS 的目录行）。
    db.upsert_credential_meta("model_api_key", "6366")

    records = {record.key_id: record for record in store.list_records()}
    record = records["model_api_key"]
    assert record.last4 == "7890"  # OS 实时值，不是过期的 6366
    assert record.backend == "os"


def test_windows_store_list_records_honest_when_os_and_db_empty(file_client, monkeypatch, tmp_path):
    """OS 无凭据且 DB 无明文 → last4 如实报空（状态未知），不展示过期快照。"""
    from investment_steward_core import credential_store as cs
    from investment_steward_core.storage.database import Database

    db = Database(tmp_path / "t.sqlite3")
    store = cs.WindowsCredentialManagerStore(db)

    monkeypatch.setattr(cs, "_cred_read", lambda target: None)
    db.upsert_credential_meta("model_api_key", "6366")  # 过期元数据

    record = store.list_records()[0]
    assert record.last4 == ""  # 不再谎报「6366 已保存」


def test_resolve_credential_last4_fallback(tmp_path):
    """尾号兜底：凭据引用填 last4（如 HGDI）也能解析到对应凭据（用户反馈：凭直觉填尾号）。"""
    from investment_steward_core import credential_store as cs
    from investment_steward_core import model_client
    from investment_steward_core.storage.database import Database

    db = Database(tmp_path / "t.sqlite3")
    store = cs.DbCredentialStore(db)
    store.store("model_api_key", "sk-test-1234567890")
    assert model_client.resolve_credential(store, "model_api_key") == "sk-test-1234567890"
    assert model_client.resolve_credential(store, "7890") == "sk-test-1234567890"
    assert model_client.resolve_credential(store, "9999") == ""
    assert model_client.resolve_credential(store, "nonexistent") == ""


# —— G5-1/G5-2：书目 CRUD 与荐读计划 ——


def test_book_crud_roundtrip(file_client):
    test_client, headers = file_client
    created = test_client.post(
        "/library/books",
        headers=headers,
        json={"title": "投资最重要的事", "author": "Howard Marks", "progress": 0.3},
    )
    assert created.status_code == 201
    book_id = created.json()["book_id"]
    assert created.json()["data_leaves_device"] is False

    books = test_client.get("/library/books", headers=headers).json()
    assert len(books) == 1 and books[0]["book_id"] == book_id

    assert test_client.delete(f"/library/books/{book_id}", headers=headers).status_code == 204
    assert test_client.get("/library/books", headers=headers).json() == []


def test_generate_plan_lands_in_learning_flow_never_policy(file_client):
    """G5-4：荐读计划落 today.learning、只进学习流；绝不写 investment-policies。"""
    test_client, headers = file_client
    before_policies = test_client.get("/investment-policies", headers=headers).json()
    assert before_policies == []

    booked = test_client.post(
        "/library/books",
        headers=headers,
        json={"title": "投资最重要的事", "author": "Howard Marks"},
    ).json()
    plan = test_client.post(
        "/library/plan/generate",
        headers=headers,
        json={"source_book_id": booked["book_id"]},
    )
    assert plan.status_code == 200
    plan_body = plan.json()
    assert plan_body["slot"] == "today.learning"
    assert plan_body["renderer"] == "summary_row"
    assert plan_body["book_ref"] == booked["book_id"]
    for key in ("daily_task", "source_chapter", "rationale"):
        assert plan_body[key]

    # 荐读计划出现于今天页复盘流（today.learning 卡）。
    cards = test_client.get("/cards/slots/today.learning", headers=headers).json()
    assert any(card["card_id"].startswith("libraryplan-") for card in cards)

    # 关键断言：投资原则一次也没被动过。
    after_policies = test_client.get("/investment-policies", headers=headers).json()
    assert after_policies == []
    audit = test_client.get("/audit", headers=headers).json()
    assert not any(event["action"].startswith("policy.") for event in audit)


# —— G5-3：批注转研究问题 ——


def test_annotation_to_research_reuses_research_run(file_client):
    test_client, headers = file_client
    booked = test_client.post(
        "/library/books",
        headers=headers,
        json={"title": "投资最重要的事", "notes": ["批注A：这里存疑", "批注B"]},
    ).json()
    book_id = booked["book_id"]

    # 注 {book_id}:0 对应第一条批注 → 建立 ResearchRun。
    resp = test_client.post(
        f"/library/annotations/{book_id}:0/to-research",
        headers=headers,
        json={"user_question": "Howard Marks 关于市场周期的判断如何影响我的持仓观察？"},
    )
    assert resp.status_code == 201
    runs = test_client.get("/research/runs", headers=headers).json()
    assert resp.json()["run_id"] in {run["run_id"] for run in runs}

    # 不存在的批注 → 404，不臆造研究问题。
    assert (
        test_client.post(
            f"/library/annotations/{book_id}:99/to-research",
            headers=headers,
            json={"user_question": "问题"},
        ).status_code
        == 404
    )