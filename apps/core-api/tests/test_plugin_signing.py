"""阶段 9：插件制品签名与完整性校验测试。

用测试内生成的 Edwards25519 密钥对验证：合法签名可通过、篡改哈希拒绝、
错误密钥/未签名拒绝、版本锁拒绝跨版本直装。全部自含，不依赖 .runtime-local 私钥。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient
from investment_steward_core import channels, signing
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import (
    Evidence,
    PluginInstallation,
    PluginInstallationState,
    UpdateChannel,
)

PLUGINS_ROOT = Path(__file__).resolve().parents[3] / "plugins"


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _new_keypair():
    key = ed25519.Ed25519PrivateKey.generate()
    public_key_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return key, public_key_pem


def _sign_payload(payload: dict, key: ed25519.Ed25519PrivateKey) -> dict:
    digest, signature = signing.sign_manifest(payload, key)
    signed = dict(payload)
    signed["artifact_sha256"] = digest
    signed["signature"] = signature
    return signed


# —— signing 模块单元测试 ——


def test_verify_accepts_valid_signature():
    key, public_key_pem = _new_keypair()
    payload = {"plugin_id": "p.example", "release_version": "1.0.0", "capabilities": ["read_market_data"]}
    signed = _sign_payload(payload, key)
    signing.verify_manifest_integrity(signed, public_key_pem)  # 不抛异常即通过


def test_verify_rejects_tampered_content():
    key, public_key_pem = _new_keypair()
    payload = {"plugin_id": "p.example", "release_version": "1.0.0", "capabilities": ["read_market_data"]}
    signed = _sign_payload(payload, key)
    tampered = dict(signed)
    tampered["capabilities"] = ["submit_evidence"]  # 篡改内容但保留旧签名/哈希
    with pytest.raises(ValueError, match="SHA-256"):
        signing.verify_manifest_integrity(tampered, public_key_pem)


def test_verify_rejects_wrong_key_signature():
    publisher_key, _ = _new_keypair()
    _, not_publisher_public_key_pem = _new_keypair()  # 被内嵌的「发布者公钥」实际是别人的
    payload = {"plugin_id": "p.example", "release_version": "1.0.0"}
    signed = _sign_payload(payload, publisher_key)  # 发布者私钥签名
    with pytest.raises(ValueError, match="签名"):
        signing.verify_manifest_integrity(signed, not_publisher_public_key_pem)  # 用错误公钥验签


def test_verify_rejects_unsigned_fixture():
    _, public_key_pem = _new_keypair()
    payload = {"plugin_id": "p.example", "release_version": "1.0.0"}
    unsigned = dict(payload)
    unsigned["artifact_sha256"] = signing.content_sha256(payload)
    unsigned["signature"] = signing.UNSIGNED_MARKER
    with pytest.raises(ValueError, match="未签名"):
        signing.verify_manifest_integrity(unsigned, public_key_pem)


def test_verify_rejects_non_ed25519_public_key(tmp_path):
    _new_keypair()
    payload = {"plugin_id": "p.example", "release_version": "1.0.0"}
    with pytest.raises(ValueError):
        signing.verify_manifest_integrity(payload, "not-a-pem")


# —— 集成测试：临时签名注册表 ——


def _make_catalog_app(tmp_path: Path, registry_dir: Path, public_key_pem: str):
    pub_file = tmp_path / "pub.pem"
    pub_file.write_text(public_key_pem, encoding="utf-8")
    token = "test-session-token"
    app = create_app(
        CoreSettings(session_token=token, data_dir=tmp_path / "data", plugin_public_key_file=pub_file),
    )
    return TestClient(app), {"X-Core-Session-Token": token}


def _copy_single_plugin(registry_dir: Path, plugin_id: str) -> Path:
    src = PLUGINS_ROOT / "official" / plugin_id
    dest = registry_dir / "official" / plugin_id
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    return dest


def _write_version(
    registry_dir: Path,
    plugin_id: str,
    version: str,
    mutate: dict | None = None,
    key: ed25519.Ed25519PrivateKey | None = None,
) -> Path:
    """把某插件的一个版本写进临时注册表（rglob 可发现多版本）。"""
    src = PLUGINS_ROOT / "official" / plugin_id
    payload = json.loads(src.joinpath("manifest.json").read_text(encoding="utf-8"))
    payload["release_version"] = version
    if mutate:
        payload.update(mutate)
    return _write_signed(registry_dir, plugin_id, version, payload, key)


def _write_signed(
    registry_dir: Path,
    plugin_id: str,
    version: str,
    payload: dict,
    key: ed25519.Ed25519PrivateKey | None = None,
) -> Path:
    if key is None:
        key, _ = _new_keypair()
    signed = _sign_payload(payload, key)
    dest = registry_dir / "official" / plugin_id / version / "manifest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


# —— 阶段 9 更新事务 ——


def _make_versioned_catalog(tmp_path, registry_dir, public_key_pem):
    pub_file = tmp_path / "pub.pem"
    pub_file.write_text(public_key_pem, encoding="utf-8")
    token = "test-session-token"
    app = create_app(
        CoreSettings(session_token=token, data_dir=tmp_path / "data", plugin_public_key_file=pub_file),
    )
    return TestClient(app), {"X-Core-Session-Token": token}


def test_update_transaction_commits_to_new_version(tmp_path, monkeypatch):
    key, public_key_pem = _new_keypair()
    registry_dir = tmp_path / "registry"
    _write_version(registry_dir, "cn-market-data", "0.1.0", key=key)
    _write_version(registry_dir, "cn-market-data", "0.2.0", key=key)
    monkeypatch.setenv("STEWARD_PLUGIN_REGISTRY_DIR", str(registry_dir))
    test_client, headers = _make_versioned_catalog(tmp_path, registry_dir, public_key_pem)

    installed = test_client.post("/plugins/official.cn-market-data/install", headers=headers)
    assert installed.status_code == 200
    assert installed.json()["release_version"] == "0.1.0"

    updated = test_client.post(
        "/plugins/official.cn-market-data/update",
        headers=headers,
        json={"target_version": "0.2.0"},
    )
    assert updated.status_code == 200
    assert updated.json()["release_version"] == "0.2.0"
    assert updated.json()["state"] == "enabled"
    # 更新成功后无残留候选
    assert test_client.app.state.core.database.list_update_candidates() == []


def test_update_health_check_failure_rolls_back(tmp_path, monkeypatch):
    """目标版本声明的 schema 是 Core 未知 → 健康检查失败 → 409 且旧版本保持可用、候选清理。"""
    key, public_key_pem = _new_keypair()
    registry_dir = tmp_path / "registry"
    _write_version(registry_dir, "cn-market-data", "0.1.0", key=key)
    _write_version(
        registry_dir, "cn-market-data", "0.2.0", key=key, mutate={"schema_versions": {"Bogus": "1.0"}}
    )
    monkeypatch.setenv("STEWARD_PLUGIN_REGISTRY_DIR", str(registry_dir))
    test_client, headers = _make_versioned_catalog(tmp_path, registry_dir, public_key_pem)

    test_client.post("/plugins/official.cn-market-data/install", headers=headers)
    response = test_client.post(
        "/plugins/official.cn-market-data/update",
        headers=headers,
        json={"target_version": "0.2.0"},
    )
    assert response.status_code == 409
    assert "回滚" in response.json()["detail"]

    still = test_client.app.state.core.database.get_plugin_installation("official.cn-market-data")
    assert still.release_version == "0.1.0"
    assert still.state == PluginInstallationState.ENABLED
    assert test_client.app.state.core.database.list_update_candidates() == []


def test_update_rejects_not_installed_and_same_version(client):
    test_client, headers = client
    # 未安装（注册表里有，但从未安装）→ 409，且不进入注册表版本解析
    not_installed = test_client.post(
        "/plugins/official.golden-evidence/update",
        headers=headers,
        json={"target_version": "0.1.0"},
    )
    assert not_installed.status_code == 409
    assert "未安装" in not_installed.json()["detail"]
    # cn-market-data 已引导安装 0.1.0，同版本 → 409 一致
    same = test_client.post(
        "/plugins/official.cn-market-data/update",
        headers=headers,
        json={"target_version": "0.1.0"},
    )
    assert same.status_code == 409
    assert "一致" in same.json()["detail"]


# —— 阶段 9 撤销与安全降级 ——


def test_revoke_blocks_running_but_preserves_history(client):
    test_client, headers = client
    core = test_client.app.state.core
    core.database.insert_evidence(
        Evidence(
            evidence_id=uuid4(),
            tenant_id=core.local_user_id,
            subject_refs=["instrument:CN:ETF:510300"],
            evidence_type="quote",
            source_name="历史样例",
            license_status="test-fixture",
            content_hash="cafe" + "0" * 28,
            producer_plugin_id="official.cn-market-data",
            schema_version="1.0",
            summary="撤销前的历史证据，应保留并仍可读",
        )
    )
    revoked = test_client.post("/plugins/official.cn-market-data/revoke", headers=headers)
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"

    # 阻断运行：行情端点要求 ENABLED，REVOKED 拒绝。
    assert (
        test_client.get("/market/candles/510300", headers=headers).status_code == 409
    )
    # 历史证据保留并可读。
    assert len(test_client.get("/evidence", headers=headers).json()) >= 1


def test_revoke_unknown_plugin_is_404(client):
    test_client, headers = client
    assert (
        test_client.post("/plugins/official.never-installed/revoke", headers=headers).status_code
        == 404
    )


def test_install_accepts_valid_signed_manifest(tmp_path, monkeypatch):
    key, public_key_pem = _new_keypair()
    registry_dir = tmp_path / "registry"
    dest = _copy_single_plugin(registry_dir, "cn-market-data")
    payload = json.loads(dest.joinpath("manifest.json").read_text(encoding="utf-8"))
    signed = _sign_payload(payload, key)
    dest.joinpath("manifest.json").write_text(json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("STEWARD_PLUGIN_REGISTRY_DIR", str(registry_dir))

    test_client, headers = _make_catalog_app(tmp_path, registry_dir, public_key_pem)
    response = test_client.post("/plugins/official.cn-market-data/install", headers=headers)
    assert response.status_code == 200
    assert response.json()["release_version"] == "0.1.0"
    assert response.json()["state"] == "enabled"


def test_install_rejects_tampered_signed_manifest(tmp_path, monkeypatch):
    key, public_key_pem = _new_keypair()
    registry_dir = tmp_path / "registry"
    # 引导依赖的 cn-market-data 保持合法，避免启动即失败；另一插件被篡改用于测试安装拒绝。
    valid_dest = _copy_single_plugin(registry_dir, "cn-market-data")
    valid_payload = json.loads(valid_dest.joinpath("manifest.json").read_text(encoding="utf-8"))
    valid_signed = _sign_payload(valid_payload, key)
    valid_dest.joinpath("manifest.json").write_text(
        json.dumps(valid_signed, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    tampered_dest = _copy_single_plugin(registry_dir, "golden-evidence")
    payload = json.loads(tampered_dest.joinpath("manifest.json").read_text(encoding="utf-8"))
    signed = _sign_payload(payload, key)
    signed["description"] = "被篡改的描述"  # 改内容但保留原签名/哈希
    tampered_dest.joinpath("manifest.json").write_text(
        json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    monkeypatch.setenv("STEWARD_PLUGIN_REGISTRY_DIR", str(registry_dir))

    test_client, headers = _make_catalog_app(tmp_path, registry_dir, public_key_pem)
    response = test_client.post("/plugins/official.golden-evidence/install", headers=headers)
    assert response.status_code == 422


def test_install_rejects_unknown_plugin(tmp_path, monkeypatch):
    _, public_key_pem = _new_keypair()
    empty_registry = tmp_path / "empty-registry"
    empty_registry.mkdir(parents=True)
    monkeypatch.setenv("STEWARD_PLUGIN_REGISTRY_DIR", str(empty_registry))

    # 空注册表：引导安装会因 cn-market-data 不在已签名注册表而拒绝启动。
    with pytest.raises(ValueError, match="不在已签名注册表"):
        _make_catalog_app(tmp_path, empty_registry, public_key_pem)


def test_install_version_lock_rejects_downgrade_overwrite(client):
    """安装锁：已装其它精确版本时，同 id 再次直装被拒绝（不默认覆盖）。"""
    test_client, headers = client
    core = test_client.app.state.core
    core.database.upsert_plugin_installation(
        PluginInstallation(
            plugin_id="official.cn-market-data",
            release_version="0.0.1",  # 与注册表 0.1.0 不同
            state=PluginInstallationState.ENABLED,
            granted_capabilities=[],
            artifact_sha256="a" * 64,
        )
    )
    response = test_client.post("/plugins/official.cn-market-data/install", headers=headers)
    assert response.status_code == 409


def test_install_same_version_is_idempotent(client):
    test_client, headers = client
    first = test_client.post("/plugins/official.cn-market-data/install", headers=headers)
    second = test_client.post("/plugins/official.cn-market-data/install", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200


# —— 阶段 9 三套更新通道分离（Host / 插件 / 内容） ——


def test_channel_of_maps_kinds_and_rejects_unknown():
    assert channels.channel_of("core_binary") == UpdateChannel.HOST
    assert channels.channel_of("plugin_manifest") == UpdateChannel.PLUGIN
    assert channels.channel_of("evidence") == UpdateChannel.CONTENT
    assert channels.channel_of("market_candles") == UpdateChannel.CONTENT
    with pytest.raises(ValueError, match="未知制品类型"):
        channels.channel_of("not-a-real-kind")


def test_cross_channel_overwrite_guard_flags_host_or_content_change():
    snapshot = channels.ChannelSnapshot(
        host_version="1.0.0",
        plugin_versions=(("official.cn-market-data", "0.1.0"),),
        content_fingerprint="fingerprint-a",
    )
    # 插件通道变化（更新预期）——不触发。
    channels.assert_no_cross_channel_overwrite(
        snapshot,
        channels.ChannelSnapshot(
            host_version="1.0.0",
            plugin_versions=(("official.cn-market-data", "0.2.0"),),
            content_fingerprint="fingerprint-a",
        ),
    )
    # HOST 版本被改——触发。
    with pytest.raises(ValueError, match="HOST"):
        channels.assert_no_cross_channel_overwrite(
            snapshot,
            channels.ChannelSnapshot(
                host_version="1.0.1",
                plugin_versions=snapshot.plugin_versions,
                content_fingerprint=snapshot.content_fingerprint,
            ),
        )
    # CONTENT 指纹被改——触发。
    with pytest.raises(ValueError, match="CONTENT"):
        channels.assert_no_cross_channel_overwrite(
            snapshot,
            channels.ChannelSnapshot(
                host_version=snapshot.host_version,
                plugin_versions=snapshot.plugin_versions,
                content_fingerprint="fingerprint-b",
            ),
        )


def test_channels_endpoint_reports_three_channels(client):
    test_client, headers = client
    channels_body = test_client.get("/channels", headers=headers).json()
    assert [entry["channel"] for entry in channels_body] == ["host", "plugin", "content"]
    by_name = {entry["channel"]: entry for entry in channels_body}
    assert by_name["host"]["plugin_api_readonly"] == "true"
    assert "@" in by_name["plugin"]["current"]  # pid@version
    assert by_name["content"]["current"] != ""


def test_plugin_update_never_rewrites_host_or_content_channel(tmp_path, monkeypatch):
    """互不覆盖：走一次成功更新事务，/channels 对照 HOST 版本与 CONTENT 指纹不变。"""
    key, public_key_pem = _new_keypair()
    registry_dir = tmp_path / "registry"
    _write_version(registry_dir, "cn-market-data", "0.1.0", key=key)
    _write_version(registry_dir, "cn-market-data", "0.2.0", key=key)
    monkeypatch.setenv("STEWARD_PLUGIN_REGISTRY_DIR", str(registry_dir))

    test_client, headers = _make_versioned_catalog(tmp_path, registry_dir, public_key_pem)
    core = test_client.app.state.core
    core.database.insert_evidence(
        Evidence(
            evidence_id=uuid4(),
            tenant_id=core.local_user_id,
            subject_refs=["instrument:CN:ETF:510300"],
            evidence_type="quote",
            source_name="历史样例",
            license_status="test-fixture",
            content_hash="beef" + "0" * 28,
            producer_plugin_id="official.cn-market-data",
            schema_version="1.0",
            summary="更新前后的历史证据，插件的此更新不得改写它",
        )
    )
    # 引导安装 0.1.0。
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    before = {
        entry["channel"]: entry
        for entry in test_client.get("/channels", headers=headers).json()
    }

    updated = test_client.post(
        "/plugins/official.cn-market-data/update",
        headers=headers,
        json={"target_version": "0.2.0"},
    )
    assert updated.status_code == 200, updated.text

    after = {
        entry["channel"]: entry
        for entry in test_client.get("/channels", headers=headers).json()
    }
    # HOST 版本与 CONTENT 指纹不变（互不覆盖）；PLUGIN 版本前进。
    assert after["host"]["current"] == before["host"]["current"]
    assert after["content"]["current"] == before["content"]["current"]
    assert "official.cn-market-data@0.2.0" in after["plugin"]["current"]