"""中转服务契约测试（阶段 4 首批）。全部用临时数据目录,不触网。"""

from __future__ import annotations

import hashlib
import os
import secrets

import pytest
from fastapi.testclient import TestClient

from relay_server import __version__, create_app
from relay_server.settings import RelaySettings

BOOTSTRAP = "bootstrap-test-token-123"


@pytest.fixture()
def client(tmp_path):
    settings = RelaySettings(data_dir=tmp_path / "data", bootstrap_token=BOOTSTRAP)
    app = create_app(settings)
    with TestClient(app) as http:
        # 每个测试注册一台设备,拿到 user_token
        resp = http.post("/devices", headers={"X-Relay-Bootstrap": BOOTSTRAP},
                         json={"label": "测试设备", "public_key": "PUBKEY-AAA"})
        assert resp.status_code == 201
        registered = resp.json()
        http.headers.update({"X-Relay-Token": registered["user_token"]})
        yield http, registered


def test_health_reports_limits(client):
    http, _ = client
    resp = http.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["version"] == __version__
    assert body["rules"]["max_package_bytes"] == 8 * 1024 * 1024


def test_device_registration_requires_bootstrap(client):
    http, _ = client
    assert http.post("/devices", headers={"X-Relay-Bootstrap": "wrong"}, json={}).status_code == 401
    assert http.post("/devices", json={}).status_code == 401


def test_device_list_and_revoke(client):
    http, registered = client
    devices = http.get("/devices").json()
    assert len(devices) == 1 and devices[0]["device_id"] == registered["device_id"]
    assert http.delete(f"/devices/{registered['device_id']}").json()["revoked"] is True
    assert http.delete(f"/devices/{registered['device_id']}").status_code == 404


def test_events_idempotent_upload_and_cursor_pull(client):
    http, registered = client
    event = {"kind": "watch_item.updated", "idempotency_key": "wi-1:2026-W37",
             "version": 2, "payload": hashlib.sha256(b"encrypted-envelope").hexdigest()}
    first = http.post("/sync/events", json={"device_id": registered["device_id"], "events": [event]})
    assert first.status_code == 200
    assert first.json()["results"][0]["duplicate"] is False
    # 同幂等键重传 → duplicate=true,event_id 不变
    again = http.post("/sync/events", json={"device_id": registered["device_id"], "events": [event]}).json()
    assert again["results"][0]["duplicate"] is True
    assert again["results"][0]["event_id"] == first.json()["results"][0]["event_id"]

    # 批量 200 上限
    too_many = [{"kind": "k", "idempotency_key": f"k{i}", "payload": "x"} for i in range(201)]
    assert http.post("/sync/events", json={"events": too_many}).status_code == 422
    # payload 超限
    assert http.post("/sync/events", json={"events": [
        {"kind": "k", "idempotency_key": "big", "payload": "x" * (64 * 1024 + 1)}
    ]}).status_code == 413

    # 游标拉取:只回本用户事件,游标单调
    pulled = http.get("/sync/events", params={"cursor": 0, "limit": 100}).json()
    assert pulled["cursor_max"] >= pulled["events"][0]["event_id"]
    assert all(e["kind"] == "watch_item.updated" for e in pulled["events"])
    beyond = http.get("/sync/events", params={"cursor": pulled["cursor_max"]}).json()
    assert beyond["events"] == []
    # 参数校验
    assert http.get("/sync/events", params={"limit": 501}).status_code == 422
    assert http.get("/sync/events", params={"cursor": -1}).status_code == 422


