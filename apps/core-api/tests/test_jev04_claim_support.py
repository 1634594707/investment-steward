"""JV04 契约测试：研报质量闸门的「逐 claim 引用支撑校验」（语义层）。

补的缺口（`report_quality.py` 开头第 7-8 行与 `claim_conflict_signals.conflict_note` 反复自认）：
v27/v28 的 `support` 由**确定性集合运算**得出（引用是否真实存在 / 是否覆盖所需字段 /
证据等级是否达标），**但「引用的这段内容是否真的支撑这条判断」从未校验**——
拿一条新闻稿去支撑「主业减亏」，在确定性口径下与拿财报支撑长得一模一样。

覆盖：
1. 纯函数：state 只收「有原句 + 有已取到的所引证据」的判断，三类跳过原因各自如实标注；
2. 纯函数：state 送的是证据片段 + 来源等级标签，不送整份正文；超预算整条跳过（不截一半）；
3. 纯函数：每条判断两问（`supports` choice / `invented` noul），题号与 state 的「判断 N」对齐；
4. 纯函数：合并规则——不支撑/无关降级、部分支撑只标注、编造=yes 降到 none 并出强告警；
5. 纯函数：降级后 support_counts / core_conclusion_state 同步重算（不出现自相矛盾）；
6. 纯函数：answers=None → available=False，判定**原样不动**（关掉就真的关掉）；
7. 闸门：注入回调 → 结果带 jev 回执；回调抛错 → 不阻断交付、如实记进 note；
8. 闸门：不注入回调 → 与接入前一致（无 jev 键、无 jev 告警）；
9. 端点：Jev 启用 → body["jev"] 有回执、`purpose=jev:claim-support` 落 `model_calls`；
10. 端点：Jev 未启用 → 零 Jev 请求、body["jev"] is None。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from investment_steward_core import credential_store, jev_client, report_quality

# —— 合成来源内容（「来源 id → 实际内容」，与 app.py 的 sources 同形状）——

_SOURCE_TEXTS: dict[str, str] = {
    "S2": "近期新闻（东方财富）：\n- 甲股中标大单（2026-09-01，金额未披露）\n- 机构调研纪要：行业景气度回升",
    "S4": "财务摘要（东财 F10，行项目名已归一为中文）：\n- 营业收入 10.0 亿元（同比 +12%）\n- 经营活动现金流净额 1.2 亿元\n- 归母净利润 0.8 亿元",
    "S3": "近期公告（东方财富，标题口径）：\n- 关于回购公司股份的公告",
}


def _claims() -> list[dict[str, object]]:
    """三条判断：C1 有 B 级来源 / C2 只有 C 级来源 / C3 没引用（应被跳过）。"""
    return report_quality.normalize_claims([
        {"claim_id": "C1", "text": "营收与现金流同步改善", "sources": ["S4"],
         "requires": ["revenue", "cash_flow"], "importance": "high"},
        {"claim_id": "C2", "text": "中标大单带动订单增长", "sources": ["S2"],
         "requires": ["revenue"], "importance": "high"},
        {"claim_id": "C3", "text": "行业景气度将持续回升", "sources": [],
         "requires": [], "importance": "low"},
    ])


def _answer(claim_index: int, *, support: str = "support", invented: float = 0.1):
    """一条判断的两题应答（题号口径与 `jev_claim_questions` 一致）。"""
    return {
        f"c{claim_index}_{report_quality.JEV_CLAIM_SUPPORT_QUESTION}": jev_client.JevAnswer(
            question_id=f"c{claim_index}_supports", type="choice", choice=support, confidence=0.88
        ),
        f"c{claim_index}_{report_quality.JEV_CLAIM_INVENTED_QUESTION}": jev_client.JevAnswer(
            question_id=f"c{claim_index}_invented", type="noul", noul=invented
        ),
    }


def _findings(claims: list[dict[str, object]] | None = None) -> dict[str, object]:
    """跑一遍确定性口径，拿到 `apply_jev_claim_findings` 的入参。"""
    return report_quality.check_claims(
        claims if claims is not None else _claims(), allowed={"S1", "S2", "S3", "S4", "S5"}
    )


def _by_id(findings: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(item["claim_id"]): item for item in findings["claims"]}  # type: ignore[union-attr]


# ——————————————————————————————————————————————————————————————
# 1–2. state 构造
# ——————————————————————————————————————————————————————————————


def test_state_only_admits_claims_with_cited_and_available_evidence():
    plan = report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)

    assert [item["claim_id"] for item in plan["evaluated"]] == ["C1", "C2"]
    assert [item["index"] for item in plan["evaluated"]] == [1, 2]
    # 没引用的判断不进该层：没有来源就无从比对，硬塞进去只会白付输入 token 并产出噪声
    assert plan["skipped"] == [{"claim_id": "C3", "reason": "no_cited_source"}]
    # 题号与 state 里的「判断 N」严格对齐（合并时不依赖模型自报 claim_id）
    assert "【判断 1】（claim_id=C1）" in plan["state"]
    assert "【判断 2】（claim_id=C2）" in plan["state"]


def test_state_carries_evidence_snippets_with_grade_but_never_whole_documents():
    plan = report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)
    state = plan["state"]

    # 原句 + 证据片段 + 来源标签 + 等级：等级有用，Jev 知道「这只是标题口径的公告」时判得更准
    assert "判断原句：营收与现金流同步改善" in state
    assert "所引证据 [S4]" in state
    assert "证据等级 B" in state
    assert "经营活动现金流净额" in state
    assert "所引证据 [S2]" in state and "证据等级 C" in state


def test_state_marks_truncation_explicitly():
    """截断必须显式留痕，否则读的人以为原文就这么短（原句与证据片段都要标）。"""
    plan = report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS, text_chars=6, source_chars=16)
    state = plan["state"]
    assert "判断原句：营收与现金流…（已截断）" in state
    assert "…（已截断）" in state.split("所引证据")[1]


def test_state_skips_oversized_claims_whole_instead_of_sending_half():
    """超预算整条跳过——半截证据会让 Jev 得出错误的「不支撑」。"""
    claims = report_quality.normalize_claims([
        {"claim_id": "L1", "text": "第一条", "sources": ["S4"], "requires": ["revenue"]},
        {"claim_id": "L2", "text": "第二条", "sources": ["S4"], "requires": ["revenue"]},
    ])
    full = report_quality.jev_claim_state(claims, _SOURCE_TEXTS)
    # 预算刚好装得下第一条、装不下第二条（用实测长度定阈值，不写死魔法数）
    one_block = len(full["state"]) // 2 + 40
    plan = report_quality.jev_claim_state(claims, _SOURCE_TEXTS, max_chars=one_block)

    assert [item["claim_id"] for item in plan["evaluated"]] == ["L1"]
    assert plan["skipped"] == [{"claim_id": "L2", "reason": "over_state_budget"}]


def test_state_respects_claim_limit_and_reports_overflow():
    claims = report_quality.normalize_claims([
        {"claim_id": f"C{i}", "text": f"判断{i}", "sources": ["S4"], "requires": ["revenue"]}
        for i in range(1, 5)
    ])
    plan = report_quality.jev_claim_state(claims, _SOURCE_TEXTS, max_claims=2)
    assert [item["claim_id"] for item in plan["evaluated"]] == ["C1", "C2"]
    assert [item["reason"] for item in plan["skipped"]] == ["over_claim_limit", "over_claim_limit"]


# ——————————————————————————————————————————————————————————————
# 3. 题目
# ——————————————————————————————————————————————————————————————


def test_questions_are_two_per_claim_and_ids_follow_state_numbering():
    plan = report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)
    questions = report_quality.jev_claim_questions(plan["evaluated"])

    assert set(questions) == {
        "c1_supports", "c1_invented", "c2_supports", "c2_invented",
    }
    supports = questions["c1_supports"]
    assert supports["type"] == "choice"
    # 四选项而不是「是/否」：「不支撑」与「无关」是两种毛病（证据相悖 vs 拿错证据），修法不同
    assert set(supports["criteria"]) == {"support", "partial", "unsupport", "irrelevant"}
    invented = questions["c1_invented"]
    assert invented["type"] == "noul"
    # 措辞让高值 = 「存在编造」（官方建议）
    assert set(invented["criteria"]) == {"true", "false"}
    assert "找不到" in invented["criteria"]["true"]


# ——————————————————————————————————————————————————————————————
# 4–5. 合并规则
# ——————————————————————————————————————————————————————————————


def test_unsupport_downgrades_full_to_partial_and_says_why():
    findings = _findings()
    assert _by_id(findings)["C1"]["support"] == report_quality.SUPPORT_FULL

    merged = report_quality.apply_jev_claim_findings(
        findings,
        evaluated=report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)["evaluated"],
        answers={**_answer(1, support="unsupport"), **_answer(2)},
        model="jev-1.13.0",
    )
    after = _by_id(merged)
    assert after["C1"]["support"] == report_quality.SUPPORT_PARTIAL
    assert after["C1"]["support_source"] == "jev_semantic"
    assert after["C1"]["jev"]["support"] == "unsupport"
    assert after["C1"]["jev"]["support_label"] == "语义不支撑"
    # 原话与确定性判定都不许被改写：`status` 仍是确定性口径的三态
    assert after["C1"]["text"] == "营收与现金流同步改善"
    assert after["C1"]["status"] == "supported"
    # C2 语义支撑一致 → 判定不动
    assert after["C2"]["support"] == report_quality.SUPPORT_PARTIAL
    assert "support_source" not in after["C2"]

    assert merged["jev"]["available"] is True
    assert merged["jev"]["downgraded"] == ["C1"]
    assert merged["jev"]["model"] == "jev-1.13.0"
    assert any("不支撑" in item for item in merged["jev_warnings"])


def test_irrelevant_citation_is_flagged_as_wrong_evidence_not_weak_evidence():
    """「引用无关」= 拿错证据，与「不支撑」同属强信号，都降级，但标签必须区分。"""
    findings = _findings()
    merged = report_quality.apply_jev_claim_findings(
        findings,
        evaluated=report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)["evaluated"],
        answers={**_answer(1, support="irrelevant"), **_answer(2)},
    )
    after = _by_id(merged)
    assert after["C1"]["support"] == report_quality.SUPPORT_PARTIAL
    assert after["C1"]["jev"]["support_label"] == "引用无关"
    assert merged["jev"]["downgraded"] == ["C1"]


def test_partial_support_downgrades_after_r6_calibration():
    """R6 已标定（2026-09-21）：「部分支撑」现在**参与降级**，不再是只标注。

    依据 `docs/evidence/jev-cjk-calibration-2026-09-21.md`：30 条中文样本上误伤率 0.0、
    漏放率 0.1，达到工具设定的门槛（样本 ≥20 且误伤 ≤5% 且漏放 ≤10%）。
    回退只需把 `JEV_PARTIAL_DOWNGRADES` 改回 False —— 这条断言就是那个开关的守门人。
    """
    assert report_quality.JEV_PARTIAL_DOWNGRADES is True

    findings = _findings()
    merged = report_quality.apply_jev_claim_findings(
        findings,
        evaluated=report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)["evaluated"],
        answers={**_answer(1, support="partial"), **_answer(2)},
    )
    after = _by_id(merged)
    assert after["C1"]["support"] == report_quality.SUPPORT_PARTIAL
    assert after["C1"]["support_source"] == "jev_semantic"
    assert merged["jev"]["downgraded"] == ["C1"]
    # 标注照旧保留，让用户知道是语义层改的、以及原因
    assert merged["jev"]["weakened"] == ["C1"]
    assert any("部分支撑" in item for item in merged["jev_warnings"])


def test_invented_yes_forces_none_and_raises_a_strong_warning():
    """判断里出现所引证据中找不到的具体事实 → 降到「无来源支撑」+ 强告警（疑似编造）。"""
    findings = _findings()
    merged = report_quality.apply_jev_claim_findings(
        findings,
        evaluated=report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)["evaluated"],
        answers={**_answer(1, support="support", invented=0.93), **_answer(2)},
    )
    after = _by_id(merged)
    assert after["C1"]["support"] == report_quality.SUPPORT_NONE
    assert after["C1"]["jev"]["invented"] == "yes"
    assert after["C1"]["jev"]["invented_value"] == 0.93
    assert merged["jev"]["invented"] == ["C1"]
    assert merged["jev"]["downgraded"] == ["C1"]
    assert any("编造" in item for item in merged["jev_warnings"])


def test_invented_uncertain_is_annotated_but_not_binarised():
    """noul 中间地带只标注——不硬二值化（与 JV03 假突破风险三路口径一致）。"""
    findings = _findings()
    merged = report_quality.apply_jev_claim_findings(
        findings,
        evaluated=report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)["evaluated"],
        answers={**_answer(1, support="support", invented=0.5), **_answer(2)},
    )
    after = _by_id(merged)
    assert after["C1"]["jev"]["invented"] == "uncertain"
    assert after["C1"]["support"] == report_quality.SUPPORT_FULL
    assert merged["jev"]["invented"] == []


def test_downgrade_recomputes_counts_and_core_state():
    """降级后汇总口径必须同步，否则「核心判断 N/M 支撑」会与逐条 support 对不上。"""
    findings = _findings()
    # 两条核心判断（C1/C2）确定性口径下：C1 full、C2 partial → core 1/2
    assert findings["core_support_counts"] == {"supported": 1, "total": 2}

    merged = report_quality.apply_jev_claim_findings(
        findings,
        evaluated=report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)["evaluated"],
        answers={**_answer(1, support="unsupport"), **_answer(2)},
    )
    # C1 被降级 → 核心判断 0/2，三态从 partial 变 unsupported
    assert merged["core_support_counts"] == {"supported": 0, "total": 2}
    assert merged["core_conclusion_state"] == "unsupported"
    assert merged["core_conclusion_supported"] is False
    assert merged["support_counts"][report_quality.SUPPORT_FULL] == 0
    assert merged["supported"] == 0


def test_missing_answers_leave_findings_untouched_and_available_false():
    """关掉就真的关掉：不注入回调时判定必须逐字节不动（与接入前完全一致）。"""
    findings = _findings()
    before = json.dumps(findings, ensure_ascii=False, sort_keys=True)

    merged = report_quality.apply_jev_claim_findings(
        findings,
        evaluated=report_quality.jev_claim_state(_claims(), _SOURCE_TEXTS)["evaluated"],
        answers=None,
        skipped=[{"claim_id": "C3", "reason": "no_cited_source"}],
    )
    assert merged["jev"]["available"] is False
    assert merged["jev"]["skipped"] == [{"claim_id": "C3", "reason": "no_cited_source"}]
    assert merged["jev_warnings"] == []
    # 逐条判定原样保留（唯一新增的是 jev 回执本身）
    for item in merged["claims"]:
        assert "jev" not in item
    stripped = {key: value for key, value in merged.items() if key not in ("jev", "jev_warnings")}
    assert json.dumps(stripped, ensure_ascii=False, sort_keys=True) == before


# ——————————————————————————————————————————————————————————————
# 6–8. 闸门接线
# ——————————————————————————————————————————————————————————————


_PAYLOAD: dict[str, object] = {
    "executive_summary": "结论：技术面缓涨，基本面稳健。",
    "report": "### 技术面\n缓涨 [S1]\n### 消息面\n中标大单 [S2]\n### 基本面\n营收增长 [S4]\n### 综合判断\n中性看待。",
    "citations": ["S1", "S2", "S4"],
    "confidence": "medium",
    "valuation": {"verdict": "合理"},
    "claims": [
        {"claim_id": "C1", "text": "营收与现金流同步改善", "sources": ["S4"],
         "requires": ["revenue", "cash_flow"], "importance": "high"},
        {"claim_id": "C2", "text": "中标大单带动订单增长", "sources": ["S2"],
         "requires": ["revenue"], "importance": "high"},
    ],
}


def _judge(state: str, questions):
    return {
        "answers": {
            "c1_supports": jev_client.JevAnswer(
                question_id="c1_supports", type="choice", choice="unsupport", confidence=0.9
            ),
            "c1_invented": jev_client.JevAnswer(question_id="c1_invented", type="noul", noul=0.05),
            "c2_supports": jev_client.JevAnswer(
                question_id="c2_supports", type="choice", choice="support", confidence=0.9
            ),
            "c2_invented": jev_client.JevAnswer(question_id="c2_invented", type="noul", noul=0.05),
        },
        "model": "jev-1.13.0",
    }


def test_gate_without_judge_behaves_exactly_like_before_jv04():
    quality = report_quality.validate_report(_PAYLOAD, allowed={"S1", "S2", "S3", "S4", "S5"})
    assert quality["jev"] is None
    assert not any("语义支撑" in item for item in quality["warnings"])
    assert _by_id(quality["claim_findings"])["C1"]["support"] == report_quality.SUPPORT_FULL


def test_gate_with_judge_applies_semantic_downgrade_and_reports_it():
    quality = report_quality.validate_report(
        _PAYLOAD,
        allowed={"S1", "S2", "S3", "S4", "S5"},
        source_texts=_SOURCE_TEXTS,
        jev_judge=_judge,
    )
    assert quality["jev"]["available"] is True
    assert quality["jev"]["evaluated"] == 2
    assert quality["jev"]["downgraded"] == ["C1"]
    assert quality["jev"]["model"] == "jev-1.13.0"
    assert _by_id(quality["claim_findings"])["C1"]["support"] == report_quality.SUPPORT_PARTIAL
    assert any("语义比对后**不支撑**" in item for item in quality["warnings"])


def test_gate_survives_judge_failure_without_blocking_delivery():
    """语义层尽力而为：回调抛错只降级 + 留痕，绝不阻断报告交付。"""

    def _boom(state, questions):
        raise jev_client.JevUnavailable("Jev 凭据在本机凭据库中不存在")

    quality = report_quality.validate_report(
        _PAYLOAD, allowed={"S1", "S2", "S3", "S4", "S5"},
        source_texts=_SOURCE_TEXTS, jev_judge=_boom,
    )
    assert quality["jev"]["available"] is False
    assert "JevUnavailable" in quality["jev"]["note"]
    # 判定按确定性口径如实保留，报告照常交付
    assert _by_id(quality["claim_findings"])["C1"]["support"] == report_quality.SUPPORT_FULL
    assert quality["citations"] == ["S1", "S2", "S4"]


def test_gate_says_so_when_no_claim_has_usable_evidence():
    """没有任何判断带可比对证据时不发请求，且如实说明（不是静默通过）。"""
    calls: list[str] = []

    def _judge_must_not_be_called(state, questions):
        # 记录而非断言：本用例要验证的是「压根没发起请求」，所以只留痕、不抛错。
        calls.append(state)

    payload = {**_PAYLOAD, "claims": [
        {"claim_id": "C1", "text": "无引用判断", "sources": [], "requires": []},
    ]}
    quality = report_quality.validate_report(
        payload, allowed={"S1"}, source_texts=_SOURCE_TEXTS, jev_judge=_judge_must_not_be_called,
    )
    assert calls == []
    assert quality["jev"]["available"] is False
    assert "未发起请求" in quality["jev"]["note"]
    assert quality["jev"]["skipped"] == [{"claim_id": "C1", "reason": "no_cited_source"}]


# ——————————————————————————————————————————————————————————————
# 9–10. 端点
# ——————————————————————————————————————————————————————————————

_REPORT_JSON = json.dumps({
    "title": "甲股：缓涨",
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
    "claims": [
        {"claim_id": "C1", "text": "营收与现金流同步改善", "sources": ["S4"],
         "requires": ["revenue", "cash_flow"], "importance": "high"},
        {"claim_id": "C2", "text": "中标大单带动订单增长", "sources": ["S2"],
         "requires": ["revenue"], "importance": "high"},
    ],
}, ensure_ascii=False)


def _endpoint_client(tmp_path: Path, monkeypatch, *, jev_enabled: bool):
    from investment_steward_core import model_client as mc
    from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)

    # 研报 JSON → 反方检查 JSON（两次 chat 调用）
    def _chat(base_url, json_payload, api_key, timeout):
        return {"choices": [{"message": {"content": _REPORT_JSON}}]}

    monkeypatch.setattr(mc, "_post_json", _chat)

    if jev_enabled:
        test_client.put("/credentials/jev_api_key", json={"secret": "sk-jev-secret"}, headers=headers)
        saved = test_client.put("/jev/config", headers=headers, json={
            "enabled": True,
            "base_url": jev_client.JEV_DEFAULT_BASE_URL,
            "model": jev_client.JEV_MODEL_ALIAS,
            "credential_ref": credential_store.JEV_API_KEY,
            "timeout_secs": 60,
        })
        assert saved.status_code == 200, saved.text
    return test_client, headers


def _systemone_for_claims(*, c1_support: str, c1_invented: float = 0.05):
    def _fake(base_url, payload, api_key, timeout):
        questions = payload["questions"]
        answers: dict[str, object] = {}
        for question_id in questions:
            if question_id.endswith("_supports"):
                answers[question_id] = {
                    "type": "choice",
                    "choice": c1_support if question_id.startswith("c1_") else "support",
                    "probabilities": {c1_support: 0.9},
                    "confidence": 0.9,
                }
            else:
                answers[question_id] = {
                    "type": "noul",
                    "noul": c1_invented if question_id.startswith("c1_") else 0.05,
                }
        return {"model": "jev-1.13.0", "answers": answers,
                "usage": {"input_tokens": 900, "output_tokens": 8}}

    return _fake


def test_endpoint_applies_semantic_layer_and_records_purpose(tmp_path, monkeypatch):
    test_client, headers = _endpoint_client(tmp_path, monkeypatch, jev_enabled=True)
    sent: list[dict[str, object]] = []

    def _fake(base_url, payload, api_key, timeout):
        sent.append(payload)
        return _systemone_for_claims(c1_support="unsupport")(base_url, payload, api_key, timeout)

    monkeypatch.setattr(jev_client, "_post_endpoint", _fake)

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body

    # 一次请求问完两条判断的四道题（官方 fan-out：加题几乎不加时延）
    assert len(sent) == 1
    assert set(sent[0]["questions"]) == {"c1_supports", "c1_invented", "c2_supports", "c2_invented"}
    assert "判断原句：营收与现金流同步改善" in sent[0]["state"]
    # state 只送判断原句 + 所引证据片段，不送整份研报正文（《契约》§F 白名单）
    assert "跟踪量能确认" not in sent[0]["state"]

    assert body["jev"]["available"] is True
    assert body["jev"]["downgraded"] == ["C1"]
    findings = {item["claim_id"]: item for item in body["claims"]}
    assert findings["C1"]["support"] == report_quality.SUPPORT_PARTIAL
    assert findings["C1"]["support_source"] == "jev_semantic"
    assert findings["C1"]["jev"]["support"] == "unsupport"

    # purpose 必须能按场景归集（JV10 对账要分得清哪个场景花的额度）
    db = test_client.app.state.core.database
    with sqlite3.connect(db.path) as raw:
        purposes = [row[0] for row in raw.execute("SELECT purpose FROM model_calls ORDER BY rowid")]
    assert "jev:claim-support" in purposes


def test_endpoint_with_jev_disabled_makes_no_jev_request_and_returns_null(tmp_path, monkeypatch):
    test_client, headers = _endpoint_client(tmp_path, monkeypatch, jev_enabled=False)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("Jev 未启用时不应发出任何 Jev 请求")

    monkeypatch.setattr(jev_client, "_post_endpoint", _must_not_be_called)

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body
    # 未启用 → 该层不存在（None 而不是 available=False 的空壳），前端据此区分「没跑」与「跑过」
    assert body["jev"] is None
    assert {item["claim_id"]: item["support"] for item in body["claims"]}["C1"] == report_quality.SUPPORT_FULL
