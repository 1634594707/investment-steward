"""持续有字节到达也必须遵守截止时间，并关闭底层连接。"""
import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
from investment_steward_core import model_client as mc
from investment_steward_core.domain import ModelProfile


def test_slow_body_is_cancelled_and_connection_closed():
    disconnected = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                disconnected.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    started = time.monotonic()
    try:
        with pytest.raises(mc.ModelUnavailable) as caught:
            mc.probe_completion(f"http://127.0.0.1:{server.server_port}/v1", "test", "key", timeout=0.4)
        assert caught.value.error_kind == "timeout"
        assert time.monotonic() - started < 1.5
        assert disconnected.wait(1), "截止后仍在后台接收响应，会泄漏连接与配额"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(1)


@pytest.mark.parametrize("status", [200, 403, 429])
def test_async_transport_preserves_request_and_error_classification(monkeypatch, status):
    client_class = httpx.AsyncClient

    def handle(request):
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["User-Agent"] == mc.MODEL_USER_AGENT
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(status, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(mc.httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(handle),
    ))
    if status == 200:
        assert mc.probe_completion("https://example/v1", "m", "test-key", timeout=2).content == "ok"
    else:
        monkeypatch.setattr(mc.time, "sleep", lambda seconds: None)
        with pytest.raises(mc.ModelUnavailable) as caught:
            mc.probe_completion("https://example/v1", "m", "test-key", timeout=2)
        assert caught.value.http_status == status


@pytest.mark.parametrize("failure", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout])
def test_transport_timeout_is_unknown_cost_and_never_resubmitted(monkeypatch, failure):
    """连接/读取/池等待超时都没有可用用量，不自动重发可能已接收的生成。"""
    calls, records = [], []
    client_class = httpx.AsyncClient

    def handle(request):
        calls.append(request)
        raise failure("private transport detail", request=request)

    monkeypatch.setattr(mc.httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(mc, "resolve_credential", lambda *args: "synthetic-key")
    monkeypatch.setattr(mc, "_model_call_recorder", records.append)
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example.invalid/v1",
                           model="synthetic", credential_ref="unused")
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc.call_active_model(profile, object(), [], timeout=1)
    assert caught.value.error_kind == "timeout"
    assert "private transport detail" not in str(caught.value)
    assert len(calls) == 1 and caught.value.retried is False
    assert records[0]["attempt_count"] == 1 and records[0]["outcome"] == "timeout"
    assert records[0]["total_tokens"] is None and records[0]["usage_complete"] is False


def test_total_deadline_cancels_waiting_transport(monkeypatch):
    """即使还没有响应头，外层截止也必须取消等待并关闭客户端。"""
    cancelled, closed = [], []
    client_class = httpx.AsyncClient

    async def handle(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    class Client(client_class):
        async def aclose(self):
            closed.append(True)
            await super().aclose()

        async def __aexit__(self, *args):
            closed.append(True)
            return await super().__aexit__(*args)

    monkeypatch.setattr(mc.httpx, "AsyncClient", lambda **kwargs: Client(
        **kwargs, transport=httpx.MockTransport(handle)))
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc.probe_completion("https://example.invalid/v1", "synthetic", "key", timeout=0.2)
    assert caught.value.error_kind == "timeout"
    assert cancelled and closed


def test_deadline_after_retry_wait_does_not_claim_unsent_second_request(monkeypatch):
    """等待结束时预算耗尽，没有第二个请求就不能宣称已重试/再次计费。"""
    clock, calls, records = [0.0], [], []
    monkeypatch.setattr(mc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mc.time, "sleep", lambda seconds: clock.__setitem__(0, 31.0))
    monkeypatch.setattr(mc, "resolve_credential", lambda *args: "key")
    monkeypatch.setattr(mc, "_model_call_recorder", records.append)

    def post(*args):
        calls.append(args)
        raise mc.ModelUnavailable("limited", error_kind="rate_limit", http_status=429, retry_after=1)

    monkeypatch.setattr(mc, "_post_json", post)
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example.invalid/v1",
                           model="synthetic", credential_ref="unused")
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc.call_active_model(profile, object(), [], timeout=30)
    assert len(calls) == 1
    assert caught.value.error_kind == "timeout"
    assert caught.value.retried is False
    assert records[0]["retried"] is False and records[0]["attempt_count"] == 1
