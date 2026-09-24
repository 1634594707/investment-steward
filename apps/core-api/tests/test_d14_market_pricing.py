"""D-14 市场定价层测试。

红线锁定：
- series_id 只用 100% 确定存在的 FRED 公开序列，不确定来源一律 pending（ADR-0006）；
- 市场定价层数值不进四维打分（独立端点 + 独立缓存前缀 pricing:）；
- 缓存 24h 过期重拉；过期后拉取失败回退旧缓存（as_of 如实标注）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用
from investment_steward_core.macro_pricing import PRICING_DEFS, trend_label

# ---- 共享脚手架（同 test_m52：文件凭据后端 + 安装启用插件） ----

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


def _enable_plugin(test_client, headers) -> None:
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200


def _fake_fred_observations(base_value: float, rising: bool, count: int = 25) -> list[dict]:
    """生成 desc 排序的观测（最新在前）；rising=True 表示越新越大。"""
    rows = []
    for index in range(count):
        age = count - 1 - index  # 0 = 最新
        value = base_value + (age * 0.1 if rising else -age * 0.1)
        rows.append({"obs_date": f"2026-08-{(count - index) % 28 + 1:02d}", "value": round(value, 2)})
    return rows


# ---- 纯函数：定义完整性 / 趋势计算 ----

def test_pricing_defs_cover_all_regions_and_never_fabricate():
    assert set(PRICING_DEFS.keys()) == {"us", "cn", "eu", "jp", "in"}
    for region, definitions in PRICING_DEFS.items():
        for definition in definitions:
            assert definition["key"] and definition["label"] and definition["kind"] and definition["note"]
            if "fred" in definition:
                assert definition["fred"], f"{region}/{definition['key']} series_id 不得为空串"


def test_trend_label_directions_and_insufficient_data():
    rising = _fake_fred_observations(100.0, rising=True)
    label, _ = trend_label(rising)
    assert label == "上升"
    falling = _fake_fred_observations(100.0, rising=False)
    assert trend_label(falling)[0] == "下降"
    flat = [{"obs_date": f"2026-08-{index + 1:02d}", "value": 100.0} for index in range(12)]
    assert trend_label(flat)[0] == "持平"
    # 观测不足 2*window → None（不编造）
    assert trend_label(rising[:6])[0] is None
    assert trend_label([])[0] is None


# ---- 端点：插件门禁 / region 校验 / pending（无 key） ----

def test_pricing_endpoint_requires_enabled_plugin(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    response = test_client.get("/evidence/macro/us/pricing", headers=headers)
    assert response.status_code == 409


def test_pricing_endpoint_unknown_region_404(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _enable_plugin(test_client, headers)
    response = test_client.get("/evidence/macro/xx/pricing", headers=headers)
    assert response.status_code == 404


def test_pricing_all_pending_without_fred_key(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _enable_plugin(test_client, headers)
    response = test_client.get("/evidence/macro/us/pricing", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["region"] == "us"
    assert body["rows"], "us 至少应有定价行定义"
    for row in body["rows"]:
        assert row["status"] == "pending"
        assert row["latest"] is None  # pending 不输出数值
        if "fred" in row["key"] or row["kind"] != "policy_odds":
            pass
    fred_rows = [row for row in body["rows"] if row["key"] in {"ff_target_upper", "breakeven_10y", "sp500", "vix"}]
    assert fred_rows and all("凭据库无 FRED key" in row["note"] for row in fred_rows)
    # 概率类（CME 增强）保持 pending 且不涉及 FRED key
    odds = [row for row in body["rows"] if row["key"] == "rate_odds"]
    assert odds and "CME" in odds[0]["note"]
    assert body["degraded_reason"]


# ---- 端点：mock FRED 实拉 + 缓存 + TTL ----

def test_pricing_ok_with_mocked_fred_and_cache(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _enable_plugin(test_client, headers)
    assert test_client.put(
        "/credentials/macro_data_key", json={"secret": "0123456789abcdef0123456789abcdef"}, headers=headers,
    ).status_code == 200

    calls: list[str] = []

    def fake_fetch(series_id: str, api_key: str, limit: int = 14) -> list[dict]:
        calls.append(series_id)
        return _fake_fred_observations(100.0, rising=True)

    monkeypatch.setattr("investment_steward_core.macro_feed.fetch_fred_series", fake_fetch)

    response = test_client.get("/evidence/macro/us/pricing", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    ok_rows = {row["key"]: row for row in body["rows"] if row["status"] == "ok"}
    assert set(ok_rows.keys()) == {"ff_target_upper", "breakeven_10y", "sp500", "vix"}
    sp = ok_rows["sp500"]
    assert sp["trend_5d"] == "上升"
    assert sp["ref_value"] is not None and sp["ref_date"]
    assert sp["dataset_version"].startswith("fred:SP500:")
    assert sp["source"] == "FRED（公开接口）"
    assert calls.count("SP500") == 1  # 实拉一次即落缓存

    # 第二次请求走缓存（TTL 内），不再实拉
    response2 = test_client.get("/evidence/macro/us/pricing", headers=headers)
    assert response2.status_code == 200
    assert calls.count("SP500") == 1


def test_pricing_ttl_expiry_fallback_and_refetch(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _enable_plugin(test_client, headers)
    assert test_client.put(
        "/credentials/macro_data_key", json={"secret": "0123456789abcdef0123456789abcdef"}, headers=headers,
    ).status_code == 200

    calls: list[str] = []

    def fake_fetch(series_id: str, api_key: str, limit: int = 14) -> list[dict]:
        calls.append(series_id)
        return _fake_fred_observations(100.0, rising=True)

    monkeypatch.setattr("investment_steward_core.macro_feed.fetch_fred_series", fake_fetch)
    assert test_client.get("/evidence/macro/us/pricing", headers=headers).status_code == 200
    assert calls.count("SP500") == 1

    # 缓存过期（as_of 改成 48h 前）
    from investment_steward_core.storage import Database

    db = Database(tmp_path / "steward.sqlite3")
    cached = db.get_macro_series("pricing:us:sp500")
    assert cached is not None
    cached["as_of"] = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    db.upsert_macro_series("pricing:us:sp500", cached)

    # 过期 + 拉取失败 → 回退旧缓存（真实数据不丢弃，as_of 仍标注）
    def failing_fetch(series_id: str, api_key: str, limit: int = 14) -> list[dict]:
        calls.append(series_id)
        raise RuntimeError("网络失败")

    monkeypatch.setattr("investment_steward_core.macro_feed.fetch_fred_series", failing_fetch)
    response = test_client.get("/evidence/macro/us/pricing", headers=headers)
    assert response.status_code == 200, response.text
    assert calls.count("SP500") == 2  # 过期触发了重拉
    sp = next(row for row in response.json()["rows"] if row["key"] == "sp500")
    assert sp["status"] == "ok" and sp["latest"] is not None

    # 恢复网络 → 过期重拉成功，缓存刷新
    monkeypatch.setattr("investment_steward_core.macro_feed.fetch_fred_series", fake_fetch)
    response2 = test_client.get("/evidence/macro/us/pricing", headers=headers)
    assert response2.status_code == 200
    assert calls.count("SP500") == 3
    sp2 = next(row for row in response2.json()["rows"] if row["key"] == "sp500")
    assert sp2["as_of"] is not None and sp2["as_of"] > sp["as_of"]
