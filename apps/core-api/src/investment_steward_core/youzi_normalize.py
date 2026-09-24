"""Offline Y1 adapters: no fetching, clock, file loading, or production wiring.

Amounts are yuan; ratios are percentage points, not provider side ratios.
Period/identity rules are provisional until Y0 freezes the source contract.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date

from . import lhb_feed, seat_book, tactic_annotator

# Provisional fallback equivalent to the checked-in watchlist names/suffixes.
# A caller may supply additional policy via Watchlist; never load it implicitly.
_BUCKET_NAMES = frozenset({"机构专用", "沪股通专用", "深股通专用"})
_BUCKET_SUFFIXES = ("总部",)
_KNOWN_PERIODS = frozenset({tactic_annotator.PERIOD_SINGLE_DAY, tactic_annotator.PERIOD_MULTI_DAY})
RATIO_TOLERANCE_PCT = 1e-6
FORBIDDEN_FIELDS = frozenset(
    {"RISE_PROBABILITY_3DAY", "TOTAL_BUYER_SALESTIMES_3DAY", "BUY_RATIO", "SELL_RATIO"}
)
_EVIDENCE_FIELDS = frozenset(
    {
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
)
_PROVIDER_STAT_TEXT = ("成功率", "胜率", "上涨概率", "上涨比例", *sorted(FORBIDDEN_FIELDS))
RatioResult = tuple[float | None, str | None]


def _identity_kind(row: lhb_feed.SeatRow, watchlist: seat_book.Watchlist | None) -> str:
    code = row.operatedept_code.strip()
    name = row.operatedept_name.strip()
    if (
        code in ("", "0")
        or name in _BUCKET_NAMES
        or name.endswith(_BUCKET_SUFFIXES)
        or (watchlist is not None and watchlist.is_bucket_name(name))
    ):
        return tactic_annotator.IDENTITY_BUCKET
    # Numeric provider code plus an explicitly named branch, not an arbitrary
    # label. This identifies a channel, never an account or trader.
    if (
        code.isascii()
        and code.isdecimal()
        and code.strip("0")
        and name.endswith(("营业部", "分公司"))
    ):
        return tactic_annotator.IDENTITY_BRANCH
    return tactic_annotator.IDENTITY_UNKNOWN


def normalize_seat_row(
    row: lhb_feed.SeatRow,
    *,
    group_index: int = 0,
    watchlist: seat_book.Watchlist | None = None,
) -> tactic_annotator.NormalizedSeatRecord:
    """Adapt one row, preserving its explanation and direction-side amount.

    group_index is accepted for compatibility but never contributes to identity.
    Source order and duplicate occurrence counts cannot change evidence IDs.
    source_record_id hashes content plus report, explanation and amount.
    TRADE_ID is NOT row-unique: 73 unique values in 730 fixture rows. It must
    never be the sole identity; SeatRow does not retain it, so it is not used.
    Different explanations/amounts are never merged/summed.
    None/zero/negative/NaN/infinite amounts survive for annotator exclusion.
    """
    if isinstance(group_index, bool) or not isinstance(group_index, int) or group_index < 0:
        raise ValueError("group_index must be a non-negative integer")
    if row.direction == lhb_feed.DIRECTION_BUY:
        amount, report = row.buy, lhb_feed.REPORT_SEAT_BUY
    elif row.direction == lhb_feed.DIRECTION_SELL:
        amount, report = row.sell, lhb_feed.REPORT_SEAT_SELL
    else:
        raise ValueError(f"invalid direction: {row.direction!r}")
    day = lhb_feed.normalise_trade_date(row.trading_day)
    composite = (
        day,
        row.security_code,
        row.operatedept_code,
        row.direction,
        report,
        row.explanation,
        float(amount) if _finite_number(amount) else None,
    )
    # Invalid amounts hash as null; retain the raw amount for explicit exclusion.
    source_id = hashlib.sha256(
        json.dumps(composite, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return tactic_annotator.NormalizedSeatRecord(
        trading_day=day,
        security_code=row.security_code,
        operatedept_code=row.operatedept_code,
        operatedept_name=row.operatedept_name,
        direction=row.direction,
        amount=amount,
        period=tactic_annotator.classify_period_from_explanation(row.explanation),
        identity_kind=_identity_kind(row, watchlist),
        explanation=row.explanation,
        source_report=report,
        source_record_id=source_id,
    )


def normalize_seat_rows(
    rows: Iterable[lhb_feed.SeatRow], *, watchlist: seat_book.Watchlist | None = None
) -> list[tactic_annotator.NormalizedSeatRecord]:
    """Preserve rows without order-sensitive identities or implicit summation."""
    return [normalize_seat_row(row, watchlist=watchlist) for row in rows]


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _valid_interval(interval: tuple[str, str] | None) -> bool:
    if not isinstance(interval, tuple) or len(interval) != 2:
        return False
    for day in interval:
        if not isinstance(day, str) or len(day) != 8 or not day.isascii() or not day.isdigit():
            return False
        try:
            date(int(day[:4]), int(day[4:6]), int(day[6:]))
        except ValueError:
            return False
    return interval[0] <= interval[1]


def amount_ratio_pct(
    numerator: float | None,
    denominator: float | None,
    *,
    numerator_period: str | None,
    denominator_period: str | None,
    numerator_interval: tuple[str, str] | None = None,
    denominator_interval: tuple[str, str] | None = None,
) -> RatioResult:
    """Return unrounded 100 * side amount / turnover, or None and a reason.

    Periods must be explicitly known and equal. For multi_day inputs the caller
    must establish that both amounts refer to the SAME disclosed interval;
    matching enum labels cannot establish boundaries. No bounds are invented.
    Error precedence: denominator, period, numerator, arithmetic.
    Finite zero numerator yields a genuine 0.0, never missing data.
    """
    if denominator is None:
        return None, "denominator_missing"
    if not _finite_number(denominator):
        return None, "denominator_non_finite"
    if denominator <= 0:
        return None, "denominator_non_positive"
    if numerator_period not in _KNOWN_PERIODS or denominator_period not in _KNOWN_PERIODS:
        return None, "period_unknown"
    if numerator_period != denominator_period:
        return None, "period_mismatch"
    if numerator_period == tactic_annotator.PERIOD_MULTI_DAY:
        if not _valid_interval(numerator_interval) or not _valid_interval(denominator_interval):
            return None, "interval_unknown"
        if numerator_interval != denominator_interval:
            return None, "interval_mismatch"
    if numerator is None:
        return None, "numerator_missing"
    if not _finite_number(numerator):
        return None, "numerator_non_finite"
    value = 100.0 * (numerator / denominator)
    if not math.isfinite(value):
        return None, "ratio_non_finite"
    return value, None


def _row_ratio_pct(
    row: lhb_feed.SeatRow | lhb_feed.BillboardRow,
    side: str,
    denominator: float | None,
    numerator_period: str | None,
    denominator_period: str | None,
    numerator_interval: tuple[str, str] | None = None,
    denominator_interval: tuple[str, str] | None = None,
) -> RatioResult:
    if isinstance(row, lhb_feed.BillboardRow):
        if denominator is not None:
            raise ValueError("billboard denominator must come from ACCUM_AMOUNT")
        denominator = row.accum_amount
        numerator = (
            row.billboard_buy_amt if side == lhb_feed.DIRECTION_BUY else row.billboard_sell_amt
        )
    else:
        # Structural ratios refer to named columns, not NET, on either report.
        numerator = row.buy if side == lhb_feed.DIRECTION_BUY else row.sell
    # Deliberately do NOT infer the numerator period from row.explanation here.
    # Both sides of a ratio must be caller-supplied: inferring one side from
    # text while the other arrives as an explicit argument can silently pair a
    # guessed period with a declared one, defeating the same-interval check.
    # `normalize_billboard_row` (and any other caller) must pass both periods.
    return amount_ratio_pct(
        numerator,
        denominator,
        numerator_period=numerator_period,
        denominator_period=denominator_period,
        numerator_interval=numerator_interval,
        denominator_interval=denominator_interval,
    )


def buy_amt_ratio_pct(
    row: lhb_feed.SeatRow | lhb_feed.BillboardRow,
    *,
    denominator: float | None = None,
    numerator_period: str | None = None,
    denominator_period: str | None = None,
    numerator_interval: tuple[str, str] | None = None,
    denominator_interval: tuple[str, str] | None = None,
) -> RatioResult:
    """BUY / turnover (%); seats require an explicit denominator.

    Billboard denominator is always ACCUM_AMOUNT. Explicit numerator_period is
    caller-supplied verification, not an inferred single-day classification.
    Default denominator period is unknown, never silently assumed to match.
    """
    return _row_ratio_pct(
        row,
        lhb_feed.DIRECTION_BUY,
        denominator,
        numerator_period,
        denominator_period,
        numerator_interval,
        denominator_interval,
    )


def sell_amt_ratio_pct(
    row: lhb_feed.SeatRow | lhb_feed.BillboardRow,
    *,
    denominator: float | None = None,
    numerator_period: str | None = None,
    denominator_period: str | None = None,
    numerator_interval: tuple[str, str] | None = None,
    denominator_interval: tuple[str, str] | None = None,
) -> RatioResult:
    """SELL / turnover (%), under the same explicit-period contract as buy."""
    return _row_ratio_pct(
        row,
        lhb_feed.DIRECTION_SELL,
        denominator,
        numerator_period,
        denominator_period,
        numerator_interval,
        denominator_interval,
    )


@dataclass(frozen=True)
class RatioCrossCheck:
    """Technical consistency only; passed=None means no valid comparison."""

    passed: bool | None
    difference_pct: float | None
    reason: str | None
    tolerance_pct: float = RATIO_TOLERANCE_PCT


def cross_check_ratios(
    buy_pct: float | None, sell_pct: float | None, total_pct: float | None
) -> RatioCrossCheck:
    """Compare abs(buy + sell - total) <= 1e-6 percentage points, without rounding.

    Inputs may be computed sides and provider total; this helper never fetches
    or retains forbidden provider BUY_RATIO / SELL_RATIO fields.
    """
    values = (buy_pct, sell_pct, total_pct)
    if any(value is None for value in values):
        return RatioCrossCheck(None, None, "percentage_missing")
    if not all(_finite_number(value) for value in values):
        return RatioCrossCheck(None, None, "percentage_non_finite")
    assert buy_pct is not None and sell_pct is not None and total_pct is not None
    difference = abs(buy_pct + sell_pct - total_pct)
    if not math.isfinite(difference):
        return RatioCrossCheck(None, None, "difference_non_finite")
    passed = difference <= RATIO_TOLERANCE_PCT
    return RatioCrossCheck(passed, difference, None if passed else "total_mismatch")


def normalize_billboard_row(
    row: lhb_feed.BillboardRow,
    *,
    turnover_rate: float | None = None,
    free_market_cap: float | None = None,
    numerator_period: str | None = None,
    denominator_period: str | None = None,
    numerator_interval: tuple[str, str] | None = None,
    denominator_interval: tuple[str, str] | None = None,
) -> dict[str, object]:
    """Whitelist structure fields for later consumers, without production wiring.

    Existing BillboardRow drops TURNOVERRATE/FREE_MARKET_CAP; callers must pass
    those source values explicitly (percent/yuan). Missing/non-finite values
    remain unavailable, not zero. Neither prices nor provider notes are copied.
    """
    buy, buy_reason = buy_amt_ratio_pct(
        row,
        numerator_period=numerator_period,
        denominator_period=denominator_period,
        numerator_interval=numerator_interval,
        denominator_interval=denominator_interval,
    )
    sell, sell_reason = sell_amt_ratio_pct(
        row,
        numerator_period=numerator_period,
        denominator_period=denominator_period,
        numerator_interval=numerator_interval,
        denominator_interval=denominator_interval,
    )
    return {
        "source_report": lhb_feed.REPORT_DAILY_BILLBOARD,
        "security_code": row.security_code,
        "trading_day": lhb_feed.normalise_trade_date(row.trading_day),
        "source_explanation": row.explanation,
        "turnover_rate": turnover_rate if _finite_number(turnover_rate) else None,
        "turnover_rate_unit": "pct",
        "free_market_cap": free_market_cap if _finite_number(free_market_cap) else None,
        "free_market_cap_unit": "yuan",
        "buy_amt_ratio_pct": buy,
        "buy_amt_ratio_reason": buy_reason,
        "sell_amt_ratio_pct": sell,
        "sell_amt_ratio_reason": sell_reason,
    }


def assert_evidence_whitelist(output: Mapping[str, object]) -> None:
    """Reject extra/provider fields and stat text outside the source quotation.

    source_explanation is the sole permitted raw quotation. Provider statistical
    text may occur verbatim there, but is never parsed into our own fields.
    """
    assert set(output) == _EVIDENCE_FIELDS, "evidence locator whitelist violation"
    assert not FORBIDDEN_FIELDS.intersection(key.upper() for key in output)
    assert isinstance(output["source_explanation"], str)
    for key, value in output.items():
        if key != "source_explanation" and isinstance(value, str):
            assert not any(token in value for token in _PROVIDER_STAT_TEXT), (
                "provider statistical text outside source_explanation"
            )


def evidence_chain(record: tactic_annotator.NormalizedSeatRecord) -> dict[str, object]:
    """Serialize explicit source locators only; never asdict/merge provider data."""
    amount = record.amount
    reason = None
    if amount is None:
        reason = "amount_missing"
    elif not _finite_number(amount):
        reason = (
            "amount_non_finite"
            if isinstance(amount, (float, int)) and not isinstance(amount, bool)
            else "amount_invalid"
        )
        amount = None
    safe_record = replace(record, amount=amount)
    output: dict[str, object] = {
        "source_report": record.source_report,
        "security_code": record.security_code,
        "trading_day": record.trading_day,
        "operatedept_code": record.operatedept_code,
        "direction": record.direction,
        "source_explanation": record.explanation,
        "amount": amount,
        "amount_reason": reason,
        "source_record_id": record.source_record_id,
        "evidence_id": tactic_annotator.evidence_id_of(safe_record),
    }
    assert_evidence_whitelist(output)
    return output
