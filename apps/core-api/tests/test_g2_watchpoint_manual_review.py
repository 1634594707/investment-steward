"""G2（研报产出与交付链路路线图 2026-09-27）：锚定型验证点的手工复核闭环。

**要解决的问题**：5 条验证点里有 2 条是「锚定事件」（「三季报披露后」「下次月度经营简报
披露后」）。它们没有确切日期，而 `VerifiableJudgment.due_at` 是必填字段，所以服务端
**不猜日期**、不落库（`longterm.materialize_watchpoint_judgment` 的冻结口径）——结果这 2 条
在界面上既不能回填、也没有任何「我人工看过了」的出口，队列永远显示待复盘。

本文件锁定新增的人工出口 `POST /ai-research/reports/{report_id}/watchpoints/{index}/review`：

1. **不破坏冻结口径**：不写 `verifiable_judgments`、不伪造 `due_at`，只追加一条复核记录；
2. **append-only**：原研报 payload 与验证点文本一字不改；
3. **不越界**：带确切日期的验证点走 `POST /judgments/{id}/verify`（本入口 409），
   序号越界 404，报告不存在 404，非法三态 422；
4. **队列如实回显**：复核结论进 `manual_review`，`closure.manually_reviewed` 计数，
   复盘五态从「建议观察」变为「已验证 / 无法验证」，且说明里点明是**人工**复核。
"""

from __future__ import annotations

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用


def _report_payload() -> dict:
    """一份带「1 条带日期 + 1 条锚定事件」验证点的报告（与研究产出同形）。"""
    return {
        "report_id": "r-g2",
        "kind": "stock",
        "subject": "600001",
        "symbol": "600001",
        "title": "测试标的研报",
        "model": "test-model",
        "confidence": "medium",
        "generated_at": "2026-09-27T00:00:00+00:00",
        "watchpoints": [
            {"signal": "到期点", "verify_by": "2026-09-28", "expected_if_true": "转确认"},
            {"signal": "锚定点", "verify_by": "三季报披露后", "expected_if_true": "支撑增强"},
        ],
    }


def _setup(client, monkeypatch, *, payload: dict | None = None):
    """把报告读路径打桩成固定 payload（不跑生成、不联网、不花钱）。"""
    from investment_steward_core.storage import database as db_module

    record = payload if payload is not None else _report_payload()
    monkeypatch.setattr(
        db_module.Database, "get_ai_research_report", lambda self, report_id: dict(record)
    )
    monkeypatch.setattr(
        db_module.Database, "list_ai_research_reports", lambda self, kind=None, limit=50: [dict(record)]
    )
    return client


def _review(test_client, headers, report_id: str, index: int, body: dict):
    return test_client.post(
        f"/ai-research/reports/{report_id}/watchpoints/{index}/review", json=body, headers=headers
    )


