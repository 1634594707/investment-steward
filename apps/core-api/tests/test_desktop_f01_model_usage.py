"""F01（桌面端升级路线图 2026-09-18）：模型用量落表与 /model-usage 聚合的契约测试。

覆盖：usage 解析（缺失记 null，不按字数反推）、thinking-disabled 重试的 retried 标记与
token 相加、失败/超时也落 outcome、GET /model-usage?window=7d|30d 按方案聚合与窗口过滤。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from investment_steward_core import model_client as mc
from investment_steward_core.domain.models import ModelProfile
from investment_steward_core.storage.database import Database


def _profile() -> ModelProfile:
    return ModelProfile(
        profile_id=uuid4(),
        name="测试方案",
        base_url="https://model.example",
        model="test-model",
        credential_ref="key_id",
    )


def _store_with_key():
    class _Store:
        def get(self, key: str):
            return "sk-test"

    return _Store()


_DEFAULT_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
_NO_USAGE = object()


def _payload(content: str = "ok", *, usage: object = _NO_USAGE) -> dict:
    payload = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
    }
    if usage is not _NO_USAGE:
        payload["usage"] = usage
    return payload


def test_usage_parsed_into_reply(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.sqlite3")
    records: list[dict] = []
    mc.set_model_call_recorder(lambda record: records.append(record) or db.record_model_call(**record))
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: _payload(usage=_DEFAULT_USAGE))
    reply = mc.call_active_model(_profile(), _store_with_key(), [{"role": "user", "content": "hi"}], purpose="研报")
    assert (reply.prompt_tokens, reply.completion_tokens, reply.total_tokens) == (10, 5, 15)
    assert reply.retried is False and reply.purpose == "研报"
    # 落表：一条 ok 记录，token 三项一致。
    assert len(records) == 1 and records[0]["outcome"] == "ok"
    assert records[0]["total_tokens"] == 15
    mc.set_model_call_recorder(None)


def test_missing_usage_recorded_as_null(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.sqlite3")
    records: list[dict] = []
    mc.set_model_call_recorder(lambda record: records.append(record) or db.record_model_call(**record))
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: _payload(usage=None))
    reply = mc.call_active_model(_profile(), _store_with_key(), [{"role": "user", "content": "hi"}])
    assert reply.prompt_tokens is None and reply.total_tokens is None, "usage 缺失记 null，不按字数反推"
    assert records[0]["prompt_tokens"] is None
    summary = db.summarize_model_usage(7)
    assert summary[0]["usage_missing"] == 1, "聚合里如实给出未返回用量的次数"
    mc.set_model_call_recorder(None)


def test_retry_flag_and_token_sum(tmp_path, monkeypatch):
    """finish_reason=length → thinking-disabled 重试：retried=True，token 为两次之和。"""
    db = Database(tmp_path / "s.sqlite3")
    records: list[dict] = []
    mc.set_model_call_recorder(lambda record: records.append(record) or db.record_model_call(**record))
    calls = {"n": 0}

    def _fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
        return _payload(usage={"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50})

    monkeypatch.setattr(mc, "_post_json", _fake_post)
    reply = mc.call_active_model(_profile(), _store_with_key(), [{"role": "user", "content": "hi"}])
    assert reply.retried is True
    assert reply.prompt_tokens == 120 and reply.completion_tokens == 80 and reply.total_tokens == 200, "两次计费请求 token 相加"
    assert records[0]["retried"] is True and records[0]["total_tokens"] == 200
    mc.set_model_call_recorder(None)


def test_failure_recorded_with_outcome(tmp_path, monkeypatch):
    db = Database(tmp_path / "s.sqlite3")
    records: list[dict] = []
    mc.set_model_call_recorder(lambda record: records.append(record) or db.record_model_call(**record))
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("boom")))
    try:
        mc.call_active_model(_profile(), _store_with_key(), [{"role": "user", "content": "hi"}])
    except mc.ModelUnavailable:
        pass
    assert len(records) == 1 and records[0]["outcome"] == "timeout"
    assert records[0]["prompt_tokens"] is None
    mc.set_model_call_recorder(None)


def test_model_usage_endpoint_window_and_aggregation(tmp_path):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings

    token = "f01-usage-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    http = TestClient(app)
    headers = {"X-Core-Session-Token": token}
    db = http.app.state.core.database

    def seed(profile_id: str, model: str, outcome: str, tokens: int | None, days_ago: int, retried: bool = False) -> None:
        db.record_model_call(
            profile_id=profile_id,
            model=model,
            purpose="研报",
            prompt_tokens=tokens,
            completion_tokens=tokens,
            total_tokens=tokens,
            latency_ms=500,
            retried=retried,
            outcome=outcome,
        )
        # record 用 now；把 40 天前那条直接拨回（仅本测试验证窗口过滤）。
        if days_ago:
            with sqlite3.connect(db.path) as raw:
                raw.execute("UPDATE model_calls SET created_at = ? WHERE rowid = (SELECT MAX(rowid) FROM model_calls)",
                            ((datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),))
                raw.commit()

    import sqlite3

    pid = str(uuid4())
    seed(pid, "model-a", "ok", 100, 0)
    seed(pid, "model-a", "error", None, 0)
    seed(pid, "model-a", "ok", 999, 40)  # 30 天窗外

    inside = http.get("/model-usage", params={"window": "7d"}, headers=headers).json()
    assert inside["ok"] is True and inside["window"] == "7d"
    rows = inside["summaries"]
    assert len(rows) == 1 and rows[0]["model"] == "model-a"
    assert rows[0]["calls"] == 2 and rows[0]["ok_calls"] == 1 and rows[0]["errors"] == 1
    assert rows[0]["prompt_tokens"] == 100 and rows[0]["usage_missing"] == 1, "缺失用量计入 usage_missing，不猜 0"

    month = http.get("/model-usage", params={"window": "30d"}, headers=headers).json()
    month_row = month["summaries"][0]
    assert month_row["calls"] == 2, "40 天前的记录在 30 天窗口之外"

    bad = http.get("/model-usage", params={"window": "90d"}, headers=headers)
    assert bad.status_code == 422
