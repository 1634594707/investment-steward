"""列表/恢复查询不出网、不泄漏原始证据，重启后能发现旧任务。"""
from investment_steward_core import collab, model_client
from test_stock_research_tools import _file_client


def test_saved_runs_are_discoverable_without_model_calls(tmp_path, monkeypatch):
    client, headers = _file_client(tmp_path, monkeypatch)
    db = client.app.state.core.database
    run = collab.create_run("600001", "研究问题", "private-tech", {"S1": "private-source"}, {}, None, "m")
    db.save_collab_run(run)
    collab._runs.clear()

    def no_model(*args, **kwargs):
        raise AssertionError("查看任务不能触发模型")

    monkeypatch.setattr(model_client, "call_active_model", no_model)
    restarted, headers = _file_client(tmp_path, monkeypatch)
    assert restarted.get("/evidence/collab-state").status_code in (401, 403)
    listed = restarted.get("/evidence/collab-state", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["run_id"] == run["run_id"]
    assert "private-source" not in listed.text and "private-tech" not in listed.text
    restored = restarted.get("/evidence/collab-state", params={"run_id": run["run_id"]}, headers=headers)
    assert restored.status_code == 200
    assert restored.json()["run"]["next_index"] == 0
    assert "private-source" not in restored.text


def test_archived_runs_do_not_hide_older_pending_runs(tmp_path, monkeypatch):
    client, headers = _file_client(tmp_path, monkeypatch)
    db = client.app.state.core.database
    pending = collab.create_run("600001", "pending", "tech", {}, {}, None, "m")
    db.save_collab_run(pending)
    for _ in range(22):
        archived = collab.create_run("600002", "done", "tech", {}, {}, None, "m")
        archived["next_index"] = 4
        db.save_collab_run(archived)
        db.insert_ai_research_report({"report_id": archived["run_id"], "kind": "stock", "subject": "600002", "generated_at": archived["created_at"]})
    items = client.get("/evidence/collab-state", headers=headers).json()["items"]
    assert [item["run_id"] for item in items] == [pending["run_id"]]


def test_summary_distinguishes_failed_pending_cancelled_and_archive_without_raw_errors(tmp_path, monkeypatch):
    client, headers = _file_client(tmp_path, monkeypatch)
    db = client.app.state.core.database
    runs = {}
    for state in ("pending", "failed", "running", "cancelled", "archive"):
        run = collab.create_run("600001", state, "private-tech", {"S1": "private-source"}, {}, None, "m")
        if state in ("failed", "running"):
            run["next_index"] = 1
            run["stages"][0]["status"] = "done"
            run["stages"][1].update(status=state, error="private-error")
        if state == "archive":
            run["next_index"] = 4
        if state == "cancelled":
            run["cancelled_at"] = "2026-10-26T01:00:00Z"
        db.save_collab_run(run)
        runs[state] = run
    before = db.export_payload_tables()["collab_runs"]
    response = client.get("/evidence/collab-state", headers=headers)
    items = {item["question"]: item for item in response.json()["items"]}
    assert {key: item["recovery_state"] for key, item in items.items()} == {
        "pending": "pending", "failed": "failed", "running": "running_or_interrupted",
        "cancelled": "cancelled", "archive": "archive_pending",
    }
    assert items["failed"]["stage"] == "draft"
    assert items["failed"]["stage_label"] == runs["failed"]["stages"][1]["label"]
    assert "private-" not in response.text
    assert db.export_payload_tables()["collab_runs"] == before
