"""分享池阶段 A 端点测试:发布/列表/回放/Fork 走 HTTP。

外带 `/quant/factors/mine` 的契约测试(QL06 搜索策略 + QL09 窗口来源标注)。
"""

from __future__ import annotations

from investment_steward_core import market_feed, quant_factors
from investment_steward_core.api import app as core_app
from test_quant_pool import _bars


def _headers():
    return {"X-Core-Session-Token": "test-session-token"}


def _install_all(client):
    test_client, headers = client
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    assert test_client.post("/plugins/official.support-resistance/install", headers=headers).status_code == 200


def _patch_bars(monkeypatch, bars, source: str = "eastmoney"):
    """桩掉端点用的行情拉取。

    注意:``app.py`` 是 ``from ...market_feed import fetch_cn_kline`` 的**按值导入**,
    因此只 patch ``market_feed.fetch_cn_kline`` 对端点无效——端点会走真实网络。
    要保证测试不看网络,必须 patch 端点所在模块(app)的名字。
    """
    monkeypatch.setattr(core_app, "fetch_cn_kline", lambda symbol, limit=250, period="day": (bars, source))
    return market_feed


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


def test_factor_mine_endpoint_reports_search_grammar_and_window_provenance(client, monkeypatch):
    """QL06/QL09 端点契约:搜索几何 + 语法导出 + 「请求档位 vs 实取行数」。

    夹具固定只给 250 根,而默认档位是 750 → 响应**必须**标注 ``truncated``,
    不允许把少给的样本当足量样本用。
    """
    test_client, headers = client
    _install_all(client)
    bars = _bars(250)
    _patch_bars(monkeypatch, bars)

    response = test_client.post("/quant/factors/mine/510300?search=beam&correction=fdr", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["search"]["strategy"] == "beam"
    assert body["search"]["beam_width"] == quant_factors.BEAM_WIDTH
    assert body["search"]["selection_segment"] == "train"
    assert body["search"]["candidates_generated"] > 0
    assert body["grammar"]["atoms"] == list(quant_factors.FEATURE_NAMES)
    assert body["grammar"]["time_series"]["tokens"] == list(quant_factors.TS_TOKENS)
    assert body["caliber"]["formula_spec_version"] == quant_factors.FORMULA_SPEC_VERSION
    assert body["caliber"]["mine_spec_version"] == quant_factors.MINE_SPEC_VERSION

    window = body["window"]
    assert window["bars"] == quant_factors.DEFAULT_WINDOW_BARS
    assert window["bars_actual"] == len(bars)
    assert window["truncated"] is True
    assert window["source"] == "eastmoney"
    assert window["first_bar"] == str(bars[0]["timestamp"])[:10]
    assert "truncation_note" in window and str(len(bars)) in window["truncation_note"]
    assert window["truncation_note"] in body["note"]  # 截断提示必须出现在人读的 note 里


def test_factor_mine_endpoint_rejects_unknown_search_mode(client, monkeypatch):
    test_client, headers = client
    _install_all(client)
    _patch_bars(monkeypatch, _bars(250))
    bad = test_client.post("/quant/factors/mine/510300?search=genetic", headers=headers)
    assert bad.status_code == 422
    assert "search" in bad.json()["detail"]


def test_factor_mine_endpoint_exhaustive_mode_keeps_legacy_counts(client, monkeypatch):
    test_client, headers = client
    _install_all(client)
    _patch_bars(monkeypatch, _bars(250))
    response = test_client.post("/quant/factors/mine/510300?search=exhaustive", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["search"]["strategy"] == "exhaustive"
    assert body["multiple_testing"]["candidates_total"] == 1680
