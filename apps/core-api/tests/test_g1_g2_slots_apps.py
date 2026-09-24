"""G1 插槽注册表 / UICard 分发 + G2 双挂载与应用区。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient
from investment_steward_core import signing
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

PLUGINS_ROOT = Path(__file__).resolve().parents[3] / "plugins"

# 设计稿 SLOTS 槽：slot / page（前端 slotPageOf 映射；app.tactics 为 2026-09-07 战法雷达新增，
# app.youzi 为 2026-09-16 游资雷达新增）。
EXPECTED_SLOTS = [
    ("today.brief", "today", "L3"),
    ("today.learning", "today", "L2"),
    ("invest.market_view", "investment", "L3"),
    ("invest.thesis", "investment", "L2"),
    ("research.board", "research", "L3"),
    ("review.plan", "review", "L2"),
    ("notification.global", "settings", "L0"),
    ("app.library", "library", "L3"),
    ("app.macro", "macro", "L3"),
    ("app.tactics", "tactics", "L3"),
    ("app.youzi", "youzi", "L3"),
]


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


def _copy_and_sign(registry_dir: Path, plugin_id: str, key: ed25519.Ed25519PrivateKey) -> str:
    src = PLUGINS_ROOT / "official" / plugin_id
    payload = json.loads(src.joinpath("manifest.json").read_text(encoding="utf-8"))
    return _write_signed(registry_dir, plugin_type_dir=plugin_id, payload=payload, key=key)


def _write_signed(registry_dir: Path, plugin_type_dir: str, payload: dict, key: ed25519.Ed25519PrivateKey) -> str:
    signed = _sign_payload(payload, key)
    dest = registry_dir / "official" / plugin_type_dir / "manifest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(signed, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(dest)


def _own_page_payload(plugin_id: str) -> dict:
    return {
        "publisher": "investment-steward",
        "plugin_id": plugin_id,
        "release_version": "0.1.0",
        "display_name": f"测试应用 {plugin_id}",
        "description": "契约测试用的独立应用插件。",
        "plugin_type": "own_page_app",
        "license": "internal-fixture",
        "sdk_min": "1.0",
        "sdk_max": "1.x",
        "capabilities": [],
        "ui_slots": [
            {"slot": "app.library", "level": "L3"},
            {"slot": "today.learning", "level": "L2"},
        ],
        "mount": "own_page",
        "schema_versions": {},
        "entrypoint": "own.page",
        "network_allowlist": [],
        "side_effects": [],
        "requires_confirmation": False,
        "supports_markets": ["CN"],
    }


def _make_own_page_catalog(tmp_path: Path, key, public_key_pem, plugin_ids, cap: int):
    registry_dir = tmp_path / "registry"
    # 引导依赖的 cn-market-data 必须存在且合法，否则启动失败。
    _copy_and_sign(registry_dir, "cn-market-data", key)
    for plugin_id in plugin_ids:
        _write_signed(
            registry_dir,
            plugin_type_dir=plugin_id,
            payload=_own_page_payload(plugin_id),
            key=key,
        )
    pub_file = tmp_path / "pub.pem"
    pub_file.write_text(public_key_pem, encoding="utf-8")
    token = "test-session-token"
    app = create_app(
        CoreSettings(
            session_token=token,
            data_dir=tmp_path / "data",
            plugin_public_key_file=pub_file,
            app_library_cap=cap,
        )
    )
    return TestClient(app), {"X-Core-Session-Token": token}


# —— G1-1：插槽注册表 ——


def test_slots_registry_returns_rows_matching_design(client):
    test_client, headers = client
    rows = test_client.get("/slots", headers=headers).json()
    # 硬编码槽数会让每次新增插槽都要改这里的魔数；改为与冻结的 EXPECTED_SLOTS 对齐
    # （顺序与内容由下面那条断言逐项校验，这里只是长度一致性的显式表达）。
    assert len(rows) == len(EXPECTED_SLOTS)
    slots = [(row["slot"], row["page"], row["level"]) for row in rows]
    assert slots == EXPECTED_SLOTS
    # 状态栏五段：cap 与注册表一致，used/cap 都非负。
    for row in rows:
        for key in ("targeted", "used", "cap", "queued"):
            assert key in row
        assert 0 <= row["used"] <= row["cap"]
        assert row["queued"] == max(0, row["targeted"] - row["cap"])


def test_slots_cn_market_data_targets_market_view(client):
    test_client, headers = client
    rows = {row["slot"]: row for row in test_client.get("/slots", headers=headers).json()}
    # 引导安装的 cn-market-data 仅声明 invest.market_view（L3），故此处 targeted=1。
    assert rows["invest.market_view"]["targeted"] == 1
    assert rows["invest.market_view"]["used"] == 1


# —— G1-4：UICard 分发 ——


def test_cards_l0_silent_slot_always_empty(client):
    test_client, headers = client
    # 无论账本如何，notification.global 永不产卡（L0 静默，仅证据账本）。
    assert test_client.get("/cards/slots/notification.global", headers=headers).json() == []


def test_cards_unknown_slot_404(client):
    test_client, headers = client
    assert test_client.get("/cards/slots/not.a.slot", headers=headers).status_code == 404


def test_cards_today_learning_binds_real_holding(client):
    test_client, headers = client
    # 空账本 → 无卡（不编造）。
    assert test_client.get("/cards/slots/today.learning", headers=headers).json() == []
    # 有了持仓 → 学习卡绑定真实标的。
    test_client.post(
        "/holdings",
        headers=headers,
        json={"instrument": "CN:ETF:510300", "label": "沪深300", "status": "holding"},
    )
    cards = test_client.get("/cards/slots/today.learning", headers=headers).json()
    assert len(cards) == 1
    assert cards[0]["renderer"] == "summary_row"
    assert cards[0]["slot"] == "today.learning"
    assert cards[0]["limitations"]


def test_cards_research_board_empty_without_runs(client):
    test_client, headers = client
    assert test_client.get("/cards/slots/research.board", headers=headers).json() == []


# —— G2-3：catalog 输出去向 ——


def test_catalog_resolved_outputs_from_slot_registry(client):
    test_client, headers = client
    entries = test_client.get("/plugins/catalog", headers=headers).json()
    by_id = {entry["manifest"]["plugin_id"]: entry for entry in entries}
    market = by_id["official.cn-market-data"]
    assert market["resolved_outputs"] == [
        {"slot": "invest.market_view", "page": "investment", "level": "L3"}
    ]
    learning = by_id["official.investing-learning"]
    assert learning["resolved_outputs"] == [
        {"slot": "today.learning", "page": "today", "level": "L2"}
    ]


# —— G2-1 / G2-2：独立应用配额与应用壳 ——


def test_own_page_quota_409_when_app_library_full(tmp_path, monkeypatch):
    key, public_key_pem = _new_keypair()
    # cap=1：第 1 个独立应用占满 app.library，第 2 个被 409 拒绝。
    plugin_ids = ["official.fake-app-a", "official.fake-app-b"]
    monkeypatch.setenv("STEWARD_PLUGIN_REGISTRY_DIR", str(tmp_path / "registry"))
    test_client, headers = _make_own_page_catalog(tmp_path, key, public_key_pem, plugin_ids, cap=1)

    first = test_client.post(f"/plugins/{plugin_ids[0]}/install", headers=headers)
    assert first.status_code == 200

    second = test_client.post(f"/plugins/{plugin_ids[1]}/install", headers=headers)
    assert second.status_code == 409
    assert "应用区已满" in second.json()["detail"]


def test_own_page_catalog_app_envelope_and_mount(tmp_path, monkeypatch):
    key, public_key_pem = _new_keypair()
    plugin_id = "official.fake-app"
    monkeypatch.setenv("STEWARD_PLUGIN_REGISTRY_DIR", str(tmp_path / "registry"))
    test_client, headers = _make_own_page_catalog(tmp_path, key, public_key_pem, [plugin_id], cap=4)

    assert test_client.post(f"/plugins/{plugin_id}/install", headers=headers).status_code == 200
    # app.library 占 1/4。
    slots = {row["slot"]: row for row in test_client.get("/slots", headers=headers).json()}
    assert slots["app.library"]["used"] == 1
    # 应用壳端点返回 envelope。
    envelope = test_client.get(f"/plugins/{plugin_id}/app", headers=headers)
    assert envelope.status_code == 200
    body = envelope.json()
    for key in ("page_title", "kicker", "content_schema_version", "payload"):
        assert key in body
    assert body["page_title"] == f"测试应用 {plugin_id}"


def test_app_envelope_rejects_in_page_plugin(client):
    test_client, headers = client
    # cn-market-data 不是独立应用，应用壳端点拒绝。
    assert (
        test_client.get("/plugins/official.cn-market-data/app", headers=headers).status_code
        == 404
    )


# —— G2-4：研读图书馆插件上架 ——


def test_reading_library_manifest_in_registry_and_valid(client):
    """上架清单必须在真实注册表里、能解析、签名与固定公钥相符、解析出去向。"""
    test_client, headers = client
    entries = test_client.get("/plugins/catalog", headers=headers).json()
    by_id = {entry["manifest"]["plugin_id"]: entry for entry in entries}
    reading = by_id["official.reading-library"]
    manifest = reading["manifest"]
    assert manifest["mount"] == "own_page"
    assert manifest["plugin_type"] == "library_app"
    # ui_slots：app.library（L3）+ today.learning（L2）。
    assert [(s["slot"], s["level"]) for s in manifest["ui_slots"]] == [
        ("app.library", "L3"),
        ("today.learning", "L2"),
    ]
    # catalog 解析出去向（page 由服务端插槽注册表解析）。
    assert reading["resolved_outputs"] == [
        {"slot": "app.library", "page": "library", "level": "L3"},
        {"slot": "today.learning", "page": "today", "level": "L2"},
    ]
    # 清单签名与固定发布者公钥相符（可上架的前提）。
    payload = json.loads(
        (PLUGINS_ROOT / "official" / "reading-library" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    pub_key_pem = (
        Path(__file__).resolve().parents[3]
        / "apps" / "core-api" / "keys" / "steward-plugin-publishing.pub.pem"
    ).read_text(encoding="utf-8")
    signing.verify_manifest_integrity(payload, pub_key_pem)  # 不抛即通过


def test_reading_library_install_occupies_app_library(client):
    """安装研读图书馆：占 app.library 1/4，应用壳端点返回荐读计划 payload。"""
    test_client, headers = client
    assert (
        test_client.post("/plugins/official.reading-library/install", headers=headers).status_code
        == 200
    )
    slots = {row["slot"]: row for row in test_client.get("/slots", headers=headers).json()}
    assert slots["app.library"]["used"] == 1
    envelope = test_client.get("/plugins/official.reading-library/app", headers=headers)
    assert envelope.status_code == 200
    body = envelope.json()
    assert body["page_title"] == "研读图书馆"
    assert set(body["payload"]) == {"latest_plan"}
    # 未生成荐读计划时 payload.latest_plan 为 None（不编造）。
    assert body["payload"]["latest_plan"] is None