"""固定证据的生产生成+闸门评测；不覆盖检索、降级、持久化或协同。"""
import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from investment_steward_core import model_client as mc
from investment_steward_core import research
from investment_steward_core.domain import Evidence, ModelProfile, ResearchRun
from investment_steward_core.prompting import RESEARCH_PROMPT_VERSION
from investment_steward_core.response_gate import ResponseGateError

CORPUS = Path(__file__).resolve().parents[1] / "evals/research/v2/cases.json"
DIAGNOSTIC_FIELDS = ("latency_ms", "attempt_count", "returned_model", "usage_complete",
                     "prompt_tokens", "completion_tokens", "total_tokens", "outcome", "error_kind", "http_status")


def evaluate(*, base_url, model, key, ids, timeout=60, path=CORPUS, output=None):
    raw = path.read_bytes()
    corpus = json.loads(raw)
    cases = {case["id"]: case for case in corpus["cases"]}
    if not key or not ids or len(set(ids)) != len(ids) or any(cid not in cases for cid in ids):
        raise ValueError("需要凭据及有效、不重复的案例 id")
    profile = ModelProfile(profile_id=uuid4(), name="generation-eval", base_url=base_url,
                           model=model, credential_ref="ephemeral", status="使用中", timeout_secs=timeout)

    class MemoryDb:
        def list_model_profiles(self):
            return [profile]

        def get_credential(self, key_id):
            return key if key_id == "ephemeral" else None

    result = {"mode": "production_generation_and_gate", "adapter_version": "evidence-snapshot-3",
              "prompt_version": RESEARCH_PROMPT_VERSION, "corpus_sha256": hashlib.sha256(raw).hexdigest(),
              "corpus_version": corpus["version"], "gateway": base_url, "requested_model": model,
              "created_at": datetime.now(UTC).isoformat(), "timeout_per_case_secs": timeout,
              "scope_excludes": ["retrieval", "local_fallback", "persistence", "collaboration"],
              "requested_ids": list(ids), "complete": False, "cases": []}

    def sanitized():
        return json.loads(json.dumps(result, ensure_ascii=False).replace(key, "[REDACTED]"))

    def checkpoint():
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(sanitized(), ensure_ascii=False, indent=2), encoding="utf-8")

    previous_call, previous_recorder = mc.call_active_model, mc._model_call_recorder
    checkpoint()
    try:
        for cid in ids:
            case = cases[cid]
            # 不推断来源归属/关系/期间；原始任务证据完整保留在一个未知关系的合成快照中。
            snapshot = {"as_of": corpus["as_of"], "evidence": case["evidence"]}
            if "candidate_citations" in case:
                snapshot["candidate_citations"] = case["candidate_citations"]
            text = json.dumps(snapshot, ensure_ascii=False)
            declared = case["evidence"].get("financial_source_metadata", {})
            metadata = {name: declared[name] for name in (
                "published_at", "financial_period_end", "financial_period_basis"
            ) if name in declared}
            run = ResearchRun(run_id=uuid4(), user_id=uuid4(), user_question=case["question"])
            evidence = Evidence(evidence_id=uuid5(NAMESPACE_URL, f"research-generation/{cid}"),
                tenant_id=run.user_id, subject_refs=["SYNTHETIC"],
                evidence_type="financial" if "cash_flow" in case["evidence"] else "analysis",
                source_name="合成任务证据快照（内部来源编号见原始字段）", summary=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(), **metadata)
            row = {"id": cid, "quality_status": "unreviewed", "evidence_snapshot": evidence.model_dump(mode="json"),
                   "raw_replies": [], "diagnostics": []}
            records = []

            def capture(*args, _row=row, **kwargs):
                messages = args[2]
                _row["input_messages"] = messages
                _row["input_sha256"] = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()
                reply = previous_call(*args, **kwargs)
                _row["raw_replies"].append(reply.content)
                return reply

            mc.call_active_model = capture
            mc.set_model_call_recorder(records.append)
            try:
                response = research._try_model(MemoryDb(), run, run.user_question, [], [], [evidence])
                row["response"] = response.model_dump(mode="json")
                row["status"] = "model_answer_gated"
            except mc.ModelUnavailable:
                row["status"] = "call_failed"
            except ResponseGateError:
                row["status"] = "gate_rejected"
            row["diagnostics"] = [{name: record.get(name) for name in DIAGNOSTIC_FIELDS} for record in records]
            result["cases"].append(row)
            checkpoint()
    finally:
        mc.call_active_model = previous_call
        mc.set_model_call_recorder(previous_recorder)
    result["complete"] = True
    checkpoint()
    return sanitized()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--model", required=True)
    parser.add_argument("--ids", nargs="+", required=True)
    parser.add_argument("--timeout", type=int, choices=range(1, 121), default=60, metavar="1..120")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(base_url=args.base_url, model=args.model, key=os.environ.get("RESEARCH_EVAL_API_KEY", ""),
                      ids=args.ids, timeout=args.timeout, path=args.corpus, output=args.output)
    print(json.dumps({"statuses": [row["status"] for row in result["cases"]]}))
    raise SystemExit(any(row["status"] != "model_answer_gated" for row in result["cases"]))
