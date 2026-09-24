"""E1 契约测试：/runner/config 出网白名单聚合与零密钥策略、plugin_runtime 可观测面。"""
from __future__ import annotations

from investment_steward_core import plugin_runtime


def test_runner_config_exposes_allowlist_and_zero_key_policy(client):
    test_client, headers = client
    response = test_client.get("/runner/config", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["env_credentials_policy"] == "zero"
    assert isinstance(body["network_allowlist"], list)
    # E5 主源（腾讯）+回退（东财）：两域都须授权，供 runner 子域放行。
    assert "web.ifzq.gtimg.cn" in body["network_allowlist"]
    assert "push2his.eastmoney.com" in body["network_allowlist"]
    assert isinstance(body["secret_env_markers"], list)


def test_runner_config_requires_session(client):
    test_client, _ = client
    assert test_client.get("/runner/config").status_code == 401


def test_aggregate_network_allowlist_is_union_unique(client):
    # capabilities 中多处声明取数/宏观域，聚合应去重并包含 E5 主源与回退域
    allowlist = plugin_runtime.aggregate_network_allowlist()
    assert allowlist == sorted(set(allowlist))
    assert "web.ifzq.gtimg.cn" in allowlist
    assert "push2his.eastmoney.com" in allowlist


def test_secret_env_markers_detects_known_marker(monkeypatch):
    monkeypatch.setenv("STEWARD_TEST_API_KEY", "secret-value-should-not-leak")
    assert "STEWARD_TEST_API_KEY" in plugin_runtime.secret_env_markers()


def test_secret_env_markers_never_returns_values():
    # 只暴露变量名，值不被返回（供审计表面消费，不透出密钥本体）
    markers = plugin_runtime.secret_env_markers()
    assert all(isinstance(name, str) and "=" not in name for name in markers)