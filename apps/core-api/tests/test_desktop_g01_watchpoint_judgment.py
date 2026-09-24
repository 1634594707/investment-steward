"""G01（桌面端升级路线图 2026-09-18）：研报验证点可回填。

冻结口径（这些断言是契约，改动前先读端点 docstring）：
1. **幂等落库**：读取 `/ai-research/review-queue` 会把**带确切日期的**验证点落成
   `verifiable_judgments`，判断 id 由 `report_id` + 序号确定性派生——重复读不会长出重复记录；
2. **不猜日期**：锚定事件（「三季报披露后」）没有 `due_on`，而 `due_at` 是必填字段，故不落库并
   如实给出原因（`reason` 文案里必须能看出是「没有日期」，不是随机失败）；
3. **原文冻结**：回填走既有 `POST /judgments/{id}/verify`——只追加验证结果并推状态，
   判断 `statement` 与研报 payload **逐字节不变**（append-only，与 `report_quality` 既有约定一致）；
4. **闭环可数**：`closure` 的分母只含已落库的判断，锚定事件点不计入分母。

行情/新闻/财报/估值取数一律 monkeypatch，绝不真实联网。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from uuid import UUID

import pytest

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import longterm as longterm_service


def _report_json(**extra) -> str:
    base = {
        "title": "乙股：验证点回填",
        "executive_summary": "结论：技术面缓涨，基本面稳健。",
        "report": (
            "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大，结构完好 [S1]\n"
            "### 消息面\n公司公告中标大单，市场关注度明显上升 [S2]\n"
            "### 基本面\n营收净利稳健增长，经营现金流为正 [S4]\n"
            "### 综合判断\n趋势向好但估值已不便宜，总体中性看待，跟踪量能确认。"
        ),
        "citations": ["S1", "S2", "S4"],
        "confidence": "medium",
        "valuation": {"verdict": "合理"},
    }
    base.update(extra)
    return json.dumps(base, ensure_ascii=False)


def _two_call_model(report: str, counter: str | None):
    calls: list[dict] = []

    def _fake(base_url, json_payload, api_key, timeout):
        calls.append(json_payload)
        content = report if len(calls) == 1 else (counter if counter is not None else report)
        return {"choices": [{"message": {"content": content}}]}

    return _fake, calls


def _seed_report_with_watchpoints(tmp_path, monkeypatch, watchpoints: list[dict[str, object]]):
    """落一份带验证点的研报，返回 `(test_client, headers, due_report_id)`。

    与 v27 用例同路：真实走 `/evidence/stock-research-report`，模型调用被 monkeypatch。
    """
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report = _report_json(watchpoints=watchpoints)
    fake, _ = _two_call_model(report, None)
    monkeypatch.setattr(mc, "_post_json", fake)
    assert test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600002"}, headers=headers
    ).json()["ok"] is True
    return test_client, headers


def _dated_watchpoints() -> list[dict[str, object]]:
    today = date.today()
    return [
        {"signal": "已到期的信号", "verify_by": (today - timedelta(days=3)).isoformat(),
         "expected_if_true": "倾向乐观"},
        {"signal": "未来的信号", "verify_by": (today + timedelta(days=30)).isoformat(),
         "expected_if_true": "倾向乐观"},
        {"signal": "事件锚定信号", "verify_by": "三季报披露后", "expected_if_true": "偏空"},
    ]


def test_review_queue_materializes_dated_watchpoints_idempotently(tmp_path, monkeypatch):
    """★ 读两次不会多出记录：判断 id 是 `uuid5(report_id, index)` 确定性派生。"""
    test_client, headers = _seed_report_with_watchpoints(tmp_path, monkeypatch, _dated_watchpoints())
    database = test_client.app.state.core.database

    first = test_client.get("/ai-research/review-queue", headers=headers).json()
    dated = [item for item in first["items"] if item["judgment_id"]]
    assert len(dated) == 2  # 已到期 + 未来各一条；锚定事件不落库

    report_id = str(dated[0]["report_id"])
    # id 与派生规则逐字一致（不是随机 UUID、也不是数据库自增）。
    assert dated[0]["judgment_id"] == str(longterm_service.watchpoint_judgment_id(report_id, 0))

    before = len(database.list_judgments(test_client.app.state.core.local_user_id))
    second = test_client.get("/ai-research/review-queue", headers=headers).json()
    after = len(database.list_judgments(test_client.app.state.core.local_user_id))
    assert before == after == 2
    assert [item["judgment_id"] for item in second["items"]] == [item["judgment_id"] for item in first["items"]]


def test_materialized_judgment_carries_source_report_and_original_wording(tmp_path, monkeypatch):
    """判断字段全部来自原验证点，不润色、不补方向；来源报告与证据锚点挂在判断上。"""
    test_client, headers = _seed_report_with_watchpoints(tmp_path, monkeypatch, _dated_watchpoints())
    core = test_client.app.state.core

    body = test_client.get("/ai-research/review-queue", headers=headers).json()
    overdue = body["items"][0]
    judgment = core.database.get_judgment(UUID(str(overdue["judgment_id"])), core.local_user_id)
    assert judgment is not None
    assert judgment.statement == overdue["signal"] == "已到期的信号"
    assert judgment.direction == longterm_service.WATCHPOINT_DIRECTION
    assert judgment.source_report_id == str(overdue["report_id"])
    # 验证点的「验证方式」原话进 source_refs（回填时能回到原文口径）。
    assert judgment.source_refs == [str(overdue["verify_by"])]
    assert judgment.trigger_conditions == ["倾向乐观"]
    # 到期时间只取解析出的确切日期（当天 00:00 UTC），不猜、不补。
    assert judgment.due_at.date().isoformat() == str(overdue["due_on"])


def test_event_anchored_watchpoint_is_not_materialized_with_reason(tmp_path, monkeypatch):
    """锚定事件没有日期 → 不落库，且原因要能看出是「没有确切日期」而非随机失败。"""
    test_client, headers = _seed_report_with_watchpoints(tmp_path, monkeypatch, _dated_watchpoints())

    body = test_client.get("/ai-research/review-queue", headers=headers).json()
    anchored = [item for item in body["items"] if item["bucket"] == "event_anchored"]
    assert len(anchored) == 1
    assert anchored[0]["judgment_id"] is None
    assert anchored[0]["verification"] is None
    assert "不猜日期" in str(anchored[0]["materialization_note"])
    assert anchored[0]["status"] == "pending_review"  # 未落库时保留研报内嵌口径
    assert body["closure"]["not_materialized"] == 1


def test_refill_advances_status_and_keeps_report_payload_byte_identical(tmp_path, monkeypatch):
    """★ 验收主例：到期判断可被标为「失效」，且原文与研报 payload 一字不改（append-only）。"""
    test_client, headers = _seed_report_with_watchpoints(tmp_path, monkeypatch, _dated_watchpoints())
    core = test_client.app.state.core

    before_queue = test_client.get("/ai-research/review-queue", headers=headers).json()
    target = before_queue["items"][0]
    report_id = str(target["report_id"])
    payload_before = test_client.get(f"/ai-research/reports/{report_id}", headers=headers).text
    assert before_queue["closure"] == {
        "materialized": 2, "filled": 0, "overdue_unfilled": 1, "not_materialized": 1,
    }

    response = test_client.post(
        f"/judgments/{target['judgment_id']}/verify",
        json={"result": "refuted", "outcome": "跌破 MA20 且量能萎缩"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["judgment"]["status"] == "refuted"

    after_queue = test_client.get("/ai-research/review-queue", headers=headers).json()
    refilled = next(item for item in after_queue["items"] if item["judgment_id"] == target["judgment_id"])
    assert refilled["status"] == "refuted"
    assert refilled["verification"]["result"] == "refuted"
    assert refilled["verification"]["outcome"] == "跌破 MA20 且量能萎缩"
    assert after_queue["closure"] == {
        "materialized": 2, "filled": 1, "overdue_unfilled": 0, "not_materialized": 1,
    }

    # append-only：判断原文与研报 payload 都不因回填而变化。
    judgment = core.database.get_judgment(UUID(str(target["judgment_id"])), core.local_user_id)
    assert judgment is not None and judgment.statement == "已到期的信号"
    assert judgment.source_report_id == report_id
    assert test_client.get(f"/ai-research/reports/{report_id}", headers=headers).text == payload_before
    # 回填记录本身是可追溯的追加行（含回填时的实际结果描述）。
    verifications = core.database.list_judgment_verifications(UUID(str(target["judgment_id"])))
    assert [v.result.value for v in verifications] == ["refuted"]


def test_review_queue_reports_filled_and_overdue_counts_on_fresh_database(tmp_path, monkeypatch):
    """空库时闭环计数全 0，且不出现「有样本」的假象。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    body = test_client.get("/ai-research/review-queue", headers=headers).json()
    assert body["items"] == []
    assert body["closure"] == {
        "materialized": 0, "filled": 0, "overdue_unfilled": 0, "not_materialized": 0,
    }
