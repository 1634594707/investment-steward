"""E2 研究后验最小闭环：POST /evidence/decision-outcome 到期补录测试。

铁律：只允许补「结果 / 事后评价」两个事后字段；原始判断（theme / decision_summary /
rationale / linked_evidence_ids / made_at）不可被改写（不用事后信息改写原始研究）。
"""

from __future__ import annotations

from uuid import uuid4


def test_decision_outcome_appends_post_hoc_fields_only(client):
    test_client, headers = client
    created = test_client.post(
        "/decisions",
        headers=headers,
        json={
            "theme": "沪深 300 ETF",
            "decision_summary": "维持观察，不因单日波动改变仓位",
            "rationale": "中期上行结构完好",
            "outcome": "",
            "retrospective": "",
            "linked_evidence_ids": [str(uuid4())],
        },
    )
    assert created.status_code == 201
    original = created.json()

    recorded = test_client.post(
        "/evidence/decision-outcome",
        headers=headers,
        json={
            "decision_id": original["decision_id"],
            "outcome": "两周后价格站稳均线，观察正确",
            "retrospective": "下次同样先看结构再动",
        },
    )
    assert recorded.status_code == 200
    updated = recorded.json()
    # 事后字段已补录
    assert updated["outcome"] == "两周后价格站稳均线，观察正确"
    assert updated["retrospective"] == "下次同样先看结构再动"
    # 原始判断不可被改写
    assert updated["theme"] == original["theme"]
    assert updated["decision_summary"] == original["decision_summary"]
    assert updated["rationale"] == original["rationale"]
    assert updated["made_at"] == original["made_at"]
    assert updated["linked_evidence_ids"] == original["linked_evidence_ids"]


def test_decision_outcome_unknown_id_404(client):
    test_client, headers = client
    response = test_client.post(
        "/evidence/decision-outcome",
        headers=headers,
        json={"decision_id": str(uuid4()), "outcome": "不影响任何人的结果"},
    )
    assert response.status_code == 404


def test_decision_outcome_requires_text(client):
    test_client, headers = client
    created = test_client.post(
        "/decisions",
        headers=headers,
        json={"theme": "主题", "decision_summary": "概要"},
    )
    assert created.status_code == 201
    # outcome 为空串 → 422（契约要求 min_length=1，避免「补了个空结果」绕过验收）
    response = test_client.post(
        "/evidence/decision-outcome",
        headers=headers,
        json={"decision_id": created.json()["decision_id"], "outcome": ""},
    )
    assert response.status_code == 422
