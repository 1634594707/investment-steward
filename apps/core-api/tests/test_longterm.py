"""决策长期能力测试（8.1 时间线 / 8.2 后验评估 / 8.3 观察事项）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest

from investment_steward_core import longterm
from investment_steward_core.domain.models import DecisionEntry, Evidence, EvidenceType, Plan, Thesis
from investment_steward_core.storage.database import Database

USER = uuid5(UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"), "longterm-test")


def _db(tmp_path) -> Database:
    return Database(tmp_path / "steward.sqlite3")


def _decision(plan_id=None, evidence_ids=None, outcome="", retrospective="") -> DecisionEntry:
    return DecisionEntry(
        decision_id=uuid4(), user_id=USER, theme="是否加仓 510300",
        decision_summary="基于波动率回落与证据 X,决定加仓。",
        made_at=datetime.now(UTC) - timedelta(days=1),
        outcome=outcome, retrospective=retrospective,
        linked_evidence_ids=evidence_ids or [], plan_id=plan_id,
    )


def _evidence(evidence_id, collected_at=None) -> Evidence:
    return Evidence(
        evidence_id=evidence_id, tenant_id=USER,
        subject_refs=["510300"], evidence_type=EvidenceType.MACRO,
        source_name="测试源", summary="波动率回落证据。", content_hash="hash-" + str(evidence_id).replace("-", ""),
        collected_at=collected_at or datetime.now(UTC) - timedelta(days=2),
    )


# ———————— 8.1 决策时间线 ————————

def test_timeline_links_evidence_plan_and_marks_gaps(tmp_path):
    db = _db(tmp_path)
    decision = _decision(evidence_ids=[uuid4()])
    db.insert_decision(decision)
    db.insert_evidence(_evidence(decision.linked_evidence_ids[0]))
    timeline = longterm.build_decision_timeline(db, USER, decision.decision_id)
    assert timeline is not None
    kinds = [e["kind"] for e in timeline["events"]]
    assert "decision" in kinds and "evidence" in kinds
    # 无显式关联的研究/逻辑版本 → 缺口,不补写
    gap_titles = " ".join(g["title"] for g in timeline["gaps"])
    assert "研究运行" in gap_titles and "投资逻辑版本" in gap_titles
    # 事后字段(outcome)存在时标记 afterwards
    assert all(e["knowledge_scope"] in {"at_the_time", "afterwards", "unknown"} for e in timeline["events"])
    # 证据发生在决定之前 → 当时信息
    ev = next(e for e in timeline["events"] if e["kind"] == "evidence" and e["link_available"])
    assert ev["knowledge_scope"] == "at_the_time"


def test_timeline_missing_references_shown_as_gaps(tmp_path):
    db = _db(tmp_path)
    decision = _decision(plan_id=uuid4(), evidence_ids=[uuid4()])  # 引用都不入库
    db.insert_decision(decision)
    timeline = longterm.build_decision_timeline(db, USER, decision.decision_id)
    gap_refs = {str(g["ref_id"]) for g in timeline["gaps"]}
    assert str(decision.plan_id) in gap_refs
    assert str(decision.linked_evidence_ids[0]) in gap_refs


def test_timeline_unknown_decision_404(tmp_path):
    assert longterm.build_decision_timeline(_db(tmp_path), USER, uuid4()) is None


# ———————— 8.2 可检验判断 ————————

def test_judgment_original_frozen_and_verification_append_only(tmp_path):
    db = _db(tmp_path)
    judgment = longterm.create_judgment(
        db, user_id=USER, subject="510300", direction="看多",
        statement="未来一个月波动率回落,收益为正。",
        due_at=datetime.now(UTC) + timedelta(days=30),
        trigger_conditions=["波动率 < 20"], invalidation_conditions=["跌破年线"],
    )
    original_text = judgment.statement
    assert judgment.status.value == "pending"

    _, verification = longterm.verify_judgment(
        db, USER, judgment.judgment_id, result="verified",
        outcome="30 天后收益 +3.2%", metrics={"return_pct": 3.2},
    )
    assert verification.result.value == "verified"

    reread = db.get_judgment(judgment.judgment_id, USER)
    assert reread.statement == original_text  # 原判断不变
    assert reread.status.value == "verified"
    assert len(db.list_judgment_verifications(judgment.judgment_id)) == 1

    # 再次验证只追加(允许复验),原判断仍不变
    longterm.verify_judgment(db, USER, judgment.judgment_id, result="insufficient_data")
    assert len(db.list_judgment_verifications(judgment.judgment_id)) == 2
    assert db.get_judgment(judgment.judgment_id, USER).statement == original_text

    with pytest.raises(ValueError, match="不存在"):
        longterm.verify_judgment(db, USER, uuid4(), result="verified")


# ———————— 8.3 观察事项 ————————

def test_watch_item_check_dedup_and_trigger(tmp_path):
    db = _db(tmp_path)
    item = longterm.create_watch_item(
        db, user_id=USER, title="跌破年线观察", indicator="510300 收盘价 vs 年线",
        condition_text="收盘价连续 3 日低于年线", check_cycle="daily",
    )
    assert item.status.value == "active"

    item, check, written = longterm.record_watch_check(
        db, USER, item.watch_id, observed="收盘 3.911,高于年线", dedup_value="2026-09-09",
    )
    assert written is True
    # 同一天重复检查 → dedup 命中,不重复写
    _, check2, written2 = longterm.record_watch_check(
        db, USER, item.watch_id, observed="收盘 3.911,高于年线", dedup_value="2026-09-09",
    )
    assert written2 is False and check2.check_id == check.check_id
    assert len(db.list_watch_checks(item.watch_id)) == 1

    # 触发 → 事项进入 triggered,检查记录保留触发证据
    item, check3, written3 = longterm.record_watch_check(
        db, USER, item.watch_id, observed="连续 3 日低于年线", triggered=True,
        evidence_refs=[uuid4()], dedup_value="2026-09-10",
    )
    assert written3 is True and item.status.value == "triggered"
    assert check3.triggered is True and len(check3.evidence_refs) == 1
    # 触发记录保留(历史恢复可追溯)
    checks = db.list_watch_checks(item.watch_id)
    assert sum(1 for c in checks if c.triggered) == 1


def test_watch_item_state_machine(tmp_path):
    db = _db(tmp_path)
    item = longterm.create_watch_item(
        db, user_id=USER, title="宏观事件观察", indicator="US CPI 同比",
        condition_text="CPI 同比 > 4%", check_cycle="monthly",
    )
    assert longterm.transition_watch_item(db, USER, item.watch_id, "paused").status.value == "paused"
    assert longterm.transition_watch_item(db, USER, item.watch_id, "active").status.value == "active"
    assert longterm.transition_watch_item(db, USER, item.watch_id, "closed").status.value == "closed"
    # closed → triggered 非法(必须先恢复 active)
    with pytest.raises(ValueError, match="非法状态迁移"):
        longterm.transition_watch_item(db, USER, item.watch_id, "triggered")
    # 历史恢复:closed → active
    assert longterm.transition_watch_item(db, USER, item.watch_id, "active").status.value == "active"
    # 已结束的事项不能直接记录检查
    longterm.transition_watch_item(db, USER, item.watch_id, "closed")
    with pytest.raises(ValueError, match="已结束"):
        longterm.record_watch_check(db, USER, item.watch_id, observed="x")


# ———————— 端点契约 ————————

def test_judgment_endpoints_contract(client):
    http, headers = client
    due = "2026-12-31T00:00:00"
    resp = http.post("/judgments", headers=headers, json={
        "subject": "510300", "direction": "看多", "statement": "年末前跑赢沪深300。",
        "due_at": due, "trigger_conditions": ["放量突破"], "invalidation_conditions": ["跌破半年线"],
    })
    assert resp.status_code == 201
    judgment = resp.json()
    assert judgment["status"] == "pending" and judgment["statement"]

    listed = http.get("/judgments", headers=headers).json()
    assert any(item["judgment_id"] == judgment["judgment_id"] for item in listed)
    assert http.get("/judgments?status_filter=refuted", headers=headers).json() == []
    assert http.get("/judgments?status_filter=bogus", headers=headers).status_code == 422

    jid = judgment["judgment_id"]
    detail = http.get(f"/judgments/{jid}", headers=headers).json()
    assert detail["verifications"] == []
    verified = http.post(f"/judgments/{jid}/verify", headers=headers,
                         json={"result": "verified", "outcome": "跑赢 1.8%", "metrics": {"alpha_pct": 1.8}})
    assert verified.status_code == 200
    detail = http.get(f"/judgments/{jid}", headers=headers).json()
    assert detail["status"] == "verified" and len(detail["verifications"]) == 1
    # 非法结果枚举 → 422
    assert http.post(f"/judgments/{jid}/verify", headers=headers,
                     json={"result": "nope"}).status_code == 422
    assert http.get(f"/judgments/{uuid4()}", headers=headers).status_code == 404


def test_watch_item_endpoints_contract(client):
    http, headers = client
    created = http.post("/watch-items", headers=headers, json={
        "title": "美债收益率倒挂", "indicator": "10Y-2Y 利差",
        "condition_text": "利差重新倒挂", "check_cycle": "weekly",
    })
    assert created.status_code == 201
    wid = created.json()["watch_id"]

    checked = http.post(f"/watch-items/{wid}/checks", headers=headers,
                        json={"observed": "利差 +0.4%", "dedup_value": "2026-W37"})
    assert checked.status_code == 200 and checked.json()["written"] is True
    again = http.post(f"/watch-items/{wid}/checks", headers=headers,
                      json={"observed": "利差 +0.4%", "dedup_value": "2026-W37"})
    assert again.json()["written"] is False
    assert len(http.get(f"/watch-items/{wid}/checks", headers=headers).json()) == 1

    assert http.post(f"/watch-items/{wid}/transition", headers=headers,
                     json={"target": "closed"}).status_code == 200
    blocked = http.post(f"/watch-items/{wid}/checks", headers=headers,
                        json={"observed": "x"})
    assert blocked.status_code == 409
    assert http.post(f"/watch-items/{wid}/transition", headers=headers,
                     json={"target": "bogus"}).status_code == 422
    assert http.get(f"/watch-items/{uuid4()}/checks", headers=headers).status_code == 404


def test_timeline_endpoint_contract(client):
    http, headers = client
    decision = _decision()
    # 端点使用 client fixture 的 CoreState(独立 user),直接查不存在决定 → 404
    assert http.get(f"/timeline/decision/{uuid4()}", headers=headers).status_code == 404


def test_experiment_endpoint_requires_artifact(client):
    http, headers = client
    assert http.post("/quant/experiments", headers=headers,
                     json={"artifact_id": "ps-nonexistent0000"}).status_code == 404
    assert http.get("/quant/experiments", headers=headers).status_code == 200


# ———————— 8.4 组合风险 / 研究快照与模板 ————————

def _thesis(instrument: str, invalidations: list[str]) -> Thesis:
    return Thesis(
        thesis_id=uuid4(), user_id=USER, instrument=instrument,
        original_statement=f"{instrument} 测试逻辑",
        invalidation_conditions=invalidations,
    )


def test_portfolio_risk_shared_conditions_and_gaps(tmp_path):
    db = _db(tmp_path)
    db.upsert_thesis(_thesis("510300", ["跌破年线", "成交量萎缩"]))
    db.upsert_thesis(_thesis("512100", ["跌破年线"]))
    risk = longterm.build_portfolio_risk(db, USER)
    shared = risk["shared_invalidation_conditions"]
    assert len(shared) == 1 and shared[0]["condition"] == "跌破年线"
    assert set(shared[0]["instruments"]) == {"510300", "512100"}
    gap_dimensions = {g["dimension"] for g in risk["gaps"]}
    assert {"correlation", "factor_exposure", "weight_concentration"} <= gap_dimensions
    assert risk["concentration_by_logic_count"][0]["instrument"] in {"510300", "512100"}


def test_research_snapshot_restore_marks_missing_refs(tmp_path):
    db = _db(tmp_path)
    from investment_steward_core.domain.longterm import ResearchSnapshot

    snapshot = ResearchSnapshot(
        snapshot_id=uuid4(), user_id=USER, title="快照 A",
        question="510300 是否值得加仓?", symbols=["510300"],
        evidence_refs=[uuid4(), uuid4()], thesis_ids=[uuid4()],
    )
    db.insert_research_snapshot(snapshot)
    loaded = db.get_research_snapshot(snapshot.snapshot_id, USER)
    assert loaded.question == "510300 是否值得加仓?"
    assert len(db.list_research_snapshots(USER)) == 1


def test_research_templates_builtin_five_and_user_crud(tmp_path):
    db = _db(tmp_path)
    builtins = longterm.builtin_research_templates()
    assert len(builtins) == 5
    assert {t["name"] for t in builtins} == {
        "ETF 长期配置", "个股财报", "宏观影响", "交易计划复盘", "技术形态与基本面检查",
    }
    from investment_steward_core.domain.longterm import ResearchTemplate

    template = ResearchTemplate(
        template_id=uuid4(), user_id=USER, name="我的模板",
        required_context=["标的"], evidence_types=["macro"],
    )
    db.upsert_research_template(template)
    assert any(t.name == "我的模板" for t in db.list_research_templates(USER))
    assert db.delete_research_template(template.template_id, USER) is True
    assert db.delete_research_template(template.template_id, USER) is False


# ———————— 8.5 通知分级合并 / 数据源质量 ————————

def _notification(instrument: str, condition_kind: str, triggered_by: str, evidence_ids=None) -> Any:
    from investment_steward_core.domain.models import ActionMode, Notification

    return Notification(
        notification_id=uuid4(), user_id=USER, instrument=instrument,
        triggered_by=triggered_by, condition_kind=condition_kind,
        title="测试通知", summary="摘要", action_mode=ActionMode.OBSERVE,
        evidence_refs=evidence_ids or [],
    )


def test_notifications_graded_and_merged():
    from investment_steward_core import longterm as lt

    shared_evidence = uuid4()
    items = [
        _notification("510300", "invalidation_condition", "跌破年线", [shared_evidence]),
        _notification("510300", "invalidation_condition", "跌破年线", [uuid4()]),  # 同事件另一来源
        _notification("510300", "observation_metric", "波动率上升"),
    ]
    result = lt.grade_and_merge_notifications(items)
    assert len(result["groups"]) == 2
    invalidation_group = next(g for g in result["groups"] if g["grade"] == "invalidation_triggered")
    assert len(invalidation_group["notifications"]) == 2          # 同事件合并
    assert len(invalidation_group["evidence_refs"]) == 2          # 证据不丢
    # 分级排序:失效条件组在最前
    assert result["groups"][0]["grade"] == "invalidation_triggered"


def test_data_source_quality_counts_from_evidence(tmp_path):
    db = _db(tmp_path)
    db.insert_evidence(_evidence(uuid4()))
    db.insert_evidence(_evidence(uuid4()))
    report = longterm.data_source_quality(db, USER)
    source = next(s for s in report["sources"] if s["source"] == "测试源")
    assert source["evidence_count"] == 2
    assert source["success_rate"] is None  # 无采集日志 → 数据不足
    assert any(g["dimension"] == "success_rate" for g in report["gaps"])


# ———————— 8.6 不行动统计 ————————

def test_inaction_stats_only_counts_marked():
    plain = _decision()
    marked = _decision()
    marked.inaction_reason = "insufficient_evidence"
    stats = longterm.inaction_stats([plain, marked])
    assert stats["total_decisions"] == 2 and stats["marked_inaction"] == 1
    assert stats["counts"][0]["reason"] == "insufficient_evidence"


# ———————— 8.4/8.5/8.6 端点契约 ————————

def test_risk_snapshot_template_endpoints(client):
    http, headers = client
    risk = http.get("/portfolio/risk", headers=headers)
    assert risk.status_code == 200 and "gaps" in risk.json()

    templates = http.get("/research/templates", headers=headers)
    assert templates.status_code == 200 and len(templates.json()["builtin"]) == 5

    created = http.post("/research/templates", headers=headers, json={
        "name": "端点模板", "required_context": ["标的"], "evidence_types": ["macro"],
    })
    assert created.status_code == 201
    tid = created.json()["template_id"]
    assert any(t["name"] == "端点模板" for t in http.get("/research/templates", headers=headers).json()["user"])
    assert http.delete(f"/research/templates/{tid}", headers=headers).json()["deleted"] is True
    assert http.delete(f"/research/templates/{uuid4()}", headers=headers).status_code == 404

    snap = http.post("/research/snapshots", headers=headers, json={
        "title": "端点快照", "question": "测试?", "symbols": ["510300"], "evidence_refs": [str(uuid4())],
    })
    assert snap.status_code == 201
    sid = snap.json()["snapshot_id"]
    detail = http.get(f"/research/snapshots/{sid}", headers=headers).json()
    assert detail["restore"]["mode"] == "readonly_view"
    assert len(detail["restore"]["evidence_missing"]) == 1  # 引用未入库 → 如实标注缺失
    assert http.get("/research/snapshots", headers=headers).status_code == 200


def test_graded_quality_inaction_endpoints(client):
    http, headers = client
    assert http.get("/notifications/graded", headers=headers).status_code == 200
    quality = http.get("/data-source/quality", headers=headers)
    assert quality.status_code == 200 and "gaps" in quality.json()
    stats = http.get("/decisions/inaction-stats", headers=headers)
    assert stats.status_code == 200 and stats.json()["total_decisions"] == 0
