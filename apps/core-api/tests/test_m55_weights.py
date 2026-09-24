"""M5.5 权重版本链 + M6 异动提交测试。

核心锁定：「同一份数据 + 同一版权重 → 同一状态带」——调权后历史状态带可精确复算。
"""

from __future__ import annotations

from investment_steward_core.domain import MacroIndicatorReading
from investment_steward_core.macro_scoring import compute_positioning


def _cn_values(cpi: float, unrate: float) -> dict[str, dict]:
    return {
        "cpi_yoy": {"observations": [{"obs_date": "2025", "value": cpi}]},
        "unemployment_ilo": {"observations": [{"obs_date": "2025", "value": unrate}]},
    }


def _cn_readings() -> list[MacroIndicatorReading]:
    return [
        MacroIndicatorReading(indicator="cpi_yoy", label="CPI 同比", dim="inflation", status="ok",
                              latest=2.5, unit="%YoY", source="test"),
        MacroIndicatorReading(indicator="unemployment_ilo", label="失业率（ILO 估算）", dim="employment", status="ok",
                              latest=6.2, unit="%", source="test"),
    ]


# ---- 权重链：调权改变结果，但历史（同数据+同权重）复算不变 ----


def test_custom_weights_change_composite_deterministically():
    data = _cn_values(2.5, 6.2)
    readings = _cn_readings()
    default = compute_positioning("cn", readings, data)
    heavy_inflation = compute_positioning(
        "cn", readings, data,
        weights={"growth": 0.35, "employment": 0.10, "inflation": 0.45, "monetary": 0.10},
        weight_version="v2",
    )
    assert default is not None and heavy_inflation is not None
    assert heavy_inflation.weight_version == "v2"
    # 通胀维 70 分权重上调 → 综合分应上升（方向确定）
    assert heavy_inflation.composite > default.composite
    # 复算同一输入 → 完全一致（历史状态带不变）
    replay = compute_positioning(
        "cn", readings, data,
        weights={"growth": 0.35, "employment": 0.10, "inflation": 0.45, "monetary": 0.10},
        weight_version="v2",
    )
    assert replay.composite == heavy_inflation.composite
    assert replay.band == heavy_inflation.band


def test_highly_personalized_limitation_fired():
    readings = _cn_readings()
    data = _cn_values(2.5, 6.2)
    positioning = compute_positioning(
        "cn", readings, data,
        weights={"growth": 0.80, "employment": 0.10, "inflation": 0.05, "monetary": 0.05},
        weight_version="v3",
    )
    assert positioning is not None
    assert any("高度个性化" in item for item in positioning.limitations)


def test_default_version_still_labeled_v1():
    positioning = compute_positioning("cn", _cn_readings(), _cn_values(2.5, 6.2))
    assert positioning is not None and positioning.weight_version == "v1"
    assert not any("高度个性化" in item for item in positioning.limitations)


# ---- 端点：权重保存 / 校验 / 快照使用最新版本 ----


def test_weights_endpoints_roundtrip(client):
    test_client, headers = client
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200

    listing = test_client.get("/evidence/macro/weights", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["active_version"] == "v1"

    save = test_client.post(
        "/evidence/macro/weights",
        json={"weights": {"growth": 0.40, "employment": 0.20, "inflation": 0.30, "monetary": 0.10}, "note": "更看重通胀"},
        headers=headers,
    )
    assert save.status_code == 200, save.text
    body = save.json()
    assert body["version"] == "v2"
    # 各维偏离默认均 ≤20pp（最大 10pp）→ 不标个性化
    assert body["personalized"] == []

    after = test_client.get("/evidence/macro/weights", headers=headers).json()
    assert after["active_version"] == "v2"
    assert after["versions"][0]["note"] == "更看重通胀"


def test_weights_invalid_sum_rejected(client):
    test_client, headers = client
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    response = test_client.post(
        "/evidence/macro/weights",
        json={"weights": {"growth": 0.40, "employment": 0.20, "inflation": 0.30, "monetary": 0.20}, "note": ""},
        headers=headers,
    )
    assert response.status_code == 422


def test_weights_missing_dim_rejected(client):
    test_client, headers = client
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    response = test_client.post(
        "/evidence/macro/weights",
        json={"weights": {"growth": 0.50, "employment": 0.30, "inflation": 0.20}, "note": ""},
        headers=headers,
    )
    assert response.status_code == 422


def test_snapshot_uses_active_weight_version(client, monkeypatch):
    from investment_steward_core import macro_feed

    test_client, headers = client
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    monkeypatch.setattr(
        macro_feed, "fetch_worldbank_series",
        lambda iso3, indicator_id: [{"obs_date": "2025", "value": 2.5 if indicator_id.startswith("FP") else 6.2}],
    )
    test_client.post(
        "/evidence/macro/weights",
        json={"weights": {"growth": 0.40, "employment": 0.20, "inflation": 0.30, "monetary": 0.10}, "note": ""},
        headers=headers,
    )
    body = test_client.get("/evidence/macro/cn", headers=headers).json()
    assert body["positioning"]["weight_version"] == "v2"


# ---- M6：异动提交（幂等去重 + 无异动返回空）----


def test_anomaly_submission_dedupes(client, monkeypatch):
    from investment_steward_core import macro_feed

    test_client, headers = client
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    # 失业率 6.2 ≥ 6 → -15（不够重度）；用 8.5 ≥ 8 → -30（重度，产生异动）
    monkeypatch.setattr(
        macro_feed, "fetch_worldbank_series",
        lambda iso3, indicator_id: [{"obs_date": "2025", "value": 2.5 if indicator_id.startswith("FP") else 8.5}],
    )
    first = test_client.post("/evidence/macro/cn/anomalies", headers=headers)
    assert first.status_code == 201, first.text
    entries = first.json()
    assert len(entries) == 1
    assert "宏观异动" in entries[0]["summary"]
    assert entries[0]["evidence_type"] == "macro"
    assert "rule_based" in entries[0]["quality_flags"]
    # 同日重复提交 → 幂等返回同一条（同 evidence_id），不重复入账
    second = test_client.post("/evidence/macro/cn/anomalies", headers=headers).json()
    assert [e["evidence_id"] for e in second] == [e["evidence_id"] for e in entries]
    listed = test_client.get("/evidence", headers=headers).json()
    macro_entries = [item for item in listed if item["evidence_type"] == "macro"]
    assert len(macro_entries) == 1


def test_anomaly_submission_empty_when_no_severe_rules(client, monkeypatch):
    from investment_steward_core import macro_feed

    test_client, headers = client
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200
    # 温和数值：CPI 2.5（偏离 0.5pp → +20）、失业率 6.2（-15）→ 无重度规则
    monkeypatch.setattr(
        macro_feed, "fetch_worldbank_series",
        lambda iso3, indicator_id: [{"obs_date": "2025", "value": 2.5 if indicator_id.startswith("FP") else 6.2}],
    )
    result = test_client.post("/evidence/macro/cn/anomalies", headers=headers)
    assert result.status_code == 201
    assert result.json() == []
