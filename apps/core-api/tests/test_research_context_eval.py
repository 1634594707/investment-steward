"""实际研究评测必须区分模型通过闸门与本地降级，不能隐藏失败或泄漏密钥。"""
import json
import runpy
from pathlib import Path

from investment_steward_core import model_client as mc

EVAL = runpy.run_path(str(Path(__file__).resolve().parents[3] / "scripts/evaluate-research-context.py"))


def test_failed_remote_call_is_local_fallback_with_safe_diagnostics(monkeypatch):
    calls = []

    def fail(*args):
        calls.append(args)
        raise mc.ModelUnavailable("secret-token", error_kind="auth", http_status=401)

    monkeypatch.setattr(mc, "_post_json", fail)
    recorder = mc._model_call_recorder
    result = EVAL["evaluate"](base_url="https://example.invalid", model="synthetic", key="secret-token")
    assert len(calls) == 1
    assert result["status"] == "local_fallback"
    assert result["quality_status"] == "unreviewed"
    assert result["diagnostics"][0]["http_status"] == 401
    assert result["raw_replies"] == []
    assert "secret-token" not in json.dumps(result)
    assert mc._model_call_recorder is recorder
    assert mc._post_json is fail
