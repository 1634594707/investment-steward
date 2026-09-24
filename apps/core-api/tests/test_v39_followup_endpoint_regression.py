"""v39 Q22：追问链路的业务回归（端点层，判据是「用户会不会被误导」）。

沿用 `test_v32_followup_and_cockpit` 的假模型与建库夹具，覆盖：
两种入口（Q10）、补证取数的成功/部分/失败三态（Q11）、连续追问承接与跨报告隔离（Q14/Q22）、
新增证据快照落库不丢（Q15）、旧记录兼容（Q03）、过期行情只作历史快照（Q06）。
断言全部围绕**输出里出现什么口径**，不是「字段存在」。
"""

from __future__ import annotations

from typing import Any

from test_v32_followup_and_cockpit import (  # 同目录 pytest 模块互用夹具
    _CannedModel,
    _setup,
    _v28_report_payload,
)

from investment_steward_core import analysis_followup


def make_report(test_client, headers) -> str:
    body = test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": "600001", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    return str(body["report_id"])


def follow_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "direct_answer": "不能仅凭原报告确认现在可以买入。",
        "answerability": "partially_answered",
        "key_conditions": ["放量站稳 15.21（行情截至 2026-09-19）"],
        "evidence_gaps": ["缺双十一区间的历史统计"],
        "affected_claims": [{"claim_id": "C1", "effect": "cannot_judge", "reason": "无新增证据。"}],
        "conclusion_change": "unchanged",
        "scenario_changes": [],
        "new_watchpoints": [
            {"signal": "双十一业务量与单票收入是否量价齐升", "verify_by": "2026-11-30",
             "date_basis": "", "event_anchor": "11 月经营简报披露后", "expected_if_true": "量价齐升则强化"},
        ],
        "price_refs": [{"level": 14.3, "role": "ma", "as_of": "", "window": "未来 5-10 个交易日"}],
        "answer": "原报告技术确认条件未满足，估值合理价值未评估。",
        "limitations": ["本轮未获取新数据"],
    }
    base.update(overrides)
    return base


def test_q10_endpoint_rejects_unknown_entry_mode(client, tmp_path, monkeypatch):
    _CannedModel(monkeypatch, report=_v28_report_payload(), followup=follow_payload())
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report_id = make_report(test_client, headers)
    response = test_client.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={"question": "现在可以买入吗", "mode": "auto_research_please"},
        headers=headers,
    )
    assert response.status_code == 422
    assert "supplement_research" in str(response.json())


def test_q10_interpret_mode_never_touches_market_feed(client, tmp_path, monkeypatch):
    """解读模式：一条新事实都不声称获取——真出网取数即失败（Q10 验收）。"""
    import investment_steward_core.api.app as app_module

    def _forbidden(*args: Any, **kwargs: Any):
        raise AssertionError("解读模式不应触发行情取数")

    monkeypatch.setattr(app_module, "fetch_cn_kline", _forbidden)
    _CannedModel(monkeypatch, report=_v28_report_payload(), followup=follow_payload())
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report_id = make_report(test_client, headers)
    body = test_client.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={"question": "现在可以买入吗，是不是双十一的时候股会涨？"},
        headers=headers,
    ).json()
    assert body["ok"] is True
    turn = body["turn"]
    assert turn["mode"] == "interpret" and turn["retrieval_status"]["state"] == "none"
    # 纯解读、又没贴材料 → 不挂「五环全缺」的证据链（那是噪声，不是诚实）。
    assert turn["catalyst_chain"] == {} and turn["answer_flags"] == []
    assert "解读本报告" in turn["mode_label"]
    # 无新材料 → 修订轴未修订 + 数据轴「沿用原报告判断，未更新行情」，五条判断合并成一行。
    assert turn["data_basis"] == "report_only" and turn["revision_status"] == "unrevised"
    assert "未更新行情" in turn["state_line"]
    assert turn["expanded_claims"] == [] and turn["shared_limitation"].startswith("C1")
    # 凭空截止日与「9 月均线当未来阈值」都被闸门拦下。
    point = turn["new_watchpoints"][0]
    assert point["due_on"] == "" and point["verify_by"] == "11 月经营简报披露后"
    assert "量价齐升＝业务量上升且单票收入上升" in " ".join(point["assumptions"])
    ref = turn["price_refs"][0]
    assert ref["as_of"] == turn["data_as_of"] and ref["snapshot_only"] is True
    assert "实时阈值" in ref["note"]
    # 估值口径不再笼统「无数据」：本样本给出了正常化盈利与公允价值 → 按「已评估」表述。
    view = turn["valuation_view"]
    assert view["state"] == "fair_value_assessed"
    assert view["verdict_display"] != "无数据" and view["note"]


