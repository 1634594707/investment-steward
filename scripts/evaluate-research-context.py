"""独立进程以合成证据运行真实研究引擎，保留原始回答与闸门结果。"""
import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from investment_steward_core import model_client as mc
from investment_steward_core import research
from investment_steward_core.domain import Evidence, ModelProfile, ResearchRun
from investment_steward_core.prompting import RESEARCH_PROMPT_VERSION, research_system_prompt
from investment_steward_core.storage.database import Database


def evaluate(*, base_url, model, key, timeout=60):
    if not key:
        raise ValueError("需要进程内评测凭据")
    records, replies = [], []
    previous_recorder, previous_post = mc._model_call_recorder, mc._post_json

    def capture(*args, **kwargs):
        payload = previous_post(*args, **kwargs)
        choices = payload.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        replies.append({"content": message.get("content"), "finish_reason": first.get("finish_reason")})
        return payload

    result = {"mode": "production_research_synthetic_context", "prompt_version": RESEARCH_PROMPT_VERSION,
              "gateway": base_url, "requested_model": model, "created_at": datetime.now(UTC).isoformat(),
              "timeout_secs": timeout, "quality_status": "unreviewed", "archived": False}
    try:
        mc._post_json = capture
        mc.set_model_call_recorder(records.append)
        with TemporaryDirectory(prefix="research-context-eval-") as directory:
            db = Database(Path(directory) / "eval.sqlite3")
            try:
                db.get_credential = lambda _ref: key  # 仅内存，不写凭据表或密钥文件。
                db.upsert_model_profile(ModelProfile(profile_id=uuid4(), name="synthetic-context-eval",
                    base_url=base_url, model=model, credential_ref="ephemeral", status="使用中", timeout_secs=timeout))
                run = ResearchRun(run_id=uuid4(), user_id=uuid4(), user_question=(
                    "请计算合成公司现金流覆盖倍数（经营现金流/归母净利润）和资本开支后余额，并说明能否比较。"))
                db.upsert_research_run(run)
                items = []
                for index, (summary, basis) in enumerate([
                    ("经营现金流100亿元，人民币，来源年初累计", "cumulative"),
                    ("归母净利润80亿元，人民币，来源单季", "single_quarter"),
                    ("资本开支40亿元，人民币，期间口径未知", "unknown"),
                ]):
                    item = Evidence(evidence_id=uuid4(), tenant_id=run.user_id, subject_refs=["SYNTHETIC"],
                        evidence_type="financial", source_name=f"synthetic-{index}", summary=summary,
                        content_hash=f"synthetic-context-{index:016d}", financial_period_end="2026-06-30",
                        financial_period_basis=basis)
                    items.append(item)
                    db.insert_evidence(item)
                context = research._context_for_prompt(run.user_question, [], [], items)
                messages = mc.format_messages(research_system_prompt(), context)
                result["input_messages"] = messages
                result["input_sha256"] = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()
                response = research.run_research(db, run, model_client=object())
                result["response"] = response.model_dump(mode="json")
                result["status"] = "model_answer_gated" if response.model_provider != "local-deterministic" else "local_fallback"
            finally:
                db.close()
    finally:
        mc._post_json = previous_post
        mc.set_model_call_recorder(previous_recorder)
    result["raw_replies"] = replies
    result["diagnostics"] = [{name: value for name, value in row.items() if name != "profile_id"} for row in records]
    return json.loads(json.dumps(result, ensure_ascii=False).replace(key, "[REDACTED]"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(base_url=args.base_url, model=args.model, key=os.environ.get("RESEARCH_EVAL_API_KEY", ""))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "requests": len(result["raw_replies"])}))