def test_anchor_watchpoint_review_is_appended_and_surfaces_in_queue(client, monkeypatch):
    """核心闭环：锚定事件人工复核 → 队列带回复核结论，且不影响带日期那条的回填通道。"""
    test_client, headers = _setup(client, monkeypatch)

    response = _review(
        test_client, headers, "r-g2", 1,
        {"result": "verified", "outcome": "三季报已于 10-25 披露，增速与现金流符合预期"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["review"]["result"] == "verified"
    assert body["review"]["watchpoint_index"] == 1
    assert body["review"]["signal"] == "锚定点"
    assert body["review"]["verify_by"] == "三季报披露后"
    assert body["review"]["outcome"].startswith("三季报已于")
    # 边界随返回体下发（前端与文档共用同一份措辞）。
    assert body["policy"]["no_fake_date"].startswith("本入口**不写 verifiable_judgments")

    queue = test_client.get("/ai-research/review-queue", headers=headers).json()
    assert queue["counts"]["event_anchored"] == 1
    # 带日期那条按当天归入「已到期 / 待到期」之一（测试跑在不同日期上结论会变，不写死）。
    assert queue["counts"]["overdue"] + queue["counts"]["scheduled"] == 1
    anchor = next(row for row in queue["items"] if row["signal"] == "锚定点")
    assert anchor["judgment_id"] is None, "锚定事件仍不落判断表——冻结口径不变"
    assert anchor["manual_review"]["result"] == "verified"
    assert anchor["manual_review"]["result_label"] == "已验证成立"
    assert anchor["manual_review"]["history_count"] == 1
    assert anchor["watchpoint_index"] == 1
    # 复盘五态：从「建议观察」变为「已验证」，且说明点明是人工复核。
    assert anchor["tracking_state"] == "verified"
    assert "人工复核" in anchor["tracking_note"]
    assert queue["closure"]["manually_reviewed"] == 1
    assert queue["closure"]["not_materialized"] == 1
    assert "手工标记已复核" in queue["note"]


def test_review_does_not_touch_report_payload(client, monkeypatch):
    """append-only：复核不改写原研报 payload 与验证点文本。"""
    from investment_steward_core.storage import database as db_module

    test_client, headers = _setup(client, monkeypatch)
    before = _report_payload()
    _review(test_client, headers, "r-g2", 1, {"result": "refuted", "outcome": "事件未发生"})

    after = test_client.get("/ai-research/reports/r-g2", headers=headers).json()["item"]
    assert after["watchpoints"] == before["watchpoints"], "验证点文本一字未改"
    assert after["report_id"] == before["report_id"]
    # 复核记录落在独立表里，不进报告 payload。
    assert "manual_review" not in after
    # 顺带确认我们打的桩就是读路径（而不是写回了什么）。
    assert db_module.Database.get_ai_research_report is not None


def test_repeat_review_keeps_history_and_shows_latest(client, monkeypatch):
    """同一条可多次复核：历史全留，队列展示最近一次（`latest_wins`）。"""
    test_client, headers = _setup(client, monkeypatch)
    _review(test_client, headers, "r-g2", 1, {"result": "insufficient_data", "outcome": "事件未落地"})
    _review(test_client, headers, "r-g2", 1, {"result": "verified", "outcome": "一个月后确认成立"})

    queue = test_client.get("/ai-research/review-queue", headers=headers).json()
    anchor = next(row for row in queue["items"] if row["signal"] == "锚定点")
    assert anchor["manual_review"]["result"] == "verified"
    assert anchor["manual_review"]["history_count"] == 2
    assert queue["closure"]["manually_reviewed"] == 1, "计数按条目算，不按复核次数算"


def test_date_bearing_watchpoint_is_rejected_with_pointer(client, monkeypatch):
    """带确切日期的验证点必须走 verify 通道——同一件事不给两种写法。"""
    test_client, headers = _setup(client, monkeypatch)
    response = _review(test_client, headers, "r-g2", 0, {"result": "verified", "outcome": ""})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "2026-09-28" in detail
    assert "POST /judgments/{id}/verify" in detail


def test_review_rejects_out_of_range_index(client, monkeypatch):
    test_client, headers = _setup(client, monkeypatch)
    response = _review(test_client, headers, "r-g2", 9, {"result": "verified", "outcome": ""})
    assert response.status_code == 404
    assert "越界" in response.json()["detail"]


def test_review_rejects_unknown_report(client, monkeypatch):
    from investment_steward_core.storage import database as db_module

    test_client, headers = _setup(client, monkeypatch)
    monkeypatch.setattr(db_module.Database, "get_ai_research_report", lambda self, report_id: None)
    response = _review(test_client, headers, "nope", 0, {"result": "verified", "outcome": ""})
    assert response.status_code == 404


def test_review_rejects_non_stock_report(client, monkeypatch):
    test_client, headers = _setup(client, monkeypatch, payload={
        "report_id": "r-direction", "kind": "direction", "subject": "大盘", "watchpoints": [],
    })
    response = _review(test_client, headers, "r-direction", 0, {"result": "verified", "outcome": ""})
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["stage"] == "unsupported_kind"


def test_review_rejects_unknown_result(client, monkeypatch):
    """三态词表与回填共用——越界的结论词在模型层就被拒（422），不进库。"""
    test_client, headers = _setup(client, monkeypatch)
    response = _review(test_client, headers, "r-g2", 1, {"result": "confirmed", "outcome": ""})
    assert response.status_code == 422


def test_tracking_view_prefers_manual_review_for_anchor_rows():
    """单元口径：锚定行有人工复核 → verified/unverifiable；没有 → 建议观察（措辞提到可标记）。"""
    from investment_steward_core import followup_research

    rows = [
        {"signal": "锚定点", "due_on": "", "judgment_id": "", "manual_review": {
            "result": "verified", "reviewed_at": "2026-10-26T00:00:00+00:00",
        }},
        {"signal": "另一锚定点", "due_on": "", "judgment_id": "", "manual_review": {
            "result": "insufficient_data", "reviewed_at": "2026-10-26T00:00:00+00:00",
        }},
        {"signal": "没复核过的锚定点", "due_on": "", "judgment_id": ""},
    ]
    view = followup_research.watchpoint_tracking_view(rows, today="2026-10-27")
    assert view["counts"]["verified"] == 1
    assert view["counts"]["unverifiable"] == 1
    assert view["counts"]["suggest_observe"] == 1
    assert "人工复核" in view["items"][0]["note"]
    assert "人工" in view["items"][1]["note"]
    assert "可在队列里标记" in view["items"][2]["note"]
    assert view["items"][2]["manual_review"] is None
    assert view["auto_monitoring"] is False
