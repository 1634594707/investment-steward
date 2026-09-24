"""JV06 契约测试：验证点优先级分诊（路线图 P1 第三站）。

要解决的问题：报告生成时模型给出若干**验证点**（`watchpoints[]`/`new_watchpoints[]`），
它们一律等权重地堆在「情景与验证」里——用户看不出哪一条值得现在就去查、哪一条其实可以放着，
而「查一次」的代价是一次取数 + 一次研报额度。

覆盖四层：

1. **纯函数 · state 与题面**：白名单只送验证点自身的字段（不送研报正文）；超上限/超预算的
   每一条都进 `skipped` 并写明原因；每条一道 `score`（3 级**描述性** rubric）。
2. **纯函数 · 折叠与排序**：**两个条件同时成立才折叠**（判为最低档 **且** 置信度达标）；
   任何失败/缺答/低置信一律**不折叠**——把该查的验证点藏起来，代价远大于多留一条不太急的。
   排序只按档位，**同分保持报告原有次序**（模型不该把同一档内洗一遍牌）。
3. **纯函数 · 三者留痕**：`score`（原始等级轴取值）+ `score_percent`（归一化展示分）+
   `confidence` 三者都存，缺一不可——否则事后无法复算「当时为什么把它折叠了」。
4. **端点**：报告与追问两条链路都接上同一把尺子；**分诊失败不影响验证点本身交付**；
   以及最要紧的 **Jev 关闭时零变化**（零出网、`triage` 字段不存在、回执块为 None）。

第 4 层的「失败不影响交付」是 JV06 的验收核心：这是一个**附加的排序层**，
它挂掉时报告该有的验证点一条都不能少。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from investment_steward_core import credential_store, jev_client, report_quality

# ——————————————————————————————————————————————————————————————
# 合成素材
# ——————————————————————————————————————————————————————————————


def _point(signal: str, verify_by: str = "2026-11-30", expected: str = "结论强化") -> dict[str, object]:
    return {"signal": signal, "verify_by": verify_by, "expected_if_true": expected}


#: 三档标记：fake 的判分只看块里的标记，**不依赖顺序**（测试因此不会因为模型改了措辞而假通过）。
_POINT_PRIORITY = dict(_point("【优先】量能是否持续放大"), due_on="2026-10-31")
_POINT_DEFERRED = dict(_point("【暂缓】公司是否披露新订单"), due_on="2026-12-31")
_POINT_LATER = dict(_point("【稍后】估值是否回落到合理区间"), due_on="2027-01-31")


def _score(question_id: str, score: float, confidence: float | None, levels: int = 3):
    return jev_client.JevAnswer(
        question_id=question_id, type="score", score=score,
        probabilities={str(int(score)): confidence or 0.5},
        confidence=confidence, levels=levels,
    )


def _shard(evaluated, answers, error=None, model="typesafe/jev-1.13-20260917"):
    return {"evaluated": evaluated, "answers": answers, "error": error, "attempts": 1, "model": model}


# ——————————————————————————————————————————————————————————————
# 1. 纯函数：state 与题面
# ——————————————————————————————————————————————————————————————


def test_jev_followup_state_block_carries_only_the_watchpoint_itself():
    plan = report_quality.jev_followup_state([_POINT_PRIORITY])
    assert plan["skipped"] == []
    for marker in ("【验证点 1】", "信号：【优先】量能是否持续放大", "验证时点：", "到期日：2026-10-31", "若为真则："):
        assert marker in plan["state"], marker
    # 白名单纪律：验证点已经是「接下来该验什么」的浓缩表述，再带正文只是扩大出网面
    assert "### 技术面" not in plan["state"]


def test_jev_followup_state_says_plainly_when_no_exact_date():
    plan = report_quality.jev_followup_state([_point("x", verify_by="三季报披露后")])
    assert "（未给出确切日期，按事件锚定）" in plan["state"]


def test_jev_followup_state_reports_every_skip_reason():
    # ① 超出单轮上限
    many = [_point(f"信号{i}") for i in range(report_quality.JEV_FOLLOWUP_MAX_ITEMS + 2)]
    plan = report_quality.jev_followup_state(many)
    limits = [item for item in plan["skipped"] if item["reason"] == "over_item_limit"]
    assert len(limits) == 2, "超上限的每一条都要如实计入，不能只报一条"
    assert len(plan["evaluated"]) == report_quality.JEV_FOLLOWUP_MAX_ITEMS

    # ② 单条超预算
    huge = report_quality.jev_followup_state([_point("很长的信号" * 200)], item_max_chars=100)
    assert [item["reason"] for item in huge["skipped"]] == ["over_item_budget"]
    assert huge["evaluated"] == []

    # ③ state 预算已满（不硬塞半截）
    tight = report_quality.jev_followup_state([_point(f"信号{i}") for i in range(4)], max_chars=200)
    assert {item["reason"] for item in tight["skipped"]} == {"over_state_budget"}
    assert "不硬塞半截" in tight["skipped"][0]["note"]


def test_jev_followup_questions_one_descriptive_score_per_item():
    plan = report_quality.jev_followup_state([_POINT_PRIORITY, _POINT_DEFERRED, _POINT_LATER])
    questions = report_quality.jev_followup_questions(plan["evaluated"])
    assert set(questions) == {
        f"w{index}_{report_quality.JEV_FOLLOWUP_QUESTION}" for index in (1, 2, 3)
    }
    first = questions[f"w1_{report_quality.JEV_FOLLOWUP_QUESTION}"]
    assert first["type"] == "score"
    # 3 级**描述性** rubric：只给数字等级会让概率摊平、confidence 崩掉
    assert len(first["criteria"]) == 3
    assert all(len(str(level)) > 10 for level in first["criteria"])
    # 三个判据必须出现在题干里（可证伪 / 已有证据 / 影响决策）
    assert "可证伪" in first["instructions"]
    assert "已有证据" in first["instructions"]
    assert "决策" in first["instructions"]


def test_jev_followup_shards_is_single_shard_in_reviewer_shape():
    bundle = report_quality.jev_followup_shards([_POINT_PRIORITY, _POINT_DEFERRED])
    assert len(bundle["shards"]) == 1, "验证点上限 5 条，恒为单片"
    shard = bundle["shards"][0]
    assert [item["index"] for item in shard["evaluated"]] == [1, 2]
    assert len(shard["questions"]) == 2
    assert "【验证点 1】" in shard["state"] and "【验证点 2】" in shard["state"]


def test_jev_followup_shards_returns_no_shards_when_everything_is_skipped():
    bundle = report_quality.jev_followup_shards(
        [_point("很长的信号" * 200)], item_max_chars=100
    )
    assert bundle["shards"] == []
    assert [item["reason"] for item in bundle["skipped"]] == ["over_item_budget"]


# ——————————————————————————————————————————————————————————————
# 2. 纯函数：折叠规则（两个条件同时成立）
# ——————————————————————————————————————————————————————————————


def test_followup_decision_defers_only_when_both_conditions_hold():
    deferred, note = report_quality._followup_decision(0, 0.88)
    assert deferred is True
    assert "仍可展开，不删除" in note, "折叠说明必须点明「不是删除」"

    # 任一条件不满足 → 一律不折叠。这些是「把该查的藏起来」的防呆边界。
    for percent, confidence in (
        (50, 0.88),   # 判为「可稍后」
        (100, 0.88),  # 判为「优先」
        (0, None),    # 没给置信度 → 无法确认
        (0, 0.42),    # 置信度低于地板
        (None, 0.88),  # 没取得评分
    ):
        got, reason = report_quality._followup_decision(percent, confidence)
        assert got is False, (percent, confidence)
        assert reason, "不折叠也要给理由，便于在界面解释"


def test_jev_followup_findings_keeps_score_percent_and_confidence_all_three():
    """验收明写：原始 score + 归一化展示分 + confidence **三者都要留痕**。"""
    evaluated = [{"index": 1}]
    answers = {f"w1_{report_quality.JEV_FOLLOWUP_QUESTION}": _score("w1_priority", 2.0, 0.91)}
    got = report_quality.jev_followup_findings([_shard(evaluated, answers)])[1]

    assert got["score"] == 2.0, "原始等级轴取值（0–2），不是 0–100"
    assert got["score_percent"] == 100, "归一化展示分 = 2/(3-1)×100"
    assert got["confidence"] == 0.91
    assert got["level_label"] == "优先"
    assert got["deferred"] is False
    assert got["triage_state"] == report_quality.JEV_FOLLOWUP_STATE_PRIORITY


def test_jev_followup_findings_normalizes_by_level_count():
    evaluated = [{"index": 1}, {"index": 2}, {"index": 3}]
    question = report_quality.JEV_FOLLOWUP_QUESTION
    answers = {
        f"w1_{question}": _score("w1_priority", 0.0, 0.9),
        f"w2_{question}": _score("w2_priority", 1.0, 0.9),
        f"w3_{question}": _score("w3_priority", 2.0, 0.44),
    }
    findings = report_quality.jev_followup_findings([_shard(evaluated, answers)])
    assert findings[1]["score_percent"] == 0 and findings[1]["deferred"] is True
    assert findings[2]["score_percent"] == 50 and findings[2]["level_label"] == "可稍后"
    # 判为「优先」档 → 不折叠（置信度低只影响是否折叠，不影响不折叠）
    assert findings[3]["score_percent"] == 100 and findings[3]["deferred"] is False


def test_jev_followup_findings_never_defers_on_error_or_missing_answer():
    """分片失败 / 缺答 / 取值越界 → `unannotated` 且**不折叠**。「没评」不等于「不值得查」。"""
    evaluated = [{"index": 1}, {"index": 2}]
    failed = report_quality.jev_followup_findings(
        [_shard(evaluated, {}, error="服务端返回 429（触发限流）")]
    )
    for index in (1, 2):
        assert failed[index]["deferred"] is False
        assert failed[index]["triage_state"] == report_quality.JEV_FOLLOWUP_STATE_UNANNOTATED
        assert failed[index]["score"] is None
        assert "按不折叠处理" in failed[index]["note"]
        assert "429" in failed[index]["note"], "失败原因要如实透出，便于排查"

    missing = report_quality.jev_followup_findings([_shard([{"index": 1}], {})])
    assert missing[1]["deferred"] is False
    assert missing[1]["triage_state"] == report_quality.JEV_FOLLOWUP_STATE_UNANNOTATED

    # score 越界 → 同样按「未取得判定」处理，不猜不裁
    question = report_quality.JEV_FOLLOWUP_QUESTION
    out_of_range = report_quality.jev_followup_findings(
        [_shard([{"index": 1}], {f"w1_{question}": _score("w1_priority", 9.0, 0.9)})]
    )
    assert out_of_range[1]["triage_state"] == report_quality.JEV_FOLLOWUP_STATE_UNANNOTATED
    assert out_of_range[1]["deferred"] is False


def test_followup_display_order_keeps_report_order_within_same_score():
    """排序只按档位：同分保持报告原有次序，没评的按原序跟在最后。"""
    rows = [
        (0, {"score_percent": 50}),   # 可稍后
        (1, {"score_percent": 100}),  # 优先
        (2, {"score_percent": 50}),   # 可稍后（与 0 同档，不得被洗牌）
        (3, {"score_percent": None}),  # 未分诊
    ]
    assert report_quality._followup_display_order(rows) == [1, 0, 2, 3]


# ——————————————————————————————————————————————————————————————
# 3. apply 层：接线契约
# ——————————————————————————————————————————————————————————————


def _reviewer_for(scores: dict[int, float], confidence: float = 0.88):
    """按 index 给分的假复核器（`build_shard_reviewer` 的返回形态）。"""

    def _review(bundle):
        evaluated = bundle["shards"][0]["evaluated"]
        answers = {
            f"w{item['index']}_{report_quality.JEV_FOLLOWUP_QUESTION}": _score(
                f"w{item['index']}_{report_quality.JEV_FOLLOWUP_QUESTION}",
                scores[item["index"]], confidence,
            )
            for item in evaluated
        }
        return [_shard(evaluated, answers)]

    return _review


def test_apply_returns_copy_without_triage_and_none_block_when_reviewer_missing():
    original = [_POINT_PRIORITY, _POINT_DEFERRED]
    items, block = report_quality.apply_jev_followup_findings(original, reviewer=None)
    assert block is None, "整层没跑就必须返回 None（与相邻的 `jev` 同一惯例）"
    assert all("triage" not in item for item in items)
    # 原样返回但**不污染入参**
    assert items == original
    assert items is not original


def test_apply_does_not_mutate_callers_list():
    original = [_POINT_PRIORITY, _POINT_DEFERRED]
    report_quality.apply_jev_followup_findings(original, reviewer=_reviewer_for({1: 2.0, 2: 0.0}))
    assert all("triage" not in item for item in original), "入参被就地改了"


def test_apply_annotates_three_values_and_defers_only_the_lowest_band():
    items, block = report_quality.apply_jev_followup_findings(
        [_POINT_PRIORITY, _POINT_DEFERRED, _POINT_LATER],
        reviewer=_reviewer_for({1: 2.0, 2: 0.0, 3: 1.0}),
    )
    assert [item["triage"]["score_percent"] for item in items] == [100, 0, 50]
    assert [item["triage"]["confidence"] for item in items] == [0.88, 0.88, 0.88]
    assert [item["triage"]["score"] for item in items] == [2.0, 0.0, 1.0]
    assert items[1]["triage"]["deferred"] is True
    assert items[0]["triage"]["deferred"] is False and items[2]["triage"]["deferred"] is False

    # 排序与折叠**在响应里体现**（前端照抄即可，不必各写一份排序逻辑）
    assert block["order"] == [1, 3, 2]
    assert [items[index - 1]["triage"]["rank"] for index in block["order"]] == [0, 1, 2]
    assert block["deferred"] == 1 and block["unannotated"] == 0 and block["total"] == 3
    assert block["model"] == "typesafe/jev-1.13-20260917"
    # 验证点原文一个字都没改（软校验铁律）
    for before, after in zip(
        [_POINT_PRIORITY, _POINT_DEFERRED, _POINT_LATER], items, strict=True
    ):
        assert before["signal"] == after["signal"]
        assert before["verify_by"] == after["verify_by"]


def test_apply_never_throws_and_keeps_every_watchpoint_on_failure():
    """**验收核心**：分诊失败不影响验证点本身交付——一条不少、原序、不折叠。"""

    def _boom(_bundle):
        raise RuntimeError("端点不可达")

    points = [_POINT_PRIORITY, _POINT_DEFERRED, _POINT_LATER]
    items, block = report_quality.apply_jev_followup_findings(points, reviewer=_boom)

    assert len(items) == 3, "验证点一条都不能少"
    assert [item["signal"] for item in items] == [item["signal"] for item in points]
    assert all(
        item["triage"]["triage_state"] == report_quality.JEV_FOLLOWUP_STATE_UNANNOTATED
        for item in items
    )
    assert all(item["triage"]["deferred"] is False for item in items)
    assert block["order"] == [1, 2, 3], "失败时保持报告原序"
    assert block["deferred"] == 0 and block["unannotated"] == 3
    assert "端点不可达" in items[0]["triage"]["note"], "失败原因要能回查"


def test_apply_marks_unrated_items_with_honest_reason():
    def _review(bundle):
        evaluated = bundle["shards"][0]["evaluated"]
        return [_shard(evaluated, {})]

    items, block = report_quality.apply_jev_followup_findings(
        [_POINT_PRIORITY, _POINT_DEFERRED], reviewer=_review
    )
    assert all(
        item["triage"]["triage_state"] == report_quality.JEV_FOLLOWUP_STATE_UNANNOTATED
        for item in items
    )
    assert block["unannotated"] == 2
    assert "不等于「不值得查」" in block["note"] or "未分诊" in items[0]["triage"]["note"]


# ——————————————————————————————————————————————————————————————
# 4. 端点：报告与追问两条链路
# ——————————————————————————————————————————————————————————————

_REPORT_JSON = json.dumps({
    "title": "甲股：缓涨",
    "executive_summary": "结论：技术面缓涨，基本面稳健。",
    "report": (
        "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大 [S1]\n"
        "### 消息面\n公司公告中标大单 [S2]\n"
        "### 综合判断\n趋势向好，跟踪量能确认。"
    ),
    "citations": ["S1", "S2"],
    "confidence": "medium",
    "watchpoints": [
        {"signal": "【优先】量能是否持续放大", "verify_by": "2026-10-31", "expected_if_true": "趋势强化"},
        {"signal": "【暂缓】公司是否披露新订单", "verify_by": "2026-12-31", "expected_if_true": "订单超预期"},
        {"signal": "【稍后】估值是否回落", "verify_by": "2027-01-31", "expected_if_true": "安全边际出现"},
    ],
}, ensure_ascii=False)


#: 追问应答（`normalize_followup` 的契约形状）：带两条**新增验证点**，供分诊打分。
_FOLLOWUP_JSON = json.dumps({
    "direct_answer": "不能仅凭原报告确认。",
    "answerability": "partially_answered",
    "key_conditions": ["放量站稳 15.21"],
    "evidence_gaps": ["缺区间历史统计"],
    "affected_claims": [],
    "conclusion_change": "unchanged",
    "scenario_changes": [],
    "new_watchpoints": [
        {"signal": "【优先】新订单是否落地", "verify_by": "2026-11-30", "expected_if_true": "强化"},
        {"signal": "【暂缓】管理层是否增持", "verify_by": "2026-12-31", "expected_if_true": "中性"},
    ],
    "price_refs": [],
    "answer": "原报告技术确认条件未满足。",
    "limitations": ["本轮未获取新数据"],
}, ensure_ascii=False)


def _client(tmp_path: Path, monkeypatch, *, jev_enabled: bool):
    from investment_steward_core import model_client as mc
    from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile

    http, headers = _file_client(tmp_path, monkeypatch)
    assert http.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(http, headers)
    _mock_sources(monkeypatch)

    def _chat(base_url, json_payload, api_key, timeout):
        # 按提示词特征分流：追问骨架里有「追加的分析附录」，研报没有。
        blob = json.dumps(json_payload, ensure_ascii=False)
        content = _FOLLOWUP_JSON if "追加的分析附录" in blob else _REPORT_JSON
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(mc, "_post_json", _chat)
    if jev_enabled:
        assert http.put(
            f"/credentials/{credential_store.JEV_API_KEY}", json={"secret": "sk-jev-secret"}, headers=headers
        ).status_code == 200
        saved = http.put("/jev/config", headers=headers, json={
            "enabled": True,
            "base_url": "https://openrouter.ai/api/v1/decisions",
            "model": "typesafe/jev-1.13",
            "credential_ref": credential_store.JEV_API_KEY,
            "timeout_secs": 60,
        })
        assert saved.status_code == 200, saved.text
    return http, headers


def _fake_jev():
    """按 state 里的「【优先】/【暂缓】/【稍后】」标记给分——不依赖排序。"""
    calls: list[dict[str, object]] = []

    def _fake(_base_url, payload, _api_key, _timeout):
        calls.append(payload)
        answers: dict[str, dict[str, object]] = {}
        for block in str(payload["state"]).split("【验证点 "):
            if not block.strip():
                continue
            index = block.split("】", 1)[0].strip()
            if not index.isdigit():
                continue
            if "【暂缓】" in block:
                value = 0.0
            elif "【稍后】" in block:
                value = 1.0
            else:
                value = 2.0
            answers[f"w{index}_{report_quality.JEV_FOLLOWUP_QUESTION}"] = {
                "type": "score", "score": value,
                "probabilities": {str(int(value)): 0.88}, "confidence": 0.88,
            }
        return {
            "model": "typesafe/jev-1.13-20260917",
            "answers": answers,
            "usage": {"input_tokens": 600, "output_tokens": 6},
        }

    return _fake, calls


def test_stock_report_triages_watchpoints_and_records_purpose(tmp_path, monkeypatch):
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    fake, calls = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    body = http.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()
    assert body["ok"] is True, body
    assert len(calls) == 1, "三条验证点应合成一个分片、只出网一次"
    assert set(calls[0]["questions"]) == {
        f"w{index}_{report_quality.JEV_FOLLOWUP_QUESTION}" for index in (1, 2, 3)
    }

    block = body["jev_followup"]
    assert block["available"] is True
    assert block["total"] == 3 and block["deferred"] == 1 and block["unannotated"] == 0
    assert block["order"] == [1, 3, 2], "排序与折叠必须体现在响应里"
    assert block["model"] == "typesafe/jev-1.13-20260917"
    assert block["confidence_floor"] == report_quality.JEV_FOLLOWUP_CONFIDENCE_FLOOR

    points = {item["signal"][:4]: item for item in body["watchpoints"]}
    assert points["【优先】"]["triage"]["score_percent"] == 100
    assert points["【优先】"]["triage"]["score"] == 2.0
    assert points["【优先】"]["triage"]["confidence"] == 0.88
    assert points["【暂缓】"]["triage"]["deferred"] is True
    assert points["【暂缓】"]["triage"]["level_label"] == "暂缓"
    assert points["【稍后】"]["triage"]["deferred"] is False

    db = http.app.state.core.database
    with sqlite3.connect(db.path) as raw:
        records = list(raw.execute("SELECT purpose, outcome FROM model_calls"))
    assert ("jev:followup-triage", "ok") in records


def test_stock_report_keeps_every_watchpoint_when_triage_fails(tmp_path, monkeypatch):
    """分诊失败不影响交付：验证点照常在响应里，只是没有排序、没有折叠。"""
    import urllib.error

    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)

    def _boom(*_args, **_kwargs):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(jev_client, "_post_endpoint", _boom)
    body = http.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()

    assert body["ok"] is True
    assert len(body["watchpoints"]) == 3, "验证点一条都不能少"
    block = body["jev_followup"]
    assert block["deferred"] == 0 and block["unannotated"] == 3
    assert block["order"] == [1, 2, 3], "失败时保持报告原序"
    assert all(
        item["triage"]["triage_state"] == report_quality.JEV_FOLLOWUP_STATE_UNANNOTATED
        for item in body["watchpoints"]
    )


def test_stock_report_zero_change_when_jev_disabled(tmp_path, monkeypatch):
    """**核心验收**：Jev 关闭时零变化——零出网、无 `triage` 字段、回执块为 None。"""
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=False)
    monkeypatch.setattr(jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("Jev 关闭时不得出网"))

    body = http.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()
    assert body["ok"] is True
    assert len(body["watchpoints"]) == 3
    assert body["jev_followup"] is None
    for item in body["watchpoints"]:
        assert "triage" not in item, f"Jev 关闭时不该有分诊字段：{sorted(item)}"


def test_followup_turn_triages_new_watchpoints_with_same_ruler(tmp_path, monkeypatch):
    """追问新增的验证点走**同一把尺子**（同一个纯函数、同一个 purpose）。"""
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    fake, calls = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    base = http.post("/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers).json()
    report_id = str(base["report_id"])

    calls.clear()
    turn = http.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={"question": "最不可靠的结论是什么？"},
        headers=headers,
    ).json()["turn"]

    assert len(calls) == 1, "追问新增验证点应只出网一次"
    block = turn["jev_followup"]
    assert block["available"] is True
    assert block["total"] == 2
    points = {item["signal"][:4]: item for item in turn["new_watchpoints"]}
    assert points["【优先】"]["triage"]["score_percent"] == 100
    assert points["【优先】"]["triage"]["confidence"] == 0.88
    assert points["【暂缓】"]["triage"]["deferred"] is True
    assert block["deferred"] == 1 and block["order"] == [1, 2]
    for item in turn["new_watchpoints"]:
        assert set(item["triage"]) >= {"score", "score_percent", "confidence", "deferred", "rank"}


def test_followup_turn_still_delivers_when_triage_fails(tmp_path, monkeypatch):
    import urllib.error

    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    base = http.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    report_id = str(base["report_id"])

    def _boom(*_args, **_kwargs):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(jev_client, "_post_endpoint", _boom)
    created = http.post(
        f"/ai-research/reports/{report_id}/follow-ups",
        json={"question": "追问一个问题"}, headers=headers,
    ).json()
    assert created["ok"] is True, "分诊失败不能拖垮追问交付"
    assert created["turn"]["jev_followup"]["deferred"] == 0
