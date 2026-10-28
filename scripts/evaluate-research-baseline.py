"""固定题库的离线规则基线；manual 是待人工评分，不能算作模型通过。"""
import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from investment_steward_core import report_quality

CORPUS = Path(__file__).resolve().parents[1] / "evals/research/v1/cases.json"


def evaluate(path=CORPUS):
    raw = path.read_bytes()
    corpus = json.loads(raw)
    rows = []
    for case in corpus["cases"]:
        evidence = case["evidence"]
        oracle = case["oracle"]
        if oracle == "manual":
            rows.append({"id": case["id"], "status": "manual_pending"})
            continue
        if oracle == "cash_flow":
            result = report_quality.cash_flow_quality(
                **evidence["cash_flow"], allowed_sources=set(evidence["sources"]), require_currency=True)
            actual = {key: result[key] for key in case["expected"]}
        elif oracle == "source_health":
            result = report_quality.report_source_health(
                {**evidence, "source_keys": list(evidence["sources"])}, today=date.fromisoformat(corpus["as_of"]))
            actual = {"state": result["sources"][0]["state"]}
        elif oracle == "citations":
            kept, dropped = report_quality.filter_citations(case["candidate_citations"], set(evidence["sources"]))
            actual = {"kept": kept, "dropped": dropped}
        else:
            raise ValueError(f"Unknown oracle: {oracle}")
        rows.append({"id": case["id"], "status": "passed" if actual == case["expected"] else "failed",
                     "expected": case["expected"], "actual": actual})
    return {"version": corpus["version"], "corpus_sha256": hashlib.sha256(raw).hexdigest(),
            "mode": "offline_rules_only", "model_calls": 0, "cases": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    args = parser.parse_args()
    result = evaluate(args.corpus)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(any(row["status"] == "failed" for row in result["cases"]))
