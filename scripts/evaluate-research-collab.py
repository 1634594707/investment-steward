"""独立进程：合成财务证据经真实四阶段引擎；不归档、不启用额外语义模型。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from investment_steward_core import collab, model_client
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import ModelProfile
from investment_steward_core.prompting import STOCK_REPORT_PROMPT_VERSION
from investment_steward_core.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]


def evaluate(*, corpus_path, case_id, base_url, model, key, output, timeout=60, replay_prefix=None):
    raw = corpus_path.read_bytes()
    corpus = json.loads(raw)
    case = next(case for case in corpus["cases"] if case["id"] == case_id)
    if case["oracle"] != "cash_flow" or not key:
        raise ValueError("仅支持合成财务题且必须提供临时凭据")
    evidence = case["evidence"]
    sources = dict(evidence["sources"])
    sources["S4"] += "\n" + json.dumps(evidence["cash_flow"], ensure_ascii=False)
    profile = ModelProfile(profile_id=uuid4(), name="synthetic-collab-eval", base_url=base_url,
                           model=model, credential_ref="ephemeral", timeout_secs=timeout)
    run = collab.create_run("600001", case["question"], "", sources, {},
                            str(profile.profile_id), model, report_mode="quick")
    prefix = []
    if replay_prefix:
        saved = json.loads(replay_prefix.read_text(encoding="utf-8"))
        expected = {"case_id": case_id, "corpus_sha256": hashlib.sha256(raw).hexdigest(),
                    "prompt_version": STOCK_REPORT_PROMPT_VERSION, "gateway": base_url, "requested_model": model}
        if any(saved.get(field) != value for field, value in expected.items()) or any(
            saved.get("run", {}).get(field) != run[field]
            for field in ("evidence", "question", "pipeline_version", "report_mode")
        ):
            raise ValueError("重放前缀与当前题库、证据、配置或版本不一致")
        prefix = saved.get("responses", [])
        if not prefix or len(prefix) > len(collab.STAGES) or any(
            row.get("stage") != collab.STAGES[index] or row.get("finish_reason") != "stop"
            or not isinstance(row.get("content"), str) for index, row in enumerate(prefix)
        ):
            raise ValueError("重放只允许按顺序保存的完整stop回复")
    temporary = TemporaryDirectory(prefix="research-eval-")
    database = Database(Path(temporary.name) / "eval.sqlite3")
    core = SimpleNamespace(settings=CoreSettings(data_dir=Path(temporary.name), model_access_enabled=False),
                           database=database, local_user_id=uuid4())

    class MemoryStore:
        def get(self, key_id):
            return key if key_id == "ephemeral" else None

    records, responses = [], []
    previous = model_client._model_call_recorder
    previous_post = model_client._post_json
    result = {"mode": "production_collab_engine_synthetic", "case_id": case_id,
              "corpus_version": corpus["version"], "corpus_sha256": hashlib.sha256(raw).hexdigest(),
              "prompt_version": STOCK_REPORT_PROMPT_VERSION, "gateway": base_url, "requested_model": model,
              "extra_semantic_judges": False, "archived": False, "status": "unreviewed",
              "diagnostics": records, "responses": responses, "run": run}
    result.update(replayed_requests=0, live_requests=0,
                  replay_source=replay_prefix.name if replay_prefix else None)
    if prefix:
        result["mode"] = "recorded_prefix_live_tail_collab"
    origin = "live"

    def checkpoint():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2).replace(key, "[REDACTED]"), encoding="utf-8")

    def capture_post(*args, **kwargs):
        nonlocal origin
        index = run["next_index"]
        if index < len(prefix):
            origin = "replayed"
            row = prefix[index]
            result["replayed_requests"] += 1
            payload = {"choices": [{"message": {"content": row["content"]}, "finish_reason": "stop"}]}
        else:
            origin = "live"
            result["live_requests"] += 1
            payload = previous_post(*args, **kwargs)
        choices = payload.get("choices")
        first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = first.get("message")
        responses.append({"stage": collab.STAGES[run["next_index"]], "origin": origin,
                          "finish_reason": first.get("finish_reason"),
                          "content": message.get("content") if isinstance(message, dict) else None})
        checkpoint()
        return payload

    try:
        model_client._post_json = capture_post
        model_client.set_model_call_recorder(lambda row: records.append(
            {**{name: value for name, value in row.items() if name != "profile_id"}, "origin": origin}))
        for _ in collab.STAGES:
            stage = collab.execute_next(core, run, profile, MemoryStore())
            if stage["status"] != "done":
                result["status"] = "stage_failed"
                checkpoint()
                break
            checkpoint()
    finally:
        model_client._post_json = previous_post
        model_client.set_model_call_recorder(previous)
        database.close()
        temporary.cleanup()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "evals/research/v2/cases.json")
    parser.add_argument("--case", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-prefix", type=Path)
    parser.add_argument("--timeout", type=int, choices=range(30, 121), default=60, metavar="30..120")
    args = parser.parse_args()
    if not os.environ.get("RESEARCH_EVAL_API_KEY"):
        parser.error("请通过进程环境 RESEARCH_EVAL_API_KEY 提供凭据")
    result = evaluate(corpus_path=args.corpus, case_id=args.case, base_url=args.base_url, model=args.model,
                      key=os.environ["RESEARCH_EVAL_API_KEY"], output=args.output, timeout=args.timeout,
                      replay_prefix=args.replay_prefix)
    print(json.dumps({"status": result["status"], "next_index": result["run"]["next_index"]}))
    raise SystemExit(result["status"] == "stage_failed")
