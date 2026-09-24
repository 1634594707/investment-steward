"""E2 · 统一标的身份契约测试。

覆盖 `instruments.normalize_instrument` 与 `GET /instruments/resolve`：
1. 多种常见形态（裸码 / 带市场前缀 / 带点后缀）归一到相同规范 key；
2. kind / market 按首段约定判定（股票 sh/sz/bj、ETF、其他、unknown）；
3. 空 / 无法解析输入 → unknown 而不抛错；
4. 端点回显同一身份。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.instruments import normalize_instrument


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("510300", "510300"),
        ("sh510300", "510300"),
        ("SH.510300", "510300"),
        ("510300.sh", "510300"),
        ("sh.510300", "510300"),
        ("600519", "600519"),
        ("sz000001", "000001"),
    ],
)
def test_variant_forms_normalize_to_same_key(raw, expected):
    assert normalize_instrument(raw).key == expected


@pytest.mark.parametrize(
    "raw,kind,market",
    [
        ("510300", "etf", "sh"),
        ("600519", "stock", "sh"),
        ("000001", "stock", "sz"),
        ("301234", "stock", "sz"),
        ("830001", "stock", "bj"),
    ],
)
def test_kind_and_market_detection(raw, kind, market):
    identity = normalize_instrument(raw)
    assert identity.kind == kind
    assert identity.market == market


@pytest.mark.parametrize("raw", ["", None, "BTC", "ETH-USDT"])
def test_unparseable_returns_unknown_without_raising(raw):
    identity = normalize_instrument(raw)
    assert identity.kind == "unknown"
    assert identity.market == "none"


def test_resolve_endpoint_returns_canonical_identity(tmp_path):
    app = create_app(CoreSettings(session_token="t", data_dir=tmp_path))
    response = TestClient(app).get("/instruments/resolve", params={"symbol": "sh510300"})
    assert response.status_code == 200
    assert response.json() == {"key": "510300", "kind": "etf", "market": "sh", "display": "510300"}


def test_holdings_queryable_by_canonical_identity(tmp_path):
    """以 "510300" 建立持仓后，任意同义形态（"sh510300"）经统一身份命中。"""
    token = {"X-Core-Session-Token": "t"}
    app = create_app(CoreSettings(session_token="t", data_dir=tmp_path))
    client = TestClient(app)

    created = client.post(
        "/holdings", headers=token, json={"instrument": "510300", "label": "沪深300ETF", "status": "holding"}
    )
    assert created.status_code == 201

    exact = client.get("/holdings/by-instrument", headers=token, params={"instrument": "510300"})
    assert exact.status_code == 200
    assert [h["instrument"] for h in exact.json()] == ["510300"]

    alias = client.get("/holdings/by-instrument", headers=token, params={"instrument": "sh510300"})
    assert alias.status_code == 200
    assert [h["instrument"] for h in alias.json()] == ["510300"]

    unrelated = client.get("/holdings/by-instrument", headers=token, params={"instrument": "000001"})
    assert unrelated.json() == []