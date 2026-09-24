"""F03（桌面端升级路线图 2026-09-18）：「仅重试失败项」的探测登记与端点契约。

覆盖：探测登记必须覆盖全部已埋点来源（减去声明为「需外部输入」的两条）、探测绕缓存才会
产生样本、逐源报告（一个源失败不影响其余）、端点校验（不提供无差别全量重试）、
重试结果经 F02 埋点落库。
"""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime

from investment_steward_core import feed_health, feed_probe, market_feed
from investment_steward_core.storage.database import Database


def test_probe_registry_covers_every_instrumented_source():
    """新增埋点标签时不会漏登记探测：ALL_SOURCES = PROBES ∪ UNSUPPORTED。"""
    covered = set(feed_probe.PROBES) | set(feed_probe.UNSUPPORTED)
    assert covered == set(feed_health.ALL_SOURCES), f"未覆盖：{set(feed_health.ALL_SOURCES) - covered}"
    assert not set(feed_probe.PROBES) & set(feed_probe.UNSUPPORTED), "可探测与不可探测不能同时成立"


def test_unsupported_sources_say_why_in_user_words():
    """「能否重试」必须答得出来：不能重试的两条各有面向用户的原因，而不是空按钮。"""
    for source, reason in feed_probe.UNSUPPORTED.items():
        assert source in feed_health.ALL_SOURCES
        assert "交易日" in reason, f"{source} 的原因须点明需要交易日（自然日≠交易日）"


def test_probe_sources_reports_per_source_and_never_raises():
    store = object()

    def _ok(_store):
        return "fine"

    def _boom(_store):
        raise urllib.error.URLError("connection refused")

    def _missing(_store):
        raise feed_probe._Skip("凭据库无 key")

    probes = {feed_health.SOURCE_MACRO_OKX: _ok, feed_health.SOURCE_COMTRADE: _boom, feed_health.SOURCE_MACRO_FRED: _missing}
    original = dict(feed_probe.PROBES)
    try:
        feed_probe.PROBES.update(probes)
        results = feed_probe.probe_sources(
            [feed_health.SOURCE_MACRO_OKX, feed_health.SOURCE_COMTRADE, feed_health.SOURCE_MACRO_FRED, feed_health.SOURCE_CFFEX],
            store,
        )
    finally:
        feed_probe.PROBES.clear()
        feed_probe.PROBES.update(original)

    by_source = {item["source"]: item for item in results}
    assert by_source[feed_health.SOURCE_MACRO_OKX] == {
        "source": feed_health.SOURCE_MACRO_OKX, "supported": True, "ok": True, "detail": "fine",
    }
    assert by_source[feed_health.SOURCE_COMTRADE]["ok"] is False
    assert "URLError" in by_source[feed_health.SOURCE_COMTRADE]["reason"]
    skipped = by_source[feed_health.SOURCE_MACRO_FRED]
    assert skipped["ok"] is None and skipped["skipped"] is True, "缺前置条件不算失败也不算成功"
    unsupported = by_source[feed_health.SOURCE_CFFEX]
    assert unsupported["supported"] is False and unsupported["reason"] == feed_probe.UNSUPPORTED[feed_health.SOURCE_CFFEX]
    assert len(results) == 4, "一个源失败不影响其余源"


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, *args):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_probe_bypasses_cache_so_it_produces_a_sample(tmp_path, monkeypatch):
    """探测必须产生一条真实尝试记录——否则面板上"重试了但没有新样本"比不重试更困惑。"""
    db = Database(tmp_path / "s.sqlite3")
    feed_health.set_feed_attempt_recorder(lambda record: db.record_feed_attempt(**record))
    payload = json.dumps(
        {
            "data": {
                "sh510300": {
                    "qfqday": [
                        ["2026-09-17", "4.0", "4.01", "4.02", "3.99", "100"],
                        ["2026-09-18", "4.01", "4.03", "4.04", "4.00", "200"],
                    ]
                }
            }
        }
    ).encode("utf-8")
    try:
        market_feed._cache.clear()
        market_feed._fail_cache.clear()
        monkeypatch.setattr(market_feed.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))
        detail = feed_probe._probe_cn_market_tencent()
        # 再探一次：低层函数不经 90 秒缓存，第二次仍然是一次真实尝试。
        feed_probe._probe_cn_market_tencent()
    finally:
        feed_health.set_feed_attempt_recorder(None)
        market_feed._cache.clear()
        market_feed._fail_cache.clear()

    assert "2 根日 K" in detail
    attempts = db.list_feed_attempts(7)
    assert len(attempts) == 2, "探测走低层函数，绕开 TTL 缓存（缓存命中不会产生样本）"
    assert all(row["source"] == feed_health.SOURCE_CN_MARKET_TENCENT and row["result"] == "ok" for row in attempts)


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings

    token = "f03-retry-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def test_retry_endpoint_validates_input(tmp_path):
    http, headers = _client(tmp_path)
    # 既不列来源也不只重试失败项 → 拒绝（不提供无差别全量重试）。
    assert http.post("/data-source/retry", json={}, headers=headers).status_code == 422
    # 未登记来源 → 422 且点明可选值。
    bad = http.post("/data-source/retry", json={"sources": ["不存在的源"]}, headers=headers)
    assert bad.status_code == 422 and "未登记的数据源" in bad.json()["detail"]
    assert http.post("/data-source/retry", json={"sources": [], "only_failed": True, "window": "1y"}, headers=headers).status_code == 422
    # 窗口内没有失败项 → 空操作而不是报错。
    empty = http.post("/data-source/retry", json={"only_failed": True}, headers=headers)
    assert empty.status_code == 200 and empty.json()["attempted"] == 0


