"""Offline adapters: fixture arithmetic is not source-period verification."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from investment_steward_core import lhb_feed as feed
from investment_steward_core import tactic_annotator as annotator
from investment_steward_core import youzi_normalize as normalize

FIXTURES = Path(__file__).parent / "fixtures" / "youzi"
SINGLE = "single_day"
MULTI = "multi_day"
SEAT_KEYS = {
    "trading_day",
    "security_code",
    "operatedept_code",
    "operatedept_name",
    "direction",
    "amount",
    "period",
    "identity_kind",
    "explanation",
    "source_report",
    "source_record_id",
}
BILLBOARD_KEYS = {
    "source_report",
    "security_code",
    "trading_day",
    "source_explanation",
    "turnover_rate",
    "turnover_rate_unit",
    "free_market_cap",
    "free_market_cap_unit",
    "buy_amt_ratio_pct",
    "buy_amt_ratio_reason",
    "sell_amt_ratio_pct",
    "sell_amt_ratio_reason",
}
EVIDENCE_KEYS = {
    "source_report",
    "security_code",
    "trading_day",
    "operatedept_code",
    "direction",
    "source_explanation",
    "amount",
    "amount_reason",
    "source_record_id",
    "evidence_id",
}
FORBIDDEN = {
    "RISE_PROBABILITY_3DAY",
    "TOTAL_BUYER_SALESTIMES_3DAY",
    "BUY_RATIO",
    "SELL_RATIO",
    "CHANGE_RATE",
    "CLOSE_PRICE",
    "D1_CLOSE_ADJCHRATE",
    "D3_CLOSE_ADJCHRATE",
    "D5_CLOSE_ADJCHRATE",
    "D10_CLOSE_ADJCHRATE",
    "D20_CLOSE_ADJCHRATE",
    "D30_CLOSE_ADJCHRATE",
    "EXPLAIN",
    "explain_note",
    "change_rate",
    "close_price",
}


def payload(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def seats():
    return [
        row
        for direction, name in (
            (feed.DIRECTION_BUY, "em_lhb_detailbuy_20260916.json"),
            (feed.DIRECTION_SELL, "em_lhb_detailsell_20260916.json"),
        )
        for row in feed.parse_seat_rows(payload(name), direction)
    ]


@pytest.fixture(scope="module")
def billboard_payload():
    return payload("em_lhb_details_20260916.json")


@pytest.fixture(scope="module")
def billboard(billboard_payload):
    return feed.parse_billboard_rows(billboard_payload)[0]


@pytest.mark.parametrize(
    "direction,name,report",
    [
        (feed.DIRECTION_BUY, "em_lhb_detailbuy_20260916.json", feed.REPORT_SEAT_BUY),
        (feed.DIRECTION_SELL, "em_lhb_detailsell_20260916.json", feed.REPORT_SEAT_SELL),
    ],
)
def test_every_seat_uses_actual_direction_column(direction, name, report):
    raw = payload(name)
    rows = feed.parse_seat_rows(raw, direction)
    records = normalize.normalize_seat_rows(iter(rows))
    assert len(rows) == len(records) == len(raw["result"]["data"]) == 365
    for item, row, record in zip(raw["result"]["data"], rows, records, strict=True):
        assert row.buy == item["BUY"]
        assert row.sell == item["SELL"]
        assert record.amount == item[direction.upper()]
        assert record == normalize.normalize_seat_row(row)
        assert record.direction == direction
        assert record.source_report == report
        assert record.trading_day == "20260916"
        assert record.explanation == row.explanation
        assert record.period == annotator.classify_period_from_explanation(row.explanation)
        assert set(asdict(record)) == SEAT_KEYS
        assert not FORBIDDEN.intersection(asdict(record))
    if direction == feed.DIRECTION_BUY:
        assert rows[0].security_code == "000592"
        assert rows[0].operatedept_code == "10456753"
        assert rows[0].buy == 100941459.08
        assert rows[0].sell == 131368.0
        assert records[0].amount != rows[0].net


def test_fixture_identity_contract(seats):
    assert len(seats) == 730
    records = normalize.normalize_seat_rows(seats)
    buckets = Counter(r.operatedept_name for r in records if r.identity_kind == "bucket")
    assert {name: buckets[name] for name in ("机构专用", "沪股通专用", "深股通专用")} == {
        "机构专用": 146,
        "沪股通专用": 20,
        "深股通专用": 43,
    }
    headquarters = [r for r in records if r.operatedept_name.endswith("总部")]
    assert headquarters
    assert all(r.identity_kind == "bucket" for r in headquarters)
    branches = [r for r in records if r.identity_kind == "branch"]
    assert branches
    assert all(
        r.operatedept_code.isascii()
        and r.operatedept_code.isdecimal()
        and r.operatedept_name.endswith(("营业部", "分公司"))
        for r in branches
    )


@pytest.mark.parametrize(
    "code,name,kind",
    [
        ("100123", "测试证券营业部", "branch"),
        ("100123", "测试证券分公司", "branch"),
        ("100123", "测试总部", "bucket"),
        ("100123", "机构专用", "bucket"),
        ("0", "测试营业部", "bucket"),
        ("", "测试营业部", "bucket"),
        ("ABC", "测试营业部", "unknown"),
        ("１２３", "测试营业部", "unknown"),
        ("100123", "无法确认的渠道", "unknown"),
        ("000", "测试营业部", "unknown"),
    ],
)
def test_identity_cases(seats, code, name, kind):
    row = replace(seats[0], operatedept_code=code, operatedept_name=name)
    assert normalize.normalize_seat_row(row).identity_kind == kind


def test_identity_ids_are_stable_for_duplicates_and_reordering(seats):
    first = seats[0]
    second = replace(first, explanation=first.explanation + "；另一上榜原因")
    assert first.identity_key == second.identity_key
    a, b = normalize.normalize_seat_rows([first, second])
    assert a.source_record_id != b.source_record_id
    assert annotator.evidence_id_of(a) != annotator.evidence_id_of(b)
    assert normalize.normalize_seat_rows([second, first]) == [b, a]
    assert normalize.normalize_seat_rows([first, first, second, first]) == [a, a, b, a]
    assert normalize.normalize_seat_rows(iter([first, second])) == [a, b]
    # Compatibility argument must never become an occurrence-dependent identity.
    assert normalize.normalize_seat_row(first, group_index=9) == a
    assert annotator.evidence_id_of(
        normalize.normalize_seat_row(first)
    ) == annotator.evidence_id_of(a)


@pytest.mark.parametrize("amount", [None, 0.0, -1.0, float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("direction", ["buy", "sell"])
def test_invalid_seat_amount_survives(seats, amount, direction):
    row = replace(seats[0], direction=direction, **{direction: amount})
    record = normalize.normalize_seat_row(row)
    if isinstance(amount, float) and math.isnan(amount):
        assert math.isnan(record.amount)
    else:
        assert record.amount == amount
    assert set(asdict(record)) == SEAT_KEYS


@pytest.mark.parametrize(
    "numerator,denominator,np,dp,reason",
    [
        (1, None, SINGLE, SINGLE, "denominator_missing"),
        *[
            (1, n, SINGLE, SINGLE, "denominator_non_finite")
            for n in (float("nan"), float("inf"), -float("inf"))
        ],
        *[(1, n, SINGLE, SINGLE, "denominator_non_positive") for n in (0, -1)],
        (1, 100, None, SINGLE, "period_unknown"),
        (1, 100, SINGLE, None, "period_unknown"),
        (1, 100, "unknown", SINGLE, "period_unknown"),
        (1, 100, SINGLE, "unknown", "period_unknown"),
        (1, 100, MULTI, SINGLE, "period_mismatch"),
        (1, 100, SINGLE, MULTI, "period_mismatch"),
        (None, 100, SINGLE, SINGLE, "numerator_missing"),
        *[
            (n, 100, SINGLE, SINGLE, "numerator_non_finite")
            for n in (float("nan"), float("inf"), -float("inf"))
        ],
    ],
)
def test_ratio_reasons(numerator, denominator, np, dp, reason):
    assert normalize.amount_ratio_pct(
        numerator, denominator, numerator_period=np, denominator_period=dp
    ) == (None, reason)


def test_zero_is_a_real_ratio():
    assert normalize.amount_ratio_pct(
        0, 100, numerator_period=SINGLE, denominator_period=SINGLE
    ) == (0.0, None)


@pytest.mark.parametrize(
    "ni,di,expected",
    [
        (None, None, (None, "interval_unknown")),
        (("20260914", "20260916"), None, (None, "interval_unknown")),
        (("20260914", "20260916"), ("20260915", "20260916"), (None, "interval_mismatch")),
        (("20260914", "20260916"), ("20260914", "20260916"), (25.0, None)),
    ],
)
def test_multi_day_requires_verified_matching_intervals(ni, di, expected):
    assert (
        normalize.amount_ratio_pct(
            25,
            100,
            numerator_period=MULTI,
            denominator_period=MULTI,
            numerator_interval=ni,
            denominator_interval=di,
        )
        == expected
    )


@pytest.mark.parametrize(
    "helper,field,seat_field",
    [
        (normalize.buy_amt_ratio_pct, "billboard_buy_amt", "buy"),
        (normalize.sell_amt_ratio_pct, "billboard_sell_amt", "sell"),
    ],
)
def test_ratio_helpers(billboard, seats, helper, field, seat_field):
    value, reason = helper(billboard, numerator_period=SINGLE, denominator_period=SINGLE)
    assert value == 100 * (getattr(billboard, field) / billboard.accum_amount)
    assert reason is None
    assert helper(billboard) == (None, "period_unknown")
    assert helper(billboard, denominator_period=SINGLE) == (None, "period_unknown")
    with pytest.raises(ValueError, match="ACCUM_AMOUNT"):
        helper(billboard, denominator=100, numerator_period=SINGLE, denominator_period=SINGLE)
    assert helper(
        seats[0], denominator=None, numerator_period=SINGLE, denominator_period=SINGLE
    ) == (None, "denominator_missing")
    value, reason = helper(
        seats[0], denominator=100, numerator_period=SINGLE, denominator_period=SINGLE
    )
    assert value == 100 * (getattr(seats[0], seat_field) / 100)
    assert reason is None
    interval = ("20260914", "20260916")
    assert (
        helper(
            billboard,
            numerator_period=MULTI,
            denominator_period=MULTI,
            numerator_interval=interval,
            denominator_interval=interval,
        )[1]
        is None
    )


@pytest.mark.parametrize(
    "difference,passed,reason",
    [
        (0.0, True, None),
        (1e-6, True, None),
        (1e-5, False, "total_mismatch"),
    ],
)
def test_cross_check_tolerance_boundary(difference, passed, reason):
    # Use zero base so subtraction does not perturb the exact boundary float.
    check = normalize.cross_check_ratios(0.0, 0.0, difference)
    assert check.passed is passed
    assert check.reason == reason
    assert check.difference_pct == difference
    assert check.tolerance_pct == 1e-6
    assert set(asdict(check)) == {"passed", "difference_pct", "reason", "tolerance_pct"}


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize(
    "bad,reason",
    [
        (None, "percentage_missing"),
        (float("nan"), "percentage_non_finite"),
        (float("inf"), "percentage_non_finite"),
    ],
)
def test_cross_check_unavailable(index, bad, reason):
    values = [10.0, 20.0, 30.0]
    values[index] = bad
    check = normalize.cross_check_ratios(*values)
    assert check.passed is None
    assert check.difference_pct is None
    assert check.reason == reason


def test_all_billboard_fixture_totals_arithmetic_only(billboard_payload):
    rows = feed.parse_billboard_rows(billboard_payload)
    raw = billboard_payload["result"]["data"]
    assert len(rows) == len(raw) == 71
    differences = []
    for row, item in zip(rows, raw, strict=True):
        # Explicit equal periods test arithmetic ONLY, not verified fixture intervals.
        buy, br = normalize.buy_amt_ratio_pct(
            row, numerator_period=SINGLE, denominator_period=SINGLE
        )
        sell, sr = normalize.sell_amt_ratio_pct(
            row, numerator_period=SINGLE, denominator_period=SINGLE
        )
        assert br is sr is None
        assert buy == 100 * (item["BILLBOARD_BUY_AMT"] / item["ACCUM_AMOUNT"])
        assert sell == 100 * (item["BILLBOARD_SELL_AMT"] / item["ACCUM_AMOUNT"])
        check = normalize.cross_check_ratios(buy, sell, item["DEAL_AMOUNT_RATIO"])
        assert check.passed is True
        assert check.reason is None
        differences.append(check.difference_pct)
        output = normalize.normalize_billboard_row(row)
        assert set(output) == BILLBOARD_KEYS
        assert not FORBIDDEN.intersection(output)
    assert max(differences) == pytest.approx(4.9e-13, rel=0.03, abs=0)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -float("inf")])
def test_billboard_missing_and_nonfinite_structure_values(billboard, value):
    output = normalize.normalize_billboard_row(
        billboard, turnover_rate=value, free_market_cap=value
    )
    assert set(output) == BILLBOARD_KEYS
    assert output["turnover_rate"] is None
    assert output["free_market_cap"] is None
    assert output["turnover_rate_unit"] == "pct"
    assert output["free_market_cap_unit"] == "yuan"
    json.dumps(output, allow_nan=False)


def test_billboard_zero_and_provider_text_are_not_promoted(billboard):
    quote = "上榜原因；成功率28.87%"
    row = replace(billboard, explanation=quote, explain_note="胜率99%；BUY_RATIO=88")
    output = normalize.normalize_billboard_row(row, turnover_rate=0.0, free_market_cap=123.0)
    assert set(output) == BILLBOARD_KEYS
    assert output["turnover_rate"] == 0.0
    assert output["free_market_cap"] == 123.0
    assert output["source_explanation"] == quote
    assert not FORBIDDEN.intersection(output)
    assert all(
        "成功率" not in str(v) and "胜率" not in str(v)
        for k, v in output.items()
        if k != "source_explanation"
    )


def test_every_fixture_evidence_has_exact_whitelist(seats):
    for record in normalize.normalize_seat_rows(seats):
        output = normalize.evidence_chain(record)
        assert set(output) == EVIDENCE_KEYS
        assert output["evidence_id"] == annotator.evidence_id_of(record)
        assert output["source_record_id"] == record.source_record_id
        assert output["source_explanation"] == record.explanation
        assert not FORBIDDEN.intersection(output)
        normalize.assert_evidence_whitelist(output)
        json.dumps(output, allow_nan=False)


def test_valid_evidence_amount_and_id_are_preserved(seats):
    record = normalize.normalize_seat_row(seats[0])
    output = normalize.evidence_chain(record)
    assert output["amount"] == 100941459.08
    assert output["amount_reason"] is None
    assert output["evidence_id"] == annotator.evidence_id_of(record)
    for key in EVIDENCE_KEYS:
        incomplete = {k: v for k, v in output.items() if k != key}
        with pytest.raises(AssertionError):
            normalize.assert_evidence_whitelist(incomplete)


def test_evidence_stat_text_only_allowed_in_source_explanation(seats):
    record = normalize.normalize_seat_row(replace(seats[0], explanation="成功率28.87%；原文"))
    output = normalize.evidence_chain(record)
    assert set(output) == EVIDENCE_KEYS
    assert output["source_explanation"] == "成功率28.87%；原文"
    assert all("成功率" not in str(v) for k, v in output.items() if k != "source_explanation")
    for field in FORBIDDEN:
        with pytest.raises(AssertionError):
            normalize.assert_evidence_whitelist({**output, field: 99})
    with pytest.raises(AssertionError):
        normalize.assert_evidence_whitelist({**output, "security_code": "成功率28.87%"})


@pytest.mark.parametrize(
    "amount,reason",
    [
        (None, "amount_missing"),
        (float("nan"), "amount_non_finite"),
        (float("inf"), "amount_non_finite"),
        (-float("inf"), "amount_non_finite"),
    ],
)
def test_evidence_invalid_amount_is_json_safe(seats, amount, reason):
    record = normalize.normalize_seat_row(replace(seats[0], buy=amount))
    output = normalize.evidence_chain(record)
    assert set(output) == EVIDENCE_KEYS
    assert output["amount"] is None
    assert output["amount_reason"] == reason
    assert output["evidence_id"] == annotator.evidence_id_of(replace(record, amount=None))
    json.dumps(output, allow_nan=False)
