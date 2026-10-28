"""合成证据短问答探测；独立进程执行，不代表生产研报流水线验收。"""
import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_steward_core import model_client as mc
from investment_steward_core.domain import ModelProfile

CORPUS = Path(__file__).resolve().parents[1] / "evals/research/v1/cases.json"
PROMPT_VERSION = "evidence-probe-2"
SYSTEM = "仅依据给定合成证据回答问题，标明来源、缺口和判断限制。不要补造事实。用中文简答，最多250字。"
DIAGNOSTIC_FIELDS = (
    "latency_ms", "attempt_count", "attempts", "returned_model", "usage_complete",
    "prompt_tokens", "completion_tokens", "total_tokens", "outcome", "error_kind", "http_status",
)


def messages_for(case, as_of):
    # 白名单构造输入，禁止发送 oracle/expected/rubric 等评分信息。
    payload = {"as_of": as_of, "question": case["question"], "evidence": case["evidence"]}
    if "candidate_citations" in case:
        payload["candidate_citations"] = case["candidate_citations"]
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(
        payload, ensure_ascii=False)}]


def evaluate(*, base_url, model, key, ids, timeout=60, path=CORPUS, output=None):
    raw = path.read_bytes()
    corpus = json.loads(raw)
    cases = {case["id"]: case for case in corpus["cases"]}
    if not key or not ids or len(set(ids)) != len(ids) or any(cid not in cases for cid in ids):
        raise ValueError("需要凭据及有效、不重复的案例 id")
    profile = ModelProfile(profile_id=uuid4(), name="synthetic-eval", base_url=base_url,
                           model=model, credential_ref="ephemeral")

    class MemoryStore:
        def get(self, key_id):
            return key if key_id == "ephemeral" else None

    rows = []
    result = {"mode": "synthetic_evidence_probe", "prompt_version": PROMPT_VERSION,
              "system_prompt": SYSTEM, "corpus_sha256": hashlib.sha256(raw).hexdigest(),
              "corpus_version": corpus["version"], "requested_model": model, "gateway": base_url,
              "created_at": datetime.now(UTC).isoformat(), "timeout_per_case_secs": timeout,
              "production_pipeline": False, "requested_ids": list(ids), "complete": False, "cases": rows}

    def checkpoint():
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    checkpoint()
    previous_recorder = mc._model_call_recorder
    try:
        for cid in ids:
            records = []
            mc.set_model_call_recorder(records.append)
            messages = messages_for(cases[cid], corpus["as_of"])
            row = {"id": cid, "status": "unreviewed", "input_sha256": hashlib.sha256(
                json.dumps(messages, ensure_ascii=False).encode()).hexdigest()}
            try:
                reply = mc.call_active_model(profile, MemoryStore(), messages, timeout=timeout,
                                             purpose="合成证据评测")
                row["answer"] = reply.content.replace(key, "[REDACTED]")
            except mc.ModelUnavailable:
                row["status"] = "call_failed"
            row["diagnostics"] = {field: records[-1].get(field) for field in DIAGNOSTIC_FIELDS} if records else {}
            rows.append(row)
            checkpoint()
    finally:
        mc.set_model_call_recorder(previous_recorder)
    result["complete"] = True
    checkpoint()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--model", required=True)
    parser.add_argument("--ids", nargs="+", required=True)
    parser.add_argument("--timeout", type=int, choices=range(1, 121), default=60, metavar="1..120")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("RESEARCH_EVAL_API_KEY"):
        parser.error("请通过进程环境 RESEARCH_EVAL_API_KEY 提供凭据")
    result = evaluate(base_url=args.base_url, model=args.model, key=os.environ["RESEARCH_EVAL_API_KEY"],
                      ids=args.ids, timeout=args.timeout, path=args.corpus, output=args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"statuses": [row["status"] for row in result["cases"]]}))
    raise SystemExit(any(row["status"] == "call_failed" for row in result["cases"]))
