"""P0-A：失败应指导修复，不能泄漏正文或让一次失败重复计数。"""
import io
import urllib.error
from uuid import uuid4

import pytest
from investment_steward_core import model_client as mc
from investment_steward_core.domain import ModelProfile
from investment_steward_core.storage.database import Database


def test_non_json_success_explains_protocol_without_echoing_body(monkeypatch):
    class Response(io.BytesIO):
        status = 200

        @property
        def headers(self):
            return {"Content-Type": "text/event-stream"}

    monkeypatch.setattr(mc, "_completion_urlopen", lambda *a, **k: Response(b'data: secret-material\n\n'))
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc._post_json("https://example/v1", {}, "secret-key", 30)
    assert caught.value.http_status == 200
    assert caught.value.error_kind == "protocol"
    assert "text/event-stream" in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("status,kind", [(400, "request"), (401, "auth"), (403, "auth"), (429, "rate_limit"), (503, "upstream")])
def test_http_error_classification_does_not_retry_paid_calls(monkeypatch, status, kind):
    calls = []

    def fail(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, status, "secret", {}, io.BytesIO(b'secret'))

    monkeypatch.setattr(mc, "_completion_urlopen", fail)
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc._post_json("https://example/v1", {}, "secret-key", 30)
    assert caught.value.http_status == status
    assert caught.value.error_kind == kind
    assert str(status) in str(caught.value)
    assert "secret" not in str(caught.value)
    assert len(calls) == 1


def test_missing_credential_records_one_failure(monkeypatch):
    records = []
    monkeypatch.setattr(mc, "_model_call_recorder", records.append)
    monkeypatch.setattr(mc, "resolve_credential", lambda *a: "")
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example/v1", model="m", credential_ref="key")
    with pytest.raises(mc.ModelUnavailable):
        mc.call_active_model(profile, object(), [])
    assert len(records) == 1, "一次失败不能重复计数，否则失败率和调用量失真"
    assert records[0]["attempt_count"] == 0, "未出网不能记为已请求"


def test_empty_response_does_not_echo_model_fields():
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc._extract_content({"choices": [{"message": {"content": "", "secret": "private-evidence"}}]})
    assert "private-evidence" not in str(caught.value)


