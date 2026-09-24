"""阶段 4 客户端同步测试：信封加密 / 白名单构建 / 引擎全流程 / 端点契约。"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from investment_steward_core import sync_client
from investment_steward_core.domain.longterm import WatchItem
from investment_steward_core.domain.models import (
    ActionMode,
    Notification,
    Plan,
    PlanStatus,
    ResearchRun,
    RunStatus,
)
from test_longterm import USER  # 本文件另有 `_db(tmp_path)` 包装（见下方），不在导入层借用同名函数

# ———————— 信封 ————————

def test_envelope_roundtrip_and_wrong_key_rejected():
    key = sync_client.generate_sync_key()
    sealed = sync_client.seal_payload(key, {"hello": "世界", "n": 1})
    assert "世界" not in sealed  # 服务器视角只有密文
    assert sync_client.open_payload(key, sealed) == {"hello": "世界", "n": 1}
    with pytest.raises(ValueError, match="解密失败"):
        sync_client.open_payload(sync_client.generate_sync_key(), sealed)


# ———————— 白名单构建 ————————

def _db(tmp_path):
    from test_longterm import _db as make_db

    return make_db(tmp_path)


def test_build_events_whitelist_kinds(tmp_path):
    db = _db(tmp_path)
    db.upsert_watch_item(WatchItem(
        watch_id=uuid4(), user_id=USER, title="利差观察", indicator="10Y-2Y",
        condition_text="重新倒挂", check_cycle="weekly",
    ))
    db.insert_plan(Plan(plan_id=uuid4(), user_id=USER, title="定投计划", status=PlanStatus.ACTIVE))
    db.upsert_notification(Notification(
        notification_id=uuid4(), user_id=USER, instrument="510300", triggered_by="跌破年线",
        condition_kind="invalidation_condition", title="n1", summary="s1", action_mode=ActionMode.OBSERVE,
    ), dedup_key="test-notif-1")
    db.upsert_research_run(ResearchRun(run_id=uuid4(), user_id=USER, user_question="是否加仓?",
                                       status=RunStatus.COMPLETED))
    events, gaps = sync_client.build_events(db, USER)
    kinds = {e["kind"] for e in events}
    assert {"watch_item.updated", "plan.status", "notification.raised", "research.summary"} <= kinds
    assert all(e["idempotency_key"].startswith(e["kind"].split(".")[0].split("_")[0]) for e in events)
    assert any(g["kind"] == "today_summary" for g in gaps)  # 如实标注:Today 摘要由 digest 覆盖


# ———————— 引擎全流程（mock 中转客户端） ————————

@pytest.fixture()
def relay_env(monkeypatch, tmp_path):
    db = _db(tmp_path)
    db.set_sync_state("user_id", str(USER))
    db.upsert_watch_item(WatchItem(
        watch_id=uuid4(), user_id=USER, title="利差观察", indicator="10Y-2Y",
        condition_text="重新倒挂", check_cycle="weekly",
    ))
    key = sync_client.generate_sync_key()
    uploaded: list[list[dict]] = []

    def fake_upload(self, events):
        uploaded.append(events)
        return {"results": [{"idempotency_key": e["idempotency_key"], "duplicate": False,
                             "event_id": 100 + i} for i, e in enumerate(events)]}

    def fake_pull(self, cursor, limit=200):
        # 远端有 2 条:一条用同一密钥加密(可解),一条乱密文(解不开)
        good = sync_client.seal_payload(key, {"msg": "来自手机端"})
        return {"cursor": cursor, "cursor_max": 7, "events": [
            {"event_id": 6, "kind": "watch_item.updated", "idempotency_key": "remote-1",
             "version": 1, "payload": good},
            {"event_id": 7, "kind": "watch_item.updated", "idempotency_key": "remote-2",
             "version": 1, "payload": "bm90LXZhbGlk"},
        ]}

    monkeypatch.setattr(sync_client.RelayClient, "upload_events", fake_upload)
    monkeypatch.setattr(sync_client.RelayClient, "pull_events", fake_pull)
    config = sync_client.RelayConfig(base_url="https://st.example", user_token="t" * 30, device_id="d-test")
    return db, key, config, uploaded


def test_run_sync_full_flow_and_rerun_idempotent(relay_env):
    db, key, config, uploaded = relay_env
    first = sync_client.run_sync(db, None, config, key, USER)
    assert first["ok"] is True and first["uploaded"] > 0 and first["duplicates"] == 0
    assert first["pulled"] == 1 and first["decrypt_failures"] == 1   # 坏密文如实计数,不静默
    assert first["pull_cursor"] == 7
    assert len(uploaded) == 1
    # 上传的 payload 是密文:不含明文关键词
    assert all("重新倒挂" not in e["payload"] for e in uploaded[0])

    inbox = db.list_sync_inbox()
    assert inbox[0]["idempotency_key"] == "remote-1"
    assert "来自手机端" in inbox[0]["payload"]

    # 重跑:outbox 幂等键去重 → enqueued=0,pending=0;远端事件 remote-1 重复 → 不重复入 inbox
    second = sync_client.run_sync(db, None, config, key, USER)
    assert second["enqueued"] == 0
    assert second["outbox"]["pending"] == 0
    assert second["pulled"] == 0 and second["decrypt_failures"] == 1  # 同一批远端事件游标已推进,不再拉取
    assert db.sync_inbox_count() == 1


def test_run_sync_with_local_user_id(relay_env):
    db, key, config, _ = relay_env
    # local_user_id 由调用方(端点)传入,不再依赖 sync_state
    result = sync_client.run_sync(db, None, config, key, USER)
    assert result["ok"] is True and result["uploaded"] >= 1


# ———————— 端点契约 ————————

def test_sync_endpoints_contract(client, monkeypatch):
    # 端点测试会真实写系统凭据库 → 必须 patch 为文件后端,否则 pytest 会覆盖真实 CredMan token!
    from investment_steward_core.api import app as api_app
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    http, headers = client
    # 未配置 → run 409
    assert http.post("/sync/run", headers=headers).status_code == 409

    relay_url = "https://st.example"
    created = http.post("/sync/config", headers=headers, json={
        "relay_url": relay_url, "user_token": "t" * 30,
        "device_id": "d-contract", "user_id": "u-contract",
    })
    assert created.status_code == 200 and created.json()["sync_key_generated"] is True

    status_body = http.get("/sync/status", headers=headers).json()
    assert status_body["configured"] is True and status_body["has_token"] is True
    assert "user_token" not in status_body and "sync_key" not in status_body  # 永不回显

    # run:mock 中转
    def fake_upload(self, events):
        return {"results": [{"idempotency_key": e["idempotency_key"], "duplicate": False,
                             "event_id": i + 1} for i, e in enumerate(events)]}

    def fake_pull(self, cursor, limit=200):
        return {"cursor": cursor, "cursor_max": 0, "events": []}

    monkeypatch.setattr(sync_client.RelayClient, "upload_events", fake_upload)
    monkeypatch.setattr(sync_client.RelayClient, "pull_events", fake_pull)
    run = http.post("/sync/run", headers=headers).json()
    assert run["ok"] is True
    inbox = http.get("/sync/inbox", headers=headers)
    assert inbox.status_code == 200
    assert http.get("/sync/inbox", params={"limit": 501}, headers=headers).status_code == 422


def test_sync_pairing_code_endpoint(client, monkeypatch):
    """配对码端点:未配置 409;配置后 mock relay 返回码;审计与响应不回显密钥。"""
    from investment_steward_core.api import app as api_app
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    http, headers = client
    assert http.post("/sync/pairing-code", headers=headers).status_code == 409
    assert http.post("/sync/config", headers=headers, json={
        "relay_url": "https://st.example", "user_token": "t" * 30,
        "device_id": "d-pair", "user_id": "u-pair",
    }).status_code == 200

    def fake_pairing(self, sync_key, *, ttl_seconds=600):
        assert sync_key
        return {"code": "A2C4E6", "expires_at": "2026-09-10T05:00:00+00:00", "ttl_seconds": ttl_seconds}

    monkeypatch.setattr(sync_client.RelayClient, "create_pairing_code", fake_pairing)
    resp = http.post("/sync/pairing-code", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["code"] == "A2C4E6"
    assert "sync_key" not in body and "user_token" not in body  # 响应永不回显密钥


# ———————— inbox 应用（apply_inbox） ————————

def _push_remote_event(db, key, *, kind, idem, payload, remote_id=50):
    """模拟远端事件直接进 inbox(已解密)。"""
    db.insert_sync_inbox(remote_id, kind, idem, 1,
                         json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def test_apply_inbox_watch_item_create_update_and_idempotent(tmp_path):
    db = _db(tmp_path)
    remote_watch = str(uuid4())
    _push_remote_event(db, KEY_HOLD := sync_client.generate_sync_key(), kind="watch_item.updated",
                       idem="remote-w1", payload={"watch_id": remote_watch, "title": "远端观察",
                                                  "indicator": "RSI", "condition_text": "超买",
                                                  "check_cycle": "daily", "status": "active"},
                       remote_id=60)
    report = sync_client.apply_inbox(db, USER)
    assert report["applied_watch_items"] == 1
    mirrored = next(w for w in db.list_watch_items(USER) if w.dedup_key == f"sync:watch_item:{remote_watch}")
    assert mirrored.indicator == "RSI"
    # 状态更新:同 dedup_key,新事件改状态
    _push_remote_event(db, KEY_HOLD, kind="watch_item.updated", idem="remote-w2",
                       payload={"watch_id": remote_watch, "title": "远端观察", "indicator": "RSI",
                                "condition_text": "超买", "check_cycle": "daily", "status": "triggered"},
                       remote_id=61)
    report2 = sync_client.apply_inbox(db, USER)
    assert report2["updated_watch_items"] == 2 and report2["applied_watch_items"] == 0  # 全量升序重放
    mirrored = next(w for w in db.list_watch_items(USER) if w.dedup_key == f"sync:watch_item:{remote_watch}")
    assert mirrored.status.value == "triggered"   # 新事件最后生效,重放不回滚
    # 再次全量应用:最终状态不变(重放幂等)
    report3 = sync_client.apply_inbox(db, USER)
    assert report3["applied_watch_items"] == 0
    mirrored = next(w for w in db.list_watch_items(USER) if w.dedup_key == f"sync:watch_item:{remote_watch}")
    assert mirrored.status.value == "triggered"


def test_apply_inbox_notification_dedup_and_plan_research_skipped(tmp_path):
    db = _db(tmp_path)
    _push_remote_event(db, sync_client.generate_sync_key(), kind="notification.raised",
                       idem="remote-n1", payload={"notification_id": "n-remote-1", "instrument": "510300",
                                                  "triggered_by": "跌破年线", "title": "远端通知",
                                                  "summary": "s"}, remote_id=70)
    _push_remote_event(db, sync_client.generate_sync_key(), kind="plan.status",
                       idem="remote-p1", payload={"plan_id": "p-remote", "status": "active"}, remote_id=71)
    _push_remote_event(db, sync_client.generate_sync_key(), kind="research.summary",
                       idem="remote-r1", payload={"run_id": "r1", "question": "q", "status": "completed"},
                       remote_id=72)
    _push_remote_event(db, sync_client.generate_sync_key(), kind="alien.kind",
                       idem="remote-x1", payload={}, remote_id=73)
    report = sync_client.apply_inbox(db, USER)
    assert report["applied_notifications"] == 1
    assert report["skipped_plans"] == ["p-remote"]      # 计划无映射 → 如实跳过,不自动创建
    assert report["skipped_research"] == 1
    assert "alien.kind" in report["unknown_kinds"]
    notifications = db.list_notifications(USER)
    assert sum(1 for n in notifications if n.title == "远端通知") == 1
    # 幂等:重复应用,通知数不变(dedup_key 去重),计划/研究依旧跳过
    report2 = sync_client.apply_inbox(db, USER)
    assert report2["applied_notifications"] == 1  # upsert 仍执行,但 dedup 不产生第二条
    assert len([n for n in db.list_notifications(USER) if n.title == "远端通知"]) == 1


def test_apply_inbox_mobile_note_lands_as_notification_idempotent(tmp_path):
    """手机端 mobile.note(研报/消息)→ 通知中心;重复应用幂等。"""
    db = _db(tmp_path)
    _push_remote_event(db, sync_client.generate_sync_key(), kind="mobile.note",
                       idem="mobile_note:1700000000", remote_id=80,
                       payload={"title": "宁德时代调研纪要", "text": "排产环比+15%,储能需求超预期。",
                                "device_id": "d-test", "created_at": "2026-09-10T04:00:00+00:00"})
    report = sync_client.apply_inbox(db, USER)
    assert report["applied_mobile_notes"] == 1
    notes = [n for n in db.list_notifications(USER) if "宁德时代调研纪要" in n.title]
    assert len(notes) == 1
    assert notes[0].title.startswith("📱")
    assert notes[0].summary.startswith("排产环比")
    assert notes[0].condition_kind == "mobile_note"
    # 幂等:重复应用不产生第二条
    report2 = sync_client.apply_inbox(db, USER)
    assert report2["applied_mobile_notes"] == 1
    assert len([n for n in db.list_notifications(USER) if "宁德时代调研纪要" in n.title]) == 1
    # 无标题兜底
    _push_remote_event(db, sync_client.generate_sync_key(), kind="mobile.note",
                       idem="mobile_note:1700000001", remote_id=81, payload={"text": "只有正文"})
    sync_client.apply_inbox(db, USER)
    assert any(n.title == "📱 手机消息" for n in db.list_notifications(USER))


def test_apply_inbox_mobile_watch_action_and_research_request(tmp_path):
    """手机操作观察事项(镜像更新)+研究请求落通知;无镜像如实跳过。"""
    db = _db(tmp_path)
    key = sync_client.generate_sync_key()
    remote_watch = str(uuid4())
    _push_remote_event(db, key, kind="watch_item.updated", idem="w-sync-1", remote_id=90,
                       payload={"watch_id": remote_watch, "title": "同步观察", "indicator": "RSI",
                                "condition_text": "超买", "check_cycle": "daily", "status": "triggered"})
    # 手机:暂停 → 恢复 → 确认已处理(升序重放最终 closed)
    _push_remote_event(db, key, kind="mobile.watch_action", idem="mwa-1", remote_id=91,
                       payload={"watch_id": remote_watch, "action": "pause", "note": "先观察"})
    _push_remote_event(db, key, kind="mobile.watch_action", idem="mwa-2", remote_id=92,
                       payload={"watch_id": remote_watch, "action": "resume", "note": ""})
    _push_remote_event(db, key, kind="mobile.watch_action", idem="mwa-3", remote_id=93,
                       payload={"watch_id": remote_watch, "action": "confirm", "note": "已兑现"})
    # 研究请求
    _push_remote_event(db, key, kind="mobile.research_request", idem="mrq-1", remote_id=94,
                       payload={"symbol": "600519", "question": "近期回调是否为上车机会", "device_id": "d-x"})
    # 无镜像的 watch_action → skipped
    _push_remote_event(db, key, kind="mobile.watch_action", idem="mwa-4", remote_id=95,
                       payload={"watch_id": "ghost", "action": "pause", "note": ""})
    report = sync_client.apply_inbox(db, USER)
    assert report["applied_mobile_watch"] == 3
    assert report["skipped_mobile_watch"] == ["ghost:pause"]
    assert report["applied_mobile_research"] == 1
    mirrored = next(w for w in db.list_watch_items(USER) if w.dedup_key == f"sync:watch_item:{remote_watch}")
    assert mirrored.status.value == "closed"
    assert "📱已兑现" in mirrored.condition_text
    rq = [n for n in db.list_notifications(USER) if n.title.startswith("🔬 研究请求")]
    assert len(rq) == 1 and "回调" in rq[0].summary and rq[0].instrument == "600519"
    # 幂等:重放数量不变
    report2 = sync_client.apply_inbox(db, USER)
    assert report2["applied_mobile_watch"] == 3 and report2["applied_mobile_research"] == 1


# ———————— §7.1 申请制中转：封包/验签/端点 ————————

def test_transfer_seal_open_roundtrip_and_tamper_detection():
    key = sync_client.generate_sync_key()
    envelope = sync_client.seal_transfer(key, artifact_type="parameter_set", version=3,
                                         payload={"symbol": "510300", "formula": "roc(20)"})
    opened = sync_client.open_transfer(key, envelope)
    assert opened["payload"]["formula"] == "roc(20)" and opened["version"] == 3
    # 篡改哈希 → 拒绝
    bad = dict(envelope, sha256="0" * 64)
    with pytest.raises(ValueError, match="哈希不一致"):
        sync_client.open_transfer(key, bad)
    # 篡改签名 → 拒绝
    bad_sig = dict(envelope, signature="f" * 64)
    with pytest.raises(ValueError, match="签名验证失败"):
        sync_client.open_transfer(key, bad_sig)
    # 换密钥 → 解密失败
    with pytest.raises(ValueError):
        sync_client.open_transfer(sync_client.generate_sync_key(), envelope)


def test_transfer_endpoints_full_flow(client, monkeypatch):
    """端到端(mock 网络):申请 → 同意 → 发送(公告入 outbox) → 接收(显式元数据路径)。"""
    from investment_steward_core.api import app as api_app
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    http, headers = client
    http.post("/sync/config", headers=headers, json={
        "relay_url": "https://st.example", "user_token": "t" * 30,
        "device_id": "d-t", "user_id": "u-t",
    })

    fake = {"requests": [], "uploaded": None}

    class FakeTransfer:
        def __init__(self, config):
            pass
        def create_request(self, artifact_type, note=""):
            rid = f"req-{len(fake['requests']) + 1}"
            fake["requests"].append({"request_id": rid, "artifact_type": artifact_type, "status": "pending"})
            return {"request_id": rid}
        def list_requests(self):
            return fake["requests"]
        def decide(self, request_id, decision):
            row = next(r for r in fake["requests"] if r["request_id"] == request_id)
            row["status"] = "approved" if decision == "approve" else "rejected"
            return {"request_id": request_id, "status": row["status"]}
        def send_approved(self, request_id, envelope):
            fake["uploaded"] = dict(envelope)
            return {"package_id": "pkg-1", "bytes": 10}
        def receive_ready(self, request_id):
            return {"ciphertext": fake["uploaded"]["ciphertext"], "sha256": fake["uploaded"]["sha256"],
                    "expires_at": "2099-01-01T00:00:00+00:00"}

    monkeypatch.setattr(sync_client, "TransferClient", FakeTransfer)
    monkeypatch.setattr(sync_client.RelayClient, "upload_events",
                        lambda self, events: {"results": [
                            {"idempotency_key": e["idempotency_key"], "duplicate": False, "event_id": 1}
                            for e in events]})
    monkeypatch.setattr(sync_client.RelayClient, "pull_events",
                        lambda self, cursor, limit=200: {"cursor": cursor, "cursor_max": 0, "events": []})

    rid = http.post("/transfer/requests", headers=headers,
                    json={"artifact_type": "parameter_set", "note": "要 510300 参数集"}).json()["request_id"]
    # 未同意就能列表;批准
    assert http.post(f"/transfer/requests/{rid}/decide", headers=headers,
                     json={"decision": "approve"}).json()["status"] == "approved"
    sent = http.post(f"/transfer/requests/{rid}/send", headers=headers, json={
        "artifact_type": "parameter_set", "version": 2,
        "payload": {"symbol": "510300", "formula": "roc(20)"},
        "snapshot_hash": "snap-abc123",
    }).json()
    assert sent["ok"] is True and sent["signature"] and sent["sha256"]
    # 接收:显式元数据路径(公告自动路径由真实联调验证)
    received = http.post(f"/transfer/requests/{rid}/receive", headers=headers, json={
        "artifact_type": "parameter_set", "version": 2,
        "sha256": sent["sha256"], "signature": sent["signature"], "snapshot_hash": "snap-abc123",
    })
    assert received.status_code == 200
    body = received.json()
    assert body["ok"] is True and body["signature_verified"] is True
    assert body["payload"]["formula"] == "roc(20)"
    assert body["snapshot_hash"] == "snap-abc123"
    # 元数据缺失且无公告 → 409(诚实缺口)
    rid2 = http.post("/transfer/requests", headers=headers, json={"artifact_type": "dataset"}).json()["request_id"]
    http.post(f"/transfer/requests/{rid2}/decide", headers=headers, json={"decision": "approve"})
    missing = http.post(f"/transfer/requests/{rid2}/send", headers=headers, json={
        "artifact_type": "dataset", "version": 1, "payload": {"rows": 1}})
    assert missing.json()["ok"] is True
    # 接收 rid2:无公告、无显式元数据 → 409
    assert http.post(f"/transfer/requests/{rid2}/receive", headers=headers, json={}).status_code == 409