def test_retry_only_failed_targets_recorded_failures(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    db = http.app.state.core.database
    db.record_feed_attempt(
        source=feed_health.SOURCE_MACRO_OKX,
        endpoint="https://www.okx.com/api/v5/market/candles",
        params_fingerprint="fp",
        started_at=datetime.now(UTC).isoformat(),
        latency_ms=8000,
        result="error",
        error_kind="timeout",
    )
    db.record_feed_attempt(
        source=feed_health.SOURCE_MACRO_WORLDBANK,
        endpoint="https://api.worldbank.org/v2/country",
        params_fingerprint="fp2",
        started_at=datetime.now(UTC).isoformat(),
        latency_ms=300,
        result="ok",
        error_kind=None,
    )
    assert db.list_failed_feed_sources(7) == [feed_health.SOURCE_MACRO_OKX], "只挑失败过的源"

    seen: list[list[str]] = []

    def _fake_probe_sources(sources, store):
        seen.append(list(sources))
        return [{"source": name, "supported": True, "ok": True, "detail": "重试成功"} for name in sources]

    monkeypatch.setattr("investment_steward_core.api.app.feed_probe.probe_sources", _fake_probe_sources)
    body = http.post("/data-source/retry", json={"only_failed": True}, headers=headers).json()
    assert seen == [[feed_health.SOURCE_MACRO_OKX]]
    assert body["attempted"] == 1 and body["results"][0]["ok"] is True

    # 显式列出多个来源时逐个探测，且去重。
    body2 = http.post(
        "/data-source/retry",
        json={"sources": [feed_health.SOURCE_MACRO_OKX, feed_health.SOURCE_MACRO_OKX, feed_health.SOURCE_CN_MARKET_SINA]},
        headers=headers,
    ).json()
    assert body2["attempted"] == 2 and seen[-1] == [feed_health.SOURCE_MACRO_OKX, feed_health.SOURCE_CN_MARKET_SINA]


def test_retry_is_audited(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    monkeypatch.setattr(
        "investment_steward_core.api.app.feed_probe.probe_sources",
        lambda sources, store: [{"source": s, "supported": True, "ok": True, "detail": "ok"} for s in sources],
    )
    http.post("/data-source/retry", json={"sources": [feed_health.SOURCE_MACRO_OKX]}, headers=headers)
    audit = http.get("/audit", params={"q": "data_source.retry", "with_total": "true"}, headers=headers).json()
    assert audit["total"] == 1
    assert audit["items"][0]["action"] == "data_source.retry"


def test_retry_endpoint_is_registered_once():
    """路由唯一性（B04 的守护）：/data-source/retry 只有一份注册。"""
    from pathlib import Path
    import tempfile

    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings

    app = create_app(CoreSettings(session_token="t", data_dir=Path(tempfile.mkdtemp())))
    matches = [route for route in app.routes if getattr(route, "path", None) == "/data-source/retry"]
    assert len(matches) == 1
    assert sorted(matches[0].methods) == ["POST"]
