"""Synthetic network-boundary tests; no actual upstream requests or sleeps."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from investment_steward_core import youzi_transport as transport
from investment_steward_core.youzi_paging import Budget, PagingKernel, PagingPolicy


class Response:
    def __init__(self, body=b'{"success":true}', *, status=200, headers=None, on_read=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.on_read = on_read
        self.reads = []
        self.closed = False

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        self.reads.append(size)
        if self.on_read:
            self.on_read()
        data, self.body = self.body[:size], self.body[size:]
        return data

    def close(self):
        self.closed = True


class Socket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


@pytest.fixture
def wire(monkeypatch):
    calls = []
    responses = []
    clock = [0.0]
    monkeypatch.setattr(transport.time, "monotonic", lambda: clock[0])

    class Connection:
        def __init__(self, host, timeout):
            self.host, self.timeout = host, timeout
            self.saved_socket = self.sock = Socket()
            self.closed = False
            self.requests = []
            calls.append(self)

        def request(self, method, path, headers):
            self.requests.append((method, path, headers))

        def getresponse(self):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            self.sock = None  # normal Connection: close detachment
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    return calls, responses, clock


def test_exactly_one_fixed_host_get_and_no_parameter_mutation(wire):
    calls, responses, _ = wire
    response = Response()
    responses.append(response)
    params = {"reportName": "OFFLINE", "filter": "中文 & x=1", "pageNumber": 2}
    before = dict(params)
    assert transport.eastmoney_once(params, 2) == {"success": True}
    assert params == before
    assert len(calls) == len(calls[0].requests) == 1
    assert calls[0].host == "datacenter-web.eastmoney.com"
    method, path, headers = calls[0].requests[0]
    assert method == "GET" and path.startswith("/api/data/v1/get?")
    assert parse_qs(urlsplit(path).query)["filter"] == [params["filter"]]
    assert headers["Accept-Encoding"] == "identity"
    assert calls[0].timeout == 2
    assert calls[0].closed and response.closed
    assert calls[0].saved_socket.timeouts


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 400, 429, 500, 503])
def test_status_never_redirects_or_retries(wire, status):
    calls, responses, _ = wire
    response = Response(status=status, headers={"Location": "https://example.invalid/"})
    responses.append(response)
    with pytest.raises(transport.TransportError, match=f"http_status_{status}"):
        transport.eastmoney_once({}, 2)
    assert len(calls) == 1 and len(calls[0].requests) == 1
    assert response.reads == [] and response.closed and calls[0].closed


@pytest.mark.parametrize("value", [None, True, 0, -1, float("nan"), float("inf"), "2"])
def test_invalid_timeout_never_connects(wire, value):
    with pytest.raises((ValueError, TypeError)):
        transport.eastmoney_once({}, value)
    assert wire[0] == []


@pytest.mark.parametrize(
    "params", [{"x": True}, {"x": None}, {"x": []}, {1: "x"}, {"x": "a" * 16385}]
)
def test_invalid_query_never_connects(wire, params):
    with pytest.raises(ValueError):
        transport.eastmoney_once(params, 1)
    assert wire[0] == []


@pytest.mark.parametrize(
    "headers,reason",
    [
        ({"Content-Encoding": "gzip"}, "unsupported_content_encoding"),
        ({"Content-Length": "2000001"}, "response_byte_limit"),
        ({"Content-Length": "-1"}, "invalid_content_length"),
        ({"Content-Length": "NaN"}, "invalid_content_length"),
        ({"Content-Length": "４"}, "invalid_content_length"),
    ],
)
def test_headers_rejected_before_body(wire, headers, reason):
    response = Response(headers=headers)
    wire[1].append(response)
    with pytest.raises(transport.TransportError, match=reason):
        transport.eastmoney_once({}, 2)
    assert response.reads == [] and response.closed and wire[0][0].closed


def test_unknown_length_body_is_bounded(wire, monkeypatch):
    monkeypatch.setattr(transport, "MAX_BODY_BYTES", 8)
    response = Response(b"x" * 100)
    wire[1].append(response)
    with pytest.raises(transport.TransportError, match="response_byte_limit"):
        transport.eastmoney_once({}, 2)
    assert sum(response.reads) == 9
    assert len(response.body) == 91 and response.closed


def test_body_at_limit_and_bom_are_accepted(wire, monkeypatch):
    body = bytes([239, 187, 191]) + b'{"v":0}'
    monkeypatch.setattr(transport, "MAX_BODY_BYTES", len(body))
    wire[1].append(Response(body, headers={"Content-Length": str(len(body))}))
    assert transport.eastmoney_once({}, 2) == {"v": 0}


@pytest.mark.parametrize(
    "body", [b"not json", b"\xff", b'{"v":NaN}', b'{"v":Infinity}', b'{"v":1e999}']
)
def test_bad_payload_fails_closed_without_echo(wire, body):
    response = Response(body)
    wire[1].append(response)
    with pytest.raises(transport.TransportError, match="^transport_or_payload_error$"):
        transport.eastmoney_once({}, 2)
    assert response.closed and wire[0][0].closed


def test_short_body_is_not_accepted(wire):
    wire[1].append(Response(b"{}", headers={"Content-Length": "3"}))
    with pytest.raises(transport.TransportError, match="content_length_mismatch"):
        transport.eastmoney_once({}, 2)


def test_late_read_closes_response_and_updates_detached_socket(wire):
    calls, responses, clock = wire
    response = Response(on_read=lambda: clock.__setitem__(0, 3.0))
    responses.append(response)
    with pytest.raises(transport.TransportError, match="deadline_exceeded"):
        transport.eastmoney_once({}, 2)
    assert response.closed and calls[0].closed
    assert calls[0].saved_socket.timeouts == [2, 2]


def test_settimeout_failure_on_closing_socket_does_not_abort_completed_read(wire, monkeypatch):
    calls, responses, _ = wire

    class DyingSocket(Socket):
        def __init__(self):
            super().__init__()
            self.remaining_calls = 1

        def settimeout(self, value):
            if self.remaining_calls <= 0:
                raise OSError(10038, "operation on non-socket")
            self.remaining_calls -= 1
            super().settimeout(value)

    socket = DyingSocket()

    real_https = transport.http.client.HTTPSConnection

    class Conn(real_https):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sock = self.saved_socket = socket

    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Conn)
    body = b'{"success":true}'
    response = Response(body, headers={"Content-Length": str(len(body))})
    responses.append(response)
    assert transport.eastmoney_once({}, 2) == {"success": True}
    assert response.closed and socket.timeouts == [2]
    assert calls[0].closed


def test_socket_error_does_not_retry_or_expose_details(wire):
    wire[1].append(OSError("secret query value"))
    with pytest.raises(transport.TransportError, match="^transport_or_payload_error$"):
        transport.eastmoney_once({}, 2)
    assert len(wire[0]) == 1 and wire[0][0].closed


def test_kernel_owns_retries_and_budget_across_two_pages(wire):
    calls, responses, clock = wire

    def page(number):
        return Response(
            json.dumps(
                {
                    "success": True,
                    "result": {
                        "data": [{"id": number}],
                        "count": 2,
                        "pages": 2,
                        "pageNumber": number,
                    },
                }
            ).encode()
        )

    responses.extend([Response(status=503), page(1), page(2)])
    budget = Budget(max_attempts=3, clock=lambda: clock[0])
    kernel = PagingKernel(transport.eastmoney_once, policy=PagingPolicy(page_size=1))
    result = kernel.fetch(
        {"reportName": "OFFLINE"},
        sort_params={"sortColumns": "id"},
        filter_params={},
        budget=budget,
    )
    assert result.coverage == "complete" and result.rows == [{"id": 1}, {"id": 2}]
    assert budget.attempts == result.attempts_consumed == len(calls) == 3
    assert all(c.closed and len(c.requests) == 1 for c in calls)


def test_kernel_exhaustion_does_not_make_hidden_fourth_request(wire):
    calls, responses, clock = wire
    responses.extend([Response(status=503), Response(status=503)])
    budget = Budget(max_attempts=2, clock=lambda: clock[0])
    kernel = PagingKernel(transport.eastmoney_once)
    result = kernel.fetch({}, sort_params={"sortColumns": "id"}, filter_params={}, budget=budget)
    assert result.coverage == "truncated" and result.reason == "attempt_budget_exhausted"
    assert budget.attempts == len(calls) == 2