def test_relay_package_lifecycle_one_time_download_and_delete(client):
    http, _ = client
    blob = secrets.token_bytes(1024)
    sha = hashlib.sha256(blob).hexdigest()

    created = http.post("/relay/packages", json={"declared_size": len(blob), "sha256": sha, "ttl_seconds": 600})
    assert created.status_code == 201
    package_id = created.json()["package_id"]

    # 未上传不能签发下载令牌
    assert http.post(f"/relay/packages/{package_id}/token").status_code == 404
    # sha 不匹配拒绝落盘
    wrong = http.put(f"/relay/packages/{package_id}/data", content=b"x" * len(blob))
    assert wrong.status_code == 422
    # 大小不一致拒绝
    wrong_size = http.put(f"/relay/packages/{package_id}/data", content=blob[:-1])
    assert wrong_size.status_code == 413

    uploaded = http.put(f"/relay/packages/{package_id}/data", content=blob)
    assert uploaded.status_code == 200 and uploaded.json()["status"] == "ready"
    # 重复上传 → 404(已不是 pending)
    assert http.put(f"/relay/packages/{package_id}/data", content=blob).status_code == 404

    token_resp = http.post(f"/relay/packages/{package_id}/token")
    assert token_resp.status_code == 200
    download_token = token_resp.json()["download_token"]

    # 一次性下载:第一次成功且内容逐位一致
    first = http.get("/relay/download", params={"token": download_token})
    assert first.status_code == 200
    assert first.content == blob
    assert first.headers["X-Package-Sha256"] == sha
    # 中转完成:服务器副本已删,状态 relayed
    status_body = http.get(f"/relay/packages/{package_id}").json()
    assert status_body["status"] == "relayed" and status_body["relayed_at"]
    # 第二次下载 → 410;令牌复用 → 410
    assert http.get("/relay/download", params={"token": download_token}).status_code == 410
    # 无效令牌 → 410
    assert http.get("/relay/download", params={"token": "bogus"}).status_code == 410


def test_relay_package_ttl_and_size_limits(client):
    http, _ = client
    assert http.post("/relay/packages", json={"declared_size": 8 * 1024 * 1024 + 1, "sha256": "0" * 64}).status_code == 413
    assert http.post("/relay/packages", json={"declared_size": 100, "sha256": "zz"}).status_code == 422
    assert http.post("/relay/packages", json={"declared_size": 100, "sha256": "0" * 64, "ttl_seconds": 10}).status_code == 422
    assert http.post("/relay/packages", json={"declared_size": 0, "sha256": "0" * 64}).status_code == 413


def test_auth_required_everywhere(client):
    http, _ = client
    http.headers.pop("X-Relay-Token")  # 摘掉 fixture 默认头,验证无令牌一律 401
    assert http.get("/devices").status_code == 401
    assert http.get("/sync/events").status_code == 401
    assert http.post("/relay/packages", json={}).status_code == 401
    assert http.post("/maintenance/cleanup").status_code == 401


# ———————— §7.1 申请制中转 ————————

def test_transfer_request_lifecycle_requires_explicit_approval(client):
    http, _ = client
    blob = secrets.token_bytes(256)
    sha = hashlib.sha256(blob).hexdigest()

    # 类型白名单
    assert http.post("/requests", json={"artifact_type": "unknown_kind"}).status_code == 422
    created = http.post("/requests", json={"artifact_type": "parameter_set", "note": "申请参数集"})
    assert created.status_code == 201
    request_id = created.json()["request_id"]

    # 未批准 → 上传绑定被拒(验收:未授权申请)
    denied = http.post("/relay/packages", json={"declared_size": len(blob), "sha256": sha,
                                                "request_id": request_id, "ttl_seconds": 600})
    assert denied.status_code == 409
    # 未批准 → 领令牌被拒
    assert http.get(f"/requests/{request_id}/token").status_code == 409

    # 显式同意 → 绑定上传 → uploaded
    assert http.post(f"/requests/{request_id}/approve").json()["status"] == "approved"
    pkg = http.post("/relay/packages", json={"declared_size": len(blob), "sha256": sha,
                                             "request_id": request_id, "ttl_seconds": 600}).json()
    assert http.put(f"/relay/packages/{pkg['package_id']}/data", content=blob).status_code == 200
    listed = http.get("/requests").json()
    assert next(r for r in listed if r["request_id"] == request_id)["status"] == "uploaded"

    # 接收方(同用户另一设备语义)领一次性令牌 → 下载 → delivered
    tok = http.get(f"/requests/{request_id}/token").json()
    first = http.get("/relay/download", params={"token": tok["download_token"]})
    assert first.status_code == 200 and first.content == blob
    status_row = next(r for r in http.get("/requests").json() if r["request_id"] == request_id)
    assert status_row["status"] == "delivered" and status_row["delivered_at"]
    # 重复下载被拒(令牌一次性) & 重复领令牌被拒(包已 relayed)
    assert http.get("/relay/download", params={"token": tok["download_token"]}).status_code == 410
    assert http.get(f"/requests/{request_id}/token").status_code == 410


