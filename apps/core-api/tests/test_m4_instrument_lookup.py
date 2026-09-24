"""M4（2026-09-15 第二轮路线图）E01–E03：新建持仓——代码出名称，名称出代码。

夹具 `tests/fixtures/instrument_lookup/` 是 2026-09-15 23:4x **真实抓取**的原始响应体
（东财搜索建议 UTF-8 JSON / 腾讯 smartbox GBK 文本），不做任何手写或改写：
解析器按真实报文结构写，测试按真实报文断言，避免「按猜的字段名写解析器」。

E01 验收：`q=001201` → 东瑞股份；`q=510300` → 沪深300ETF；`q=东瑞` 命中 001201；
非法输入返回空列表而不是 500。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from investment_steward_core import instrument_lookup as lookup

FIXTURES = Path(__file__).parent / "fixtures" / "instrument_lookup"


def _em(name: str) -> dict:
    return json.loads((FIXTURES / f"em_suggest_{name}.json").read_text(encoding="utf-8"))


def _tx(name: str) -> str:
    return (FIXTURES / f"tx_smartbox_{name}.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# E01：东财搜索建议解析（主源）
# ---------------------------------------------------------------------------

def test_parse_em_suggest_name_query_hits_a_stock():
    rows = lookup.parse_em_suggest(_em("东瑞"))
    assert rows[0] == {
        "key": "001201",
        "name": "东瑞股份",
        "kind": "stock",
        "market": "sz",
        "display": "东瑞股份（001201）",
    }


def test_parse_em_suggest_drops_hk_and_non_six_digit():
    """港股（02348）与不足 6 位的代码一律剔除——持仓只支持 A 股与场内 ETF。"""
    rows = lookup.parse_em_suggest(_em("东瑞"))
    assert [row["key"] for row in rows] == ["001201"]
    assert all(len(row["key"]) == 6 for row in rows)


def test_parse_em_suggest_code_query_drops_options():
    """`q=001201` 真实响应里混着 8 位期权代码；只留 A 股那一条。"""
    rows = lookup.parse_em_suggest(_em("001201"))
    assert [row["key"] for row in rows] == ["001201"]
    assert rows[0]["kind"] == "stock"


def test_parse_em_suggest_etf():
    rows = lookup.parse_em_suggest(_em("510300"))
    assert rows == [{
        "key": "510300",
        "name": "沪深300ETF华泰柏瑞",
        "kind": "etf",
        "market": "sh",
        "display": "沪深300ETF华泰柏瑞（510300）",
    }]


def test_parse_em_suggest_no_match_is_empty():
    assert lookup.parse_em_suggest(_em("zzzzzz")) == []
    assert lookup.parse_em_suggest(_em("999999")) == []


def test_parse_em_suggest_garbage_payload():
    assert lookup.parse_em_suggest(None) == []
    assert lookup.parse_em_suggest({}) == []
    assert lookup.parse_em_suggest({"QuotationCodeTable": {"Data": "不是列表"}}) == []


# ---------------------------------------------------------------------------
# E01：腾讯 smartbox 解析（回退源）
# ---------------------------------------------------------------------------

def test_parse_tx_smartbox_hits_a_stock():
    rows = lookup.parse_tx_smartbox(_tx("东瑞"))
    assert rows[0] == {
        "key": "001201",
        "name": "东瑞股份",
        "kind": "stock",
        "market": "sz",
        "display": "东瑞股份（001201）",
    }
    # GP（港股）/QZ（权证）被剔除
    assert [row["key"] for row in rows] == ["001201"]


def test_parse_tx_smartbox_etf():
    assert lookup.parse_tx_smartbox(_tx("510300")) == [{
        "key": "510300",
        "name": "沪深300ETF华泰柏瑞",
        "kind": "etf",
        "market": "sh",
        "display": "沪深300ETF华泰柏瑞（510300）",
    }]


def test_parse_tx_smartbox_no_match():
    """无结果时腾讯返回 `v_hint="N"`。"""
    assert lookup.parse_tx_smartbox(_tx("zzzzzz")) == []
    assert lookup.parse_tx_smartbox(_tx("999999")) == []


def test_parse_tx_smartbox_garbage_text():
    assert lookup.parse_tx_smartbox("") == []
    assert lookup.parse_tx_smartbox("完全不是这个格式") == []
    assert lookup.parse_tx_smartbox('v_hint=""') == []


# ---------------------------------------------------------------------------
# E01：lookup_instruments 编排（主源 → 回退源 → 空列表）
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_cache():
    lookup.reset_lookup_cache()
    yield
    lookup.reset_lookup_cache()


def test_lookup_uses_primary_source(monkeypatch):
    monkeypatch.setattr(lookup, "_fetch_em", lambda q: lookup.parse_em_suggest(_em(q)))
    monkeypatch.setattr(lookup, "_fetch_tx", lambda q: pytest.fail("主源命中时不应回退"))
    rows = lookup.lookup_instruments("东瑞")
    assert [row["key"] for row in rows] == ["001201"]


def test_lookup_falls_back_when_primary_fails(monkeypatch):
    def boom(_q: str):
        raise RuntimeError("东财不可达")

    monkeypatch.setattr(lookup, "_fetch_em", boom)
    monkeypatch.setattr(lookup, "_fetch_tx", lambda q: lookup.parse_tx_smartbox(_tx(q)))
    rows = lookup.lookup_instruments("东瑞")
    assert [row["key"] for row in rows] == ["001201"]


def test_lookup_returns_empty_when_all_sources_fail(monkeypatch):
    def boom(_q: str):
        raise RuntimeError("两个源都不可达")

    monkeypatch.setattr(lookup, "_fetch_em", boom)
    monkeypatch.setattr(lookup, "_fetch_tx", boom)
    assert lookup.lookup_instruments("东瑞") == []


def test_lookup_empty_query_short_circuits(monkeypatch):
    monkeypatch.setattr(lookup, "_fetch_em", lambda q: pytest.fail("空查询不应发请求"))
    assert lookup.lookup_instruments("") == []
    assert lookup.lookup_instruments("   ") == []


def test_lookup_prefix_form_extracts_digits(monkeypatch):
    """用户可能贴 `sh510300`；先按原样查，再按抽出的 6 位码查。"""
    seen: list[str] = []

    def fake_em(query: str):
        seen.append(query)
        return lookup.parse_em_suggest(_em(query)) if len(query) == 6 else []

    monkeypatch.setattr(lookup, "_fetch_em", fake_em)
    monkeypatch.setattr(lookup, "_fetch_tx", lambda q: [])
    rows = lookup.lookup_instruments("sh510300")
    assert seen == ["sh510300", "510300"]
    assert rows[0]["key"] == "510300"


def test_lookup_respects_limit(monkeypatch):
    monkeypatch.setattr(lookup, "_fetch_em", lambda q: lookup.parse_em_suggest(_em("东瑞")))
    monkeypatch.setattr(lookup, "_fetch_tx", lambda q: [])
    assert len(lookup.lookup_instruments("东瑞", limit=1)) == 1


# ---------------------------------------------------------------------------
# E01：HTTP 端点（不抛 500，非法输入空列表）
# ---------------------------------------------------------------------------

def test_endpoint_lookup_ok(client, monkeypatch):
    monkeypatch.setattr(lookup, "_fetch_em", lambda q: lookup.parse_em_suggest(_em(q)))
    monkeypatch.setattr(lookup, "_fetch_tx", lambda q: [])
    http, headers = client
    response = http.get("/instruments/lookup", params={"q": "东瑞"}, headers=headers)
    assert response.status_code == 200
    assert response.json()[0]["key"] == "001201"


def test_endpoint_lookup_invalid_input_is_empty_not_500(client, monkeypatch):
    monkeypatch.setattr(lookup, "_fetch_em", lambda q: [])
    monkeypatch.setattr(lookup, "_fetch_tx", lambda q: [])
    http, headers = client
    for query in ("zzzzzz", "999999", "！@#￥%", "x"):
        response = http.get("/instruments/lookup", params={"q": query}, headers=headers)
        assert response.status_code == 200, query
        assert response.json() == [], query


def test_endpoint_lookup_survives_provider_outage(client, monkeypatch):
    def boom(_q: str):
        raise RuntimeError("供应商挂掉")

    monkeypatch.setattr(lookup, "_fetch_em", boom)
    monkeypatch.setattr(lookup, "_fetch_tx", boom)
    http, headers = client
    response = http.get("/instruments/lookup", params={"q": "东瑞"}, headers=headers)
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# E03：保存契约不变——lookup 给出的 key 一定能通过 POST /holdings
# ---------------------------------------------------------------------------

def test_e03_lookup_key_is_accepted_by_create_holding(client, monkeypatch):
    """E03：名称查找成功后走原 `POST /holdings`，instrument 用 lookup 的 key。"""
    monkeypatch.setattr(lookup, "_fetch_em", lambda q: lookup.parse_em_suggest(_em(q)))
    monkeypatch.setattr(lookup, "_fetch_tx", lambda q: [])
    http, headers = client
    found = http.get("/instruments/lookup", params={"q": "东瑞"}, headers=headers).json()[0]

    created = http.post(
        "/holdings",
        json={
            "instrument": found["key"],
                "label": found["name"],
                "status": "watchlist",
                # 契约里 strategy_note 是**字符串**（策略备注选填 = 空串），不是 null。
                "strategy_note": "",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["instrument"] == "001201"
