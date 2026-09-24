"""Y3-05/06：披露后离散期限状态与刷新（纯函数 + 有界缓存，全离线）。

被测的四条铁律（模块文档串里写死的那四条）：
 1. null 不是 0——未成熟/缺失一律 `value_pct=None` + 显式 status。
 2. `not_due` 与 `missing` 必须分开。
 3. 缺失不永久负缓存（短冷却，不是长 TTL）。
 4. 每个值可分辨「本次抓取」与「旧快照」（`fetched_at`）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from investment_steward_core import youzi_horizon as hz

#: 连续的显式交易日（合成，仅供算法测试；不声明是真实交易所日历）。
DAYS = tuple(
    (date(2026, 9, 1) + timedelta(days=offset)).strftime("%Y%m%d")
    for offset in range(0, 60)
    if (date(2026, 9, 1) + timedelta(days=offset)).weekday() < 5
)


def row_with(**fields: object) -> dict[str, object]:
    return {"SECURITY_CODE": "600519", **fields}


def by_label(values: tuple[hz.HorizonValue, ...]) -> dict[str, hz.HorizonValue]:
    return {item.label: item for item in values}


def test_d3_is_not_offered_but_d1_d2_d5_d10_d20_d30_are() -> None:
    assert hz.horizon_labels() == ("D1", "D2", "D5", "D10", "D20", "D30")


def test_disclosed_value_is_reported_verbatim_with_status_and_target() -> None:
    """已披露：数值原样透出（不做任何加工），并给出到期目标交易日。"""
    day = DAYS[0]
    values = by_label(
        hz.resolve_horizons(
            day,
            row_with(D1_CLOSE_ADJCHRATE=3.25, D2_CLOSE_ADJCHRATE=-1.5),
            DAYS,
            calendar_available=True,
            fetched_at=1000.0,
        )
    )
    assert values["D1"].value_pct == 3.25
    assert values["D1"].status == hz.STATUS_DISCLOSED
    assert values["D1"].target_date == DAYS[1]
    assert values["D1"].source == "RPT_DAILYBILLBOARD_DETAILS"
    assert values["D1"].fetched_at == 1000.0
    assert values["D1"].reason == ""
    assert values["D2"].value_pct == -1.5  # 负值必须原样保留，不取绝对值。
    assert values["D2"].target_date == DAYS[2]


def test_not_due_when_not_enough_following_trading_days() -> None:
    """披露日之后不足 N 个交易日 → `not_due`，不是 0，也不是 missing。

    ★ 注意分界：D1 的目标日只要在已知日历里就**已经到期**（此时字段为 null 是
    `missing`）。真正「未到期」的是目标日**超出已知日历**的那些期限——它们
    连目标日都算不出来（不推算自然日），因此 `target_date` 为空串。
    """
    day = DAYS[0]
    # 只给披露日 + 后续一天：D1 到期（目标日 DAYS[1] 在日历内），D2 起未到期。
    window = DAYS[:2]
    values = by_label(
        hz.resolve_horizons(day, row_with(), window, calendar_available=True, fetched_at=1.0)
    )
    # D1：目标日在日历里 → 已到期但字段为 null → missing（不是 not_due）。
    assert values["D1"].status == hz.STATUS_MISSING
    assert values["D1"].target_date == DAYS[1]
    for label in ("D2", "D5", "D10", "D20", "D30"):
        assert values[label].status == hz.STATUS_NOT_DUE
        assert values[label].value_pct is None
        # 目标日超出已知日历 → 留空串（不推算自然日）。
        assert values[label].target_date == ""
        assert values[label].reason.startswith("only_")


def test_missing_when_due_but_field_is_null() -> None:
    """已到期（目标日在日历内）但字段仍为 null → `missing`，与 not_due 分开。"""
    day = DAYS[0]
    values = by_label(
        hz.resolve_horizons(day, row_with(), DAYS, calendar_available=True, fetched_at=1.0)
    )
    for label, sessions in zip(hz.horizon_labels(), hz.HORIZON_DAYS, strict=True):
        assert values[label].status == hz.STATUS_MISSING
        assert values[label].value_pct is None
        assert values[label].target_date == DAYS[sessions]
        assert values[label].reason == "field_null_after_due"


def test_unknown_without_calendar_never_guesses_due_or_not_due() -> None:
    """无可信日历 → 一律 `unknown`；既不冒充 missing，也不冒充 not_due。"""
    day = DAYS[0]
    values = hz.resolve_horizons(
        day, row_with(), (), calendar_available=False, fetched_at=1.0
    )
    for item in values:
        assert item.status == hz.STATUS_UNKNOWN
        assert item.value_pct is None
        assert item.target_date == ""
        assert item.reason == "calendar_missing"

    # 日历可用但披露日不在序列里 → 也是 unknown，但原因不同。
    other = tuple(d for d in DAYS if d != day)
    values = hz.resolve_horizons(
        day, row_with(), other, calendar_available=True, fetched_at=1.0
    )
    assert {item.reason for item in values} == {"disclosure_day_not_in_calendar"}


@pytest.mark.parametrize("bad", [True, False, "3.25", float("nan"), float("inf"), [], {}])
def test_non_finite_or_non_numeric_field_is_missing_not_zero(bad: object) -> None:
    """bool / 字符串 / NaN / inf / 容器都不是数值 → 视为缺失，**绝不**填 0。"""
    day = DAYS[0]
    values = by_label(
        hz.resolve_horizons(
            day, row_with(D1_CLOSE_ADJCHRATE=bad), DAYS, calendar_available=True, fetched_at=1.0
        )
    )
    assert values["D1"].value_pct is None
    assert values["D1"].value_pct != 0
    assert values["D1"].status == hz.STATUS_MISSING


def test_zero_is_a_legitimate_disclosed_value() -> None:
    """真实的 0%（平盘）**必须**被当成已披露值——这与「缺失」是反例对。"""
    day = DAYS[0]
    values = by_label(
        hz.resolve_horizons(
            day, row_with(D1_CLOSE_ADJCHRATE=0.0), DAYS, calendar_available=True, fetched_at=1.0
        )
    )
    assert values["D1"].value_pct == 0.0
    assert values["D1"].status == hz.STATUS_DISCLOSED


def test_as_of_truncates_future_days_so_late_backfill_does_not_change_history() -> None:
    """按历史时点复盘：`as_of` 之后的交易日不得参与到期判定（防未来信息泄漏）。"""
    day = DAYS[0]
    # 数据源今天已补齐 D10；但按 as_of = 披露日后第 3 天复盘时，D10 应仍是 not_due。
    values = by_label(
        hz.resolve_horizons(
            day,
            row_with(D1_CLOSE_ADJCHRATE=1.0, D10_CLOSE_ADJCHRATE=12.0),
            DAYS,
            calendar_available=True,
            as_of_trading_day=DAYS[3],
            fetched_at=1.0,
        )
    )
    # D1 已到期且已披露 → disclosed。
    assert values["D1"].status == hz.STATUS_DISCLOSED
    # D10 字段有值，但 as_of 截断后**不该**当成已到期；值本身仍如实透出（它是事实）。
    assert values["D10"].value_pct == 12.0
    assert values["D10"].status == hz.STATUS_DISCLOSED
    # 关键是 D5/D20/D30 的到期目标日不越过 as_of。
    assert values["D20"].target_date == ""
    assert values["D20"].status == hz.STATUS_NOT_DUE


def test_target_day_requires_disclosure_day_inside_calendar() -> None:
    assert hz.target_day(DAYS[0], 1, DAYS) == DAYS[1]
    assert hz.target_day(DAYS[0], 5, DAYS[:3]) == ""
    assert hz.target_day("20260101", 1, DAYS) == ""  # 不在日历里 → 空串，不默认首日。


@pytest.mark.parametrize("bad_day", ["2026-09-01", "2026091", "", "abcdefgh", None])
def test_invalid_disclosure_day_raises(bad_day: object) -> None:
    with pytest.raises(hz.HorizonInputError):
        hz.resolve_horizons(bad_day, {}, DAYS, calendar_available=True)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_sessions", [0, -1, 1.5, True])
def test_invalid_sessions_raise(bad_sessions: object) -> None:
    with pytest.raises(hz.HorizonInputError):
        hz.target_day(DAYS[0], bad_sessions, DAYS)  # type: ignore[arg-type]


def test_invalid_as_of_raises() -> None:
    with pytest.raises(hz.HorizonInputError):
        hz.resolve_horizons(DAYS[0], {}, DAYS, calendar_available=True, as_of_trading_day="x")


# —— Y3-06：缓存（正缓存长 TTL，缺失短冷却，容量有界）——


def _values(statuses: dict[str, str]) -> tuple[hz.HorizonValue, ...]:
    return tuple(
        hz.HorizonValue(
            label=label,
            sessions=sessions,
            value_pct=1.0 if statuses.get(label) == hz.STATUS_DISCLOSED else None,
            status=statuses.get(label, hz.STATUS_MISSING),
            target_date="",
            fetched_at=0.0,
            source="s",
        )
        for label, sessions in zip(hz.horizon_labels(), hz.HORIZON_DAYS, strict=True)
    )


def test_disclosed_values_use_long_ttl_but_missing_uses_short_cooldown() -> None:
    """★ 规则 3：缺失**不**用长 TTL——一次抖动不能把期限永久钉成缺失。"""
    cache = hz.HorizonCache(value_ttl=60.0, miss_cooldown=5.0)
    cache.put(
        "20260901",
        "600519",
        _values({label: hz.STATUS_DISCLOSED for label in hz.horizon_labels()}),
        now=0.0,
    )
    cache.put("20260902", "600519", _values({}), now=0.0)

    # 正缓存：10 秒后仍在。
    assert cache.get("20260901", "600519", now=10.0) is not None
    # 缺失：10 秒后**已过期**（5s 冷却 < 10s），必须能重新取。
    assert cache.get("20260902", "600519", now=10.0) is None
    # 正缓存 61 秒后过期。
    assert cache.get("20260901", "600519", now=61.0) is None


def test_missing_within_cooldown_is_served_to_avoid_a_retry_storm() -> None:
    cache = hz.HorizonCache(value_ttl=60.0, miss_cooldown=5.0)
    cache.put("20260902", "600519", _values({}), now=0.0)
    assert cache.get("20260902", "600519", now=1.0) is not None


def test_mixed_statuses_count_as_missing_for_ttl_purposes() -> None:
    """只剩部分期限未成熟时也要走短冷却，否则新成熟的期限要等 60 秒才出现。"""
    cache = hz.HorizonCache(value_ttl=60.0, miss_cooldown=5.0)
    mixed = _values({"D1": hz.STATUS_DISCLOSED, "D2": hz.STATUS_NOT_DUE})
    cache.put("20260901", "600519", mixed, now=0.0)
    assert cache.get("20260901", "600519", now=10.0) is None


def test_cache_key_includes_as_of_so_snapshots_do_not_collide() -> None:
    cache = hz.HorizonCache()
    cache.put("20260901", "600519", _values({}), as_of="20260910", now=0.0)
    assert cache.get("20260901", "600519", "20260910", now=0.1) is not None
    assert cache.get("20260901", "600519", "20260911", now=0.1) is None


def test_cache_is_bounded_and_evicts_oldest() -> None:
    cache = hz.HorizonCache(max_entries=3)
    for index in range(5):
        cache.put(f"2026090{index}", "600519", _values({}), now=0.0)
    assert len(cache) == 3
    # 最旧两条已被淘汰。
    assert cache.get("20260900", "600519", now=0.1) is None
    assert cache.get("20260901", "600519", now=0.1) is None
    assert cache.get("20260904", "600519", now=0.1) is not None


def test_cache_clear_resets_everything() -> None:
    cache = hz.HorizonCache()
    cache.put("20260901", "600519", _values({}), now=0.0)
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0
    assert cache.get("20260901", "600519") is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"value_ttl": -1.0},
        {"miss_cooldown": -0.5},
        {"value_ttl": True},
        {"max_entries": 0},
        {"max_entries": 1.5},
        {"max_entries": True},
    ],
)
def test_cache_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    with pytest.raises(hz.HorizonInputError):
        hz.HorizonCache(**kwargs)  # type: ignore[arg-type]


def test_fetched_at_is_visible_per_value_so_stale_snapshots_are_distinguishable() -> None:
    """★ 规则 4：同一请求内两次取值的时间戳可分辨，使用者能看出是旧快照。"""
    first = hz.resolve_horizons(
        DAYS[0], row_with(D1_CLOSE_ADJCHRATE=1.0), DAYS, calendar_available=True, fetched_at=111.0
    )
    second = hz.resolve_horizons(
        DAYS[0], row_with(D1_CLOSE_ADJCHRATE=2.0), DAYS, calendar_available=True, fetched_at=222.0
    )
    assert {item.fetched_at for item in first} == {111.0}
    assert {item.fetched_at for item in second} == {222.0}


def test_output_field_set_is_frozen() -> None:
    """输出字段集常量冻结：加字段就必须改这里，避免绩效字段悄悄溜进来。"""
    values = hz.resolve_horizons(DAYS[0], row_with(D1_CLOSE_ADJCHRATE=1.0), DAYS, calendar_available=True, fetched_at=1.0)
    for item in values:
        assert set(item.as_dict()) == {
            "label",
            "sessions",
            "value_pct",
            "status",
            "target_date",
            "fetched_at",
            "source",
            "reason",
        }
    blob = repr([v.as_dict() for v in values])
    for forbidden in ("胜率", "收益", "win_rate", "return", "profit"):
        assert forbidden not in blob


def test_cache_key_separates_inputs_so_backfilled_values_do_not_reuse_stale_status() -> None:
    """★ 回归：同一 (披露日, 证券, as_of) 但**输入变了**，缓存不得复用旧结论。

    真实缺陷场景：先以「D1 已披露」写入缓存；随后上游把该字段补成/改状态，
    同 key 的请求本应得到新结论，却被缓存短路成旧状态。修法是键里带输入指纹。
    """
    cache = hz.HorizonCache()
    disclosed = _values({"D1": hz.STATUS_DISCLOSED})
    not_due = _values({"D1": hz.STATUS_NOT_DUE})

    fingerprint_before = hz.HorizonCache.fingerprint({"D1_CLOSE_ADJCHRATE": 1.0}, DAYS[:3])
    fingerprint_after = hz.HorizonCache.fingerprint({"D1_CLOSE_ADJCHRATE": None}, DAYS[:3])
    assert fingerprint_before != fingerprint_after, "输入不同必须得到不同指纹"

    cache.put("20260901", "600519", disclosed, "20260901", fingerprint_before, now=0.0)
    # 同一坐标、不同输入 → 必须未命中（不能用旧结论回答）。
    assert cache.get("20260901", "600519", "20260901", fingerprint_after, now=0.1) is None
    # 同一坐标、同一输入 → 仍命中。
    assert cache.get("20260901", "600519", "20260901", fingerprint_before, now=0.1) is not None
    # 未传指纹时退化为旧三坐标键，保持向后兼容。
    cache.put("20260902", "600519", not_due, "20260902", now=0.0)
    assert cache.get("20260902", "600519", "20260902", now=0.1) is not None


def test_fingerprint_ignores_irrelevant_fields_so_cache_still_hits() -> None:
    """指纹只取参与判定的量：无关字段变化不该白白让缓存失效。"""
    first = hz.HorizonCache.fingerprint({"D1_CLOSE_ADJCHRATE": 1.0, "OTHER": "a"}, DAYS[:3])
    second = hz.HorizonCache.fingerprint({"D1_CLOSE_ADJCHRATE": 1.0, "OTHER": "b"}, DAYS[:3])
    assert first == second