def test_q11_research_mode_reports_every_gap_as_not_done(client, tmp_path, monkeypatch):
    """补充研究：本轮取数结果逐项如实登记，缺口永远不把「研究完成」说满（Q11 验收）。

    问句命中季节性口径时，「卖方一致预期」尚无来源可接（`plan_retrieval` 落成 not_available），
    因此补证状态**最高只能是 partial**——这就是「失败不被包装成完成」的机器可检查形式。
    """
    _CannedModel(monkeypatch, report=_v28_report_payload(), followup=follow_payload())
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report_id = make_report(test_client, headers)
    body = test_client.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={
            "question": "现在可以买入吗，估值到底贵不贵，是不是双十一的时候股会涨？",
            "mode": "supplement_research",
        },
        headers=headers,
    ).json()
    assert body["ok"] is True
    turn = body["turn"]
    status = turn["retrieval_status"]
    snapshot = turn["evidence_snapshot"]
    assert status["requested"] == len(snapshot) and status["requested"] >= 3
    assert status["succeeded"] + status["failed"] == status["requested"]
    # 一致预期无来源 → 必然记为失败项，状态不可能是 all_succeeded。
    assert status["state"] in ("partial", "all_failed")
    assert any("一致预期" in str(item["label"]) for item in status["failed_items"]), status["failed_items"]
    assert (
        sorted(item["label"] for item in status["failed_items"])
        == sorted(row["label"] for row in snapshot if row["status"] == "failed")
    )
    for row in snapshot:
        if row["status"] == "failed":
            assert row["error"]
        else:
            assert not row["error"] and row["retrieved_at"]
        assert "未提供可访问链接" in row["url_note"] and row["analysis_version"] == "1.2"
    kinds = {row["kind"] for row in snapshot}
    assert {"kline", "valuation", "seasonality", "missing_seasonality"} == kinds
    assert turn["data_basis"] in ("refreshed_data", "report_only")
    # 日历只覆盖到今天（数据未刷到 11 月）：模型的 11-30 依旧降级为事件锚定。
    assert turn["new_watchpoints"][0]["date_demoted_from"] == "2026-11-30"
    assert turn["new_watchpoints"][0]["due_on"] == ""


def test_q14_q22_follow_on_context_is_carried_and_never_crosses_reports(client, tmp_path, monkeypatch):
    """连续追问：第二轮带上首轮结论；切报告不串上下文；历史回答不被覆盖（Q14/Q22）。"""
    canned = _CannedModel(
        monkeypatch,
        report=_v28_report_payload(),
        followup=follow_payload(),
    )
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report_a = make_report(test_client, headers)
    report_b = make_report(test_client, headers)

    first = test_client.post(
        f"/ai-research/reports/{report_a}/follow-ups",
        json={"question": "现在可以买入吗"},
        headers=headers,
    ).json()["turn"]
    assert first["prior_turn_ids"] == [] and first["follows_prior_turn"] is False

    second = test_client.post(
        f"/ai-research/reports/{report_a}/follow-ups",
        json={"question": "那等突破呢"},
        headers=headers,
    ).json()["turn"]
    assert second["turn_index"] == 2
    assert second["prior_turn_ids"] == [first["analysis_turn_id"]]
    assert second["follows_prior_turn"] is True

    on_b = test_client.post(
        f"/ai-research/reports/{report_b}/follow-ups",
        json={"question": "那等突破呢"},
        headers=headers,
    ).json()["turn"]
    assert on_b["prior_turn_ids"] == [] and first["analysis_turn_id"] not in on_b["prior_turn_ids"]

    # 历史回答逐字不变（append-only），新证据不覆盖旧附录。
    stored = test_client.get(f"/ai-research/reports/{report_a}/follow-ups", headers=headers).json()["items"]
    assert len(stored) == 2
    assert stored[0]["answer"] == first["answer"]
    assert stored[0]["evidence_snapshot"] == first["evidence_snapshot"]
    assert canned.call_count == 5  # 2 次报告 + 3 次追问


def test_q03_legacy_turn_still_reports_honest_axes(client, tmp_path, monkeypatch):
    """1.1 版历史记录：三条轴按当时可得信息补全，且不暗示做过复核（Q03 验收）。"""
    legacy = {
        "conclusion_change": "unchanged",
        "answer": "直接回答：不能仅凭原报告确认买入。",
        "affected_claims": [{"claim_id": "C1", "effect": "cannot_judge", "reason": "无新信息"}],
        "supplement_evidence": [],
        "prompt_version": "1.1",
    }
    view = analysis_followup.status_view(legacy)
    assert view["legacy"] is True
    assert view["data_basis_label"] == "沿用原报告判断，未更新行情"
    assert view["revision_status"] == "unrevised"
    assert "旧版记录未评估" in view["answerability_label"]


