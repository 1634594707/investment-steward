"""交易日历（Y0-07）单元测试：离线、可控、不联网。

真实上游核验单列在 `.runtime-local/y0-probes/y0_07_calendar_verify.py`；
这里用**注入的假 opener** 与**真实只读夹具**验证解析、交集、降级与窗口语义，
确保断网时 pytest 仍能全绿。
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest
from investment_steward_core import trade_calendar as cal

FIXTURES = Path(__file__).parent / "fixtures" / "youzi"
#: 真实夹具：2024-01-02..2026-09-17 的 658 个交易日（三只证券内容一致）。
REAL_FIXTURE = FIXTURES / "em_valuedays_600519_20240102_20260917.json"


class _FakeResponse(io.BytesIO):
    """最小响应替身：只提供 read()，与 urllib 的上下文管理兼容。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _opener_for(payload: object):
    def _opener(request, timeout=None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return _FakeResponse(raw)

    return _opener


def _payload_for_days(days: list[str]) -> dict[str, object]:
    return {
        "success": True,
        "result": {
            "count": len(days),
            "pages": 1,
            "data": [{"TRADE_DATE": f"{d[:4]}-{d[4:6]}-{d[6:]} 00:00:00"} for d in days],
        },
    }


def _empty_payload() -> dict[str, object]:
    return {"success": False, "result": None, "code": 9201, "message": "返回数据为空"}


@pytest.fixture(autouse=True)
def _clear_caches():
    cal.reset_caches()
    yield
    cal.reset_caches()


def _real_days() -> list[str]:
    payload = json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))
    rows = payload["result"]["data"]
    return sorted({str(row["TRADE_DATE"])[:10].replace("-", "") for row in rows})


# —— 真实夹具：日历内容 ——


def test_real_fixture_has_658_real_trading_days():
    """真实夹具必须给出 2024-01-02..2026-09-17 的 658 个交易日。"""
    days = _real_days()
    assert len(days) == 658
    assert days[0] == "20240102"
    assert days[-1] == "20260917"


def test_real_fixture_excludes_all_weekends():
    days = _real_days()
    weekends = [d for d in days if cal._as_date(d).weekday() >= 5]
    assert weekends == [], f"交易日历不应包含周末：{weekends[:5]}"


@pytest.mark.parametrize(
    "holiday",
    [
        "20250101",  # 元旦
        "20250128",  # 春节
        "20250129",
        "20250130",
        "20250131",
        "20250203",
        "20250204",
        "20250404",  # 清明
        "20250501",  # 劳动节
        "20250502",
        "20250505",
        "20250602",  # 端午
        "20251001",  # 国庆
        "20251002",
        "20251003",
        "20251006",
        "20251007",
        "20251008",
        "20260101",  # 元旦
        "20260102",
        "20260216",  # 春节
        "20260217",
        "20260218",
        "20260219",
        "20260220",
        "20260223",
        "20260406",  # 清明
        "20260501",  # 劳动节
        "20260504",
        "20260505",
        "20260619",  # 端午
    ],
)
def test_real_fixture_excludes_official_holidays(holiday):
    """长假必须整体缺失（Y0-07：不得只排除周末）。"""
    assert holiday not in set(_real_days())


@pytest.mark.parametrize(
    "day",
    [
        "20250102",
        "20250127",
        "20250205",
        "20250403",
        "20250506",
        "20250930",
        "20251009",
        "20260213",
        "20260224",
        "20260917",
    ],
)
def test_real_fixture_keeps_adjacent_trading_days(day):
    """长假两侧的交易日必须保留，避免把「假期前后」误判成连续。"""
    assert day in set(_real_days())


def test_long_holiday_boundaries_are_not_adjacent_by_natural_days():
    """2025 春节：01-27 与 02-05 相邻交易日，但自然日相差 9 天。"""
    result = cal.CalendarResult(True, tuple(_real_days()))
    window = result.window_ending_at("20250205", count=2)
    assert window == ("20250127", "20250205")
    next_day = result.next_after("20250127", count=1)
    assert next_day == ("20250205",)


# —— 交集语义：单只停牌不污染日历 ——


