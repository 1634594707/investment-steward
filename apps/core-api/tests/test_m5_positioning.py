"""M5 定位引擎测试：每个阈值边界 + 缺维再归一 + 确定性 + 端点集成。"""

from __future__ import annotations

import pytest
from investment_steward_core.domain import MacroIndicatorReading
from investment_steward_core.macro_scoring import band_of, compute_positioning


def _reading(indicator: str, dim: str, latest: float | None, status: str = "ok") -> MacroIndicatorReading:
    return MacroIndicatorReading(
        indicator=indicator, label=indicator, dim=dim, status=status, latest=latest,
        unit="%", source="test",
    )


# ---- 状态带边界（下限含边界值）----


@pytest.mark.parametrize(
    ("composite", "expected"),
    [
        (100.0, "扩张"),
        (70.0, "扩张"),      # 边界：70 归扩张
        (69.9, "放缓"),
        (45.0, "放缓"),      # 边界：45 归放缓
        (44.9, "承压"),
        (30.0, "承压"),      # 边界：30 归承压
        (29.9, "衰退风险"),
        (0.0, "衰退风险"),
    ],
)
def test_band_boundaries(composite: float, expected: str):
    assert band_of(composite) == expected


# ---- 就业维阈值边界（美国口径）----


def _us_values(unrate: float, prior_unrate: float | None = None) -> dict[str, dict]:
    unemployment = [{"obs_date": "2025-08", "value": unrate}]
    if prior_unrate is not None:
        unemployment += [{"obs_date": "2025-07", "value": prior_unrate}, {"obs_date": "2025-06", "value": prior_unrate}]
    return {"unemployment": {"observations": unemployment}}


def test_us_unrate_threshold_edges():
    readings = [_reading("unemployment", "employment", 5.0)]
    # 恰好 5.0 → -30 分档
    dims = {d.dim: d for d in compute_positioning("us", readings, _us_values(5.0)).dims}
    assert dims["employment"].score == 20.0
    assert any("≥ 5.0" in rule for rule in dims["employment"].rules_fired)

    # 恰好 4.5 → -15 分档（不到 -30 档）
    dims = {d.dim: d for d in compute_positioning("us", readings, _us_values(4.5)).dims}
    assert dims["employment"].score == 35.0
    assert any("≥ 4.5" in rule for rule in dims["employment"].rules_fired)

    # 恰好 4.0 → +10 分档
    dims = {d.dim: d for d in compute_positioning("us", readings, _us_values(4.0)).dims}
    assert dims["employment"].score == 60.0
    assert any("≤ 4.0" in rule for rule in dims["employment"].rules_fired)

    # 4.5 与 5.0 之间（如 4.7）落在 -15 档且不触发连升
    dims = {d.dim: d for d in compute_positioning("us", readings, _us_values(4.7)).dims}
    assert dims["employment"].score == 35.0


def test_us_unrate_three_rising_periods():
    values = {"unemployment": {
        "observations": [
            {"obs_date": "2025-08", "value": 4.6},
            {"obs_date": "2025-07", "value": 4.4},
            {"obs_date": "2025-06", "value": 4.2},
        ],
    }}
    readings = [_reading("unemployment", "employment", 4.6)]
    dims = {d.dim: d for d in compute_positioning("us", readings, values).dims}
    # 4.6 → -15；3 期连升 → 再 -20；合计 50-35 = 15
    assert dims["employment"].score == 15.0
    assert any("3 期连升" in rule for rule in dims["employment"].rules_fired)


# ---- 通胀维阈值边界（对 2% 目标偏离）----


def test_inflation_deviation_edges():
    cases = [
        (3.0, 70.0),   # 偏离恰 1pp → +20
        (4.0, 50.0),   # 偏离恰 2pp → 0
        (5.0, 30.0),   # 偏离恰 3pp → -20
        (5.1, 15.0),   # 偏离 >3pp → -35
        (1.0, 70.0),   # 低通胀同样按偏离度计
    ]
    for cpi, expected in cases:
        readings = [_reading("cpi_yoy", "inflation", cpi)]
        dims = {d.dim: d for d in compute_positioning("us", readings, {"cpi_yoy": {"observations": [{"obs_date": "2025-08", "value": cpi}]}}).dims}
        assert dims["inflation"].score == expected, f"cpi={cpi}"


# ---- 缺维再归一与不编造 ----


def test_missing_dims_renormalized_and_not_fabricated():
    # 只有通胀有 ok 指标：货币/就业/增长未参与，综合 = 通胀分（单维归一）
    readings = [_reading("cpi_yoy", "inflation", 2.5), _reading("fedfunds", "monetary", None, status="pending")]
    positioning = compute_positioning("us", readings, {"cpi_yoy": {"observations": [{"obs_date": "2025-08", "value": 2.5}]}})
    assert positioning is not None
    assert positioning.composite == 70.0  # 通胀偏离 0.5pp → 70
    assert any("monetary" in item or "货币" in item for item in positioning.limitations)
    assert all(d.score is None for d in positioning.dims if d.dim == "monetary")


def test_no_ok_readings_returns_none():
    readings = [_reading("cpi_yoy", "inflation", None, status="pending")]
    assert compute_positioning("us", readings, {}) is None


def test_positioning_is_deterministic():
    values = {"unemployment": {"observations": [{"obs_date": "2025-08", "value": 4.2}]}}
    readings = [_reading("unemployment", "employment", 4.2)]
    first = compute_positioning("us", readings, values)
    second = compute_positioning("us", readings, values)
    assert first.composite == second.composite
    assert first.band == second.band
    assert [d.rules_fired for d in first.dims] == [d.rules_fired for d in second.dims]


# ---- 端点集成：快照携带 positioning ----


def test_macro_endpoint_includes_positioning(client, monkeypatch):

    from investment_steward_core import macro_feed

    monkeypatch.setattr(macro_feed, "fetch_okx_series", lambda inst_id, limit=15: [{"obs_date": "2026-09-01", "value": 2400.0}])

    monkeypatch.setattr(macro_feed, "fetch_em_macro_series", lambda series_id, limit=15: [{"obs_date": "2026-08-01", "value": 49.8}])
    from investment_steward_core import macro_feed

    test_client, headers = client
    assert test_client.post("/plugins/official.macro-radar/install", headers=headers).status_code == 200

    def fake_wb(iso3: str, indicator_id: str):
        data = {("CHN", "FP.CPI.TOTL.ZG"): 2.5, ("CHN", "SL.UEM.TOTL.ZS"): 5.2}
        return [{"obs_date": "2025", "value": data[(iso3, indicator_id)]}]

    monkeypatch.setattr(macro_feed, "fetch_worldbank_series", fake_wb)
    body = test_client.get("/evidence/macro/cn", headers=headers).json()
    positioning = body["positioning"]
    assert positioning is not None
    assert positioning["region"] == "cn"
    assert positioning["weight_version"] == "v1"
    scored = {d["dim"]: d["score"] for d in positioning["dims"] if d["score"] is not None}
    # 制造业/非制造业 PMI 已接东财数据中心 → growth 维参与加权
    assert set(scored) == {"employment", "inflation", "growth"}
    assert positioning["band"] in {"扩张", "放缓", "承压", "衰退风险"}
    assert positioning["limitations"]
    # 背景层不产定位（不参与国别打分）
    global_body = test_client.get("/evidence/macro/global", headers=headers).json()
    assert global_body["positioning"] is None
