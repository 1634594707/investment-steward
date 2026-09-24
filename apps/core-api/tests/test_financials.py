"""阶段 8 · cn-company-evidence 财报证据（东财 F10 免费接口直连）。

取数逻辑与 `finance/packages/financial_data/akshare_provider.py` 逐字段对齐
（_ENDPOINTS / _ALIASES / _eastmoney_symbol 前缀规则），不自行猜测标识符。
测试通过注入假数据源客户端（仅需 `empty` 与 `to_dict(orient="records")`），
完全解耦 pandas / 网络：验证行项目归一化、内容哈希、端点入账、撤销阻断、
数据源失败与非法代码时的显式降级（不生成证据）。
"""

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import financial_evidence
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings


class _Frame:
    def __init__(self, records: list[dict]):
        self._records = records
        self.empty = not records

    def to_dict(self, orient: str = "records") -> list[dict]:
        assert orient == "records"
        return self._records


class _FakeClient:
    def __init__(
        self,
        income_records: list[dict],
        balance_records: list[dict],
        cash_flow_records: list[dict],
    ) -> None:
        self.stock_profit_sheet_by_report_em = self._mk(income_records)
        self.stock_balance_sheet_by_report_em = self._mk(balance_records)
        self.stock_cash_flow_sheet_by_report_em = self._mk(cash_flow_records)

    def _mk(self, records: list[dict]):
        def endpoint(symbol: str) -> _Frame:
            return _Frame(records)

        return endpoint


def _fake_client():
    return _FakeClient(
        income_records=[
            {
                "REPORT_DATE": "2026-06-30",
                "NOTICE_DATE": "2026-08-20",
                "TOTAL_OPERATE_INCOME": 100000000.0,
                "PARENT_NETPROFIT": 20000000.0,
                "BASIC_EPS": 0.5,
            }
        ],
        balance_records=[
            {
                "REPORT_DATE": "2026-06-30",
                "NOTICE_DATE": "2026-08-20",
                "TOTAL_ASSETS": 500000000.0,
                "TOTAL_LIABILITIES": 300000000.0,
            }
        ],
        cash_flow_records=[
            {
                "REPORT_DATE": "2026-06-30",
                "NOTICE_DATE": "2026-08-20",
                "NETCASH_OPERATE": 40000000.0,
            }
        ],
    )


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


# —— 纯函数：A 股代码前缀规则 ——


def test_eastmoney_symbol_prefix_rules():
    assert financial_evidence._eastmoney_symbol("600000") == "SH600000"
    assert financial_evidence._eastmoney_symbol("510300") == "SH510300"
    assert financial_evidence._eastmoney_symbol("910000") == "SH910000"
    assert financial_evidence._eastmoney_symbol("430047") == "BJ430047"
    assert financial_evidence._eastmoney_symbol("000001") == "SZ000001"
    with pytest.raises(ValueError):
        financial_evidence._eastmoney_symbol("abcd")
    with pytest.raises(ValueError):
        financial_evidence._eastmoney_symbol("12345")


# —— 取数逻辑：行项目归一化与稳定哈希（注入假 akshare 客户） ——


def test_fetch_financials_maps_line_items_consistently():
    rows = financial_evidence.fetch_financials("600000", limit_per_type=5, client=_fake_client())
    by_type = {row.statement_type: row for row in rows}
    assert set(by_type) == {"income", "balance", "cash_flow"}
    assert Decimal(by_type["income"].items["revenue"]) == Decimal(100000000)
    assert Decimal(by_type["income"].items["net_profit"]) == Decimal(20000000)
    assert Decimal(by_type["balance"].items["total_assets"]) == Decimal(500000000)
    assert Decimal(by_type["balance"].items["total_debt"]) == Decimal(300000000)
    assert Decimal(by_type["cash_flow"].items["operating_cash_flow"]) == Decimal(40000000)

    again = financial_evidence.fetch_financials("600000", limit_per_type=5, client=_fake_client())
    assert {r.identity for r in rows} == {r.identity for r in again}  # 稳定内容哈希


def test_fetch_financials_skips_rows_without_period_or_notice():
    client = _FakeClient(
        income_records=[{"REPORT_DATE": "2026-06-30", "NOTICE_DATE": "2026-08-20", "TOTAL_OPERATE_INCOME": 1.0}],
        balance_records=[{"REPORT_DATE": "2026-06-30", "TOTAL_ASSETS": 2.0}],  # 缺公告期 → 跳过
        cash_flow_records=[{"NOTICE_DATE": "2026-08-20", "NETCASH_OPERATE": 3.0}],  # 缺报告期 → 跳过
    )
    rows = financial_evidence.fetch_financials("600000", limit_per_type=5, client=client)
    assert {row.statement_type for row in rows} == {"income"}


# —— 契约：端点入账为 financial 证据 ——


def test_endpoint_produces_financial_evidence(monkeypatch, client):
    test_client, headers = client
    monkeypatch.setattr(financial_evidence, "_direct_client", lambda: _fake_client())

    response = test_client.get("/evidence/financials/600000", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    by_type = {entry["summary"].split("·")[0]: entry for entry in body["entries"]}
    assert set(by_type) == {"income", "balance", "cash_flow"}
    first = body["entries"][0]
    assert first["evidence_type"] == "financial"
    assert first["license_status"] == "local-personal-use"
    assert first["subject_refs"] == ["instrument:CN:600000"]
    assert first["source_uri"] == "https://data.eastmoney.com/bbsj/"
    assert len(first["content_hash"]) >= 16

    evidence = test_client.get("/evidence", headers=headers).json()
    ids = {item["evidence_id"] for item in evidence}
    assert all(item["evidence_id"] in ids for item in body["entries"])


def test_endpoint_degrades_when_data_source_fails(monkeypatch, client):
    test_client, headers = client
    before = len(test_client.get("/evidence", headers=headers).json())

    def _missing():
        raise financial_evidence.FinancialsError("东财 F10 接口不可用")

    monkeypatch.setattr(financial_evidence, "_direct_client", _missing)
    response = test_client.get("/evidence/financials/600000", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["entries"] == []
    assert "财报取数失败" in body["limitations"][0]
    assert len(test_client.get("/evidence", headers=headers).json()) == before  # 不生成证据


def test_endpoint_degrades_on_invalid_symbol(monkeypatch, client):
    test_client, headers = client
    before = len(test_client.get("/evidence", headers=headers).json())
    monkeypatch.setattr(financial_evidence, "_direct_client", lambda: _fake_client())
    response = test_client.get("/evidence/financials/non-6-digit", headers=headers)
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert len(test_client.get("/evidence", headers=headers).json()) == before


def test_endpoint_blocks_when_plugin_revoked(monkeypatch, client):
    test_client, headers = client
    monkeypatch.setattr(financial_evidence, "_direct_client", lambda: _fake_client())
    assert test_client.post("/plugins/official.cn-market-data/revoke", headers=headers).status_code == 200
    assert test_client.get("/evidence/financials/600000", headers=headers).status_code == 409