@pytest.mark.parametrize("content", ['{"valid_json":true}', '{"incomplete":', "partial plain text"])
def test_nonempty_length_stop_is_not_delivered_or_automatically_rebilled(monkeypatch, content):
    calls, records = [], []

    def post(*args):
        calls.append(1)
        return {"choices": [{"message": {"content": content}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}

    monkeypatch.setattr(mc, "_post_json", post)
    monkeypatch.setattr(mc, "resolve_credential", lambda *args: "key")
    monkeypatch.setattr(mc, "_model_call_recorder", records.append)
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example.invalid", model="m", credential_ref="key")
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc.call_active_model(profile, object(), [])
    assert caught.value.error_kind == "output_truncated"
    assert len(calls) == 1, "已返回部分正文不能自动再发一次计费请求"
    assert records[0]["outcome"] == "error" and records[0]["total_tokens"] == 30
    assert records[0]["usage_complete"] is True
    assert records[0]["attempts"][0]["finish_reason"] == "length"
    assert content not in str(caught.value)


def test_connectivity_probe_has_smaller_budget_than_report(monkeypatch):
    payloads = []

    def post(base, payload, key, timeout):
        payloads.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(mc, "_post_json", post)
    mc.probe_completion("https://example/v1", "m", "key", timeout=30)
    assert payloads[0]["max_tokens"] == 512
    assert mc._chat_completion_payload([], "m", 30)["max_tokens"] == 16000


def test_rate_limit_retry_shares_timeout_and_stops_after_two_requests(monkeypatch):
    """限流可恢复，但不能无限请求或每次重新获得完整超时。"""
    clock = [0.0]
    calls = []
    records = []
    monkeypatch.setattr(mc.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mc.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(mc, "resolve_credential", lambda *a: "key")
    monkeypatch.setattr(mc, "_model_call_recorder", records.append)

    def fail(request, timeout):
        calls.append(timeout)
        clock[0] += 2
        raise urllib.error.HTTPError(request.full_url, 429, "limited", {"Retry-After": "3"}, io.BytesIO())

    monkeypatch.setattr(mc, "_completion_urlopen", fail)
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example/v1", model="m", credential_ref="key")
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc.call_active_model(profile, object(), [], timeout=30)
    assert calls == [30, 25]
    assert caught.value.retried is True
    assert len(records) == 1 and records[0]["retried"] is True
    assert records[0]["attempt_count"] == 2


def test_probe_retry_failure_stays_model_unavailable(monkeypatch):
    """关闭思考的第二次请求超时也必须进入统一错误出口。"""
    calls = []

    def post(*a):
        calls.append(a)
        if len(calls) == 1:
            return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        raise TimeoutError("private-server-detail")

    monkeypatch.setattr(mc, "_post_json", post)
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc.probe_completion("https://example/v1", "m", "key", timeout=30)
    assert "超时" in str(caught.value)
    assert "private-server-detail" not in str(caught.value)


def test_saved_connectivity_test_uses_probe_budget(monkeypatch):
    captured = []
    monkeypatch.setattr(mc, "resolve_credential", lambda *a: "key")

    def post(base, payload, key, timeout):
        captured.append(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(mc, "_post_json", post)
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example/v1", model="m", credential_ref="key")
    mc.call_active_model(profile, object(), [], purpose="连通性测试")
    assert captured[0]["max_tokens"] == 512


@pytest.mark.parametrize("status,wait", [(400, "0"), (401, "0"), (403, "0"), (500, "0"), (429, "60"), (503, "invalid")])
def test_non_retryable_or_long_wait_does_not_resubmit(monkeypatch, status, wait):
    """不能绕过等待要求，也不能重试参数、认证与未列入策略的状态。"""
    calls = []

    def fail(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, status, "failed", {"Retry-After": wait}, io.BytesIO())

    monkeypatch.setattr(mc, "_completion_urlopen", fail)
    with pytest.raises(mc.ModelUnavailable):
        mc.probe_completion("https://example/v1", "m", "key", timeout=30)
    assert len(calls) == 1


def test_timeout_does_not_resubmit_unknown_remote_result(monkeypatch):
    calls = []

    def fail(*args):
        calls.append(args)
        raise TimeoutError

    monkeypatch.setattr(mc, "_post_json", fail)
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc.probe_completion("https://example/v1", "m", "key", timeout=30)
    assert len(calls) == 1 and caught.value.error_kind == "timeout"


def test_returned_model_and_diagnostics_reach_database(tmp_path, monkeypatch):
    """网关替换模型时保留请求名与返回名，避免把替换结果冒充原模型。"""
    import sqlite3

    db = Database(tmp_path / "calls.sqlite3")
    monkeypatch.setattr(mc, "_model_call_recorder", lambda record: db.record_model_call(**record))
    monkeypatch.setattr(mc, "resolve_credential", lambda *a: "private-key")
    monkeypatch.setattr(mc, "_post_json", lambda *a: {"model": "actual-model", "choices": [{"message": {"content": "ok"}}]})
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example/v1", model="requested-model", credential_ref="key")
    reply = mc.call_active_model(profile, object(), [])
    assert reply.model == "requested-model" and reply.returned_model == "actual-model"
    with sqlite3.connect(db.path) as connection:
        row = connection.execute("SELECT model, returned_model, attempt_count, error_kind, http_status FROM model_calls").fetchone()
    assert row == ("requested-model", "actual-model", 1, None, None)


def test_protocol_failure_is_persisted_without_body(tmp_path, monkeypatch):
    import sqlite3

    db = Database(tmp_path / "calls.sqlite3")
    monkeypatch.setattr(mc, "_model_call_recorder", lambda record: db.record_model_call(**record))
    monkeypatch.setattr(mc, "resolve_credential", lambda *a: "private-key")

    def fail(*args):
        raise mc.ModelUnavailable("protocol", error_kind="protocol", http_status=200)

    monkeypatch.setattr(mc, "_post_json", fail)
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example/v1", model="m", credential_ref="key")
    with pytest.raises(mc.ModelUnavailable):
        mc.call_active_model(profile, object(), [])
    with sqlite3.connect(db.path) as connection:
        row = connection.execute("SELECT error_kind, http_status, attempt_count, returned_model FROM model_calls").fetchone()
    assert row == ("protocol", 200, 1, None)


def test_version_36_upgrade_preserves_old_calls_and_is_repeatable(tmp_path):
    """旧记录与未知元数据必须原样保留，重启不能重复加列。"""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    db = Database(path)
    db.record_model_call(profile_id=None, model="legacy", purpose=None, prompt_tokens=None,
                         completion_tokens=None, total_tokens=None, latency_ms=10, retried=True, outcome="error")
    db.engine.dispose()
    with sqlite3.connect(path) as connection:
        for column in ("returned_model", "attempt_count", "error_kind", "http_status", "usage_complete", "attempts"):
            connection.execute(f"ALTER TABLE model_calls DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 36")
        before = connection.execute("SELECT * FROM model_calls").fetchone()
    first = Database(path)
    first.engine.dispose()
    second = Database(path)
    second.engine.dispose()
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT * FROM model_calls").fetchone()
        assert row[:len(before)] == before
        assert row[len(before):] == (None, None, None, None, None, None)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 40


def test_retry_with_missing_usage_is_not_presented_as_full_cost(tmp_path, monkeypatch):
    """保留已知用量，但第二次缺用量时必须明确整次调用成本不完整。"""
    import sqlite3

    db = Database(tmp_path / "cost.sqlite3")
    monkeypatch.setattr(mc, "_model_call_recorder", lambda record: db.record_model_call(**record))
    monkeypatch.setattr(mc, "resolve_credential", lambda *a: "key")
    responses = iter([
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}},
        {"choices": [{"message": {"content": "ok"}}]},
    ])
    monkeypatch.setattr(mc, "_post_json", lambda *a: next(responses))
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example/v1", model="m", credential_ref="key")
    reply = mc.call_active_model(profile, object(), [])
    assert reply.total_tokens == 30 and reply.usage_complete is False
    with sqlite3.connect(db.path) as connection:
        assert connection.execute("SELECT total_tokens, usage_complete FROM model_calls").fetchone() == (30, 0)
    assert db.summarize_model_usage(7)[0]["usage_missing"] == 1


def test_late_result_is_not_accepted_as_on_time_success(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(mc.time, "monotonic", lambda: clock[0])

    def post(*args):
        clock[0] = 31.0
        return {"choices": [{"message": {"content": "late"}}]}

    monkeypatch.setattr(mc, "_post_json", post)
    with pytest.raises(mc.ModelUnavailable) as caught:
        mc.probe_completion("https://example/v1", "m", "key", timeout=30)
    assert caught.value.error_kind == "timeout"


@pytest.mark.parametrize("value", [True, -1, "10", 1.5])
def test_invalid_token_counts_are_unknown_instead_of_cost(value):
    assert mc._extract_usage({"usage": {"prompt_tokens": value}})[0] is None


def test_version_37_upgrade_preserves_diagnostics(tmp_path):
    import sqlite3

    path = tmp_path / "v37.sqlite3"
    db = Database(path)
    db.record_model_call(profile_id=None, model="m", purpose=None, prompt_tokens=1,
                         completion_tokens=2, total_tokens=3, latency_ms=10, retried=False, outcome="ok",
                         returned_model="actual", attempt_count=1)
    db.engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE model_calls DROP COLUMN usage_complete")
        connection.execute("ALTER TABLE model_calls DROP COLUMN attempts")
        connection.execute("PRAGMA user_version = 37")
        before = connection.execute("SELECT * FROM model_calls").fetchone()
    upgraded = Database(path)
    upgraded.engine.dispose()
    with sqlite3.connect(path) as connection:
        after = connection.execute("SELECT * FROM model_calls").fetchone()
    assert after[:-2] == before and after[-2:] == (None, None)


def test_each_attempt_keeps_safe_usage_and_failure_details(tmp_path, monkeypatch):
    db = Database(tmp_path / "attempts.sqlite3")
    monkeypatch.setattr(mc, "_model_call_recorder", lambda record: db.record_model_call(**record))
    monkeypatch.setattr(mc, "resolve_credential", lambda *a: "private-key")
    count = [0]

    def post(*args):
        count[0] += 1
        if count[0] == 1:
            return {"model": "reported", "choices": [{"message": {"content": "", "private": "secret"}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
        raise TimeoutError("private-server-error")

    monkeypatch.setattr(mc, "_post_json", post)
    profile = ModelProfile(profile_id=uuid4(), name="test", base_url="https://example/v1", model="m", credential_ref="key")
    with pytest.raises(mc.ModelUnavailable):
        mc.call_active_model(profile, object(), [{"role": "user", "content": "private-question"}])
    attempts = db.list_recent_model_calls(7)[0]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["error_kind"] == "reasoning_budget" and attempts[0]["total_tokens"] == 5
    assert attempts[1]["error_kind"] == "timeout" and attempts[1]["thinking_disabled"] is True
    assert all(a["latency_ms"] >= 0 for a in attempts)
    assert "private" not in repr(attempts) and "secret" not in repr(attempts)
