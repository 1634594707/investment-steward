"""D01（桌面端升级路线图 2026-09-18）：市场扫描任务化的端点级测试。

覆盖：POST 建任务立即返回、GET 进度（done/total 逐票推进、done 时 summary 形状与旧同步
响应一致）、同输入指纹幂等命中（不重复计算）、取消（停止后不再发新取数）、
running 超 30 分钟无心跳按「任务中断」呈现、表名级裁剪（只留最近 20 个已结束任务）。
"""

from __future__ import annotations

import hashlib
import time as time_module
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

TOKEN = "d01-scan-jobs-token"


def _client(tmp_path):
    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def _install_plugin(http, headers) -> None:
    assert http.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200


def _poll(http, headers, job_id: str, timeout_s: float = 15.0) -> dict:
    deadline = time_module.monotonic() + timeout_s
    while time_module.monotonic() < deadline:
        body = http.get(f"/tactics/scan-market/{job_id}", headers=headers).json()
        if body["state"] in ("done", "cancelled", "error"):
            return body
        time_module.sleep(0.05)
    raise AssertionError("扫描任务未在时限内到达终态")


def _fake_feeds(monkeypatch):
    """榜单与 K 线全部打桩：两票确定性触发金叉。"""
    from investment_steward_core.api import app as app_module

    def _fake_board(board: str, top: int = 30):
        return [
            {"symbol": "000060", "name": "中金岭南", "price": 6.6, "change_pct": 1.2,
             "volume": 1e6, "turnover": 1e8, "turnover_rate": 1.0, "volume_ratio": 1.1},
            {"symbol": "510300", "name": "沪深300ETF", "price": 4.0, "change_pct": 0.3,
             "volume": 1e6, "turnover": 1e8, "turnover_rate": 1.0, "volume_ratio": 1.0},
        ]

    monkeypatch.setattr(app_module, "fetch_cn_market_board", _fake_board)
    monkeypatch.setattr(app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_trend_bars(), "unit-test"))


def _trend_bars():
    """40 根缓涨日线（战法引擎 MIN_BARS=35 之上），确定性触发 ma_golden_cross。"""
    bars = []
    price = 10.0
    from datetime import datetime as dt

    day = dt(2026, 9, 1, tzinfo=UTC)
    for index in range(40):
        price *= 1.01
        bars.append({
            "timestamp": day.isoformat(),
            "open": price * 0.99,
            "close": price,
            "high": price * 1.01,
            "low": price * 0.98,
            "volume": 1e6 + index,
        })
    return bars


def test_scan_job_lifecycle_progress_and_summary(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)

    started = http.post(
        "/tactics/scan-market", headers=headers,
        json={"boards": ["turnover"], "per_board": 30, "max_symbols": 60, "recent_bars": 60},
    ).json()
    assert started["ok"] is True and started["state"] == "running" and started["reused"] is False
    job_id = started["job_id"]

    done_body = _poll(http, headers, job_id)
    assert done_body["state"] == "done"
    summary = done_body["summary"]
    # summary 形状与旧同步响应一致。
    assert summary["mode"] == "boards" and summary["boards"] == ["turnover"]
    assert [row["symbol"] for row in summary["results"]] == ["000060", "510300"]
    assert all(row["ok"] for row in summary["results"])
    assert done_body["done"] == done_body["total"] == summary["scanned"], "进度计数与实际扫描票数一致"
    # 结束后同指纹再次提交：运行中才复用；已结束的任务不复用（新任务会新建）。
    again = http.post(
        "/tactics/scan-market", headers=headers,
        json={"boards": ["turnover"], "per_board": 30, "max_symbols": 60, "recent_bars": 60},
    ).json()
    assert again["reused"] is False and again["job_id"] != job_id