def test_transfer_request_reject_and_double_decision(client):
    http, _ = client
    request_id = http.post("/requests", json={"artifact_type": "model_weights"}).json()["request_id"]
    assert http.post(f"/requests/{request_id}/reject").json()["status"] == "rejected"
    # 已处理 → 再决策 409
    assert http.post(f"/requests/{request_id}/approve").status_code == 409
    assert http.post(f"/requests/{request_id}/reject").status_code == 409
    # rejected 状态绑定上传 → 409
    blob = secrets.token_bytes(16)
    sha = hashlib.sha256(blob).hexdigest()
    assert http.post("/relay/packages", json={"declared_size": len(blob), "sha256": sha,
                                              "request_id": request_id, "ttl_seconds": 600}).status_code == 409


def test_market_catalog_not_configured_and_github_unavailable(client, monkeypatch, tmp_path):
    http, _ = client
    # 未配置 RELAY_MARKET_REPO → 如实返回 not_configured,不伪造目录
    monkeypatch.delenv("RELAY_MARKET_REPO", raising=False)
    body = http.get("/market/catalog").json()
    assert body["source"] == "not_configured" and body["releases"] == []
    # 配置了仓库但 GitHub 不可达 + 无缓存 → unavailable
    monkeypatch.setenv("RELAY_MARKET_REPO", "acme/no-such-plugins")
    import builtins
    real_urlopen = builtins.open  # noqa: F841 - 占位说明用 urlopen patch
    from urllib import request as urlreq
    def fail_urlopen(*args, **kwargs):
        raise OSError("network down")
    monkeypatch.setattr(urlreq, "urlopen", fail_urlopen)
    body2 = http.get("/market/catalog").json()
    assert body2["source"] == "unavailable" and body2["releases"] == []
    # 有缓存 → stale 回退,目录仍可读(验收:GitHub 暂不可用)
    catalog_file = tmp_path / "data" / "market_catalog.json"
    catalog_file.write_text('{"source":"github","stale":false,"releases":[{"plugin_id":"official.cn-market-data",'
                            '"release_version":"0.1.0","revoked":false,"manifest":{"plugin_id":"official.cn-market-data"}}],'
                            '"revoked":[],"fetched_at":"2026-09-09T00:00:00+00:00","repo":"acme/x"}', encoding="utf-8")
    body3 = http.get("/market/catalog").json()
    assert body3["source"] == "relay-cache" and body3["stale"] is True
    assert body3["releases"][0]["plugin_id"] == "official.cn-market-data"


def test_device_attach_shares_user_and_revocation(client):
    """多设备接入:attach 签发设备 token,同 user 可见事件;撤销后立即失效。"""
    http, registered = client
    resp = http.post("/devices/attach", json={"label": "手机"})
    assert resp.status_code == 201
    attached = resp.json()
    assert attached["user_id"] == registered["user_id"]
    assert attached["device_token"]
    # 设备 token 可拉取同 user 事件（先注册一次设备并上传,再验证对端可见）
    http.headers.update({"X-Relay-Token": attached["device_token"]})
    up = http.post("/sync/events", json={"device_id": attached["device_id"], "events": [
        {"kind": "notification.raised", "idempotency_key": "n:attach-1", "version": 1,
         "payload": "x"}]})
    assert up.status_code == 200
    # 主 token 拉取能看到手机上传的事件
    http.headers.update({"X-Relay-Token": registered["user_token"]})
    pull = http.get("/sync/events", params={"cursor": 0}).json()
    assert any(e["idempotency_key"] == "n:attach-1" for e in pull["events"])
    # 主 token 撤销手机设备 → 设备 token 立即 401
    revoke = http.delete(f"/devices/{attached['device_id']}")
    assert revoke.status_code == 200
    http.headers.update({"X-Relay-Token": attached["device_token"]})
    assert http.get("/sync/events", params={"cursor": 0}).status_code == 401


