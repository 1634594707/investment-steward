"""续跑不能把新模型配置或新提示词混入已付费的研究任务。"""
import pytest
from investment_steward_core import collab, model_client
from test_collab_pipeline import _stage_reply_factory, _start_run
from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile


@pytest.mark.parametrize("changed", ["base_url", "model", "credential_ref", "timeout_secs", "name",
                                    "pipeline_version", "prompt_version", "legacy"])
def test_resume_validates_snapshot_before_model_call(tmp_path, monkeypatch, changed):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    reply = _stage_reply_factory()
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return reply(*args, **kwargs)

    monkeypatch.setattr(model_client, "_post_json", post)
    run = _start_run(client, headers)
    assert client.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).status_code == 200
    db = client.app.state.core.database
    before = db.get_collab_run(run["run_id"])
    if changed in {"pipeline_version", "prompt_version", "legacy"}:
        saved = db.get_collab_run(run["run_id"])
        if changed == "legacy":
            saved.pop("profile_fingerprints", None)
        else:
            saved[changed] = "incompatible"
        db.save_collab_run(saved)
    else:
        profile = client.get("/model-profiles", headers=headers).json()[0]
        changes = {"base_url": "https://changed.invalid", "model": "changed-model",
                   "credential_ref": "changed-ref", "timeout_secs": 180, "name": "改名"}
        profile[changed] = changes[changed]
        updated = client.put(f"/model-profiles/{profile['profile_id']}", json=profile, headers=headers)
        assert updated.status_code == 200, updated.text
    collab._runs.clear()
    restarted, headers = _file_client(tmp_path, monkeypatch)
    response = restarted.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers)
    allowed = changed in {"timeout_secs", "name"}
    assert response.status_code == (200 if allowed else 409), response.text
    assert len(calls) == (2 if allowed else 1), "配置冲突不得触发额外计费请求"
    after = restarted.app.state.core.database.get_collab_run(run["run_id"])
    assert after["stages"][0] == before["stages"][0], "拒绝续跑也必须保留先前结果"
    assert after["next_index"] == (2 if allowed else 1)
