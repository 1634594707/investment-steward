"""取消在途任务后，迟到模型结果不得推进或归档。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from investment_steward_core import collab, model_client
from test_collab_pipeline import _stage_reply_factory, _start_run
from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile


@pytest.mark.parametrize("completed", [0, 3])
def test_cancel_discards_inflight_result_and_survives_restart(tmp_path, monkeypatch, completed):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    reply = _stage_reply_factory()
    monkeypatch.setattr(model_client, "_post_json", reply)
    run = _start_run(client, headers)
    body = {"run_id": run["run_id"]}
    for _ in range(completed):
        assert client.post("/evidence/collab-next", json=body, headers=headers).status_code == 200
    before = client.app.state.core.database.get_collab_run(run["run_id"])
    entered, release = Event(), Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), "测试必须释放模型请求"
        return reply(*args, **kwargs)

    monkeypatch.setattr(model_client, "_post_json", blocked)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(client.post, "/evidence/collab-next", json=body, headers=headers)
        try:
            assert entered.wait(5)
            assert client.post("/evidence/collab-cancel", json=body).status_code == 401
            for _ in range(2):
                response = client.post("/evidence/collab-cancel", json=body, headers=headers)
                assert response.status_code == 200, response.text
                assert response.json()["run"]["cancelled_at"]
        finally:
            release.set()
        result = future.result(timeout=5).json()
    assert result["executed_status"] == "cancelled"
    assert not result["done"] and not result.get("collab_report_id")
    db = client.app.state.core.database
    saved = db.get_collab_run(run["run_id"])
    assert saved["next_index"] == completed
    assert saved["stages"][:completed] == before["stages"][:completed]
    assert saved["stages"][completed]["result"] is None
    assert db.get_ai_research_report(run["run_id"]) is None
    collab._runs.clear()
    restarted, headers = _file_client(tmp_path, monkeypatch)
    assert restarted.post("/evidence/collab-next", json=body, headers=headers).status_code == 409
    assert restarted.get("/evidence/collab-state", params=body, headers=headers).json()["run"]["cancelled_at"]
    items = restarted.get("/evidence/collab-state", headers=headers).json()["items"]
    assert next(item for item in items if item["run_id"] == run["run_id"])["cancelled_at"]


@pytest.mark.parametrize("completed", [0, 4])
def test_idle_cancel_or_finished_rejection(tmp_path, monkeypatch, completed):
    client, headers = _file_client(tmp_path, monkeypatch)
    client.post("/plugins/official.cn-market-data/install", headers=headers)
    _setup_active_profile(client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(model_client, "_post_json", _stage_reply_factory())
    run = _start_run(client, headers)
    body = {"run_id": run["run_id"]}
    for _ in range(completed):
        client.post("/evidence/collab-next", json=body, headers=headers)
    db = client.app.state.core.database
    before = db.get_collab_run(run["run_id"])
    response = client.post("/evidence/collab-cancel", json=body, headers=headers)
    assert response.status_code == (200 if completed == 0 else 409)
    if completed == 4:
        assert db.get_collab_run(run["run_id"]) == before
    else:
        assert client.post("/evidence/collab-next", json=body, headers=headers).status_code == 409
        assert db.get_collab_run(run["run_id"])["next_index"] == 0
    assert client.post("/evidence/collab-cancel", json={"run_id": "missing-run"}, headers=headers).status_code == 404
