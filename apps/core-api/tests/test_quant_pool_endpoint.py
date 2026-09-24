"""分享池阶段 A 端点测试:发布/列表/回放/Fork 走 HTTP。"""

from __future__ import annotations

from investment_steward_core import market_feed
from test_quant_pool import _bars


def _headers():
    return {"X-Core-Session-Token": "test-session-token"}


def _install_all(client):
    test_client, headers = client
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    assert test_client.post("/plugins/official.support-resistance/install", headers=headers).status_code == 200


def test_pool_endpoints_roundtrip(client, monkeypatch):
    test_client, headers = client
    _install_all(client)
    monkeypatch.setattr(market_feed, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(120), "eastmoney"))
    publish = test_client.post(
        "/quant/parameter-sets",
        json={"name": "动量参数集", "symbol": "510300", "formula_tokens": ["ma_ratio", "tanh"]},
        headers=headers,
    )
    assert publish.status_code == 200, publish.text
    entry = publish.json()
    assert entry["artifact_id"].startswith("ps-")
    listed = test_client.get("/quant/parameter-sets", headers=headers)
    assert listed.status_code == 200 and len(listed.json()) >= 1
    replay = test_client.get(f"/quant/parameter-sets/{entry['artifact_id']}/replay", headers=headers)
    assert replay.status_code == 200 and replay.json()["available"] is True
    fork = test_client.post(
        f"/quant/parameter-sets/{entry['artifact_id']}/fork",
        json={"name": "Fork 参数集"},
        headers=headers,
    )
    assert fork.status_code == 200 and fork.json()["parent_id"] == entry["artifact_id"]
    lineage = test_client.get(f"/quant/parameter-sets/{entry['artifact_id']}/lineage", headers=headers)
    assert lineage.status_code == 200 and len(lineage.json()) == 1


def test_pool_illegal_formula_422(client, monkeypatch):
    test_client, headers = client
    _install_all(client)
    monkeypatch.setattr(market_feed, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(120), "eastmoney"))
    bad = test_client.post(
        "/quant/parameter-sets",
        json={"name": "坏", "symbol": "510300", "formula_tokens": ["add"]},
        headers=headers,
    )
    assert bad.status_code == 422
