"""Single-attempt Eastmoney transport for the provisional Y2 paging kernel.

Not wired to production routes. No retry, redirect, proxy, credentials, fallback,
or calendar/period inference. The caller must run it inside PagingKernel so the
process-wide semaphore and shared request budget apply. Fixed existing source
only; this does not grant plugin permissions or validate provider semantics.

Socket operations use remaining time and body reads are bounded. DNS resolution
cannot be forcibly interrupted by stdlib socket timeouts; a late operation is
rejected, not advertised as a hard wall-clock cancellation guarantee.
"""

from __future__ import annotations

import http.client
import json
import math
import time
from urllib.parse import urlencode

_HOST = "datacenter-web.eastmoney.com"
_PATH = "/api/data/v1/get"
MAX_BODY_BYTES = 2_000_000
MAX_QUERY_BYTES = 16_384


class TransportError(RuntimeError):
    """Safe technical reason; never includes response text or query values."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def eastmoney_once(params: dict[str, object], timeout: float) -> object:
    """Perform at most one HTTPS GET; every retry belongs to PagingKernel.

    Returns parsed JSON only, not a claim about complete source coverage. Rejects
    non-200 status (including redirects), encoded bodies and oversized payloads.
    Connections always close. Zero requests on invalid local input.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a finite positive number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    if not isinstance(params, dict) or any(
        not isinstance(key, str) or type(value) not in (str, int) for key, value in params.items()
    ):
        raise ValueError("query values must be explicit strings or integers")
    query = urlencode(params)
    if len(query.encode("ascii")) > MAX_QUERY_BYTES:
        raise ValueError("query_byte_limit")
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TransportError("deadline_exceeded")
        return value

    connection = http.client.HTTPSConnection(_HOST, timeout=remaining())
    response = None
    try:
        connection.request(
            "GET",
            f"{_PATH}?{query}",
            headers={
                "User-Agent": "InvestmentSteward/0.1",
                "Referer": "https://data.eastmoney.com/",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        # HTTPConnection may detach its socket for Connection: close while the
        # response still owns the readable stream. Retain it for timeout updates.
        sock = connection.sock
        if sock is not None:
            sock.settimeout(remaining())
        response = connection.getresponse()
        remaining()
        if response.status != 200:
            raise TransportError(f"http_status_{response.status}")
        encoding = response.getheader("Content-Encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise TransportError("unsupported_content_encoding")
        length = response.getheader("Content-Length")
        if length is not None:
            if not length.isascii() or not length.isdigit():
                raise TransportError("invalid_content_length")
            if int(length) > MAX_BODY_BYTES:
                raise TransportError("response_byte_limit")
        body = bytearray()
        while True:
            left = remaining()
            if sock is not None:
                # http.client may close the detached socket once the full body
                # has been read; a then-failing settimeout must not abort a
                # completed read. The deadline is still enforced by remaining().
                try:
                    sock.settimeout(left)
                except OSError:
                    sock = None
            # read1 does not deliberately fill the entire buffer across many
            # slow socket reads; check the deadline between chunks as well.
            chunk = response.read1(min(65_536, MAX_BODY_BYTES + 1 - len(body)))
            remaining()
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_BODY_BYTES:
                raise TransportError("response_byte_limit")
        if length is not None and len(body) != int(length):
            raise TransportError("content_length_mismatch")
        payload = json.loads(
            body.decode("utf-8-sig"),
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        remaining()
        return payload
    except TransportError:
        raise
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as error:
        raise TransportError("transport_or_payload_error") from error
    finally:
        if response is not None:
            response.close()
        connection.close()
