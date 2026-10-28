"""固定合成检索基线，评估集合命中而非排序或模型生成质量。"""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from investment_steward_core.domain import Evidence, EvidenceSearchScope
from investment_steward_core.research import select_relevant_evidence

CORPUS = Path(__file__).resolve().parents[1] / "evals/retrieval/v1/cases.json"


def evaluate(path=CORPUS):
    raw = path.read_bytes()
    corpus = json.loads(raw)
    ids = [row["id"] for row in corpus["evidence"]]
    if len(set(ids)) != len(ids):
        raise ValueError("重复证据id")
    evidence = []
    labels = {}
    for row in corpus["evidence"]:
        name = row["id"]
        uid = uuid5(NAMESPACE_URL, "retrieval-eval-v1/" + name)
        labels[uid] = name
        evidence.append(Evidence(evidence_id=uid, tenant_id=NAMESPACE_URL, evidence_type="announcement",
            content_hash="synthetic-retrieval-" + name, collected_at=corpus["as_of"],
            **{key: value for key, value in row.items() if key != "id"}))
    cases = []
    totals = {"true_positive": 0, "false_positive": 0, "false_negative": 0}
    if len({row["id"] for row in corpus["cases"]}) != len(corpus["cases"]):
        raise ValueError("重复问题id")
    for case in corpus["cases"]:
        expected = set(case["expected_ids"])
        if not expected.issubset(ids):
            raise ValueError("预期引用未知证据id")
        scope = EvidenceSearchScope.model_validate(case["scope"]) if case.get("scope") else None
        selected = select_relevant_evidence(case["question"], evidence, scope=scope,
                                            now=datetime.fromisoformat(corpus["as_of"]))
        actual = {labels[item.evidence_id] for item in selected}
        counts = {"true_positive": len(actual & expected), "false_positive": len(actual - expected),
                  "false_negative": len(expected - actual)}
        for key, value in counts.items():
            totals[key] += value
        cases.append({"id": case["id"], "status": "passed" if actual == expected else "failed",
                      "actual_ids": sorted(actual), "expected_ids": sorted(expected), **counts})
    tp, fp, fn = (totals[key] for key in ("true_positive", "false_positive", "false_negative"))
    return {"version": corpus["version"], "as_of": corpus["as_of"], "data_kind": "synthetic",
            "mode": "offline_retrieval_set_matching", "model_calls": 0,
            "corpus_sha256": hashlib.sha256(raw).hexdigest(), "cases": cases, "metrics": {
                **totals, "micro_precision": tp / (tp + fp) if tp + fp else None,
                "micro_recall": tp / (tp + fn) if tp + fn else None}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(args.corpus)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    raise SystemExit(any(row["status"] == "failed" for row in result["cases"]))