def test_intersection_drops_days_missing_from_one_reference():
    """只有全部基准证券都开市的日子才算交易日（停牌不得被当成休市）。"""

    # 600519 缺 20240103（模拟停牌）：交集应只剩另外两天。
    def selective(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        days = ["20240102", "20240104"] if "600519" in url else ["20240102", "20240103", "20240104"]
        return _FakeResponse(
            json.dumps(_payload_for_days(days), ensure_ascii=False).encode("utf-8")
        )

    result = cal.fetch_calendar(
        "20240102",
        "20240104",
        reference_securities=("600519", "601398", "000001"),
        opener=selective,
    )
    assert result.available is True
    assert result.days == ("20240102", "20240104")
    assert result.reference_count == 3


def test_insufficient_references_make_calendar_unavailable():
    """成功基准不足时宁可不可用，不拿单只票冒充全市场日历。"""
    result = cal.fetch_calendar(
        "20240102",
        "20240104",
        reference_securities=("600519", "601398", "000001"),
        opener=_opener_for(_empty_payload()),
    )
    assert result.available is False
    assert result.days == ()
    assert "基准证券不足" in result.reason


def test_calendar_never_falls_back_to_natural_days():
    """不可用时必须显式降级：绝不返回自然日序列。"""
    result = cal.fetch_calendar(
        "20250128",  # 春节
        "20250204",
        reference_securities=("600519",),
        opener=_opener_for(_empty_payload()),
    )
    assert result.available is False
    assert result.days == ()
    assert result.reason


def test_one_failing_reference_does_not_break_calendar():
    """单只基准证券网络抛错，其余足够时仍可用。"""
    ok = _opener_for(_payload_for_days(["20240102", "20240103", "20240104"]))

    def flaky(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "600519" in url:
            raise OSError("network down")
        return ok(request, timeout)

    result = cal.fetch_calendar(
        "20240102",
        "20240104",
        reference_securities=("600519", "601398", "000001", "600036"),
        opener=flaky,
    )
    assert result.available is True
    assert result.reference_count == 3
    assert result.days == ("20240102", "20240103", "20240104")


def test_all_references_failing_reports_unavailable():
    def always_fail(request, timeout=None):
        raise OSError("network down")

    result = cal.fetch_calendar(
        "20240102",
        "20240104",
        reference_securities=("600519", "601398", "000001"),
        opener=always_fail,
    )
    assert result.available is False
    assert result.days == ()
    assert "基准证券不足" in result.reason


# —— 边界校验 ——


@pytest.mark.parametrize("bad", ["", "2024-1-2", "202401021", "abcdefgh", "20241301"])
def test_invalid_dates_are_rejected(bad):
    with pytest.raises(cal.CalendarError):
        cal.fetch_calendar(bad, "20240104", opener=_opener_for(_empty_payload()))


def test_reversed_range_is_rejected():
    with pytest.raises(cal.CalendarError):
        cal.fetch_calendar("20240104", "20240102", opener=_opener_for(_empty_payload()))


def test_oversized_span_is_rejected():
    with pytest.raises(cal.CalendarError, match="上限"):
        cal.fetch_calendar("19000101", "20260917", opener=_opener_for(_empty_payload()))


@pytest.mark.parametrize(
    "day,expected",
    [
        ("20240102", True),
        ("2024010", False),
        ("20241301", False),
        ("", False),
        ("2024-01-02", True),
    ],
)
def test_is_plausible_day(day, expected):
    assert cal.is_plausible_day(day) is expected


# —— 缓存与冷却 ——


def test_cache_avoids_second_upstream_call():
    calls = {"n": 0}

    def counting(request, timeout=None):
        calls["n"] += 1
        return _FakeResponse(
            json.dumps(_payload_for_days(["20240102"]), ensure_ascii=False).encode("utf-8")
        )

    kwargs = {"reference_securities": ("600519", "601398", "000001"), "opener": counting}
    first = cal.fetch_calendar("20240102", "20240102", **kwargs)
    second = cal.fetch_calendar("20240102", "20240102", **kwargs)
    assert first.available and second.available
    assert calls["n"] == 3, f"命中缓存不应再次请求上游，实际 {calls['n']}"


def test_failure_enters_cooldown_without_repeat_calls():
    calls = {"n": 0}

    def failing(request, timeout=None):
        calls["n"] += 1
        raise OSError("down")

    kwargs = {"reference_securities": ("600519", "601398", "000001"), "opener": failing}
    assert cal.fetch_calendar("20240102", "20240104", **kwargs).available is False
    after_first = calls["n"]
    second = cal.fetch_calendar("20240102", "20240104", **kwargs)
    assert second.available is False
    assert "冷却" in second.reason
    assert calls["n"] == after_first, "冷却期内不应再次打上游"


def test_use_cache_false_bypasses_cache():
    calls = {"n": 0}

    def counting(request, timeout=None):
        calls["n"] += 1
        return _FakeResponse(
            json.dumps(_payload_for_days(["20240102"]), ensure_ascii=False).encode("utf-8")
        )

    kwargs = {"reference_securities": ("600519", "601398", "000001"), "opener": counting}
    cal.fetch_calendar("20240102", "20240102", **kwargs)
    cal.fetch_calendar("20240102", "20240102", use_cache=False, **kwargs)
    assert calls["n"] == 6


# —— 窗口语义（跨日事实的输入） ——


def _real_result() -> cal.CalendarResult:
    return cal.CalendarResult(True, tuple(_real_days()))


def test_window_ending_at_returns_five_trading_days():
    """五交易日窗口跨越周末时必须连续（09-11 五 → 09-17 四）。"""
    window = _real_result().window_ending_at("20260917", count=5)
    assert window == ("20260911", "20260914", "20260915", "20260916", "20260917")


def test_window_ending_at_non_trading_day_is_empty():
    """休市日/未来日不生成窗口：不拿别的日子凑数。"""
    result = _real_result()
    assert result.window_ending_at("20260918", count=5) == ()  # 尚未收盘的非交易日
    assert result.window_ending_at("20260913", count=5) == ()  # 周六
    assert result.window_ending_at("20270101", count=5) == ()  # 未来


def test_window_spanning_spring_festival_is_contiguous_in_trading_days():
    """春节窗口：五交易日必须跳过整段假期，而不是自然日。"""
    window = _real_result().window_ending_at("20250207", count=5)
    assert window == ("20250124", "20250127", "20250205", "20250206", "20250207")
    assert "20250128" not in window


def test_window_before_calendar_start_is_empty():
    """窗口前取不足时返回空：不生成「不完整却看似完整」的窗口。"""
    short = cal.CalendarResult(True, ("20240102", "20240103"))
    assert short.window_ending_at("20240103", count=5) == ()


def test_previous_and_next_are_exclusive_and_ordered():
    result = _real_result()
    assert result.previous("20260917", count=2) == ("20260915", "20260916")
    assert result.next_after("20260916", count=2) == ("20260917",)
    assert result.previous("20240102", count=1) == ()


def test_contains_requires_available_calendar():
    assert _real_result().contains("20260917") is True
    unavailable = cal.CalendarResult(False, (), "no calendar")
    assert unavailable.contains("20260917") is False


def test_recent_trading_days_ends_at_latest_and_excludes_as_of():
    """as_of 当日不作为最后一个交易日（历史复盘的截至日语义）。"""
    opener = _opener_for(_payload_for_days(_real_days()))
    result = cal.recent_trading_days(5, as_of="20260917", opener=opener)
    assert result.available is True
    assert result.days == ("20260911", "20260914", "20260915", "20260916", "20260917")


@pytest.mark.parametrize("bad", [0, -1, True, "5"])
def test_recent_trading_days_rejects_bad_count(bad):
    with pytest.raises(cal.CalendarError):
        cal.recent_trading_days(bad)


def test_recent_trading_days_rejects_excessive_count():
    with pytest.raises(cal.CalendarError):
        cal.recent_trading_days(251)


# —— 并发安全 ——


def test_concurrent_fetch_is_consistent():
    """并发取数结果一致，且缓存不产生跨请求串味。"""
    results: list[cal.CalendarResult] = []
    lock = threading.Lock()

    def worker():
        got = cal.fetch_calendar(
            "20240102",
            "20240104",
            reference_securities=("600519", "601398", "000001"),
            opener=_opener_for(_payload_for_days(["20240102", "20240103", "20240104"])),
        )
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 6
    assert all(r.available and r.days == ("20240102", "20240103", "20240104") for r in results)
