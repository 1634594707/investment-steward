"""macro_trade 单测（M5 时代 G0）：订阅/preview 双端点分支 + 触顶守卫。纯函数级 mock，不发起网络请求。"""
from __future__ import annotations

import json

from investment_steward_core.macro_trade import COMTRADE_SUBSCRIPTION_API, COMTRADE_SUBSCRIPTION_MONTHLY_API, fetch_trade, fetch_trade_monthly, trade_guard


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _rows(n: int) -> list[dict]:
    return [{"flowCode": "X", "primaryValue": 100.0}, {"flowCode": "M", "primaryValue": 40.0}][:n] if n <= 2 else [{"flowCode": "X", "primaryValue": 100.0}, {"flowCode": "M", "primaryValue": 40.0}] * (n // 2)


def test_fetch_trade_subscription_branch_uses_key_header_and_high_limit(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _FakeResponse({"data": _rows(600), "count": 600})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = fetch_trade(392, 842, 2025, api_key="dummy-key")
    assert COMTRADE_SUBSCRIPTION_API in captured["url"]
    # 订阅端点必须锁定聚合层（partner2Code/customsCode/motCode），否则 customs 拆分致求和失真
    assert "partner2Code=0" in captured["url"] and "customsCode=C00" in captured["url"] and "motCode=0" in captured["url"]
    # urllib 会把 header 名标准化为小写段；HTTP header 名不区分大小写，APIM 正常接受
    assert captured["headers"].get("Ocp-apim-subscription-key") == "dummy-key"
    assert result["source"] == "UN Comtrade subscription"
    assert result["count"] == 600 and result["truncated"] is False  # 订阅版 600 行不触顶
    assert result["export_usd"] == 30000.0 and result["import_usd"] == 12000.0
    assert result["balance_usd"] == 18000.0


def test_fetch_trade_preview_branch_flags_truncation(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _FakeResponse({"data": _rows(600), "count": 600}))
    result = fetch_trade(392, 842, 2025)  # 无 key → preview
    assert result["source"] == "UN Comtrade preview"
    assert result["truncated"] is True  # preview 下 600 行 ≥500 → 触顶标记，数值不可信


def test_trade_guard_eu_aggregate_always_pending():
    guarded = trade_guard({"count": 2, "data": []}, 97, subscription=True)
    assert guarded["pending"] is True and "残缺" in guarded["degraded_reason"]
    assert guarded["truncated"] is True  # reporter=97 恒标记截断（库内残缺，与是否订阅无关）


def test_fetch_trade_monthly_multi_period_single_request(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        return _FakeResponse({"data": [
            {"period": 202605, "flowCode": "X", "primaryValue": 67.0e8},
            {"period": 202605, "flowCode": "M", "primaryValue": 169.1e8},
            {"period": 202606, "flowCode": "X", "primaryValue": 77.6e8},
            {"period": 202606, "flowCode": "M", "primaryValue": 195.5e8},
        ], "count": 4})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = fetch_trade_monthly(276, 156, ["202604", "202605", "202606", "202607"], api_key="dummy-key")
    assert COMTRADE_SUBSCRIPTION_MONTHLY_API in captured["url"]  # 月度走 C/M/HS
    assert "period=202604%2C202605%2C202606%2C202607" in captured["url"]  # 单请求多值
    assert result["freq"] == "M"
    # 多值里未发布的月份（202604/202607 无行）自动缺省；升序排列
    assert [s["period"] for s in result["series"]] == ["202605", "202606"]
    assert result["series"][-1]["balance_usd"] == (77.6e8 - 195.5e8)
    assert all(s["count"] == 2 for s in result["series"])


def test_fetch_trade_monthly_requires_subscription_key():
    try:
        fetch_trade_monthly(276, 156, ["202606"], api_key=None)
        raise AssertionError("should have raised")
    except RuntimeError as exc:
        assert "订阅 key" in str(exc)  # preview 不支持 freq=M，无 key 拒绝而非编造
