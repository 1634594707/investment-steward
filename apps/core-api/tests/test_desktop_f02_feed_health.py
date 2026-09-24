"""F02（桌面端升级路线图 2026-09-18）：三方取数日志与数据源健康度。

覆盖：埋点逐次记录（成功/失败/重试序号）、错误按**异常类型**归类（不解析供应商文案）、
`data_source_quality` 的证据侧与采集侧**精确同名**合并、样本为 0 时成功率一律 None、
最近秩分位、`/data-source/quality` 与 `/data-source/failures` 的窗口与校验。
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest

from investment_steward_core import feed_health, longterm, market_feed
from investment_steward_core.domain.models import Evidence, EvidenceType
from investment_steward_core.storage.database import Database

USER = uuid5(UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "f02-feed-health-test")


# ———————— 纯函数：聚合与分位 ————————

def test_summarize_empty_sample_reports_none_not_zero():
    """样本为 0 → success_rate/延迟一律 None。"没有样本"与"全都失败"必须能分开。"""
    summary = feed_health.summarize([])
    assert summary["attempts"] == 0
    assert summary["success_rate"] is None
    assert summary["latency_ms"] == {"avg": None, "p50": None, "p95": None, "max": None}
    assert summary["error_kinds"] == []


def test_summarize_success_rate_and_error_kinds():
    rows = [
        {"result": "ok", "latency_ms": 100, "error_kind": None},
        {"result": "ok", "latency_ms": 300, "error_kind": None},
        {"result": "error", "latency_ms": 900, "error_kind": "timeout"},
        {"result": "error", "latency_ms": 50, "error_kind": "network"},
        {"result": "error", "latency_ms": 60, "error_kind": "timeout"},
    ]
    summary = feed_health.summarize(rows)
    assert summary["attempts"] == 5 and summary["ok"] == 2 and summary["errors"] == 3
    assert summary["success_rate"] == 0.4
    assert summary["latency_ms"]["max"] == 900
    assert summary["error_kinds"] == [{"kind": "timeout", "count": 2}, {"kind": "network", "count": 1}]


def test_percentile_uses_nearest_rank_not_interpolation():
    """最近秩法：每个分位都对应一次**真实发生过的**延迟，不插值出未出现的值。"""
    values = [float(v) for v in range(1, 101)]  # 1..100
    assert feed_health.percentile(values, 0.5) == 50.0
    assert feed_health.percentile(values, 0.95) == 95.0
    assert feed_health.percentile([7.0], 0.95) == 7.0
    assert feed_health.percentile([], 0.5) is None


def test_error_kind_explicit_attribute_wins():
    """降级信号类异常自带 error_kind → 不靠类名反推（_Degraded 这种名字对使用者无意义）。"""

    class _Signal(Exception):
        error_kind = "invalid_payload"

    assert feed_health.error_kind_of(_Signal("x")) == "invalid_payload"
    assert feed_health.error_kind_of(TimeoutError("t")) == "timeout"
    assert feed_health.error_kind_of(urllib.error.HTTPError("u", 502, "bad", None, None)) == "http_error"
    assert feed_health.error_kind_of(urllib.error.URLError("refused")) == "network"
    assert feed_health.error_kind_of(json.JSONDecodeError("x", "y", 0)) == "invalid_payload"


def test_source_labels_are_registered():
    """埋点标签必须登记在 ALL_SOURCES 里（防止悄悄引入第二套命名）。"""
    for module in (market_feed,):
        assert feed_health.SOURCE_CN_MARKET_TENCENT in feed_health.ALL_SOURCES
        assert module._PROVIDER_NAME["tencent"] == feed_health.SOURCE_CN_MARKET_TENCENT
        assert module._PROVIDER_NAME["eastmoney"] == feed_health.SOURCE_CN_MARKET_EASTMONEY


# ———————— 埋点：真实 feed 出口 ————————

class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, *args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _kline_payload() -> bytes:
    """腾讯日 K 形状的最小合法响应（data.<txsym>.qfqday = [date, open, close, high, low, volume]）。"""
    return json.dumps(
        {
            "data": {
                "sh600519": {
                    "qfqday": [
                        ["2026-09-17", "1200.0", "1210.0", "1215.0", "1190.0", "12345"],
                        ["2026-09-18", "1210.0", "1257.1", "1260.0", "1205.0", "24891"],
                    ]
                }
            }
        }
    ).encode("utf-8")


def _reset_market_caches() -> None:
    market_feed._cache.clear()
    market_feed._fail_cache.clear()


def test_attempt_recorded_on_success_with_retry_index(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.sqlite3")
    feed_health.set_feed_attempt_recorder(lambda record: db.record_feed_attempt(**record))
    try:
        _reset_market_caches()
        monkeypatch.setattr(market_feed.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_kline_payload()))
        rows, provider = market_feed.fetch_cn_kline("600519", limit=10)
        assert provider == "tencent" and len(rows) == 2
    finally:
        feed_health.set_feed_attempt_recorder(None)
        _reset_market_caches()

    attempts = db.list_feed_attempts(7)
    assert len(attempts) == 1, "一次成功取数 = 一条记录（缓存未命中才产生尝试）"
    assert attempts[0]["source"] == feed_health.SOURCE_CN_MARKET_TENCENT
    assert attempts[0]["result"] == "ok" and attempts[0]["error_kind"] is None
    assert attempts[0]["attempt_index"] == 1
    assert attempts[0]["latency_ms"] >= 0
    assert attempts[0]["params_fingerprint"], "参数指纹必须落库（按同类请求聚合的原料）"


def test_attempt_recorded_per_retry_on_failure(tmp_path, monkeypatch):
    """失败也记，且**逐次重试各记一条**（「第几次才失败」是可查事实）。"""
    db = Database(tmp_path / "s.sqlite3")
    feed_health.set_feed_attempt_recorder(lambda record: db.record_feed_attempt(**record))
    try:
        _reset_market_caches()
        monkeypatch.setattr(market_feed.time, "sleep", lambda *_: None)  # 跳过退避等待
        monkeypatch.setattr(
            market_feed.urllib.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("connection refused")),
        )
        with pytest.raises(market_feed.FeedError):
            market_feed.fetch_cn_kline("600519", limit=10)
    finally:
        feed_health.set_feed_attempt_recorder(None)
        _reset_market_caches()

    attempts = db.list_feed_attempts(7)
    # 腾讯 3 次 + 东财 3 次（都失败才会抛 FeedError）。
    assert len(attempts) == 6
    assert all(row["result"] == "error" and row["error_kind"] == "network" for row in attempts)
    tencent = [row for row in attempts if row["source"] == feed_health.SOURCE_CN_MARKET_TENCENT]
    assert sorted(row["attempt_index"] for row in tencent) == [1, 2, 3]
    assert len({row["source"] for row in attempts}) == 2


def test_recorder_absent_is_noop(tmp_path, monkeypatch):
    """未安装 recorder（离线单测/脚本）时埋点是空操作——绝不因埋点影响取数。"""
    feed_health.set_feed_attempt_recorder(None)
    _reset_market_caches()
    monkeypatch.setattr(market_feed.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(_kline_payload()))
    rows, provider = market_feed.fetch_cn_kline("600519", limit=10)
    assert provider == "tencent" and rows
    _reset_market_caches()


# ———————— 数据源质量：两侧合并 ————————

def _evidence(evidence_id, source_name: str = "测试源") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        tenant_id=USER,
        subject_refs=["510300"],
        evidence_type=EvidenceType.MACRO,
        source_name=source_name,
        summary="波动率回落证据。",
        content_hash="hash-" + str(evidence_id).replace("-", ""),
        collected_at=datetime.now(UTC) - timedelta(days=1),
    )


def test_quality_merges_evidence_and_attempts_by_exact_name(tmp_path):
    db = Database(tmp_path / "s.sqlite3")
    db.insert_evidence(_evidence(uuid4()))
    db.insert_evidence(_evidence(uuid4(), source_name=feed_health.SOURCE_CN_MARKET_TENCENT))
    for result, kind, latency in (("ok", None, 120), ("ok", None, 300), ("error", "timeout", 9000)):
        db.record_feed_attempt(
            source=feed_health.SOURCE_CN_MARKET_TENCENT,
            endpoint="https://example/kline",
            params_fingerprint="abc123",
            started_at=datetime.now(UTC).isoformat(),
            latency_ms=latency,
            result=result,
            error_kind=kind,
        )

    report = longterm.data_source_quality(db, USER, window_days=7)
    assert report["window"] == "7d" and report["window_days"] == 7

    tencent = next(s for s in report["sources"] if s["source"] == feed_health.SOURCE_CN_MARKET_TENCENT)
    # 证据侧（精确同名，1 条）与采集侧（3 次尝试）合并在同一行。
    assert tencent["evidence_count"] == 1
    assert tencent["attempts"] == 3 and tencent["ok"] == 2 and tencent["errors"] == 1
    assert tencent["success_rate"] == round(2 / 3, 4)
    assert tencent["latency_ms"]["max"] == 9000
    assert tencent["error_kinds"] == [{"kind": "timeout", "count": 1}]

    # 只有证据、没有采集日志的源：成功率为 None 且给出缺口说明（不再是模糊的「数据不足」）。
    plain = next(s for s in report["sources"] if s["source"] == "测试源")
    assert plain["evidence_count"] == 1 and plain["attempts"] == 0
    assert plain["success_rate"] is None
    assert "无采集日志" in plain["note"]
    assert any(g["dimension"] == "success_rate" for g in report["gaps"])
    assert "测试源" in next(g for g in report["gaps"] if g["dimension"] == "success_rate")["reason"]


def test_quality_window_filters_old_attempts(tmp_path):
    """窗口外的尝试不进本周视图——本周视图只反映**本周发生过的事**。"""
    db = Database(tmp_path / "s.sqlite3")
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    db.record_feed_attempt(
        source=feed_health.SOURCE_CFFEX,
        endpoint="https://cffex/1",
        params_fingerprint="x",
        started_at=old,
        latency_ms=500,
        result="error",
        error_kind="http_error",
    )
    week = longterm.data_source_quality(db, USER, window_days=7)
    assert all(s["source"] != feed_health.SOURCE_CFFEX for s in week["sources"]), "20 天前的尝试不在 7 天窗口内"
    month = longterm.data_source_quality(db, USER, window_days=30)
    row = next(s for s in month["sources"] if s["source"] == feed_health.SOURCE_CFFEX)
    assert row["attempts"] == 1 and row["success_rate"] == 0.0 and row["evidence_count"] == 0


def test_quality_without_any_feed_log_says_sample_missing(tmp_path):
    """与 test_longterm 同口径：无采集日志时成功率为 None 并保留 success_rate 缺口。"""
    db = Database(tmp_path / "s.sqlite3")
    db.insert_evidence(_evidence(uuid4()))
    report = longterm.data_source_quality(db, USER)
    source = next(s for s in report["sources"] if s["source"] == "测试源")
    assert source["evidence_count"] == 1
    assert source["success_rate"] is None
    assert any(g["dimension"] == "success_rate" for g in report["gaps"])


# ———————— 端点 ————————

def _client(tmp_path):
    from fastapi.testclient import TestClient

    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings

    token = "f02-feed-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    http = TestClient(app)
    return http, {"X-Core-Session-Token": token}


def test_endpoints_expose_health_and_failures(tmp_path):
    http, headers = _client(tmp_path)
    db = http.app.state.core.database
    now = datetime.now(UTC).isoformat()
    db.record_feed_attempt(
        source=feed_health.SOURCE_LHB_EASTMONEY,
        endpoint="https://datacenter-web.eastmoney.com/api/data/v1/get",
        params_fingerprint="fp1",
        started_at=now,
        latency_ms=42,
        result="ok",
        error_kind=None,
    )
    db.record_feed_attempt(
        source=feed_health.SOURCE_LHB_EASTMONEY,
        endpoint="https://datacenter-web.eastmoney.com/api/data/v1/get",
        params_fingerprint="fp1",
        started_at=now,
        latency_ms=15000,
        result="error",
        error_kind="timeout",
        attempt_index=2,
    )

    quality = http.get("/data-source/quality", params={"window": "7d"}, headers=headers)
    assert quality.status_code == 200
    body = quality.json()
    row = next(s for s in body["sources"] if s["source"] == feed_health.SOURCE_LHB_EASTMONEY)
    assert row["attempts"] == 2 and row["success_rate"] == 0.5
    assert body["attempts_total"] == 2 and body["attempts_truncated"] == 0

    failures = http.get("/data-source/failures", params={"window": "7d"}, headers=headers).json()
    assert failures["count"] == 1
    item = failures["failures"][0]
    assert item["source"] == feed_health.SOURCE_LHB_EASTMONEY
    assert item["error_kind"] == "timeout" and item["attempt_index"] == 2
    assert item["latency_ms"] == 15000

    assert http.get("/data-source/quality", params={"window": "90d"}, headers=headers).status_code == 422
    assert http.get("/data-source/failures", params={"limit": 0}, headers=headers).status_code == 422
    assert http.get("/data-source/failures", params={"window": "1y"}, headers=headers).status_code == 422
