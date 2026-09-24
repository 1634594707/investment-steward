"""Offline Y2-07 paging mechanics; NOT a frozen live-provider contract.

Nothing here selects dates, calendars, report names, filters or sort expressions,
imports ``lhb_feed``, or opens a connection. A caller supplies an explicit stable
sort, explicit filters (including an intentional empty mapping), and a transport
``transport(params, timeout) -> payload`` doing EXACTLY ONE HTTP attempt. Never
adapt the already-retrying ``lhb_feed._http_get_json``. Transport must enforce its
own response-byte limit and timeout; Python cannot interrupt a blocking callable.
Late payloads retain usable rows but cannot establish complete coverage.

The provisional envelope is success=True, result={data: list[dict], count: int,
pages: int, [pageNumber: int]}. Booleans are not integers here. Completion needs
consistent positive metadata, every requested page with its exact expected row
count, and no repeated page (even reordered). A successful zero-count empty
response is UNKNOWN, not proof of no disclosures. Raw rows, including different
reasons and overlaps, are never summed or row-deduplicated. Repeated whole pages
are rejected explicitly; rows from that repeated page are not appended again.

Share one Budget across all reports/pages/retries/fallbacks of an API request.
It starts its monotonic deadline at construction. A producer pays actual attempts
only after obtaining the process-global two-slot semaphore. ``attempts_consumed``
is local to this fetch, NOT a before/after subtraction of the shared budget.
Cache/cooldown hits and single-flight waiters consume zero; ``producer_attempts``
records the original producer's cost. Waiters do not extend the producer's budget
or cancel it, and respect their own deadline (including time spent queueing).

Reuse a PagingKernel instance for a transport/source/credentials namespace:
single-flight and the combined LRU TTL result/cooldown cache are instance-local,
while upstream concurrency is process-global. Different kernels deliberately do
not share data. Only complete results and upstream failures are cached, with
separate TTLs and a combined maximum of 32 entries. All result boundaries deep
copy rows. The pending-flight table and retained row/page/serialized-byte counts
are bounded too. Serialization limits apply AFTER the injected transport returns.
No cross-process coordination, background threads, network, dependencies or
production endpoint wiring are provided. Y0 validation is still required.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Literal

Coverage = Literal["complete", "unknown", "fetch_failed", "truncated"]
Source = Literal["producer", "shared", "cache", "cooldown"]
Transport = Callable[[dict[str, object], float], object]
Clock = Callable[[], float]
_UPSTREAM = threading.BoundedSemaphore(2)


def _positive_int(value: object, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive nonbool int")


def _seconds(value: float, name: str, *, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    if not allow_zero and value == 0:
        raise ValueError(f"{name} must be positive")


class Budget:
    """Thread-safe request-wide attempt counter and monotonic deadline.

    Defaults (40 attempts / 30 seconds) are implementation policy, not a frozen
    live contract. Attempts are never refunded, including errors/late responses.
    Reading cached/shared work needs time remaining, but not an unused attempt.
    """

    def __init__(
        self, max_attempts: int = 40, seconds: float = 30.0, *, clock: Clock = time.monotonic
    ) -> None:
        _positive_int(max_attempts, "max_attempts")
        _seconds(seconds, "seconds")
        self._max_attempts = max_attempts
        self._clock = clock
        self._deadline = clock() + seconds
        self._attempts = 0
        self._lock = threading.Lock()

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def stop_reason(self) -> str | None:
        with self._lock:
            if self.remaining() <= 0:
                return "deadline_exceeded"
            if self._attempts >= self._max_attempts:
                return "attempt_budget_exhausted"
            return None

    def claim(self, timeout: float) -> tuple[float, str | None]:
        """Atomically charge one attempt; call only with an upstream slot held."""
        _seconds(timeout, "timeout")
        with self._lock:
            remaining = self.remaining()
            if remaining <= 0:
                return 0.0, "deadline_exceeded"
            if self._attempts >= self._max_attempts:
                return 0.0, "attempt_budget_exhausted"
            self._attempts += 1
            return min(timeout, remaining), None


@dataclass(frozen=True)
class PagingPolicy:
    """Conservative, configurable offline defaults, with hard cache/flight caps."""

    page_size: int = 500
    max_pages: int = 40
    max_rows: int = 20_000
    max_page_rows: int = 1_000
    max_page_bytes: int = 2_000_000
    max_result_bytes: int = 8_000_000
    max_key_bytes: int = 16_384
    retries_per_page: int = 2
    request_timeout: float = 10.0
    cache_ttl: float = 60.0
    cooldown_ttl: float = 5.0
    cache_entries: int = 32
    max_inflight: int = 32

    def __post_init__(self) -> None:
        for name in (
            "page_size",
            "max_pages",
            "max_rows",
            "max_page_rows",
            "max_page_bytes",
            "max_result_bytes",
            "max_key_bytes",
            "cache_entries",
            "max_inflight",
        ):
            _positive_int(getattr(self, name), name)
        if self.page_size > self.max_page_rows:
            raise ValueError("page_size exceeds max_page_rows")
        if self.cache_entries > 32 or self.max_inflight > 32:
            raise ValueError("cache_entries and max_inflight cannot exceed 32")
        if type(self.retries_per_page) is not int or self.retries_per_page < 0:
            raise ValueError("retries_per_page must be a nonnegative nonbool int")
        _seconds(self.request_timeout, "request_timeout")
        _seconds(self.cache_ttl, "cache_ttl", allow_zero=True)
        _seconds(self.cooldown_ttl, "cooldown_ttl", allow_zero=True)


@dataclass(frozen=True)
class PagingResult:
    coverage: Coverage
    reason: str
    rows: list[dict[str, object]]
    attempts_consumed: int
    producer_attempts: int
    source: Source = "producer"
    pages_received: int = 0
    expected_count: int | None = None
    expected_pages: int | None = None
    detail: str = ""


@dataclass
class _Flight:
    done: threading.Event = field(default_factory=threading.Event)
    result: PagingResult | None = None


@dataclass
class _Entry:
    expires: float
    result: PagingResult


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _wait_event(event: threading.Event, timeout: float) -> bool:
    """Small test seam for deterministic deadline tests, not a polling loop."""
    return event.wait(timeout)


class PagingKernel:
    def __init__(
        self,
        transport: Transport,
        *,
        policy: PagingPolicy | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self._transport = transport
        self.policy = policy or PagingPolicy()
        self._clock = clock
        self._lock = threading.Lock()
        self._flights: dict[bytes, _Flight] = {}
        self._entries: OrderedDict[bytes, _Entry] = OrderedDict()

    def fetch(
        self,
        params: Mapping[str, object],
        *,
        sort_params: Mapping[str, object],
        filter_params: Mapping[str, object],
        budget: Budget,
    ) -> PagingResult:
        """Fetch pages in caller-supplied stable order; never infer expressions.

        The three mappings must have disjoint string keys; pageNumber/pageSize
        belong to the kernel. Sort mapping must be nonempty. The caller must
        actually supply a stable tie-breaker; this generic kernel cannot verify
        the meaning of provider-specific expressions or snapshot consistency.
        Parameter values must be JSON-compatible. Caller input is never mutated.
        """
        if not sort_params:
            raise ValueError("explicit stable sort_params are required")
        merged: dict[str, object] = {}
        for group in (params, sort_params, filter_params):
            for key, value in group.items():
                if not isinstance(key, str) or key in merged or key in ("pageNumber", "pageSize"):
                    raise ValueError(
                        "parameter keys must be disjoint strings, excluding paging keys"
                    )
                merged[key] = value
        merged["pageSize"] = self.policy.page_size
        try:
            key = _json(merged)
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError("parameters must be finite JSON values") from error
        if len(key) > self.policy.max_key_bytes:
            raise ValueError("canonical parameters exceed max_key_bytes")
        # Snapshot once: nested caller mutations cannot alter later page requests.
        merged = json.loads(key)
        if budget.remaining() <= 0:
            return self._empty("deadline_exceeded")
        with self._lock:
            now = self._clock()
            for old_key in list(self._entries):
                if self._entries[old_key].expires <= now:
                    del self._entries[old_key]
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                source: Source = "cache" if entry.result.coverage == "complete" else "cooldown"
                return self._reuse(entry.result, source, budget)
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                if len(self._flights) >= self.policy.max_inflight:
                    return self._empty("inflight_limit")
                flight = _Flight()
                self._flights[key] = flight
        assert flight is not None
        if not leader:
            if not _wait_event(flight.done, budget.remaining()) or budget.remaining() <= 0:
                return self._empty("waiter_deadline_exceeded", source="shared")
            assert flight.result is not None
            return self._reuse(flight.result, "shared", budget)

        # Publish even on BaseException so a cancelled producer cannot strand waiters.
        result = self._empty("producer_aborted", coverage="fetch_failed")
        try:
            result = self._produce(merged, budget)
            return deepcopy(result)
        finally:
            with self._lock:
                flight.result = result
                ttl = 0.0
                if result.coverage == "complete":
                    ttl = self.policy.cache_ttl
                elif result.reason in ("transport_error", "upstream_error"):
                    ttl = self.policy.cooldown_ttl
                if ttl > 0:
                    self._entries[key] = _Entry(self._clock() + ttl, result)
                    self._entries.move_to_end(key)
                    while len(self._entries) > self.policy.cache_entries:
                        self._entries.popitem(last=False)
                del self._flights[key]
                flight.done.set()

    @staticmethod
    def _empty(
        reason: str, *, coverage: Coverage = "truncated", source: Source = "producer"
    ) -> PagingResult:
        return PagingResult(coverage, reason, [], 0, 0, source=source)

    def _reuse(self, result: PagingResult, source: Source, budget: Budget) -> PagingResult:
        copied = replace(deepcopy(result), attempts_consumed=0, source=source)
        if budget.remaining() <= 0:
            return replace(copied, coverage="truncated", reason="deadline_exceeded")
        return copied

    def _produce(self, params: dict[str, object], budget: Budget) -> PagingResult:
        policy = self.policy
        rows: list[dict[str, object]] = []
        attempts = received = size = 0
        count: int | None = None
        pages: int | None = None
        signatures: set[bytes] = set()

        def finish(coverage: Coverage, reason: str, detail: str = "") -> PagingResult:
            return PagingResult(
                coverage,
                reason,
                rows,
                attempts,
                attempts,
                pages_received=received,
                expected_count=count,
                expected_pages=pages,
                detail=detail[:1024],
            )

        for page in range(1, policy.max_pages + 1):
            if len(rows) >= policy.max_rows:
                return finish("truncated", "row_limit")
            payload: object = None
            for retry in range(policy.retries_per_page + 1):
                stop = budget.stop_reason()
                if stop:
                    return finish("truncated", stop)
                if not _UPSTREAM.acquire(timeout=budget.remaining()):
                    return finish("truncated", "deadline_exceeded")
                error_reason = detail = ""
                try:
                    timeout, stop = budget.claim(policy.request_timeout)
                    if stop:
                        return finish("truncated", stop)
                    attempts += 1
                    try:
                        payload = self._transport({**deepcopy(params), "pageNumber": page}, timeout)
                    except Exception as error:  # noqa: BLE001 - injected transport boundary
                        error_reason, detail = "transport_error", str(error)
                finally:
                    _UPSTREAM.release()
                if (
                    not error_reason
                    and isinstance(payload, dict)
                    and payload.get("success") is False
                ):
                    error_reason = "upstream_error"
                    detail = f"code={payload.get('code')}; message={payload.get('message')}"
                if not error_reason:
                    break
                if budget.remaining() <= 0:
                    return finish("truncated", "deadline_exceeded", detail)
                if retry == policy.retries_per_page:
                    return finish("fetch_failed", error_reason, detail)
                # Immediate bounded retries; no sleeps, hidden attempts or autonomous backoff.

            if not isinstance(payload, dict) or payload.get("success") is not True:
                return finish("unknown", "invalid_envelope")
            result = payload.get("result")
            if not isinstance(result, dict):
                return finish("unknown", "invalid_result")
            data = result.get("data")
            if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
                return finish("unknown", "invalid_rows")
            if len(data) > policy.max_page_rows:
                return finish("truncated", "page_row_limit")
            try:
                encoded = [_json(row) for row in data]
            except (ValueError, TypeError, RecursionError):
                return finish("unknown", "invalid_row_values")
            page_bytes = sum(len(row) for row in encoded)
            if page_bytes > policy.max_page_bytes:
                return finish("truncated", "page_byte_limit")
            # Sorted row digests detect an upstream ignoring paging and reordering.
            signature = hashlib.sha256(
                b"".join(sorted(hashlib.sha256(r).digest() for r in encoded))
            ).digest()
            if data and signature in signatures:
                return finish("unknown", "repeated_page")
            if data:
                signatures.add(signature)
            # Keep the bounded prefix even when this page cannot be fully retained.
            for row, raw in zip(data, encoded, strict=True):
                if len(rows) >= policy.max_rows:
                    return finish("truncated", "row_limit")
                if size + len(raw) > policy.max_result_bytes:
                    return finish("truncated", "result_byte_limit")
                rows.append(deepcopy(row))
                size += len(raw)
            received += 1
            # Keep rows from a late response but never certify them as complete.
            if budget.remaining() <= 0:
                return finish("truncated", "deadline_exceeded")
            new_count, new_pages = result.get("count"), result.get("pages")
            if type(new_count) is not int or type(new_pages) is not int:
                return finish("unknown", "invalid_metadata")
            if new_count < 0 or new_pages < 0:
                return finish("unknown", "invalid_metadata")
            if count is not None and (new_count != count or new_pages != pages):
                return finish("unknown", "metadata_drift")
            count, pages = new_count, new_pages
            if "pageNumber" in result:
                echo = result["pageNumber"]
                if type(echo) is not int or echo != page:
                    return finish("unknown", "page_not_advancing")
            if count == 0 and not data:
                return finish("unknown", "empty_unverified")
            if (
                count == 0
                or pages == 0
                or pages != (count + policy.page_size - 1) // policy.page_size
            ):
                return finish("unknown", "inconsistent_metadata")
            expected = min(policy.page_size, count - (page - 1) * policy.page_size)
            if len(data) != expected:
                return finish("unknown", "missing_page" if not data else "page_length_mismatch")
            if page == pages:
                if len(rows) != count:
                    return finish("unknown", "count_mismatch")
                return finish("complete", "all_pages_validated")
        return finish("truncated", "page_limit")


__all__ = ["Budget", "PagingKernel", "PagingPolicy", "PagingResult"]