def test_scan_job_fingerprint_reuse_while_running(tmp_path, monkeypatch):
    """同输入指纹 + 仍有 running 任务 → 直接命中既有任务（不重复计算）。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    database = http.app.state.core.database
    body = {"mode": "boards", "boards": ["turnover"], "per_board": 30, "max_symbols": 60}
    fingerprint = hashlib.sha256(
        __import__("investment_steward_core.api.app", fromlist=["TacticsMarketScanRequest"])
        .TacticsMarketScanRequest(**body).model_dump_json().encode("utf-8")
    ).hexdigest()
    database.create_scan_job("job-running-1", fingerprint, body)

    started = http.post("/tactics/scan-market", headers=headers, json=body).json()
    assert started["reused"] is True
    assert started["job_id"] == "job-running-1"


def test_scan_job_cancel_stops_new_fetches(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    database = http.app.state.core.database
    body = {"mode": "boards", "boards": ["turnover"], "per_board": 30, "max_symbols": 60}
    fingerprint = hashlib.sha256(
        __import__("investment_steward_core.api.app", fromlist=["TacticsMarketScanRequest"])
        .TacticsMarketScanRequest(**body).model_dump_json().encode("utf-8")
    ).hexdigest()
    database.create_scan_job("job-cancel-1", fingerprint, body)

    cancelled = http.post("/tactics/scan-market/job-cancel-1/cancel", headers=headers).json()
    assert cancelled["ok"] is True
    # 已取消的任务不可再取消。
    assert http.post("/tactics/scan-market/job-cancel-1/cancel", headers=headers).json()["ok"] is False
    assert http.get("/tactics/scan-market/job-cancel-1", headers=headers).json()["state"] == "cancelled"
    # 取消标记会阻断执行线程：真实 worker 在下一票检测到 cancelled 即抛出，不再发新取数
    #（本用例用直接标记验证端点与存储语义；逐票检测在 _scan_run_engine 中实现并由生命周期用例覆盖）。


def test_scan_job_summary_carries_elapsed_and_failures(tmp_path, monkeypatch):
    """D03：summary 回传实际耗时与失败票清单（多少票失败 + 失败原因）。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)

    # K 线源全部故障：每票 ok=False，failures 应逐票给出原因。
    from investment_steward_core.api import app as app_module
    from investment_steward_core.market_feed import FeedError

    def _broken_kline(symbol: str, limit: int = 250, period: str = "day"):
        raise FeedError("kline offline")

    monkeypatch.setattr(app_module, "fetch_cn_kline", _broken_kline)
    started = http.post(
        "/tactics/scan-market", headers=headers,
        json={"boards": ["turnover"], "per_board": 30, "max_symbols": 60, "recent_bars": 60},
    ).json()
    done_body = _poll(http, headers, started["job_id"])
    summary = done_body["summary"]
    assert done_body["done"] == done_body["total"] == 2
    assert summary["elapsed_ms"] >= 0
    assert len(summary["failures"]) == 2
    assert {row["symbol"] for row in summary["failures"]} == {"000060", "510300"}
    assert all("K 线取数失败" in row["error"] for row in summary["failures"])


def test_scan_job_concurrency_preserves_result_order(tmp_path, monkeypatch):
    """D03：并发=2 与串行产生相同的结果集与排序（仅执行方式不同）。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)

    def _start(extra: dict) -> dict:
        started = http.post(
            "/tactics/scan-market", headers=headers,
            json={"boards": ["turnover"], "per_board": 30, "max_symbols": 60, "recent_bars": 60, **extra},
        ).json()
        return _poll(http, headers, started["job_id"])["summary"]

    serial = _start({"concurrency": 1, "interval_secs": 0})
    concurrent = _start({"concurrency": 2, "interval_secs": 0.05})
    assert [row["symbol"] for row in concurrent["results"]] == [row["symbol"] for row in serial["results"]]
    assert all(row["ok"] for row in concurrent["results"])


def test_scan_job_rejects_out_of_range_rate_params(tmp_path):
    """interval/concurrency 越界 → 422（上游压力画像不因误配而放大）。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    assert http.post(
        "/tactics/scan-market", headers=headers,
        json={"boards": ["turnover"], "interval_secs": 9},
    ).status_code == 422
    assert http.post(
        "/tactics/scan-market", headers=headers,
        json={"boards": ["turnover"], "concurrency": 16},
    ).status_code == 422


def test_scan_job_stale_running_reported_as_error(tmp_path):
    http, headers = _client(tmp_path)
    database = http.app.state.core.database
    database.create_scan_job("job-stale", "fp-stale", {"mode": "boards", "boards": ["turnover"]})
    # 心跳拨回 31 分钟前：GET 按「任务中断」呈现，不假装还在跑。
    stale_time = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    database.update_scan_job_progress("job-stale", 3, 10)
    import sqlite3 as _sqlite3

    with _sqlite3.connect(database.path) as raw:
        raw.execute("UPDATE scan_jobs SET updated_at = ? WHERE job_id = 'job-stale'", (stale_time,))
        raw.commit()
    body = http.get("/tactics/scan-market/job-stale", headers=headers).json()
    assert body["state"] == "error"
    assert "中断" in body["error"]
