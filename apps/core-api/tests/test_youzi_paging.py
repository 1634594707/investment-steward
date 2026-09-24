"""Synthetic offline Y2-07 tests: no fixtures, network, dependencies or sleeps."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from investment_steward_core import youzi_paging as paging
from investment_steward_core.youzi_paging import Budget, PagingKernel, PagingPolicy


class Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.value += seconds


def envelope(rows=None, count=1, pages=1, **extra):
    return {
        "success": True,
        "result": {
            "data": [{"id": 1}] if rows is None else rows,
            "count": count,
            "pages": pages,
            **extra,
        },
    }


class Transport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def __call__(self, params, timeout):
        self.calls.append((deepcopy(params), timeout))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


SORT = {"sortColumns": "CALLER_STABLE_KEY", "sortTypes": "CALLER_DIRECTION"}
FILTER = {"filter": "CALLER_VERIFIED_EXPRESSION"}


def fetch(kernel, budget=None, params=None, **kwargs):
    return kernel.fetch(
        {"reportName": "OFFLINE"} if params is None else params,
        budget=budget or Budget(),
        sort_params=kwargs.get("sort_params", SORT),
        filter_params=kwargs.get("filter_params", FILTER),
    )


def kernel(transport, **kwargs):
    return PagingKernel(transport, policy=PagingPolicy(page_size=2, **kwargs))


def wait(event):
    # Timeout is a deadlock guard, not a scheduling sleep or ordering assertion.
    assert event.wait(5), "thread synchronization timed out"


def test_defaults_and_complete_pages_preserve_reasons_and_order():
    assert PagingPolicy().cache_entries == 32
    transport = Transport(
        envelope([{"id": 1, "reason": "A"}, {"id": 1, "reason": "B"}], 3, 2),
        envelope([{"id": 2, "nested": {"zero": 0}}], 3, 2),
    )
    budget = Budget()
    result = fetch(kernel(transport), budget)
    assert result.coverage == "complete"
    assert result.reason == "all_pages_validated"
    assert [r.get("reason") for r in result.rows] == ["A", "B", None]
    assert result.attempts_consumed == result.producer_attempts == budget.attempts == 2
    assert result.expected_count == 3 and result.expected_pages == result.pages_received == 2
    assert [p["pageNumber"] for p, _ in transport.calls] == [1, 2]
    assert all(
        p["filter"] == FILTER["filter"] and p["sortColumns"] == SORT["sortColumns"]
        for p, _ in transport.calls
    )


def test_request_budget_shared_across_reports_and_retries():
    transport = Transport(OSError("retry"), envelope(), envelope())
    k = kernel(transport)
    budget = Budget(max_attempts=2)
    first = fetch(k, budget)
    second = fetch(k, budget, {"reportName": "SECOND"})
    assert first.coverage == "complete" and first.attempts_consumed == 2
    assert second.reason == "attempt_budget_exhausted"
    assert second.attempts_consumed == 0 and budget.attempts == 2
    # Cache has no attempt charge, even with an exhausted attempt allowance.
    cached = fetch(k, budget)
    assert cached.source == "cache" and cached.attempts_consumed == 0
    assert cached.producer_attempts == 2
    assert len(transport.calls) == 2


def test_default_budget_exactly_40_attempts_and_30_seconds():
    clock = Clock()
    budget = Budget(clock=clock)
    for _ in range(40):
        assert budget.claim(99) == (30, None)
    assert budget.claim(99) == (0, "attempt_budget_exhausted")
    clock.advance(30)
    assert budget.remaining() == 0
    assert budget.claim(99) == (0, "deadline_exceeded")


def test_retries_are_bounded_and_preserve_partial_rows():
    transport = Transport(
        envelope([{"id": 1}, {"id": 2}], 3, 2), OSError("a"), OSError("b"), OSError("last")
    )
    result = fetch(kernel(transport))
    assert result.coverage == "fetch_failed" and result.reason == "transport_error"
    assert result.rows == [{"id": 1}, {"id": 2}]
    assert result.attempts_consumed == 4
    assert result.detail == "last"
    assert [p["pageNumber"] for p, _ in transport.calls] == [1, 2, 2, 2]


def test_budget_exhausted_during_retry_preserves_partial():
    transport = Transport(envelope([{"id": 1}, {"id": 2}], 3, 2), OSError("error"))
    result = fetch(kernel(transport), Budget(max_attempts=2))
    assert result.coverage == "truncated" and result.reason == "attempt_budget_exhausted"
    assert len(result.rows) == 2 and result.attempts_consumed == 2


def test_upstream_failure_is_not_empty_and_retries_count():
    failure = {"success": False, "code": 9501, "message": "unsupported filter"}
    transport = Transport(failure, failure)
    result = fetch(kernel(transport, retries_per_page=1))
    assert result.coverage == "fetch_failed" and result.reason == "upstream_error"
    assert "9501" in result.detail and "unsupported filter" in result.detail
    assert result.attempts_consumed == 2


def test_timeout_is_clipped_and_late_valid_rows_never_complete():
    clock = Clock()
    calls = []

    def transport(params, timeout):
        calls.append(timeout)
        clock.advance(4)
        return envelope()

    result = fetch(PagingKernel(transport), Budget(seconds=3, clock=clock))
    assert calls == [3]
    assert result.reason == "deadline_exceeded" and result.coverage == "truncated"
    assert result.rows == [{"id": 1}] and result.attempts_consumed == 1


def test_timeout_recomputed_per_page_and_before_retry():
    clock = Clock()
    calls = []

    def transport(params, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            clock.advance(2)
            return envelope([{"id": 1}, {"id": 2}], 3, 2)
        clock.advance(3)
        raise TimeoutError("late error")

    result = fetch(kernel(transport), Budget(seconds=5, clock=clock))
    assert calls == [5, 3]
    assert result.reason == "deadline_exceeded"
    assert len(result.rows) == 2 and result.attempts_consumed == 2


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, "invalid_envelope"),
        ({"success": 1}, "invalid_envelope"),
        ({"success": True}, "invalid_result"),
        ({"success": True, "result": []}, "invalid_result"),
        (envelope("not a list"), "invalid_rows"),
        (envelope([{"id": 1}, None]), "invalid_rows"),
        (envelope([1]), "invalid_rows"),
        (envelope([{"id": float("nan")}]), "invalid_row_values"),
        (envelope(count=True), "invalid_metadata"),
        (envelope(pages=True), "invalid_metadata"),
        (envelope(count="1"), "invalid_metadata"),
        (envelope(pages=1.0), "invalid_metadata"),
        (envelope(count=None), "invalid_metadata"),
        (envelope(pages=-1), "invalid_metadata"),
        (envelope(count=-1), "invalid_metadata"),
        (envelope(count=1, pages=0), "inconsistent_metadata"),
        (envelope(count=0), "inconsistent_metadata"),
        (envelope(count=10, pages=1), "inconsistent_metadata"),
        (envelope([], count=0, pages=0), "empty_unverified"),
        (envelope([], count=0, pages=1), "empty_unverified"),
        (envelope([], count=1), "missing_page"),
        (envelope([{"id": 1}], count=3, pages=2), "page_length_mismatch"),
        (envelope(pageNumber=True), "page_not_advancing"),
        (envelope(pageNumber=2), "page_not_advancing"),
    ],
)
def test_invalid_or_empty_payload_cannot_certify_coverage(payload, reason):
    transport = Transport(payload)
    result = fetch(kernel(transport))
    assert result.coverage == "unknown" and result.reason == reason
    assert result.attempts_consumed == len(transport.calls) == 1


@pytest.mark.parametrize(
    "second", [envelope([], 3, 2), envelope([{"id": 3}], 4, 2), envelope([{"id": 3}], 3, 3)]
)
def test_missing_page_and_metadata_drift_keep_prior_rows(second):
    transport = Transport(envelope([{"id": 1}, {"id": 2}], 3, 2), second)
    result = fetch(kernel(transport))
    assert result.coverage == "unknown"
    assert result.reason in ("missing_page", "metadata_drift")
    assert result.rows[:2] == [{"id": 1}, {"id": 2}]
    assert result.attempts_consumed == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_page_signature_including_reordered_pages(reverse):
    rows = [{"id": 1, "reason": "A"}, {"id": 2, "reason": "B"}]
    transport = Transport(envelope(rows, 4, 2), envelope(rows[::-1] if reverse else rows, 4, 2))
    result = fetch(kernel(transport))
    assert result.reason == "repeated_page" and result.coverage == "unknown"
    assert result.rows == rows and result.pages_received == 1


def test_page_echo_not_advancing_retains_new_raw_rows():
    transport = Transport(
        envelope([{"id": 1}, {"id": 2}], 3, 2, pageNumber=1),
        envelope([{"id": 3}], 3, 2, pageNumber=1),
    )
    result = fetch(kernel(transport))
    assert result.reason == "page_not_advancing" and len(result.rows) == 3


def test_no_row_level_deduplication_or_reason_aggregation():
    transport = Transport(envelope([{"id": 1}, {"id": 1}], 3, 2), envelope([{"id": 1}], 3, 2))
    result = fetch(kernel(transport))
    assert result.coverage == "complete" and result.rows == [{"id": 1}] * 3


@pytest.mark.parametrize(
    ("options", "reason", "length"),
    [
        ({"max_pages": 1}, "page_limit", 2),
        ({"max_rows": 1}, "row_limit", 1),
        ({"max_page_bytes": 1}, "page_byte_limit", 0),
        ({"max_result_bytes": 9}, "result_byte_limit", 1),
    ],
)
def test_memory_and_page_limits(options, reason, length):
    result = fetch(kernel(Transport(envelope([{"id": 1}, {"id": 2}], 3, 2)), **options))
    assert result.coverage == "truncated" and result.reason == reason
    assert len(result.rows) == length and result.attempts_consumed == 1


def test_oversized_page_is_rejected_without_unbounded_copy():
    transport = Transport(envelope([{"id": 1}, {"id": 2}, {"id": 3}], 3, 2))
    result = fetch(kernel(transport, max_page_rows=2))
    assert result.reason == "page_row_limit" and not result.rows


def test_cache_canonicalization_ttl_and_deep_copy_isolation():
    clock = Clock()
    original = envelope([{"id": 1, "nested": {"values": [2]}}])
    transport = Transport(original, original)
    k = PagingKernel(transport, clock=clock)
    first = fetch(k, params={"a": 1, "b": {"y": 2, "x": 1}})
    first.rows[0]["nested"]["values"].append(99)
    second = fetch(k, params={"b": {"x": 1, "y": 2}, "a": 1})
    assert second.source == "cache" and second.attempts_consumed == 0
    assert second.producer_attempts == 1
    assert second.rows == original["result"]["data"]
    second.rows.clear()
    assert fetch(k, params={"a": 1, "b": {"y": 2, "x": 1}}).rows
    clock.advance(60)
    assert fetch(k, params={"a": 1, "b": {"y": 2, "x": 1}}).source == "producer"
    assert len(transport.calls) == 2


def test_cache_and_cooldown_combined_lru_bound_and_expiry():
    clock = Clock()
    calls = []

    def transport(params, timeout):
        calls.append(params["id"])
        if params["id"] % 2:
            raise OSError("offline")
        return envelope()

    k = PagingKernel(
        transport, clock=clock, policy=PagingPolicy(cache_entries=2, retries_per_page=0)
    )
    assert fetch(k, params={"id": 1}).source == "producer"
    failed = fetch(k, params={"id": 1})
    assert failed.source == "cooldown" and failed.coverage == "fetch_failed"
    assert failed.attempts_consumed == 0 and failed.producer_attempts == 1
    fetch(k, params={"id": 2})
    fetch(k, params={"id": 3})
    assert len(k._entries) == 2
    assert fetch(k, params={"id": 1}).source == "producer"  # evicted failure
    clock.advance(5)
    assert fetch(k, params={"id": 1}).source == "producer"  # expired cooldown
    assert len(k._entries) <= 2


def test_cache_namespaces_and_caller_params_are_isolated():
    params = {"nested": {"values": [1]}}
    calls = []

    def transport(actual, timeout):
        calls.append(deepcopy(actual))
        actual["nested"]["values"].append(99)
        params["nested"]["values"].append(88)
        return (
            envelope([{"id": 1}, {"id": 2}], 3, 2)
            if actual["pageNumber"] == 1
            else envelope([{"id": 3}], 3, 2)
        )

    result = fetch(kernel(transport), params=params)
    assert result.coverage == "complete"
    assert all(p["nested"]["values"] == [1] for p in calls)
    one = kernel(Transport(envelope([{"id": 1}])))
    two = kernel(Transport(envelope([{"id": 2}])))
    assert fetch(one).rows != fetch(two).rows


def test_unknown_and_request_specific_truncation_are_not_cached():
    t = Transport(envelope([], 0, 0), envelope())
    k = kernel(t)
    assert fetch(k).coverage == "unknown"
    assert fetch(k).coverage == "complete"
    t2 = Transport(envelope([{"id": 1}, {"id": 2}], 3, 2), envelope())
    k2 = kernel(t2)
    assert fetch(k2, Budget(max_attempts=1)).coverage == "truncated"
    assert fetch(k2).source == "producer"


def test_expired_budget_does_not_return_complete_cache():
    clock = Clock()
    k = kernel(Transport(envelope()))
    fetch(k)
    budget = Budget(seconds=1, clock=clock)
    clock.advance(1)
    result = fetch(k, budget)
    assert result.reason == "deadline_exceeded" and result.attempts_consumed == 0


def test_single_flight_coalesces_and_charges_only_producer(monkeypatch):
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    original_wait = paging._wait_event
    calls = []

    def observed_wait(event, timeout):
        waiting.set()
        return original_wait(event, timeout)

    monkeypatch.setattr(paging, "_wait_event", observed_wait)

    def transport(params, timeout):
        calls.append(params)
        entered.set()
        wait(release)
        return envelope([{"nested": {"value": 1}}])

    k = kernel(transport)
    producer_budget, waiter_budget = Budget(), Budget()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetch, k, producer_budget)
        wait(entered)
        second = pool.submit(fetch, k, waiter_budget)
        try:
            wait(waiting)
        finally:
            release.set()
        a, b = first.result(5), second.result(5)
    assert len(calls) == producer_budget.attempts == 1
    assert waiter_budget.attempts == b.attempts_consumed == 0
    assert a.source == "producer" and b.source == "shared" and b.producer_attempts == 1
    a.rows[0]["nested"]["value"] = 99
    assert b.rows[0]["nested"]["value"] == 1
    assert fetch(k).rows[0]["nested"]["value"] == 1


def test_waiter_own_deadline_does_not_cancel_producer(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    clock = Clock()
    waiter_budget = Budget(seconds=2, clock=clock)

    def fake_wait(event, timeout):
        assert timeout == 2 and not event.is_set()
        clock.advance(2)
        return False

    monkeypatch.setattr(paging, "_wait_event", fake_wait)

    def transport(params, timeout):
        entered.set()
        wait(release)
        return envelope()

    k = kernel(transport)
    with ThreadPoolExecutor(max_workers=1) as pool:
        producer = pool.submit(fetch, k)
        wait(entered)
        try:
            result = fetch(k, waiter_budget)
            assert result.reason == "waiter_deadline_exceeded" and result.source == "shared"
            assert result.attempts_consumed == 0 and waiter_budget.attempts == 0
        finally:
            release.set()
        assert producer.result(5).coverage == "complete"


def test_process_global_max_two_across_kernel_instances(monkeypatch):
    real_slots = paging._UPSTREAM
    first_two, third_queued, release = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    counts = {"acquires": 0, "active": 0, "peak": 0, "calls": 0}

    class ObservedSlots:
        def acquire(self, timeout):
            with lock:
                counts["acquires"] += 1
                if counts["acquires"] == 3:
                    third_queued.set()
            return real_slots.acquire(timeout=timeout)

        def release(self):
            real_slots.release()

    monkeypatch.setattr(paging, "_UPSTREAM", ObservedSlots())

    def transport(params, timeout):
        with lock:
            counts["calls"] += 1
            counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
            if counts["active"] == 2:
                first_two.set()
        try:
            wait(release)
            return envelope()
        finally:
            with lock:
                counts["active"] -= 1

    budgets = [Budget() for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch, kernel(transport), budgets[i]) for i in range(3)]
        try:
            wait(first_two)
            wait(third_queued)
            assert counts["calls"] == 2 and sum(b.attempts for b in budgets) == 2
        finally:
            release.set()
        assert all(f.result(5).coverage == "complete" for f in futures)
    assert counts["peak"] == 2 and counts["calls"] == 3


def test_semaphore_queue_deadline_does_not_charge_attempt(monkeypatch):
    clock = Clock()

    class Slots:
        def acquire(self, timeout):
            assert timeout == 3
            clock.advance(3)
            return True  # deadline must also be checked AFTER obtaining a slot

        def release(self):
            pass

    monkeypatch.setattr(paging, "_UPSTREAM", Slots())
    transport = Transport()
    budget = Budget(seconds=3, clock=clock)
    result = fetch(kernel(transport), budget)
    assert result.reason == "deadline_exceeded" and not transport.calls
    assert budget.attempts == result.attempts_consumed == 0


def test_thread_safe_shared_budget_does_not_overcharge_local_result():
    entered, release = threading.Event(), threading.Event()

    def transport(params, timeout):
        entered.set()
        wait(release)
        return envelope()

    k = kernel(transport)
    budget = Budget(max_attempts=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        producer = pool.submit(fetch, k, budget, {"id": 1})
        wait(entered)
        try:
            other = pool.submit(fetch, k, budget, {"id": 2}).result(5)
            assert other.reason == "attempt_budget_exhausted" and other.attempts_consumed == 0
        finally:
            release.set()
        assert producer.result(5).attempts_consumed == 1
    assert budget.attempts == 1


def test_inflight_table_is_bounded():
    entered, release = threading.Event(), threading.Event()

    def transport(params, timeout):
        entered.set()
        wait(release)
        return envelope()

    k = kernel(transport, max_inflight=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fetch, k)
        wait(entered)
        try:
            result = fetch(k, params={"different": True})
            assert result.reason == "inflight_limit" and result.attempts_consumed == 0
            assert len(k._flights) == 1
        finally:
            release.set()
        assert future.result(5).coverage == "complete"
    assert not k._flights


@pytest.mark.parametrize(
    "options",
    [
        {"cache_entries": 33},
        {"max_inflight": 33},
        {"page_size": 0},
        {"max_rows": True},
        {"retries_per_page": True},
        {"request_timeout": float("inf")},
        {"cache_ttl": -1},
        {"page_size": 1001},
    ],
)
def test_policy_validation(options):
    with pytest.raises(ValueError):
        replace(PagingPolicy(), **options)


@pytest.mark.parametrize(
    "options",
    [{"max_attempts": True}, {"max_attempts": 0}, {"seconds": 0}, {"seconds": float("nan")}],
)
def test_budget_validation(options):
    with pytest.raises(ValueError):
        Budget(**options)


@pytest.mark.parametrize(
    "params, sort_params, filter_params",
    [
        ({"pageNumber": 1}, SORT, {}),
        ({}, {}, {}),
        ({"x": 1}, {"x": 2}, {}),
        ({"x": float("nan")}, SORT, {}),
        ({1: "x"}, SORT, {}),
    ],
)
def test_explicit_parameters_reject_ambiguity(params, sort_params, filter_params):
    with pytest.raises(ValueError):
        fetch(
            kernel(Transport()), params=params, sort_params=sort_params, filter_params=filter_params
        )


def test_canonical_key_bound_and_explicit_empty_filter():
    with pytest.raises(ValueError, match="max_key_bytes"):
        fetch(kernel(Transport(), max_key_bytes=10))
    transport = Transport(envelope())
    assert fetch(kernel(transport), filter_params={}).coverage == "complete"
    assert "filter" not in transport.calls[0][0]


def test_different_filter_and_sort_do_not_coalesce_or_hit_cache():
    transport = Transport(envelope(), envelope(), envelope())
    k = kernel(transport)
    fetch(k)
    assert fetch(k, filter_params={"filter": "DIFFERENT"}).source == "producer"
    assert fetch(k, sort_params={"sortColumns": "DIFFERENT"}).source == "producer"
    assert len(transport.calls) == 3


def test_producer_base_exception_releases_flight_and_semaphore():
    class Cancelled(BaseException):
        pass

    def transport(params, timeout):
        raise Cancelled()

    k = kernel(transport)
    with pytest.raises(Cancelled):
        fetch(k)
    assert not k._flights and not k._entries
    assert fetch(kernel(Transport(envelope()))).coverage == "complete"


def test_waiter_inherits_producer_budget_failure_not_a_new_retry(monkeypatch):
    entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
    old_wait = paging._wait_event

    def observed_wait(event, timeout):
        waiting.set()
        return old_wait(event, timeout)

    monkeypatch.setattr(paging, "_wait_event", observed_wait)

    def transport(params, timeout):
        entered.set()
        wait(release)
        return envelope([{"id": 1}, {"id": 2}], 3, 2)

    k = kernel(transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetch, k, Budget(max_attempts=1))
        wait(entered)
        second = pool.submit(fetch, k)
        try:
            wait(waiting)
        finally:
            release.set()
        a, b = first.result(5), second.result(5)
    assert a.reason == b.reason == "attempt_budget_exhausted"
    assert b.rows == a.rows and b.attempts_consumed == 0 and b.producer_attempts == 1
    assert not k._entries


def test_invalid_second_page_retains_first_page_and_does_not_retry_schema():
    transport = Transport(envelope([{"id": 1}, {"id": 2}], 3, 2), envelope([None], 3, 2))
    result = fetch(kernel(transport))
    assert result.reason == "invalid_rows" and result.rows == [{"id": 1}, {"id": 2}]
    assert result.attempts_consumed == 2


def test_negative_cache_preserves_partial_rows_with_deep_copy():
    transport = Transport(envelope([{"id": 1}, {"id": 2}], 3, 2), OSError("offline"))
    k = kernel(transport, retries_per_page=0)
    first = fetch(k)
    first.rows.clear()
    cached = fetch(k)
    assert cached.source == "cooldown" and len(cached.rows) == 2
    assert cached.attempts_consumed == 0 and cached.producer_attempts == 2
    assert cached.coverage == "fetch_failed"


def test_budget_atomic_claim_under_event_started_threads():
    start = threading.Event()
    budget = Budget(max_attempts=7)

    def claim():
        wait(start)
        return budget.claim(1)[1]

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(claim) for _ in range(12)]
        start.set()
        reasons = [future.result(5) for future in futures]
    assert reasons.count(None) == budget.attempts == 7
    assert reasons.count("attempt_budget_exhausted") == 5


def test_cache_has_hard_32_entry_bound_with_default_policy():
    k = PagingKernel(lambda params, timeout: envelope())
    for index in range(40):
        fetch(k, params={"id": index})
    assert len(k._entries) == 32
    assert fetch(k, params={"id": 39}).source == "cache"
    assert fetch(k, params={"id": 0}).source == "producer"


def test_single_transport_attempt_per_claim_even_when_timeout_raises():
    budget = Budget(max_attempts=3)
    transport = Transport(TimeoutError("one"), TimeoutError("two"), TimeoutError("three"))
    result = fetch(kernel(transport), budget)
    assert result.reason == "transport_error"
    assert len(transport.calls) == budget.attempts == result.attempts_consumed == 3
