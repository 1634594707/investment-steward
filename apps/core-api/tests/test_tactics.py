"""战法雷达（official.stock-tactics）契约与引擎测试。

覆盖：
1. 引擎正确性：构造「横盘→下跌→放量拉升」K 线，必须识别出均线金叉、死叉、多头排列与放量突破；
2. 密钥旋转：plugins/official 全部 manifest 用 pinned 公钥过 Ed25519 验签 + 哈希校验；
3. 观察清单与笔记 CRUD（含删除不存在资源的 404）；
4. 扫描：观察清单/手动列表逐票识别，单票失败不拖垮整体（插件未启用时如实返回错误行）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import tactics
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.signing import verify_manifest_integrity

REPO_ROOT = Path(__file__).resolve().parents[3]
PUB_KEY = REPO_ROOT / "apps" / "core-api" / "keys" / "steward-plugin-publishing.pub.pem"


def _trend_bars() -> list[dict[str, object]]:
    """横盘 40 根 → 下跌 15 根 → 放量拉升 25 根：确定性触发金叉与放量突破。"""
    bars: list[dict[str, object]] = []
    base = datetime.now(UTC) - timedelta(days=80)
    for i in range(80):
        if i < 40:
            p = 10.0
        elif i < 55:
            p = 10.0 - (i - 40) * 0.15
        else:
            p = 8.0 + (i - 55) * 0.28
        volume = 1_000_000 if i < 60 else 3_000_000
        bars.append({
            "timestamp": (base + timedelta(days=i)).isoformat(),
            "open": p + 0.02, "high": p + 0.08, "low": p - 0.02,
            "close": p + 0.05, "volume": volume,
        })
    return bars


def test_engine_detects_golden_death_cross_and_breakout():
    snap = tactics.snapshot(_trend_bars())
    ids = {signal["tactic_id"] for signal in snap["signals"]}
    assert "ma_golden_cross" in ids
    assert "ma_death_cross" in ids
    assert "ma_bullish_alignment" in ids
    assert "volume_breakout" in ids
    assert snap["sufficient"] is True
    # 指标现值必须齐备且非空（拉升段末尾 RSI 应高于 50）。
    assert snap["indicators"]["rsi14"] is not None and snap["indicators"]["rsi14"] > 50


def test_engine_short_history_is_honest():
    snap = tactics.snapshot(_trend_bars()[:10])
    assert snap["sufficient"] is False


def test_all_registry_manifests_verify_with_pinned_key():
    manifests = sorted((REPO_ROOT / "plugins" / "official").rglob("manifest.json"))
    assert len(manifests) >= 9  # 8 个既有官方插件 + stock-tactics
    import json

    for path in manifests:
        payload = json.loads(path.read_text(encoding="utf-8"))
        verify_manifest_integrity(payload, PUB_KEY.read_text(encoding="utf-8"))


def test_watchlist_and_notes_crud(client):
    test_client, headers = client
    saved = test_client.put(
        "/tactics/watchlist/000060", headers=headers,
        json={"name": "中金岭南", "note": "跟踪放量突破"},
    )
    assert saved.status_code == 200

    listed = test_client.get("/tactics/watchlist", headers=headers).json()
    assert [entry["symbol"] for entry in listed] == ["000060"]
    assert listed[0]["name"] == "中金岭南"

    note = test_client.post(
        "/tactics/watchlist/000060/notes", headers=headers, json={"content": "20 日线企稳，先观察"}
    ).json()
    assert note["symbol"] == "000060"
    assert test_client.get("/tactics/watchlist/000060/notes", headers=headers).json()[0]["content"] == "20 日线企稳，先观察"

    assert test_client.delete(f"/tactics/notes/{note['note_id']}", headers=headers).status_code == 204
    assert test_client.delete("/tactics/watchlist/000060", headers=headers).status_code == 204
    assert test_client.get("/tactics/watchlist", headers=headers).json() == []
    # 删除不存在的资源如实 404
    assert test_client.delete("/tactics/watchlist/000060", headers=headers).status_code == 404


def test_scan_reports_per_symbol_without_crashing(client, monkeypatch):
    test_client, headers = client
    # 空范围：sources 全关且无手动列表 → 如实返回空扫描。
    empty = test_client.post("/tactics/scan", headers=headers, json={"sources": [], "symbols": []})
    assert empty.status_code == 200
    assert empty.json()["scanned"] == 0

    # K 线源故障：单票行如实携带错误（引擎降级），不抛 500、不拖垮整体。
    from investment_steward_core.api import app as app_module
    from investment_steward_core.market_feed import FeedError

    def _broken_kline(symbol: str, limit: int = 250, period: str = "day"):
        raise FeedError("kline source offline")

    monkeypatch.setattr(app_module, "fetch_cn_kline", _broken_kline)
    response = test_client.post(
        "/tactics/scan", headers=headers,
        json={"sources": ["manual"], "symbols": ["000060", "510300"]},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert [row["symbol"] for row in results] == ["000060", "510300"]
    assert all(row["ok"] is False for row in results)
    assert "K 线取数失败" in results[0]["error"]

    # 恢复取数：手动列表逐票识别，确定性触发金叉。
    def _fake_kline(symbol: str, limit: int = 250, period: str = "day"):
        return _trend_bars(), "unit-test"

    monkeypatch.setattr(app_module, "fetch_cn_kline", _fake_kline)
    recovered = test_client.post(
        "/tactics/scan", headers=headers,
        json={"sources": ["manual"], "symbols": ["000060"], "recent_bars": 60},
    )
    assert recovered.status_code == 200
    row = recovered.json()["results"][0]
    assert row["ok"] is True
    assert "ma_golden_cross" in row["hit_tactics"]


def test_market_scan_two_stage_merges_boards_and_ranks(client, monkeypatch):
    """市场批量扫描：榜单粗筛合并去重 → 逐票跑引擎 → 命中数排序。"""
    test_client, headers = client
    from investment_steward_core.api import app as app_module

    def _fake_board(board: str, top: int = 30):
        rows = [
            {"symbol": "000060", "name": "中金岭南", "price": 6.6, "change_pct": 1.2,
             "volume": 1e6, "turnover": 1e8, "turnover_rate": 1.0, "volume_ratio": 1.1},
            {"symbol": "510300", "name": "沪深300ETF", "price": 4.0, "change_pct": 0.3,
             "volume": 1e6, "turnover": 1e8, "turnover_rate": 1.0, "volume_ratio": 1.0},
        ]
        return rows

    monkeypatch.setattr(app_module, "fetch_cn_market_board", _fake_board)

    def _fake_kline(symbol: str, limit: int = 250, period: str = "day"):
        return _trend_bars(), "unit-test"

    monkeypatch.setattr(app_module, "fetch_cn_kline", _fake_kline)

    response = test_client.post(
        "/tactics/scan-market", headers=headers,
        json={"boards": ["turnover", "gainers"], "per_board": 30, "max_symbols": 60, "recent_bars": 60},
    )
    assert response.status_code == 200
    started = response.json()
    assert started["ok"] is True and started["state"] == "running"
    # D01：POST 只建任务，结果经 GET 轮询直至终态（summary 形状与旧同步响应一致）。
    job_id = started["job_id"]
    import time as _time

    data = None
    for _ in range(100):
        _time.sleep(0.1)
        status_response = test_client.get(f"/tactics/scan-market/{job_id}", headers=headers).json()
        if status_response["state"] == "done":
            data = status_response["summary"]
            break
    assert data is not None, "扫描任务 10s 内未完成"
    # 两榜同两票：合并去重后 scanned=2；命中数降序（两票同信号数，保持榜单序）。
    assert data["scanned"] == 2
    assert data["boards"] == ["turnover", "gainers"]
    assert [row["symbol"] for row in data["results"]] == ["000060", "510300"]
    assert all(row["ok"] for row in data["results"])
    assert "ma_golden_cross" in data["results"][0]["hit_tactics"]


def test_market_scan_rejects_unknown_boards(client):
    test_client, headers = client
    response = test_client.post("/tactics/scan-market", headers=headers, json={"boards": ["bogus"]})
    assert response.status_code == 422


def test_catalog_endpoint_lists_tactics(client):
    test_client, headers = client
    catalog = test_client.get("/tactics/catalog", headers=headers).json()
    ids = {item["id"] for item in catalog}
    assert {"ma_golden_cross", "ma_death_cross", "macd_golden_cross", "volume_breakout"} <= ids
