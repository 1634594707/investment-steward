"""阶段失败不能继续付费，也不能在评测中伪装质量通过。"""
import json
import runpy
from pathlib import Path

import pytest
from investment_steward_core import collab, prompting
from investment_steward_core import model_client as mc

ROOT = Path(__file__).resolve().parents[3]
EVAL = runpy.run_path(str(ROOT / "scripts/evaluate-research-collab.py"))


def test_every_role_receives_financial_and_source_failure_constraints():
    run = collab.create_run("600001", "合成口径测试", "", {"S4": "财务证据"},
                            {"S2": "synthetic-news-fetch-failed"}, None, "m")
    for role in collab.STAGES:
        text = collab._stage_user_prompt(run, role)
        assert "不得先列机械计算值再补不可比声明" in text, role
        assert "经营现金流 ÷ 归母净利润" in text, role
        assert "只有 source_errors 明确记录的来源才可称取数失败" in text, role
        assert "synthetic-news-fetch-failed" in text
        assert "K 线取数失败" not in text
        assert "S2: synthetic-news-fetch-failed" in text
    assert "不得先列机械计算值再补不可比声明" in prompting.STOCK_REPORT_RULES_TEXT


def test_real_engine_stops_after_failed_stage_and_saves_safe_checkpoint(monkeypatch, tmp_path):
    calls = []

    def post(base, payload, key, timeout):
        calls.append(payload)
        raise mc.ModelUnavailable("private-key", error_kind="auth", http_status=401)

    monkeypatch.setattr(mc, "_post_json", post)
    previous = mc._model_call_recorder
    output = tmp_path / "result.json"
    result = EVAL["evaluate"](corpus_path=ROOT / "evals/research/v2/cases.json",
        case_id="currency_mismatch", base_url="https://example.invalid", model="m",
        key="private-key", output=output, timeout=30)
    assert len(calls) == 1 and result["run"]["next_index"] == 0
    assert result["status"] == "stage_failed"
    assert result["diagnostics"][0]["http_status"] == 401
    assert result["archived"] is False
    assert "private-key" not in output.read_text(encoding="utf-8")
    assert "coverage_ratio" not in json.dumps(calls), "不能把派生标准答案发送给模型"
    assert mc._model_call_recorder is previous


def test_truncated_raw_reply_survives_engine_failure_without_exposing_key(monkeypatch, tmp_path):
    raw = '{"report":"private-key partial'

    def post(*args, **kwargs):
        return {"choices": [{"message": {"content": raw}, "finish_reason": "length"}]}

    monkeypatch.setattr(mc, "_post_json", post)
    output = tmp_path / "result.json"
    result = EVAL["evaluate"](corpus_path=ROOT / "evals/research/v2/cases.json",
        case_id="currency_mismatch", base_url="https://example.invalid", model="m",
        key="private-key", output=output, timeout=30)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["responses"][0]["content"] == raw.replace("private-key", "[REDACTED]")
    assert saved["responses"][0]["finish_reason"] == "length"
    assert result["status"] == "stage_failed" and result["run"]["next_index"] == 0
    assert result["diagnostics"][0]["error_kind"] == "output_truncated"
    assert mc._post_json is post


def test_truncated_draft_cannot_advance_to_risk_even_if_json_is_valid(monkeypatch, tmp_path):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        content = {"observations": [], "gaps": []} if len(calls) == 1 else {
            "report": "partial draft", "citations": ["S4"]}
        return {"choices": [{"message": {"content": json.dumps(content)},
                              "finish_reason": "stop" if len(calls) == 1 else "length"}]}

    monkeypatch.setattr(mc, "_post_json", post)
    result = EVAL["evaluate"](corpus_path=ROOT / "evals/research/v2/cases.json",
        case_id="currency_mismatch", base_url="https://example.invalid", model="m",
        key="private-key", output=tmp_path / "result.json", timeout=30)
    assert len(calls) == 2
    assert result["run"]["next_index"] == 1
    assert [stage["status"] for stage in result["run"]["stages"]] == ["done", "failed", "pending", "pending"]
    assert result["run"]["stages"][1]["result"] is None
    assert result["diagnostics"][1]["error_kind"] == "output_truncated"


def test_complete_report_reaches_risk_with_real_temporary_configuration(monkeypatch, tmp_path):
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise mc.ModelUnavailable("stop fixture at risk", error_kind="auth", http_status=401)
        content = {"observations": [], "gaps": []} if len(calls) == 1 else {
            "report": "### 基本面\n合成财务币种冲突，停止计算。[S4]", "citations": ["S4"]}
        return {"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}]}

    monkeypatch.setattr(mc, "_post_json", post)
    result = EVAL["evaluate"](corpus_path=ROOT / "evals/research/v2/cases.json",
        case_id="currency_mismatch", base_url="https://example.invalid", model="m",
        key="private-key", output=tmp_path / "result.json", timeout=30)
    assert len(calls) == 3 and result["run"]["next_index"] == 2
    assert result["run"]["stages"][1]["status"] == "done"
    cash = result["run"]["stages"][1]["result"]["cash_flow_quality"]
    assert cash["coverage_ratio"] is None
    prefix_path = tmp_path / "result.json"
    source = json.loads(prefix_path.read_text(encoding="utf-8"))
    # 只支付未保存的risk/revise；重放诊断不充作当前调用用量。
    tail = []

    def continue_post(*args, **kwargs):
        tail.append(1)
        content = {"risks": []} if len(tail) == 1 else {"report": "合成修订 [S4]", "citations": ["S4"]}
        return {"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}]}

    monkeypatch.setattr(mc, "_post_json", continue_post)
    args = {"corpus_path": ROOT / "evals/research/v2/cases.json", "case_id": "currency_mismatch",
            "base_url": "https://example.invalid", "model": "m", "key": "private-key", "timeout": 30,
            "output": tmp_path / "continued.json", "replay_prefix": prefix_path}
    continued = EVAL["evaluate"](**args)
    assert len(tail) == 2 and continued["run"]["next_index"] == 4
    assert continued["replayed_requests"] == 2 and continued["live_requests"] == 2
    assert [row["origin"] for row in continued["diagnostics"]] == ["replayed", "replayed", "live", "live"]
    assert continued["status"] == "unreviewed"
    source["prompt_version"] = "incompatible"
    prefix_path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="不一致"):
        EVAL["evaluate"](**args)
    assert len(tail) == 2, "版本冲突在网络调用前拒绝"
