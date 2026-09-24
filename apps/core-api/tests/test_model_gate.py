"""阶段 A·B 契约测试：模型调用器（model_client）+ 回答闸门（response_gate）。

全部注入式：不给真实网络发请求，验证请求形状 / 凭据解析 / 不可用降级 / 引用真实性 / JSON 抽取。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from investment_steward_core import model_client, response_gate
from investment_steward_core.domain import (
    AgentResponse,
    Evidence,
    EvidenceRelation,
    EvidenceStatus,
    EvidenceType,
    ModelProfile,
    ModelProfileStatus,
)

P2 = "22222222-2222-2222-2222-222222222222"
P1 = "11111111-1111-1111-1111-111111111111"
FAKE = "88888888-8888-8888-8888-888888888888"
fresh_uuid = uuid4


def _profile(credential_ref: str = "k1", status: ModelProfileStatus = ModelProfileStatus.ACTIVE) -> ModelProfile:
    return ModelProfile(
        profile_id=uuid4(),
        name="test-model",
        base_url="http://127.0.0.1:9999/v1",
        model="test-model-small",
        credential_ref=credential_ref,
        status=status,
    )


class _Store:
    def __init__(self, secrets: dict[str, str]):
        self._secrets = secrets

    def get(self, key_id: str) -> str | None:
        return self._secrets.get(key_id)


def _mk_ev(ev_id: UUID) -> Evidence:
    return Evidence(
        evidence_id=ev_id,
        tenant_id=uuid4(),
        subject_refs=["instrument:510300"],
        evidence_type=EvidenceType.ANNOUNCEMENT,
        source_name="测试源",
        summary="一条测试证据",
        content_hash="h" * 16,
        relation=EvidenceRelation.SUPPORTING,
        status=EvidenceStatus.ACTIVE,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _known_evidence() -> list[Evidence]:
    return [_mk_ev(UUID(P1)), _mk_ev(UUID(P2))]


# ---------- A1 / A4：model_client ----------


def test_active_model_profile_returns_single_active():
    profiles = [_profile(status=ModelProfileStatus.INACTIVE), _profile(status=ModelProfileStatus.ACTIVE)]
    assert model_client.active_model_profile(profiles) is profiles[1]


def test_active_model_profile_none_when_no_active():
    profiles = [_profile(status=ModelProfileStatus.INACTIVE)]
    assert model_client.active_model_profile(profiles) is None


def test_resolve_credential_missing_returns_empty():
    assert model_client.resolve_credential(_Store({}), "missing") == ""


def test_resolve_credential_returns_secret():
    assert model_client.resolve_credential(_Store({"api": "sk-abc"}), "api") == "sk-abc"


def test_call_forwards_credentials_and_shape(monkeypatch):
    captured: dict = {}

    def fake_post(base_url, json_payload, api_key, timeout):
        captured.update(base_url=base_url, json_payload=json_payload, api_key=api_key, timeout=timeout)
        return {"choices": [{"message": {"content": "你好"}}]}

    monkeypatch.setattr(model_client, "_post_json", fake_post)
    reply = model_client.call_active_model(_profile(), _Store({"k1": "sk-secret"}), [{"role": "user", "content": "hi"}])
    assert reply.content == "你好"
    assert reply.model == "test-model-small"
    assert captured["api_key"] == "sk-secret"
    assert captured["json_payload"]["model"] == "test-model-small"
    assert captured["json_payload"]["stream"] is False


def test_call_raises_when_credential_missing(monkeypatch):
    from investment_steward_core.model_client import ModelUnavailable

    def _never(*_a, **_k):
        raise AssertionError("凭据缺失时不应发请求")

    monkeypatch.setattr(model_client, "_post_json", _never)
    with pytest.raises(ModelUnavailable):
        model_client.call_active_model(_profile("missing-key"), _Store({}), [{"role": "user", "content": "hi"}])


def test_call_raises_on_empty_content(monkeypatch):
    from investment_steward_core.model_client import ModelUnavailable

    monkeypatch.setattr(model_client, "_post_json", lambda *_a, **_k: {"choices": [{"message": {"content": "   "}}]})
    with pytest.raises(ModelUnavailable):
        model_client.call_active_model(_profile(), _Store({"k1": "sk"}), [])


def test_call_raises_on_bad_choices(monkeypatch):
    from investment_steward_core.model_client import ModelUnavailable

    monkeypatch.setattr(model_client, "_post_json", lambda *_a, **_k: {"nope": True})
    with pytest.raises(ModelUnavailable):
        model_client.call_active_model(_profile(), _Store({"k1": "sk"}), [])


def test_post_json_uses_bearer_and_completions_endpoint(monkeypatch):
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def fake_open(request, **kwargs):
        captured["auth"] = request.get_header("Authorization")
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        return _Resp()

    monkeypatch.setattr(model_client.urllib.request, "urlopen", fake_open)
    payload = model_client._post_json("http://127.0.0.1:9999/v1", {"model": "m"}, "sk-token", 1.0)
    assert payload["choices"][0]["message"]["content"] == "ok"
    assert captured["auth"] == "Bearer sk-token"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/chat/completions")


# ---------- B1 / B4：response_gate ----------


def _gated_test(raw: str, evs: list[Evidence]):
    return response_gate.build_gate_payload(raw, evs, run_id=fresh_uuid(), user_id=fresh_uuid())


def test_gate_extracts_plain_json_and_keeps_known_refs():
    evs = _known_evidence()
    raw = (
        f'{{"user_question":"问","summary":"答","confidence_description":"中","action_mode":"observe",'
        f'"supporting_refs":["{evs[0].evidence_id}"],"contradicting_refs":["{evs[1].evidence_id}"],'
        f'"missing_information":[]}}'
    )
    payload = _gated_test(raw, evs)
    assert payload["summary"] == "答"
    assert payload["supporting_refs"] == [evs[0].evidence_id]
    assert payload["contradicting_refs"] == [evs[1].evidence_id]


def test_gate_drops_unknown_refs_and_annotates():
    evs = _known_evidence()
    known = evs[0].evidence_id
    raw = (
        f'{{"user_question":"问","summary":"答","confidence_description":"中","action_mode":"observe",'
        f'"supporting_refs":["{known}","{FAKE}"],"contradicting_refs":[]}}'
    )
    payload = _gated_test(raw, evs)
    assert payload["supporting_refs"] == [known]
    assert payload["contradicting_refs"] == []
    assert any("剔除" in message for message in payload["missing_information"])


def test_gate_rejects_non_json():
    with pytest.raises(response_gate.ResponseGateError):
        _gated_test("模型说了一段话，没有 JSON", [])


def test_gate_rejects_missing_summary():
    raw = '{"action_mode":"observe","confidence_description":"中"}'
    with pytest.raises(response_gate.ResponseGateError):
        _gated_test(raw, [])


def test_gate_extracts_from_code_fence():
    raw = (
        "好的，以下是分析：\n```json\n"
        '{"user_question":"问","summary":"答","confidence_description":"中","action_mode":"research"}'
        "\n```\n以上就是结论。"
    )
    payload = _gated_test(raw, [])
    assert payload["summary"] == "答"
    assert payload["action_mode"] == "research"


def test_assemble_produces_valid_agent_response():
    evs = _known_evidence()
    raw = (
        f'{{"user_question":"问","summary":"答","confidence_description":"中","action_mode":"observe",'
        f'"supporting_refs":["{evs[0].evidence_id}"],"contradicting_refs":[],'
        f'"model_provider":"test-provider","model_name":"test-model"}}'
    )
    payload = _gated_test(raw, evs)
    response = response_gate.assemble_agent_response(payload, response_id=uuid4())
    assert isinstance(response, AgentResponse)
    assert response.model_provider == "test-provider"
    assert response.action_mode.value == "observe"


# ---------- B3：双引擎降级（research._answer 分支）----------


def _make_run(user_id: UUID | None = None) -> object:
    from investment_steward_core.domain import ResearchRun, RunStatus

    return ResearchRun(
        run_id=uuid4(),
        user_id=user_id or uuid4(),
        user_question="测试问题",
        status=RunStatus.COMPOSING,
    )


def test_research_model_unavailable_falls_back_to_local_with_note(monkeypatch):
    """无激活方案 → ModelUnavailable → 回退本地引擎，并如实标注降级原因。"""
    from investment_steward_core import research

    class _DbNoActive:
        def list_model_profiles(self):
            return []

    class _FailClient:
        pass

    def fake_synthesize(_db, _run, _q, supporting, contradicting, unknown, **kwargs):
        return _synthesize_fixture(supporting, contradicting, unknown)

    monkeypatch.setattr(research, "_synthesize", fake_synthesize)
    # 保留 _try_model的真实逻辑：它会因 list_model_profiles 为空抛 ModelUnavailable。
    result = research._answer(
        _DbNoActive(), _make_run(), "问", [], [], [], model_client=_FailClient()
    )
    assert result.model_provider == "local-deterministic"
    assert any("回退" in line for line in result.limitations)


def _synthesize_fixture(supporting, contradicting, unknown):

    from investment_steward_core.domain import ActionMode, AgentResponse

    return AgentResponse(
        response_id=uuid4(),
        run_id=uuid4(),
        user_id=uuid4(),
        user_question="q",
        summary="本地",
        reasoning_outline=[],
        confidence_description="低",
        evidence_refs=[],
        supporting_refs=[e.evidence_id for e in supporting],
        contradicting_refs=[e.evidence_id for e in contradicting],
        missing_information=[],
        freshness_warning=[],
        limitations=["本地基准"],
        action_mode=ActionMode.RESEARCH,
        model_provider="local-deterministic",
        model_name="steward-evidence-synthesizer",
    )