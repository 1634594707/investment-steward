"""支撑阻力位识别测试:算法确定性 + 插件门槛 + 端点契约。"""

from __future__ import annotations

import pytest

from investment_steward_core import market_feed, support_resistance


def _bars(count: int = 120) -> list[dict[str, object]]:
    """合成蜡烛:先涨至 12.0 再回落至 10.0 再反弹,制造可识别摆动与支撑。"""
    bars: list[dict[str, object]] = []
    for i in range(count):
        phase = i % 16
        if phase < 8:
            price = 10.0 + phase * 0.25
        else:
            price = 12.0 - (phase - 8) * 0.25
        bars.append({
            "timestamp": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
            "open": round(price - 0.02, 3),
            "high": round(price + 0.06, 3),
            "low": round(price - 0.06, 3),
            "close": round(price, 3),
            "volume": 1000.0 + i,
        })
    return bars


def test_detect_levels_insufficient_sample_returns_empty():
    assert support_resistance.detect_levels(_bars(15)) == []


def test_detect_levels_returns_sorted_levels_with_stats():
    levels = support_resistance.detect_levels(_bars(120))
    assert levels, "合成趋势应至少识别出一个关键位"
    for level in levels:
        assert level["kind"] in {"support", "resistance"}
        assert level["price"] > 0
        assert level["samples"] >= 2
        assert level["confidence"] in {"高", "中", "低"}
        assert level["hold_rate"] is None or 0 <= level["hold_rate"] <= 1
    assert all(9.0 < level["price"] < 12.5 for level in levels)


def test_endpoint_requires_enabled_plugins(client):
    test_client, headers = client
    response = test_client.get("/evidence/support-resistance/510300", headers=headers)
    assert response.status_code == 409


def test_endpoint_ok_with_plugins(client, monkeypatch):
    from investment_steward_core.api import app as app_module

    fake_rows = _bars(120)
    monkeypatch.setattr(market_feed, "fetch_cn_kline", lambda symbol, limit=250, period="day": (fake_rows, "eastmoney"))
    if hasattr(app_module, "fetch_cn_kline"):
        monkeypatch.setattr(app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (fake_rows, "eastmoney"), raising=False)
    test_client, headers = client
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    assert test_client.post("/plugins/official.support-resistance/install", headers=headers).status_code == 200
    response = test_client.get("/evidence/support-resistance/510300", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["levels"], "合成趋势应产出关键位"
    for level in body["levels"]:
        assert level["status"] == "ok"
        assert level["price"] > 0
    assert "无公开数据源" not in str(body["levels"])
