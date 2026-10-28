"""评测不能用实际值覆盖预期，也不能把人工待评当成通过。"""
import hashlib
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
EVAL = runpy.run_path(str(ROOT / "scripts/evaluate-research-baseline.py"))


def test_v2_clarifies_denominator_without_moving_evidence_or_answers():
    v1_raw = EVAL["CORPUS"].read_bytes()
    assert hashlib.sha256(v1_raw).hexdigest() == "3fc4b04fb4e0977dac6130136019e15d88ffffca25498ea6c7313da61fd9b0ef"
    v1 = json.loads(v1_raw)
    v2_path = ROOT / "evals/research/v2/cases.json"
    v2 = json.loads(v2_path.read_bytes())
    assert v2["version"] == "research-eval-v2"
    changed = []
    for before, after in zip(v1["cases"], v2["cases"], strict=True):
        if before["oracle"] == "cash_flow":
            assert "operating_cash_flow ÷ net_income" in after["question"]
            assert "operating_cash_flow − capex" in after["question"]
            changed.append(after["id"])
            after["question"] = before["question"]
        assert after == before, "明确问题定义不能移动证据或标准答案"
    assert len(changed) == 8
    result = EVAL["evaluate"](v2_path)
    assert sum(row["status"] == "passed" for row in result["cases"]) == 16
    assert sum(row["status"] == "manual_pending" for row in result["cases"]) == 4


def test_fixed_corpus_has_real_rule_results_and_explicit_semantic_gaps():
    result = EVAL["evaluate"]()
    rows = result["cases"]
    assert len(rows) == 20 and len({row["id"] for row in rows}) == 20
    assert sum(row["status"] == "passed" for row in rows) == 16
    assert {row["id"] for row in rows if row["status"] == "manual_pending"} == {
        "no_report_body", "counter_evidence", "user_tip", "peer_denominator"}
    assert result["model_calls"] == 0


def test_wrong_expected_answer_is_reported_without_rewriting_the_corpus(tmp_path):
    corpus = json.loads(EVAL["CORPUS"].read_text(encoding="utf-8"))
    corpus["cases"][0]["expected"]["coverage_ratio"] = 999
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    before = path.read_bytes()
    result = EVAL["evaluate"](path)
    assert result["cases"][0]["status"] == "failed"
    assert result["cases"][0]["expected"]["coverage_ratio"] == 999
    assert result["cases"][0]["actual"]["coverage_ratio"] == 1.25
    assert path.read_bytes() == before
    corpus["cases"][0]["oracle"] = "unsupported"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown oracle"):
        EVAL["evaluate"](path)
