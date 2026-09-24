import json

from investment_steward_plugin_runner.protocol import handle_line


def test_health_and_golden_outputs_are_structured():
    health = json.loads(handle_line(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "runner.health"})))
    assert health["result"]["status"] == "ready"
    evidence = json.loads(
        handle_line(
            json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "golden.evidence", "params": {"summary": "fixture"}}
            )
        )
    )
    assert evidence["result"]["kind"] == "evidence"
    assert len(evidence["result"]["content_hash"]) == 64


def test_runner_rejects_unknown_methods_and_oversized_requests():
    unknown = json.loads(handle_line(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "os.exec"})))
    assert unknown["error"]["code"] == -32600
    oversized = json.loads(handle_line("x" * 300_000))
    assert oversized["error"]["code"] == -32001


def test_context_gate_denies_unauthorized_resources():
    # 只授予 read_market_data：请求 thesis 资源应被拒绝。
    request = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "runner.context",
        "params": {
            "granted_capabilities": ["read_market_data"],
            "context": {"market_data": {"symbol": "510300"}, "thesis": {"instrument": "510300"}},
        },
    }
    result = json.loads(handle_line(json.dumps(request)))
    assert result["error"]["code"] == -32600
    assert "unauthorized" in result["error"]["message"]


def test_context_gate_returns_only_granted_data():
    request = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "runner.context",
        "params": {
            "granted_capabilities": ["read_market_data", "read_portfolio"],
            "context": {"market_data": {"symbol": "510300"}, "portfolio_holdings": [{"instrument": "510300"}]},
        },
    }
    result = json.loads(handle_line(json.dumps(request)))
    assert result["result"]["granted_resources"] == ["market_data", "portfolio_holdings"]
    assert list(result["result"]["data"].keys()) == ["market_data", "portfolio_holdings"]


def test_context_gate_without_grants_returns_empty():
    request = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "runner.context",
        "params": {"granted_capabilities": [], "context": {"market_data": {"symbol": "510300"}}},
    }
    result = json.loads(handle_line(json.dumps(request)))
    assert result["result"]["granted_resources"] == []
    assert result["result"]["data"] == {}


def test_host_capabilities_override_request_claims(monkeypatch):
    monkeypatch.setenv("STEWARD_GRANTED_CAPABILITIES", json.dumps(["read_market_data"]))

    denied = json.loads(
        handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "runner.context",
                    "params": {
                        "granted_capabilities": ["read_portfolio"],
                        "context": {"portfolio_holdings": [{"instrument": "510300"}]},
                    },
                }
            )
        )
    )
    assert denied["error"]["code"] == -32600
    assert "not granted by Host" in denied["error"]["message"]

    host_authoritative = json.loads(
        handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "runner.context",
                    "params": {
                        "granted_capabilities": [],
                        "context": {"market_data": {"symbol": "510300"}},
                    },
                }
            )
        )
    )
    assert host_authoritative["result"]["granted_resources"] == ["market_data"]
    assert host_authoritative["result"]["data"]["market_data"]["symbol"] == "510300"
