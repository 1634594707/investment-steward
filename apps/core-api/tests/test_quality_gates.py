"""阶段 10 · P8 质量门禁的可自动化子集。

覆盖三类可确定性验证的门禁（不依赖外部网络，全部用本地 SQLite + 内存/临时目录）：

1. 断网演练：行情源故障时 `/market/candles` 优雅降级为演示数据（is_demo），且本地
   已入账证据在断网状态下仍可读（断网可读历史）。
2. 租户隔离：不同安装目录（各自 SQLite 数据库）之间无数据串扰——A 的持仓/证据对 B 不可见。
3. 插件攻击面：被撤销/停用的插件不能继续产出新输出（行情端点拒绝），且抽查 HTTPSOnly
   边界由签名信任锚承担（阶段 9 已覆盖，本文件不重复验签用例）。

其余 P8 项（核心流程 E2E、租户隔离全量、插件沙箱运行时、AI 安全护栏、上云回滚演练）依赖
运行时基础设施，滚动在阶段 10 后续，不在纯单元层面伪造结论。
"""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.market_feed import FeedError


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _insert_evidence(test_client, headers, content_hash: str):
    return test_client.post(
        "/evidence",
        headers=headers,
        json={
            "evidence_id": str(uuid4()),
            "tenant_id": test_client.get("/session", headers=headers).json()["user_id"],
            "subject_refs": ["instrument:CN:ETF:510300"],
            "evidence_type": "quote",
            "source_name": "offline-fixture",
            "license_status": "test-fixture",
            "summary": "湖 demo 制证据（用于断网/租户隔离演练）",
            "content_hash": content_hash,
            "relation": "supporting",
            "freshness": "as_of_fixture",
            "status": "active",
        },
    )


def test_offline_drill_history_readable_and_demo_fallback(tmp_path, monkeypatch, client):
    """断网演练：行情源抛错 → 优雅降级 is_demo=True；已入账证据仍可读。"""
    test_client, headers = client

    created = _insert_evidence(test_client, headers, "feed" + "0" * 28)
    assert created.status_code == 201

    def _offline(*_args, **_kwargs):
        raise FeedError("模拟断网：数据供应商不可达")

    monkeypatch.setattr("investment_steward_core.api.app.fetch_cn_kline", _offline)

    candles = test_client.get("/market/candles/510300", headers=headers)
    assert candles.status_code == 200
    payload = candles.json()
    assert payload["is_demo"] is True  # 断网降级，不报 500
    assert payload["limitations"]  # 明确标注降级原因

    # 断网状态可读历史：本地证据账本与数据供应商无关。
    evidence = test_client.get("/evidence", headers=headers).json()
    assert len(evidence) >= 1


def test_candles_period_forwarded_and_timeframe_mapped(tmp_path, monkeypatch, client):
    """period 查询参数必须透传给行情层，且 timeframe 按周期映射（1d/1w/1mo）。"""
    test_client, headers = client
    captured: dict[str, object] = {}

    def _fake_fetch(symbol, limit=120, period="day"):
        captured["symbol"] = symbol
        captured["limit"] = limit
        captured["period"] = period
        return [
            {
                "timestamp": "2026-09-04T00:00:00+00:00",
                "open": 4.6, "high": 4.7, "low": 4.5, "close": 4.62, "volume": 1000.0,
            }
        ], "tencent"

    monkeypatch.setattr("investment_steward_core.api.app.fetch_cn_kline", _fake_fetch)

    week = test_client.get("/market/candles/510300?period=week", headers=headers)
    assert week.status_code == 200
    body = week.json()
    assert captured == {"symbol": "510300", "limit": 120, "period": "week"}
    assert body["timeframe"] == "1w"
    assert body["is_demo"] is False
    assert "腾讯" in body["source_name"]

    invalid = test_client.get("/market/candles/510300?period=quarter", headers=headers)
    assert invalid.status_code == 422


