"""短问答评测不得泄漏答案、把失败当通过或隐藏计费用量缺失。"""
import json
import runpy
from pathlib import Path

import pytest
from investment_steward_core import model_client as mc

EVAL = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/evaluate-research-live.py"))


def test_inputs_exclude_scoring_fields():
    case = {"question": "question", "evidence": {"sources": {}}, "expected": "ANSWER",
            "oracle": "ORACLE", "rubric": "RUBRIC", "candidate_citations": ["S9"]}
    payload = json.dumps(EVAL["messages_for"](case, "2026-10-26"))
    assert all(value not in payload for value in ("ANSWER", "ORACLE", "RUBRIC"))
    assert json.loads(EVAL["messages_for"](case, "2026-10-26")[1]["content"])["candidate_citations"] == ["S9"]


def test_citation_cases_keep_distinct_tasks_without_leaking_expected_outcomes():
    corpus = json.loads(EVAL["CORPUS"].read_text(encoding="utf-8"))
    cases = [case for case in corpus["cases"] if case["oracle"] == "citations"]
    messages = [EVAL["messages_for"](case, corpus["as_of"]) for case in cases]
    assert len({json.dumps(message) for message in messages}) == len(cases)
    for case, message in zip(cases, messages, strict=True):
        payload = json.loads(message[1]["content"])
        assert payload["candidate_citations"] == case["candidate_citations"]
        assert set(payload) == {"as_of", "question", "evidence", "candidate_citations"}


def test_live_results_remain_unreviewed_and_failures_keep_unknown_usage(monkeypatch):
    responses = iter([True, False])
    previous = mc._model_call_recorder

    def post(base, payload, key, timeout):
        assert key == "private-key"
        assert "expected" not in payload["messages"][1]["content"]
        if not next(responses):
            raise mc.ModelUnavailable("private-key", error_kind="auth", http_status=401)
        return {"model": "gateway-claim", "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 10}}

    monkeypatch.setattr(mc, "_post_json", post)
    result = EVAL["evaluate"](base_url="https://example.invalid/v1", model="requested",
                               key="private-key", ids=["same_basis", "counter_evidence"])
    good, failed = result["cases"]
    assert good["status"] == "unreviewed"
    assert good["diagnostics"]["returned_model"] == "gateway-claim"
    assert good["diagnostics"]["usage_complete"] is False
    assert good["diagnostics"]["total_tokens"] is None
    assert failed["status"] == "call_failed" and "answer" not in failed
    assert failed["diagnostics"]["http_status"] == 401
    assert failed["diagnostics"]["attempt_count"] == 1
    assert "private-key" not in json.dumps(result)
    assert mc._model_call_recorder is previous


def test_interruption_preserves_completed_samples_without_claiming_whole_run_finished(tmp_path, monkeypatch):
    calls = []
    def answer(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return mc.ModelReply(content="synthetic answer", model="synthetic", provider="synthetic", latency_ms=1)
    monkeypatch.setattr(mc, "call_active_model", answer)
    output = tmp_path / "checkpoint.json"
    previous = mc._model_call_recorder
    with pytest.raises(KeyboardInterrupt):
        EVAL["evaluate"](base_url="https://example.invalid", model="synthetic", key="private-key",
            ids=["same_basis", "counter_evidence"], output=output)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["complete"] is False
    assert saved["requested_ids"] == ["same_basis", "counter_evidence"]
    assert [row["id"] for row in saved["cases"]] == ["same_basis"]
    assert saved["cases"][0]["status"] == "unreviewed"
    assert "private-key" not in output.read_text(encoding="utf-8")
    assert mc._model_call_recorder is previous
