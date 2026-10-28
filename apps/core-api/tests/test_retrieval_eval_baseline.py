"""固定题库不能随系统时间漂移，也不能用实际结果覆盖预期来伪造通过。"""
import json
import runpy
from pathlib import Path

EVAL = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/evaluate-retrieval-baseline.py"))


def test_fixed_retrieval_cases_keep_counter_evidence_and_validity_boundary():
    result = EVAL["evaluate"]()
    cases = {row["id"]: row for row in result["cases"]}
    assert len(cases) == 12
    assert all(row["status"] == "passed" for row in cases.values())
    assert cases["combined"]["actual_ids"] == ["boundary", "oppose", "support"]
    assert cases["counter_evidence"]["actual_ids"] == ["oppose"]
    assert "expired" not in cases["unscoped"]["actual_ids"]
    assert "withdrawn" not in cases["unscoped"]["actual_ids"]
    assert result["metrics"]["micro_precision"] == result["metrics"]["micro_recall"] == 1
    assert result["model_calls"] == 0


def test_wrong_expected_set_remains_a_failure_without_rewriting_input(tmp_path):
    corpus = json.loads(EVAL["CORPUS"].read_bytes())
    case = next(row for row in corpus["cases"] if row["id"] == "combined")
    case["expected_ids"].remove("oppose")
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")
    before = path.read_bytes()
    result = EVAL["evaluate"](path)
    row = next(row for row in result["cases"] if row["id"] == "combined")
    assert row["status"] == "failed" and row["false_positive"] == 1
    assert result["metrics"]["micro_precision"] < 1
    assert path.read_bytes() == before