def test_pairing_code_create_claim_one_time_and_expiry(client):
    """配对码:创建→领取一次性返回凭据;重领 404;过期 410;格式错 422。"""
    http, registered = client
    # 创建
    resp = http.post("/pairing/codes", json={"user_token": registered["user_token"],
                                             "sync_key": "test-sync-key", "ttl_seconds": 600})
    assert resp.status_code == 201
    body = resp.json()
    code = body["code"]
    assert len(code) == 6 and code.isalnum()
    # 匿名领取(无鉴权头)——先清掉 fixture 的全局头
    saved = http.headers.get("X-Relay-Token")
    del http.headers["X-Relay-Token"]
    claim = http.get("/pairing/claim", params={"code": code.lower()})  # 小写也能归一化
    assert claim.status_code == 200
    claimed = claim.json()
    assert claimed["user_id"] == registered["user_id"]
    assert claimed["user_token"] == registered["user_token"]
    assert claimed["sync_key"] == "test-sync-key"
    # 一次性:再领 404
    assert http.get("/pairing/claim", params={"code": code}).status_code == 404
    # 过期:直接改库把过期时间拨到过去
    resp2 = http.post("/pairing/codes", headers={"X-Relay-Token": saved},
                      json={"user_token": registered["user_token"], "sync_key": "k2", "ttl_seconds": 60})
    assert resp2.status_code == 201
    code2 = resp2.json()["code"]
    store = http.app.state.relay_store
    store.execute("UPDATE pairing_codes SET expires_at = '2000-01-01T00:00:00+00:00' WHERE code = :c",
                  {"c": code2})
    gone = http.get("/pairing/claim", params={"code": code2})
    assert gone.status_code == 410
    # 格式错
    assert http.get("/pairing/claim", params={"code": "ABC"}).status_code == 422
    # 创建缺参
    assert http.post("/pairing/codes", headers={"X-Relay-Token": saved},
                     json={"user_token": "x"}).status_code == 422


# ———— 配对码（小白引导） ————

def test_pairing_code_create_claim_one_time_and_expiry(client):
    http, registered = client
    # 创建:需要鉴权 + 必填字段
    assert http.post("/pairing/codes", json={}).status_code == 422
    resp = http.post("/pairing/codes", json={"user_token": registered["user_token"],
                                             "sync_key": "k" * 43, "ttl_seconds": 600})
    assert resp.status_code == 201
    code = resp.json()["code"]
    assert len(code) == 6 and not any(ch in "01IO" for ch in code)
    # 领取:一次性,返回凭据后即删
    claim = http.get("/pairing/claim", params={"code": code.lower()})  # 大小写归一
    assert claim.status_code == 200
    body = claim.json()
    assert body["user_token"] == registered["user_token"] and body["sync_key"] == "k" * 43
    assert body["user_id"] == registered["user_id"]
    # 二次领取 → 404(领取即删)
    assert http.get("/pairing/claim", params={"code": code}).status_code == 404
    # 格式错误 → 422
    assert http.get("/pairing/claim", params={"code": "ABC"}).status_code == 422


def test_pairing_code_expired(client, tmp_path):
    import sqlite3
    from relay_server import create_app as _ca
    settings = RelaySettings(data_dir=tmp_path / "d2", bootstrap_token=BOOTSTRAP)
    app2 = _ca(settings)
    with TestClient(app2) as http2:
        reg = http2.post("/devices", headers={"X-Relay-Bootstrap": BOOTSTRAP},
                         json={"label": "x"}).json()
        resp = http2.post("/pairing/codes", headers={"X-Relay-Token": reg["user_token"]},
                          json={"user_token": reg["user_token"],
                                "sync_key": "k" * 43, "ttl_seconds": 60})
        code = resp.json()["code"]
        # 直接把过期时间改到过去,验证惰性过期分支
        store = app2.state.relay_store
        store.execute("UPDATE pairing_codes SET expires_at = '2000-01-01T00:00:00+00:00' WHERE code = :c",
                      {"c": code})
        resp2 = http2.get("/pairing/claim", params={"code": code})
        assert resp2.status_code == 410
        assert http2.get("/pairing/claim", params={"code": code}).status_code == 404
