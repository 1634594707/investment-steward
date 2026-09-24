"""阶段 B/C/D 端点测试:阶段条 / 策略包验签与受限执行 / 线性模型训练发布 / 两级实盘记录。"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from investment_steward_core import market_feed, quant_models, signing
from test_quant_pool import _bars


def _headers():
    return {"X-Core-Session-Token": "test-session-token"}


def _install_market(client):
    test_client, headers = client
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200


def _pub_pem(private_key: ed25519.Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


PACK_CODE = """
def run(bars):
    closes = [float(bar["close"]) for bar in bars]
    positions = []
    for i in range(len(closes)):
        if i < 5:
            positions.append(0.0)
        else:
            positions.append(1.0 if closes[i] > closes[i - 5] else -1.0)
    return positions
"""

PACK_CODE_FILE_ACCESS = """
def run(bars):
    with open("steward-pack-escape.txt", "w") as handle:
        handle.write("blocked")
    return [0.0 for _ in bars]
"""

PACK_CODE_NETWORK = """
import socket

def run(bars):
    return [0.0 for _ in bars]
"""


def _pack_manifest(code: str, *, name: str = "双均线策略包") -> dict[str, object]:
    return {
        "kind": "strategy_pack",
        "name": name,
        "version": "1.0.0",
        "author": "steward-dev",
        "entrypoint": "pack.main:run",
        "symbol": "510300",
        "capabilities": ["market.candles.read"],
        "payload": code,
        "payload_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
    }


def _signed_manifest(manifest: dict[str, object], private_key: ed25519.Ed25519PrivateKey) -> dict[str, object]:
    digest, signature = signing.sign_manifest(manifest, private_key)
    return {**manifest, "artifact_sha256": digest, "signature": signature}


def test_stages_shape(client):
    test_client, headers = client
    response = test_client.get("/quant/stages", headers=headers)
    assert response.status_code == 200
    stages = {item["key"]: item for item in response.json()["stages"]}
    assert set(stages) == {"A", "B", "C", "D"}
    assert all(stages[key]["open"] is True for key in ("A", "B", "C", "D"))
    assert "签名" in stages["B"]["reason"]


def _pin_test_key(client, monkeypatch, tmp_path, private_key):
    key_file = tmp_path / "pub.pem"
    key_file.write_text(_pub_pem(private_key), encoding="utf-8")
    monkeypatch.setattr(
        "investment_steward_core.api.app._pinned_public_key_pem",
        lambda settings: key_file.read_text(encoding="utf-8"),
    )


def test_strategy_pack_import_run_roundtrip(client, monkeypatch, tmp_path):
    test_client, headers = client
    _install_market(client)  # 先装插件(用默认信任锚),再为策略包换测试公钥
    private_key = ed25519.Ed25519PrivateKey.generate()
    _pin_test_key(client, monkeypatch, tmp_path, private_key)

    imported = test_client.post(
        "/quant/strategy-packs",
        json={"manifest": _signed_manifest(_pack_manifest(PACK_CODE), private_key)},
        headers=headers,
    )
    assert imported.status_code == 200, imported.text
    entry = imported.json()
    assert entry["pack_id"].startswith("sp-")
    assert entry["signature_valid"] is True and entry["state"] == "installed"

    listed = test_client.get("/quant/strategy-packs", headers=headers)
    assert listed.status_code == 200 and len(listed.json()) == 1

    monkeypatch.setattr("investment_steward_core.api.app.fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(120), "eastmoney"))
    run = test_client.post(f"/quant/strategy-packs/{entry['pack_id']}/run", headers=headers)
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["available"] is True and body["equity"] > 0
    assert body["bars"] == 119 and len(body["curve"]) == 119


def test_strategy_pack_blocked_operations(client, monkeypatch, tmp_path):
    test_client, headers = client
    _install_market(client)
    private_key = ed25519.Ed25519PrivateKey.generate()
    _pin_test_key(client, monkeypatch, tmp_path, private_key)
    monkeypatch.setattr("investment_steward_core.api.app.fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(120), "eastmoney"))

    # 文件访问:受限 builtins 直接拒绝(无 open/eval);网络:越权 import 由审计钩子拦截。
    reasons: list[str] = []
    for code in (PACK_CODE_FILE_ACCESS, PACK_CODE_NETWORK):
        imported = test_client.post(
            "/quant/strategy-packs",
            json={"manifest": _signed_manifest(_pack_manifest(code, name="越权包"), private_key)},
            headers=headers,
        )
        assert imported.status_code == 200
        pack_id = imported.json()["pack_id"]
        run = test_client.post(f"/quant/strategy-packs/{pack_id}/run", headers=headers)
        assert run.status_code == 200
        body = run.json()
        assert body["available"] is False
        reasons.append(str(body["degraded_reason"]))
    assert any("阻断" in reason for reason in reasons)  # 审计钩子层对越权 import 生效


def test_strategy_pack_tampered_rejected(client, monkeypatch, tmp_path):
    test_client, headers = client
    private_key = ed25519.Ed25519PrivateKey.generate()
    _pin_test_key(client, monkeypatch, tmp_path, private_key)

    # 篡改 payload_sha256 → 拒绝
    manifest = _pack_manifest(PACK_CODE)
    bad_hash = {**manifest, "payload_sha256": "0" * 64}
    rejected = test_client.post(
        "/quant/strategy-packs", json={"manifest": bad_hash}, headers=headers
    )
    assert rejected.status_code == 422

    # 篡改名称(签名后改内容) → 验签失败
    signed = _signed_manifest(_pack_manifest(PACK_CODE), private_key)
    tampered = {**signed, "name": "篡改后的包"}
    rejected = test_client.post("/quant/strategy-packs", json={"manifest": tampered}, headers=headers)
    assert rejected.status_code == 422

    # 未签名 → 拒绝
    unsigned = test_client.post(
        "/quant/strategy-packs", json={"manifest": _pack_manifest(PACK_CODE)}, headers=headers
    )
    assert unsigned.status_code == 422


def test_model_train_publish_and_replay(client, monkeypatch):
    test_client, headers = client
    _install_market(client)
    monkeypatch.setattr(market_feed, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(250), "eastmoney"))
    trained = test_client.post("/quant/models/510300", json={"lambda": 1.0}, headers=headers)
    assert trained.status_code == 200, trained.text
    entry = trained.json()
    assert entry["available"] is True and entry["artifact_id"].startswith("mw-")
    assert entry["type"] == "model_weights" and len(entry["weights"]) == 7
    # 确定性:同输入再训一次,权重一致
    again = test_client.post("/quant/models/510300", json={"lambda": 1.0}, headers=headers)
    assert again.json()["metrics"] == entry["metrics"]

    listed = test_client.get("/quant/parameter-sets", headers=headers)
    ids = [item["artifact_id"] for item in listed.json()]
    assert entry["artifact_id"] in ids
    replay = test_client.get(f"/quant/parameter-sets/{entry['artifact_id']}/replay", headers=headers)
    assert replay.status_code == 200 and replay.json()["available"] is True


def test_track_records_two_level_flow(client, monkeypatch):
    test_client, headers = client
    _install_market(client)
    monkeypatch.setattr(market_feed, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(250), "eastmoney"))
    published = test_client.post(
        "/quant/parameter-sets",
        json={"name": "实盘记录测试", "symbol": "510300", "formula_tokens": ["ma_ratio", "tanh"]},
        headers=headers,
    )
    artifact_id = published.json()["artifact_id"]

    # 自报记录:state=self_reported,无核验依据
    self_report = test_client.post(
        f"/quant/parameter-sets/{artifact_id}/track-records",
        json={"period_start": "2025-01-01", "period_end": "2025-06-30",
              "return_pct": 12.5, "max_drawdown_pct": 8.1, "note": "作者自述"},
        headers=headers,
    )
    assert self_report.status_code == 200, self_report.text
    assert self_report.json()["state"] == "self_reported"
    assert self_report.json()["statement_sha256"] is None

    # 对账单核验:内容 SHA-256 锚定,record_id 由哈希派生(同对账单幂等)
    statement = "broker statement 2025-07 export: equity 105200.00"
    verified = test_client.post(
        f"/quant/parameter-sets/{artifact_id}/track-records/verify",
        json={"statement": statement, "source": "某券商 柜台导出",
              "period_start": "2025-07-01", "period_end": "2025-09-30",
              "return_pct": 5.2, "max_drawdown_pct": 3.3},
        headers=headers,
    )
    assert verified.status_code == 200, verified.text
    record = verified.json()
    assert record["state"] == "broker_verified"
    assert record["statement_sha256"] == hashlib.sha256(statement.encode("utf-8")).hexdigest()

    # 列表联表:两种记录都在,状态可区分
    listed = test_client.get("/quant/parameter-sets", headers=headers).json()
    target = next(item for item in listed if item["artifact_id"] == artifact_id)
    states = {r["state"] for r in target["track_records"]}
    assert states == {"self_reported", "broker_verified"}

    # 空对账单拒绝核验
    empty = test_client.post(
        f"/quant/parameter-sets/{artifact_id}/track-records/verify",
        json={"statement": "  ", "source": "x", "period_start": "a", "period_end": "b"},
        headers=headers,
    )
    assert empty.status_code == 422
