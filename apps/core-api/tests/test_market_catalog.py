"""§6 GitHub 插件市场：目录判定（本地验签）+ 安装门禁。

关键口径：
- 目录仅元数据；每条发布在本机逐条验签（Ed25519 + artifact_sha256）。
- 安装要求"代码包已随本机签名注册表就位"——GitHub 目录只做发现与验签，不复制代码。
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from investment_steward_core import signing, sync_client
from investment_steward_core.api import app as api_app
from investment_steward_core.credential_store import DbCredentialStore
from investment_steward_core.domain.models import PluginManifest

_PUBLISH_KEY = ed25519.Ed25519PrivateKey.generate()
_PUB_PEM = _PUBLISH_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode("ascii")


def _signed_manifest(plugin_id: str = "official.market-test", version: str = "0.2.0",
                     *, sdk_min: str = "0.1", sdk_max: str = "1.x",
                     schema_versions: dict | None = None) -> dict:
    payload = {
        "publisher": "aplicity",
        "plugin_id": plugin_id,
        "release_version": version,
        "display_name": "市场测试插件",
        "description": "§6 市场目录端到端测试插件",
        "plugin_type": "market_test",
        "license": "MIT",
        "sdk_min": sdk_min,
        "sdk_max": sdk_max,
        "mount": "in_page",
        "schema_versions": schema_versions or {},
        "entrypoint": "market_test:run",
    }
    digest, signature = signing.sign_manifest(payload, _PUBLISH_KEY)
    return {**payload, "artifact_sha256": digest, "signature": signature}


def _entry(manifest: dict, *, revoked: bool = False) -> dict:
    return {"plugin_id": manifest["plugin_id"], "release_version": manifest["release_version"],
            "manifest": manifest, "revoked": revoked, "prerelease": False}


def _patch_market(monkeypatch, releases: list[dict], registry: list[dict] | None = None) -> None:
    """注入:测试公钥 + 假 MarketClient + 本机注册表(缺省=目录中未撤回条目)。"""

    class FakeMarket:
        def __init__(self, config) -> None:
            pass

        def catalog(self) -> dict:
            return {"source": "github", "stale": False, "releases": releases,
                    "revoked": [e["plugin_id"] for e in releases if e["revoked"]],
                    "note": None}

    monkeypatch.setattr(sync_client, "MarketClient", FakeMarket)
    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    monkeypatch.setattr(api_app, "_pinned_public_key_pem", lambda settings: _PUB_PEM)
    registry = registry if registry is not None else [
        e["manifest"] for e in releases if not e["revoked"]
    ]
    monkeypatch.setattr(api_app, "_registry_entries",
                        lambda: [(m, PluginManifest.model_validate(m)) for m in registry])


def test_market_catalog_requires_sync_config(client):
    http, headers = client
    resp = http.get("/market/catalog", headers=headers)
    assert resp.status_code == 409
    assert "sync 未配置" in resp.json()["detail"]


def test_market_catalog_verdicts(client, monkeypatch):
    http, headers = client
    good = _signed_manifest()
    tampered = dict(_signed_manifest(plugin_id="official.tampered"), signature="bad")
    revoked = _signed_manifest(plugin_id="official.revoked")
    sdk_bad = _signed_manifest(plugin_id="official.sdkbad", sdk_min="9.0")
    schema_bad = _signed_manifest(plugin_id="official.schemabad", schema_versions={"Alien": "1"})
    _patch_market(monkeypatch, [_entry(good), _entry(tampered), _entry(revoked, revoked=True),
                                _entry(sdk_bad), _entry(schema_bad)])
    http.post("/sync/config", headers=headers, json={
        "relay_url": "https://st.example", "user_token": "t" * 30,
        "device_id": "d-mkt", "user_id": "u-mkt",
    })
    body = http.get("/market/catalog", headers=headers).json()
    assert body["ok"] is True and body["host_version"]
    verdicts = {e["plugin_id"]: e for e in body["entries"]}
    assert verdicts["official.market-test"]["signature_verified"] is True
    assert verdicts["official.market-test"]["verdict"] == "可安装"
    assert verdicts["official.tampered"]["signature_verified"] is False
    assert "验签失败" in verdicts["official.tampered"]["verdict"]
    assert verdicts["official.revoked"]["revoked"] is True
    assert "撤回" in verdicts["official.revoked"]["verdict"]
    assert verdicts["official.sdkbad"]["sdk_compatible"] is False
    assert verdicts["official.schemabad"]["schema_compatible"] is False


def test_market_install_flow_and_gates(client, monkeypatch):
    http, headers = client
    good = _signed_manifest()
    _patch_market(monkeypatch, [_entry(good), _entry(_signed_manifest(version="9.9.9")),
                                _entry(_signed_manifest(plugin_id="official.revoked"), revoked=True),
                                _entry(_signed_manifest(plugin_id="official.notlocal"))],
                  registry=[good])
    http.post("/sync/config", headers=headers, json={
        "relay_url": "https://st.example", "user_token": "t" * 30,
        "device_id": "d-mkt", "user_id": "u-mkt",
    })
    # 正常安装:目录验签 + 本机注册表同版本就位 → 安装成功
    installed = http.post("/market/install", headers=headers, json={"plugin_id": "official.market-test"})
    assert installed.status_code == 200
    assert installed.json()["release_version"] == "0.2.0"
    assert installed.json()["state"] == "enabled"
    # 重复安装同版本 → 幂等(仍 200)
    assert http.post("/market/install", headers=headers, json={"plugin_id": "official.market-test"}).status_code == 200
    # 目录无此版本 → 404
    assert http.post("/market/install", headers=headers,
                     json={"plugin_id": "official.market-test", "version": "3.0.0"}).status_code == 404
    assert http.post("/market/install", headers=headers,
                     json={"plugin_id": "official.nosuch"}).status_code == 404
    # 撤回发布 → 409
    resp = http.post("/market/install", headers=headers, json={"plugin_id": "official.revoked"})
    assert resp.status_code == 409 and "撤回" in resp.json()["detail"]
    # 验签通过但代码包未随本机注册表就位 → 409(诚实门禁,不静默降级)
    resp = http.post("/market/install", headers=headers, json={"plugin_id": "official.notlocal"})
    assert resp.status_code == 409 and "注册表" in resp.json()["detail"]


def test_market_install_rejects_artifact_mismatch(client, monkeypatch):
    """目录 manifest 与本机代码包 artifact_sha256 不一致(同版本不同内容) → 422。"""
    http, headers = client
    remote = _signed_manifest()
    local = _signed_manifest()  # 相同字段但独立签名 → 相同哈希;改 entrypoint 造差异
    remote["entrypoint"] = "market_test:run_remote"
    digest, signature = signing.sign_manifest(remote, _PUBLISH_KEY)
    remote["artifact_sha256"], remote["signature"] = digest, signature
    monkeypatch.setattr(sync_client, "MarketClient", type("M", (), {
        "__init__": lambda self, config: None,
        "catalog": lambda self: {"source": "github", "stale": False,
                                 "releases": [_entry(remote)], "revoked": [], "note": None},
    }))
    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    monkeypatch.setattr(api_app, "_pinned_public_key_pem", lambda settings: _PUB_PEM)
    monkeypatch.setattr(api_app, "_registry_entries",
                        lambda: [(local, PluginManifest.model_validate(local))])
    http.post("/sync/config", headers=headers, json={
        "relay_url": "https://st.example", "user_token": "t" * 30,
        "device_id": "d-mkt", "user_id": "u-mkt",
    })
    resp = http.post("/market/install", headers=headers, json={"plugin_id": "official.market-test"})
    assert resp.status_code == 422
    assert "artifact_sha256 不一致" in resp.json()["detail"]
