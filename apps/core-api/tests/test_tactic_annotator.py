"""tactic_annotator 纯函数合成测试（youzi-radar 二期 Y2-01..06，全部离线）。

所有算法案例提供五个显式合成交易日；旧版短案例使用下方标明的合成前缀。
周末/长假跳空仅用于检验显式序列邻接，绝不声明真实交易所日历。
窗口门禁回归直接调用 _raw_annotate，不经补齐 helper。
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, timedelta

import pytest
from investment_steward_core.tactic_annotator import (
    BOUNDARY_NOTICE,
    COVERAGE_COMPLETE,
    COVERAGE_NOT_PUBLISHED,
    COVERAGE_UNKNOWN,
    FACT_ADJACENT_BUY_SELL,
    FACT_CONSECUTIVE_BUY,
    FACT_REPEATED_PRESENCE,
    IDENTITY_BRANCH,
    IDENTITY_BUCKET,
    PERIOD_MULTI_DAY,
    PERIOD_SINGLE_DAY,
    PERIOD_UNKNOWN,
    AnnotatorInputError,
    NormalizedSeatRecord,
    classify_period_from_explanation,
    evidence_id_of,
)
from investment_steward_core.tactic_annotator import (
    annotate as _raw_annotate,
)

_CODE = "600519"


# SYNTHETIC ONLY: explicit empty, complete prefix days for legacy 1–4-day
# algorithm examples. These fixtures are not a production exchange calendar.
_SYNTHETIC_PREFIX = ("20260908", "20260909", "20260910", "20260911")


def annotate(security_code, records, trading_days, coverage, *, as_of=None):
    """Legacy algorithm helper; gate regressions MUST call _raw_annotate instead."""
    effective = sorted({day for day in trading_days if as_of is None or day <= as_of})
    # Leave malformed/empty input untouched so existing validation tests stay raw.
    valid = effective and all(len(day) == 8 and day.isdigit() for day in effective)
    prefix = []
    if valid and len(effective) < 5 and effective[0] > _SYNTHETIC_PREFIX[-1]:
        prefix = list(_SYNTHETIC_PREFIX[-(5 - len(effective)) :])
    return _raw_annotate(
        security_code,
        records,
        prefix + list(trading_days),
        {**dict.fromkeys(prefix, COVERAGE_COMPLETE), **coverage},
        as_of=as_of,
    )


def _trading_days(start: str, count: int) -> list[str]:
    day = date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    out: list[str] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day += timedelta(days=1)
    return out


def _record(
    trading_day: str,
    direction: str = "buy",
    amount: float = 1000.0,
    operatedept_code: str = "80000001",
    period: str = PERIOD_SINGLE_DAY,
    identity_kind: str = IDENTITY_BRANCH,
    explanation: str = "日涨幅偏离值达7%的证券",
    security_code: str = _CODE,
) -> NormalizedSeatRecord:
    return NormalizedSeatRecord(
        trading_day=trading_day,
        security_code=security_code,
        operatedept_code=operatedept_code,
        operatedept_name="测试证券营业部",
        direction=direction,
        amount=amount,
        period=period,
        identity_kind=identity_kind,
        explanation=explanation,
        source_report=(
            "RPT_BILLBOARD_DAILYDETAILSSELL"
            if direction == "sell"
            else "RPT_BILLBOARD_DAILYDETAILSBUY"
        ),
        source_record_id=f"{trading_day}-{operatedept_code}-{direction}",
    )


def test_excludes_bucket_and_multi_day_and_bad_amount() -> None:
    d1, d2 = _trading_days("20260914", 2)
    records = [
        _record(d1, amount=1000.0, operatedept_code="A"),
        _record(d2, amount=2000.0, operatedept_code="A", identity_kind=IDENTITY_BUCKET),
        _record(d2, amount=3000.0, operatedept_code="A", period=PERIOD_MULTI_DAY),
        _record(d2, amount=4000.0, operatedept_code="A", period=PERIOD_UNKNOWN),
        _record(d2, amount=-5.0, operatedept_code="A"),
        _record(d2, amount=0, operatedept_code="A"),
        _record(d2, amount=None, operatedept_code="A"),
    ]
    result = annotate(_CODE, records, [d1, d2], {d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE})
    assert result.facts == ()
    assert result.window_complete is True
    excluded = dict(result.excluded)
    assert excluded["identity_bucket"] == 1
    assert excluded["period_multi_day"] == 1
    assert excluded["period_unknown"] == 1
    assert excluded["amount_invalid"] == 3
    assert "day_out_of_window" not in excluded


def test_other_security_records_ignored_silently() -> None:
    d1, d2 = _trading_days("20260914", 2)
    records = [_record(d1, security_code="000001"), _record(d2, security_code="000001")]
    result = annotate(_CODE, records, [d1, d2], {d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE})
    assert result.facts == ()
    assert result.excluded == ()


# ---------------------------------------------------------------- Y2-03 相邻买卖


def test_adjacent_buy_then_next_day_sell() -> None:
    d1, d2 = _trading_days("20260914", 2)
    records = [_record(d1, "buy", 1000.0), _record(d2, "sell", 800.0)]
    result = annotate(_CODE, records, [d1, d2], {d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE})
    adjacent = [f for f in result.facts if f.fact_type == FACT_ADJACENT_BUY_SELL]
    assert len(adjacent) == 1
    assert adjacent[0].days == (d1, d2)
    assert adjacent[0].evidence[0]["amount"] == 1000.0
    assert adjacent[0].evidence[0]["direction"] == "buy"
    assert adjacent[0].evidence[1]["amount"] == 800.0
    assert adjacent[0].evidence[1]["direction"] == "sell"
    assert result.conflicts == ()


def test_adjacent_sell_then_buy_is_not_tagged() -> None:
    d1, d2 = _trading_days("20260914", 2)
    records = [_record(d1, "sell", 800.0), _record(d2, "buy", 1000.0)]
    result = annotate(_CODE, records, [d1, d2], {d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE})
    assert all(f.fact_type != FACT_ADJACENT_BUY_SELL for f in result.facts)
    presence = [f for f in result.facts if f.fact_type == FACT_REPEATED_PRESENCE]
    assert len(presence) == 1


def test_gap_day_breaks_adjacency() -> None:
    d1, d2 = _trading_days("20260914", 2)
    records = [_record(d1, "buy", 1000.0), _record(d2, "sell", 800.0)]
    result = annotate(
        _CODE,
        records,
        [d1, d2],
        {d1: COVERAGE_COMPLETE, d2: COVERAGE_NOT_PUBLISHED},
    )
    assert result.window_complete is False
    assert "not_published" in result.incomplete_reason
    assert result.facts == ()


# ---------------------------------------------------------------- Y2-04 连续买榜


def test_consecutive_buy_maximal_run_only() -> None:
    d1, d2, d3 = _trading_days("20260914", 3)
    records = [
        _record(d1, "buy", 100.0, "A"),
        _record(d2, "buy", 200.0, "A"),
        _record(d3, "buy", 300.0, "A"),
    ]
    result = annotate(_CODE, records, [d1, d2, d3], {d: COVERAGE_COMPLETE for d in (d1, d2, d3)})
    runs = [f for f in result.facts if f.fact_type == FACT_CONSECUTIVE_BUY]
    assert len(runs) == 1
    assert runs[0].days == (d1, d2, d3)
    assert [e["amount"] for e in runs[0].evidence] == [100.0, 200.0, 300.0]


def test_consecutive_buy_non_adjacent_window_days_not_a_run() -> None:
    d1, d2 = _trading_days("20260914", 2)
    d4 = "20260918"  # d2 之后跳过 0916/0917（合成跳空）
    records = [_record(d1, "buy", 100.0), _record(d4, "buy", 200.0)]
    result = annotate(_CODE, records, [d1, d2, d4], {d: COVERAGE_COMPLETE for d in (d1, d2, d4)})
    runs = [f for f in result.facts if f.fact_type == FACT_CONSECUTIVE_BUY]
    assert runs == []
    presence = [f for f in result.facts if f.fact_type == FACT_REPEATED_PRESENCE]
    assert len(presence) == 1
    assert presence[0].days == (d1, d4)


def test_gap_in_window_breaks_consecutive_run() -> None:
    # 显式提供未上榜的交易日；算法不能凭自然日缺口猜测交易所休市。
    d1, d2, d3, d4 = _trading_days("20260914", 4)
    records = [_record(d1, "buy", 100.0), _record(d2, "buy", 200.0), _record(d4, "buy", 300.0)]
    result = annotate(
        _CODE, records, [d1, d2, d3, d4], {d: COVERAGE_COMPLETE for d in (d1, d2, d3, d4)}
    )
    runs = [f for f in result.facts if f.fact_type == FACT_CONSECUTIVE_BUY]
    assert len(runs) == 1
    assert runs[0].days == (d1, d2)


# ---------------------------------------------------------------- Y2-05 反复现身


def test_repeated_presence_same_day_multi_reason() -> None:
    d1, d2 = _trading_days("20260914", 2)
    records = [
        _record(d1, "buy", 1000.0, explanation="日涨幅偏离值达7%的证券"),
        _record(d1, "buy", 1000.0, explanation="换手率达20%的证券"),
        _record(d2, "sell", 500.0),
    ]
    result = annotate(_CODE, records, [d1, d2], {d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE})
    presence = [f for f in result.facts if f.fact_type == FACT_REPEATED_PRESENCE]
    assert len(presence) == 1
    assert presence[0].days == (d1, d2)
    evidence_ids = {e["evidence_id"] for e in presence[0].evidence}
    assert len(evidence_ids) == 3
    assert any(f.fact_type == FACT_ADJACENT_BUY_SELL for f in result.facts)
    assert result == annotate(
        _CODE, list(reversed(records)), [d1, d2], {d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE}
    )


def test_single_day_only_no_presence_fact() -> None:
    d1 = _trading_days("20260914", 1)[0]
    records = [_record(d1, "buy", 1000.0)]
    result = annotate(_CODE, records, [d1], {d1: COVERAGE_COMPLETE})
    assert result.facts == ()


# ---------------------------------------------------------------- 冲突与确定性


def test_conflicting_amounts_day_direction_produce_conflict_and_no_fact() -> None:
    d1, d2 = _trading_days("20260914", 2)
    records = [
        _record(d1, "buy", 1000.0, explanation="日涨幅偏离值达7%的证券"),
        _record(d1, "buy", 1200.0, explanation="换手率达20%的证券"),
        _record(d2, "sell", 500.0),
    ]
    result = annotate(_CODE, records, [d1, d2], {d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE})
    assert all(f.fact_type != FACT_ADJACENT_BUY_SELL for f in result.facts)
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict["operatedept_code"] == "80000001"
    assert conflict["amounts"] == [1000.0, 1200.0]
    # 金额一致的多原因重复不算冲突
    same = [
        _record(d1, "buy", 1000.0, explanation="日涨幅偏离值达7%的证券"),
        _record(d1, "buy", 1000.0, explanation="换手率达20%的证券"),
    ]
    result2 = annotate(_CODE, same, [d1, d2], {d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE})
    assert result2.conflicts == ()


# ---------------------------------------------------------------- 确定性与边界


def test_deterministic_and_order_insensitive() -> None:
    d1, d2, d3 = _trading_days("20260914", 3)
    records = [
        _record(d1, "buy", 100.0),
        _record(d2, "buy", 200.0),
        _record(d3, "sell", 300.0),
    ]
    r1 = annotate(_CODE, records, [d1, d2, d3], {d: COVERAGE_COMPLETE for d in (d1, d2, d3)})
    r2 = annotate(
        _CODE,
        list(reversed(records)),
        [d3, d1, d2],
        {d3: COVERAGE_COMPLETE, d1: COVERAGE_COMPLETE, d2: COVERAGE_COMPLETE},
    )
    assert r1 == r2
    assert r1.facts  # 有可复核事实


def test_as_of_truncates_future_days_and_records() -> None:
    # Five explicit synthetic days at as_of; future input cannot alter any field.
    days = ["20260914", "20260915", "20260916", "20260917", "20260918"]
    future = "20260921"
    records = [_record(days[-2], amount=100.0), _record(days[-1], amount=200.0)]
    baseline = _raw_annotate(
        _CODE, records, days, dict.fromkeys(days, COVERAGE_COMPLETE), as_of=days[-1]
    )
    truncated = _raw_annotate(
        _CODE,
        records + [_record(future, "sell", 300.0)],
        days + [future],
        {**dict.fromkeys(days, COVERAGE_COMPLETE), future: COVERAGE_UNKNOWN},
        as_of=days[-1],
    )
    assert truncated.window_days == tuple(days)
    assert truncated.window_complete
    assert truncated.facts
    assert truncated == baseline


def test_empty_day_list_is_incomplete_with_reason() -> None:
    result = annotate(_CODE, [], [], {})
    assert result.facts == ()
    assert result.window_complete is False
    assert "交易日序列为空" in result.incomplete_reason


def test_partial_coverage_blocks_facts() -> None:
    d1, d2 = _trading_days("20260914", 2)
    records = [_record(d1, "buy", 1000.0), _record(d2, "sell", 800.0)]
    for state in (COVERAGE_NOT_PUBLISHED, COVERAGE_UNKNOWN):
        result = annotate(_CODE, records, [d1, d2], {d1: COVERAGE_COMPLETE, d2: state})
        assert result.window_complete is False
        assert result.facts == ()
        assert state in result.incomplete_reason


def test_invalid_inputs_raise() -> None:
    d1 = _trading_days("20260914", 1)[0]
    with pytest.raises(AnnotatorInputError):
        annotate(_CODE, [], ["2026-09-14"], {})
    with pytest.raises(AnnotatorInputError):
        annotate(_CODE, [], [d1], {d1: "published"})
    with pytest.raises(AnnotatorInputError):
        annotate(_CODE, [], [d1], {}, as_of="2026/09/14")
    with pytest.raises(AnnotatorInputError):
        annotate("", [], [], {})


# ---------------------------------------------------------------- 稳定 ID 与边界文案


def test_evidence_id_stable_and_distinct() -> None:
    d1 = _trading_days("20260914", 1)[0]
    a = _record(d1, "buy", 1000.0, explanation="日涨幅偏离值达7%的证券")
    b = _record(d1, "buy", 1000.0, explanation="换手率达20%的证券")
    assert evidence_id_of(a) == evidence_id_of(a)
    assert evidence_id_of(a) != evidence_id_of(b)


def test_boundary_notice_constant() -> None:
    assert BOUNDARY_NOTICE == (
        "榜外席位不可见，未检出未发生；同一营业部不等于同一账户，"
        "无法据此确认持仓、清仓或同一笔交易。"
    )


def test_classify_period_conservative() -> None:
    assert (
        classify_period_from_explanation("连续三个交易日内日收盘价格涨跌幅偏离值累计达20%")
        == PERIOD_MULTI_DAY
    )
    assert classify_period_from_explanation("日涨幅偏离值达7%的证券") == PERIOD_SINGLE_DAY
    assert classify_period_from_explanation("") == PERIOD_UNKNOWN
    # 未知措辞必须留在 unknown，不能被当成已核验单日参与跨日事实。
    assert classify_period_from_explanation("某些全新措辞") == PERIOD_UNKNOWN
    # 「N 个交易日内」即使没有「连续/累计」也是多日跨度。
    assert classify_period_from_explanation("最近三个交易日内上榜") == PERIOD_MULTI_DAY


#: Y0-03/Y0-06 冻结金样本：2026-09-16 真实买/卖席位报告里出现的**全部 20 种**原因
#: 原文，以及人工核对后的预期周期。这些文本来自只读夹具，不是合成样例。
FROZEN_REASON_GOLD_SAMPLE: tuple[tuple[str, str], ...] = (
    ("当日换手率达到20%的前5只股票", PERIOD_SINGLE_DAY),
    ("日振幅值达到15%的前5只证券", PERIOD_SINGLE_DAY),
    ("日换手率达到20%的前5只证券", PERIOD_SINGLE_DAY),
    ("日换手率达到30%的前5只证券", PERIOD_SINGLE_DAY),
    ("日涨幅偏离值达到7%的前5只证券", PERIOD_SINGLE_DAY),
    ("日涨幅达到15%的前5只证券", PERIOD_SINGLE_DAY),
    ("日跌幅偏离值达到7%的前5只证券", PERIOD_SINGLE_DAY),
    ("有价格涨跌幅限制的日价格振幅达到15%的前五只证券", PERIOD_SINGLE_DAY),
    ("有价格涨跌幅限制的日换手率达到20%的前五只证券", PERIOD_SINGLE_DAY),
    ("有价格涨跌幅限制的日换手率达到30%的前五只证券", PERIOD_SINGLE_DAY),
    ("有价格涨跌幅限制的日收盘价格涨幅偏离值达到7%的前五只证券", PERIOD_SINGLE_DAY),
    ("有价格涨跌幅限制的日收盘价格涨幅达到15%的前五只证券", PERIOD_SINGLE_DAY),
    ("有价格涨跌幅限制的日收盘价格跌幅偏离值达到7%的前五只证券", PERIOD_SINGLE_DAY),
    ("连续3个交易日内日收盘价格涨幅偏离值达到30%的可转债", PERIOD_MULTI_DAY),
    ("连续三个交易日内，涨幅偏离值累计达到20%的证券", PERIOD_MULTI_DAY),
    ("连续三个交易日内，涨幅偏离值累计达到30%的证券", PERIOD_MULTI_DAY),
    ("连续三个交易日内，跌幅偏离值累计达到20%的证券", PERIOD_MULTI_DAY),
    ("非S证券连续三个交易日内收盘价格涨幅偏离值累计达到20%的证券", PERIOD_MULTI_DAY),
    ("非S证券连续三个交易日内收盘价格跌幅偏离值累计达到20%的证券", PERIOD_MULTI_DAY),
    ("非上市首日,当日收盘价涨幅达15%的前五只可转债", PERIOD_SINGLE_DAY),
)


def test_frozen_gold_sample_classification() -> None:
    """20 种冻结原因原文必须逐一得到预期周期（6 多日 / 14 单日）。"""
    actual = {text: classify_period_from_explanation(text) for text, _ in FROZEN_REASON_GOLD_SAMPLE}
    expected = dict(FROZEN_REASON_GOLD_SAMPLE)
    assert actual == expected
    assert sum(1 for value in actual.values() if value == PERIOD_MULTI_DAY) == 6
    assert sum(1 for value in actual.values() if value == PERIOD_SINGLE_DAY) == 14


def test_frozen_gold_sample_covers_every_real_fixture_reason() -> None:
    """冻结金样本必须覆盖真实夹具里出现的每一条原因文本（防止漏项静默变 unknown）。"""
    import json
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures" / "youzi"
    reasons: set[str] = set()
    for name in ("em_lhb_detailbuy_20260916.json", "em_lhb_detailsell_20260916.json"):
        payload = json.loads((fixtures / name).read_text(encoding="utf-8"))
        for row in payload["result"]["data"]:
            reasons.add(row["EXPLANATION"])
    assert reasons == set(dict(FROZEN_REASON_GOLD_SAMPLE)), (
        f"夹具与金样本不一致：夹具多出 {sorted(reasons - set(dict(FROZEN_REASON_GOLD_SAMPLE)))}，"
        f"金样本多出 {sorted(set(dict(FROZEN_REASON_GOLD_SAMPLE)) - reasons)}"
    )


def test_real_fixture_multi_day_and_single_day_counts() -> None:
    """真实 365 行明细中，多日累计 100 行、单日 265 行（两侧一致）。"""
    import json
    from collections import Counter
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures" / "youzi"
    for name in ("em_lhb_detailbuy_20260916.json", "em_lhb_detailsell_20260916.json"):
        payload = json.loads((fixtures / name).read_text(encoding="utf-8"))
        counts = Counter(
            classify_period_from_explanation(row["EXPLANATION"])
            for row in payload["result"]["data"]
        )
        assert counts[PERIOD_MULTI_DAY] == 100
        assert counts[PERIOD_SINGLE_DAY] == 265
        assert counts[PERIOD_UNKNOWN] == 0


@pytest.mark.parametrize(
    "days",
    [
        ["20260915", "20260916", "20260917", "20260918", "20260921"],  # 合成周末
        ["20260928", "20260929", "20260930", "20261009", "20261012"],  # 合成长假
    ],
)
def test_explicit_calendar_adjacency_across_closures(days: list[str]) -> None:
    # Select the synthetic closure-spanning adjacent pair, not natural-day adjacency.
    pair = days[-2:] if days[0] == "20260915" else days[2:4]
    rows = [_record(pair[0]), _record(pair[1], "sell")]
    result = _raw_annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE))
    assert result.window_complete
    adjacent = [f for f in result.facts if f.fact_type == FACT_ADJACENT_BUY_SELL]
    assert len(adjacent) == 1
    assert adjacent[0].days == tuple(pair)


def test_five_day_window_does_not_match_older_presence() -> None:
    days = _trading_days("20260914", 6)
    rows = [_record(days[0]), _record(days[-1], "sell")]
    result = annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE))
    assert result.window_days == tuple(days[-5:])
    assert result.facts == ()


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -float("inf"), True])
def test_nonfinite_and_bool_amounts_are_excluded(amount: float) -> None:
    days = _trading_days("20260914", 2)
    result = annotate(
        _CODE,
        [_record(days[0], amount=amount), _record(days[1])],
        days,
        dict.fromkeys(days, COVERAGE_COMPLETE),
    )
    assert not result.facts
    assert dict(result.excluded)["amount_invalid"] == 1


@pytest.mark.parametrize("seat_code", ["", "0", "  "])
def test_unreliable_codes_cannot_match_even_if_branch_label(seat_code: str) -> None:
    days = _trading_days("20260914", 2)
    rows = [_record(day, operatedept_code=seat_code) for day in days]
    result = annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE))
    assert not result.facts
    assert dict(result.excluded)["identity_unknown"] == 2


@pytest.mark.parametrize("state", ["fetch_failed", "truncated"])
def test_failed_or_truncated_coverage_is_not_no_match(state: str) -> None:
    days = _trading_days("20260914", 2)
    result = annotate(
        _CODE, [_record(day) for day in days], days, {days[0]: COVERAGE_COMPLETE, days[1]: state}
    )
    assert not result.window_complete
    assert not result.facts
    assert state in result.incomplete_reason


@pytest.mark.parametrize("day", ["20260230", "20261301", "00000000"])
def test_impossible_dates_rejected(day: str) -> None:
    with pytest.raises(AnnotatorInputError):
        annotate(_CODE, [], [day], {})
    with pytest.raises(AnnotatorInputError):
        annotate(_CODE, [], [], {}, as_of=day)


def test_duplicate_records_do_not_change_facts() -> None:
    days = _trading_days("20260914", 2)
    rows = [_record(day) for day in days]
    coverage = dict.fromkeys(days, COVERAGE_COMPLETE)
    original = annotate(_CODE, rows, days, coverage)
    assert original == annotate(_CODE, rows + rows, days, coverage)
    assert all(f.operatedept_name == "测试证券营业部" for f in original.facts)


def test_conflicts_order_invariant_and_no_rounding_hides_difference() -> None:
    days = _trading_days("20260914", 2)
    rows = [
        _record(days[0], amount=1000.001),
        _record(days[0], amount=1000.002),
        _record(days[1], "sell"),
    ]
    coverage = dict.fromkeys(days, COVERAGE_COMPLETE)
    result = annotate(_CODE, rows, days, coverage)
    assert len(result.conflicts) == 1
    assert dict(result.excluded)["amount_conflict_groups"] == 1
    assert result == annotate(_CODE, list(reversed(rows)), days, coverage)
    assert not result.facts


# ---------------------------------------------------------------- Raw five-day gates and regressions


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_one_to_four_complete_days_are_incomplete(count: int) -> None:
    days = ["20260914", "20260915", "20260916", "20260917"][:count]
    result = _raw_annotate(
        _CODE, [_record(day) for day in days], days, dict.fromkeys(days, COVERAGE_COMPLETE)
    )
    assert result.window_days == tuple(days)
    assert not result.window_complete
    assert "五交易日上下文不足" in result.incomplete_reason
    assert result.facts == ()
    assert result.conflicts == ()


def test_short_window_still_validates_coverage_before_length_gate() -> None:
    days = ["20260914", "20260915"]
    with pytest.raises(AnnotatorInputError):
        _raw_annotate(_CODE, [], days, {days[0]: "published"})
    incomplete = _raw_annotate(
        _CODE, [], days, {days[0]: COVERAGE_COMPLETE, days[1]: COVERAGE_NOT_PUBLISHED}
    )
    assert not incomplete.window_complete
    assert COVERAGE_NOT_PUBLISHED in incomplete.incomplete_reason


def test_as_of_cannot_borrow_future_days_to_complete_window() -> None:
    days = ["20260914", "20260915", "20260916", "20260917", "20260918"]
    rows = [_record(day) for day in days]
    baseline = _raw_annotate(
        _CODE, rows[:4], days[:4], dict.fromkeys(days[:4], COVERAGE_COMPLETE), as_of=days[3]
    )
    future = _raw_annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE), as_of=days[3])
    assert not baseline.window_complete
    assert baseline.facts == ()
    assert baseline == future


def test_first_day_sell_conflict_without_cross_day_presence() -> None:
    days = ["20260914", "20260915", "20260916", "20260917", "20260918"]
    rows = [
        _record(days[0], "buy", 100),
        _record(days[0], "sell", 100),
        _record(days[0], "sell", 200),
    ]
    result = _raw_annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE))
    assert result.window_complete
    assert result.facts == ()
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict["trading_day"] == days[0]
    assert conflict["direction"] == "sell"
    assert conflict["amounts"] == [100.0, 200.0]
    assert conflict["evidence_ids"] == sorted(evidence_id_of(row) for row in rows[1:])
    assert dict(result.excluded)["amount_conflict_groups"] == 1
    assert result == _raw_annotate(
        _CODE, list(reversed(rows)), days, dict.fromkeys(days, COVERAGE_COMPLETE)
    )


@pytest.mark.parametrize("invalid_amount", ["not-a-number", "1", True])
def test_excluded_amount_name_cannot_contaminate_valid_amount_one_facts(invalid_amount) -> None:
    days = ["20260914", "20260915", "20260916", "20260917", "20260918"]
    rows = [_record(days[0], amount=1), _record(days[1], amount=1)]
    excluded = replace(rows[0], amount=invalid_amount, operatedept_name="无效证据的不同名称")
    coverage = dict.fromkeys(days, COVERAGE_COMPLETE)
    baseline = _raw_annotate(_CODE, rows, days, coverage)
    result = _raw_annotate(_CODE, [excluded, *rows], days, coverage)
    assert baseline.facts
    assert result.facts == baseline.facts
    assert all(f.operatedept_name == "测试证券营业部" for f in result.facts)
    assert dict(result.excluded) == {"amount_invalid": 1}
    assert replace(result, excluded=()) == baseline
    assert result == _raw_annotate(_CODE, [*rows, excluded], days, coverage)


def test_output_contains_disclosure_fields_only_not_performance() -> None:
    days = ["20260914", "20260915", "20260916", "20260917", "20260918"]
    rows = [_record(days[0]), _record(days[1]), _record(days[2], "sell")]
    result = _raw_annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE))
    payload = asdict(result)
    assert {fact.fact_type for fact in result.facts} == {
        FACT_ADJACENT_BUY_SELL,
        FACT_CONSECUTIVE_BUY,
        FACT_REPEATED_PRESENCE,
    }
    # Explicit allowlists reject any added price/return/win-rate/performance fields.
    assert set(payload) == {
        "security_code",
        "window_days",
        "coverage",
        "window_complete",
        "incomplete_reason",
        "facts",
        "conflicts",
        "excluded",
    }
    for fact in payload["facts"]:
        assert set(fact) == {
            "fact_type",
            "security_code",
            "operatedept_code",
            "operatedept_name",
            "days",
            "evidence",
            "fact_id",
        }
        for evidence in fact["evidence"]:
            assert set(evidence) == {
                "trading_day",
                "direction",
                "amount",
                "evidence_id",
                "explanation",
            }


def test_synthetic_record_uses_direction_specific_source_report() -> None:
    assert _record("20260914", "buy").source_report == "RPT_BILLBOARD_DAILYDETAILSBUY"
    assert _record("20260914", "sell").source_report == "RPT_BILLBOARD_DAILYDETAILSSELL"


# —— Y2-11 正反例矩阵补齐（未上榜交易日 / 多周期重叠 / 缺页）——


def test_no_seat_row_on_a_window_day_is_not_a_negative_claim() -> None:
    """某窗口日**完全没有席位行**（未上榜）→ 不得据此生成任何事实。

    这是「未检出」与「未发生」的分界：五日内只有一天有记录时，
    既不能凑出连续买榜，也不能凑出重复出现；而注释里必须保留该日，
    使调用方能区分「该日无记录」与「该日不在窗口内」。
    """
    days = _trading_days("20260914", 5)
    rows = [_record(days[0])]  # 只有第一天有记录，其余四天无席位行。
    result = _raw_annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE))
    assert result.window_complete
    assert result.facts == (), "单日不足以构成任何跨日事实"
    assert result.window_days == tuple(days), "无记录的日子仍属窗口，不能从窗口里消失"


def test_adjacent_days_with_multi_day_row_do_not_pair() -> None:
    """多周期（累计）行**不拆成日流水**，因此不能参与相邻买卖配对。

    多日累计行的金额是区间口径，若拿来和相邻日的单日金额配对，等于把区间值
    与单日值并列展示——那是错配，不是事实。
    """
    days = _trading_days("20260914", 5)
    rows = [
        _record(
            days[1],
            amount=1e7,
            period=PERIOD_MULTI_DAY,
            explanation="连续三个交易日内涨跌幅累计达20%",
        ),
        _record(days[2], "sell", amount=8e6),  # 单日卖榜
    ]
    result = _raw_annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE))
    assert result.window_complete
    assert [f for f in result.facts if f.fact_type == FACT_ADJACENT_BUY_SELL] == []
    assert dict(result.excluded).get("period_multi_day") == 1


def test_mixed_periods_on_same_day_do_not_merge_amounts() -> None:
    """同日同席位同方向若同时有单日行与多日行，只有单日行合格，不合并金额。"""
    days = _trading_days("20260914", 5)
    rows = [
        _record(
            days[0], amount=1e7, period=PERIOD_SINGLE_DAY, explanation="日涨幅偏离值达7%的证券"
        ),
        _record(
            days[0], amount=9e7, period=PERIOD_MULTI_DAY, explanation="连续三个交易日累计涨幅达20%"
        ),
    ]
    result = _raw_annotate(_CODE, rows, days, dict.fromkeys(days, COVERAGE_COMPLETE))
    # 无跨日事实；关键是排除计数只算多日那一行，单日行不被牵连。
    assert result.facts == ()
    assert dict(result.excluded).get("period_multi_day") == 1


@pytest.mark.parametrize(
    "state", [COVERAGE_NOT_PUBLISHED, COVERAGE_UNKNOWN, "fetch_failed", "truncated"]
)
def test_missing_page_states_never_produce_a_no_match_claim(state: str) -> None:
    """缺页/未披露/无法判断 → 窗口不完整 → `facts=()` 且给出理由，绝不等于「未检出」。"""
    days = _trading_days("20260914", 5)
    coverage = dict.fromkeys(days, COVERAGE_COMPLETE)
    coverage[days[3]] = state
    result = _raw_annotate(_CODE, [_record(days[0])], days, coverage)
    assert result.window_complete is False
    assert result.facts == ()
    assert state in result.incomplete_reason
    assert result.excluded == (), "输入不完整时不产出排除摘要（避免读者把它当成完整结论）"