def test_q13_catalyst_chain_gates_overreach_without_rewriting_the_answer(client, tmp_path, monkeypatch):
    """Q13：证据链缺「市场预期」环时，「超预期/必涨」被服务端点破，但模型原话逐字保留。"""
    _CannedModel(
        monkeypatch,
        report=_v28_report_payload(),
        followup=follow_payload(answer="双十一业务量增长、收入超预期，股价必涨。"),
    )
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report_id = make_report(test_client, headers)
    turn = test_client.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={
            "question": "双十一业务量涨了，是不是必涨？",
            "mode": "supplement_research",
            "supplements": [{
                "text": "8 月业务量同比 +18%，单票收入同比 -3%，归母净利 10.35 亿元",
                "source_name": "用户粘贴经营简报",
                "event_date": "2026-09-18",
            }],
        },
        headers=headers,
    ).json()["turn"]
    chain = turn["catalyst_chain"]
    assert [row["key"] for row in chain["steps"]] == ["volume", "price_cost", "profit", "expectation", "price_reaction"]
    assert chain["complete"] is False and chain["broken_at"] == "expectation"
    statuses = {row["key"]: row["status"] for row in chain["steps"]}
    assert statuses["volume"] == "evidenced" and statuses["price_cost"] == "evidenced"
    assert "不能宣称超预期" in "".join(row["note"] for row in chain["steps"])
    joined = " ".join(turn["answer_flags"])
    assert "超预期" in joined and "必涨" in joined and "不予采信" in joined
    assert turn["answer"] == "双十一业务量增长、收入超预期，股价必涨。"  # 只标注，不改写


def test_q05_q09_q08_legacy_report_view_is_hydrated_without_touching_storage(client, tmp_path, monkeypatch):
    """旧报告回显：估值三态、来源目录、参考价位与「N/M」统计由服务端同一套纯函数补齐。"""
    _CannedModel(monkeypatch, report=_v28_report_payload())
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    report_id = make_report(test_client, headers)
    first = test_client.get(f"/ai-research/reports/{report_id}", headers=headers).json()["item"]
    assert first["valuation"]["valuation_evidence"]["state"] in (
        "metric_missing", "normalization_unverified", "fair_value_assessed"
    )
    assert first["valuation"]["valuation_evidence"]["verdict_display"] != "无数据"
    assert first["source_catalog"] and all(row["url_note"] for row in first["source_catalog"])
    assert first["core_support"]["counts"]["total"] == len(first["core_support"]["counted_claim_ids"])
    assert first["probability_view"]["probability_label"] == "倾向（未统计校准）"
    # 幂等：同一份 payload 两次补算结果一致（视图字段只加在读路径，不回写存储）。
    second = test_client.get(f"/ai-research/reports/{report_id}", headers=headers).json()["item"]
    assert second == first


def test_q21_review_queue_reports_five_states_without_promising_monitoring(client, tmp_path, monkeypatch) -> None:
    """Q21：待复盘队列的五态。**没有落成判断的条目一律不能显示「已加入跟踪」**。

    ⚠️ 这里的 `verify_by` **必须是一个远期日期**，不能写成「今天」或某个近期日期：
    `followup_research.tracking_state` 的判定是 `due_on <= today` → `pending_verify`，
    所以原先写死的 `2026-09-21 收盘后` 在 2026-09-21 当天运行时会让该条从
    `tracked` 变成 `pending_verify`，断言「未到期 → 已加入跟踪」直接失败
    （实测：2026-09-21 全量测试 1 failed，与本条无关的代码改动无关）。
    用远期常量日期，让这条断言与运行日期彻底解耦。
    """
    payload = _v28_report_payload()
    payload["watchpoints"] = [
        {"signal": "量能放大配合站上触发价", "verify_by": "2099-12-31 收盘后", "expected_if_true": "站上则短线确认"},
        {"signal": "月度经营数据能否延续", "verify_by": "下一次月度经营简报披露后", "expected_if_true": "延续则强化"},
    ]
    _CannedModel(monkeypatch, report=payload)
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    make_report(test_client, headers)
    body = test_client.get("/ai-research/review-queue?limit=50", headers=headers).json()
    assert body["ok"] is True and body["items"]
    vocab = {"suggest_observe", "tracked", "pending_verify", "verified", "unverifiable"}
    states = {row["tracking_state"] for row in body["items"]}
    assert states <= vocab
    # 带确切日期的已落成判断 → 已加入跟踪（未到期）；事件锚定的只能是建议观察。
    assert "tracked" in states and "suggest_observe" in states
    for row in body["items"]:
        assert row["tracking_label"]
        if row["tracking_state"] != "suggest_observe":
            assert row["judgment_id"], "未落成可回填判断的条目不能显示为已加入跟踪/待验证"
        if not row.get("due_on"):
            assert row["tracking_state"] == "suggest_observe"
            assert "事件" in row["tracking_note"]
    tracking = body["tracking"]
    assert tracking["auto_monitoring"] is False and "自动监控" in tracking["note"]
    assert any(label in tracking["summary"] for label in ("建议观察", "已加入跟踪", "待验证", "已验证", "无法验证"))
    assert sum(tracking["counts"].values()) == len(body["items"])
