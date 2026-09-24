"""E1 · G3-5 plugin-runner 零密钥 + network_allowlist 出网门控契约测试。"""
from __future__ import annotations

import json

import pytest
from investment_steward_plugin_runner import protocol


def _clear_env(monkeypatch):
    for name in list(protocol.secret_env_vars()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("STEWARD_NETWORK_ALLOWLIST", raising=False)


def _dispatch(line: str) -> dict:
    return json.loads(protocol.handle_line(line))


def test_health_reports_zero_credentials_when_clean(monkeypatch) -> None:
    _clear_env(monkeypatch)
    response = _dispatch(json.dumps({"jsonrpc": "2.0", "method": "runner.health"}))
    assert response["result"]["env_credentials"] == 0
    assert response["result"]["secret_env_vars"] == []


def test_execution_refused_when_secret_env_present(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("STEWARD_SESSION_TOKEN", "no-token-for-plugins")
    response = _dispatch(
        json.dumps({"jsonrpc": "2.0", "method": "golden.evidence", "params": {"summary": "x"}})
    )
    assert response["error"]["code"] == -32600
    assert "leaks credentials" in response["error"]["message"]


def test_network_allows_allowlisted_host_and_rejects_other(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("STEWARD_NETWORK_ALLOWLIST", json.dumps(["eastmoney.com", "api.example.org"]))
    allowed = _dispatch(
        json.dumps(
            {"jsonrpc": "2.0", "method": "runner.network", "params": {"url": "https://push2his.eastmoney.com/x"}}
        )
    )
    assert allowed["result"]["allowed"] is True
    assert allowed["result"]["host"] == "push2his.eastmoney.com"

    denied = _dispatch(
        json.dumps({"jsonrpc": "2.0", "method": "runner.network", "params": {"url": "https://evil.example.com/x"}})
    )
    assert denied["result"]["allowed"] is False


def test_allowlist_matches_subdomain_and_rejects_strictly(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("STEWARD_NETWORK_ALLOWLIST", json.dumps(["eastmoney.com"]))
    protocol.assert_host_allowed("push2his.eastmoney.com")  # 子域放行
    with pytest.raises(ValueError):
        protocol.assert_host_allowed("eastmoney.com.evil.net")


def test_empty_allowlist_denies_all(monkeypatch) -> None:
    _clear_env(monkeypatch)
    with pytest.raises(ValueError):
        protocol.assert_host_allowed("anything.example.com")