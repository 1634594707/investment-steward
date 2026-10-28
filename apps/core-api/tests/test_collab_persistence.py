"""重启续跑必须复用已付费阶段和原始证据快照。"""
import pytest
from investment_steward_core import collab, model_client
from test_collab_pipeline import _stage_reply_factory, _start_run
from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile


def test_restart_keeps_completed_stage_and_original_evidence(tmp_path, monkeypatch):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    reply = _stage_reply_factory()
    calls = []

    def post(*args, **kwargs):
        calls.append(args)
        return reply(*args, **kwargs)

    monkeypatch.setattr(model_client, "_post_json", post)
    run = _start_run(client, headers)
    first = client.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).json()
    assert first["executed_stage"] == "check" and first["executed_status"] == "done"
    database = client.app.state.core.database
    saved = database.get_collab_run(run["run_id"])
    collab._runs.clear()
    restarted, headers = _file_client(tmp_path, monkeypatch)
    second = restarted.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["executed_stage"] == "draft"
    assert len(calls) == 2, "重启不能重算已完成的数据核对"
    restored = restarted.app.state.core.database.get_collab_run(run["run_id"])
    assert restored["evidence"] == saved["evidence"]
    assert restored["input_hash"] == saved["input_hash"]


def test_inflight_restart_is_explicit_failure_not_automatic_resubmission(tmp_path):
    from investment_steward_core.storage.database import Database

    db = Database(tmp_path / "runs.sqlite3")
    run = collab.create_run("600001", "question", "tech", {"S1": "evidence"}, {}, None, "m")
    run["stages"][0]["status"] = "running"
    db.save_collab_run(run)
    collab._runs.clear()
    restored = collab.get_run(run["run_id"], db)
    assert restored["stages"][0]["status"] == "failed"
    assert "远端" in restored["stages"][0]["error"]
    assert restored["next_index"] == 0
    assert db.get_collab_run(run["run_id"])["stages"][0]["status"] == "failed"


@pytest.mark.parametrize("after_insert", [False, True])
def test_archive_failure_can_resume_without_new_model_calls_or_duplicate_reports(tmp_path, monkeypatch, after_insert):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(model_client, "_post_json", _stage_reply_factory())
    run = _start_run(client, headers)
    db = client.app.state.core.database
    original = db.insert_ai_research_report

    def fail_archive(item):
        if after_insert:
            original(item)
        raise OSError("simulated archive interruption")

    monkeypatch.setattr(db, "insert_ai_research_report", fail_archive)
    for _ in range(4):
        result = client.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).json()
    assert result["done"] is True and result["persisted"] is False
    assert db.get_collab_run(run["run_id"])["next_index"] == 4
    saved = db.get_collab_run(run["run_id"])
    saved["prompt_version"] = "older-prompt"
    saved.pop("profile_fingerprints", None)
    db.save_collab_run(saved)
    monkeypatch.setattr(db, "insert_ai_research_report", original)
    collab._runs.clear()
    restarted, headers = _file_client(tmp_path, monkeypatch)

    def never_call(*args, **kwargs):
        raise AssertionError("归档补偿不得重调已付费模型")

    monkeypatch.setattr(model_client, "_post_json", never_call)
    first = restarted.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).json()
    assert first["persisted"] is True
    report_id = first["collab_report_id"]
    archive = restarted.app.state.core.database.get_ai_research_report(report_id)
    second = restarted.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).json()
    assert second["persisted"] is True and second["collab_report_id"] == report_id
    assert restarted.app.state.core.database.get_ai_research_report(report_id) == archive
    reports = restarted.app.state.core.database.list_ai_research_reports("stock")
    assert len(reports) == 1