def test_demo_fallback_preserves_period_timeframe(tmp_path, monkeypatch, client):
    """周/月线断网降级时，演示数据不得伪装成日线：timeframe 与 bar 间距须与请求周期一致。"""
    test_client, headers = client

    def _offline(*_args, **_kwargs):
        raise FeedError("模拟断网：数据供应商不可达")

    monkeypatch.setattr("investment_steward_core.api.app.fetch_cn_kline", _offline)

    week = test_client.get("/market/candles/510300?period=week", headers=headers)
    assert week.status_code == 200
    week_body = week.json()
    assert week_body["is_demo"] is True
    assert week_body["timeframe"] == "1w"
    gaps = {
        (
            datetime.fromisoformat(bar_b["timestamp"]) - datetime.fromisoformat(bar_a["timestamp"])
        ).days
        for bar_a, bar_b in zip(week_body["bars"], week_body["bars"][1:])
    }
    assert gaps == {7}

    month = test_client.get("/market/candles/510300?period=month", headers=headers)
    assert month.status_code == 200
    assert month.json()["timeframe"] == "1mo"


def test_tenant_isolation_across_installations_no_cross_leak(tmp_path):
    """租户隔离：不同 data_dir（各自 Core/SQLite）之间无数据串扰。"""
    token = "test-session-token"
    app_a = TestClient(
        create_app(CoreSettings(session_token=token, data_dir=tmp_path / "tenant-a"))
    )
    app_b = TestClient(
        create_app(CoreSettings(session_token=token, data_dir=tmp_path / "tenant-b"))
    )
    headers = {"X-Core-Session-Token": token}

    assert app_a.post(
        "/holdings", headers=headers,
        json={"instrument": "CN:ETF:510300", "label": "A 的持仓", "status": "holding"},
    ).status_code == 201
    assert _insert_evidence(app_a, headers, "aaa" + "0" * 28).status_code == 201

    # B 的账本独立于 A，读不到 A 的持仓与证据。
    assert app_b.get("/holdings", headers=headers).json() == []
    assert app_b.get("/evidence", headers=headers).json() == []

    # B 自身的写入也只属于自己的账本，不回写 A。
    assert app_b.post(
        "/holdings", headers=headers,
        json={"instrument": "CN:ETF:510100", "label": "B 的持仓", "status": "watchlist"},
    ).status_code == 201
    assert app_a.get("/holdings", headers=headers).json() == [
        item for item in app_a.get("/holdings", headers=headers).json()
        if item["instrument"] == "CN:ETF:510300"
    ]


def test_attack_surface_revoked_plugin_cannot_produce_new_output(client):
    """插件攻击面：撤销后，该插件不能继续产出新行情输出（端点拒绝），但历史证据保留。"""
    test_client, headers = client
    assert _insert_evidence(test_client, headers, "his" + "0" * 28).status_code == 201

    revoked = test_client.post("/plugins/official.cn-market-data/revoke", headers=headers)
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"

    # 已撤销插件对应产出端点被阻断。
    assert test_client.get("/market/candles/510300", headers=headers).status_code == 409
    # 历史证据不受撤销影响，仍可读。
    assert len(test_client.get("/evidence", headers=headers).json()) >= 1


def test_offline_health_and_rollback_precondition(tmp_path, monkeypatch, client):
    """门禁边角：断网时 /health 仍可用，且未安装插件不可更新（回滚前置条件存在）。"""
    test_client, headers = client
    monkeypatch.setattr("investment_steward_core.api.app.fetch_cn_kline", lambda *a, **k: (_ for _ in ()).throw(FeedError("offline")))
    assert test_client.get("/health").status_code == 200

    # 未安装 → 更新拒绝 409（回滚不作用于未安装/被撤销对象）。
    assert (
        test_client.post(
            "/plugins/official.golden-evidence/update",
            headers=headers,
            json={"target_version": "0.1.0"},
        ).status_code
        == 409
    )