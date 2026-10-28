"""英文词边界不能被删空白合并，否则同主题的相反证据会漏检。"""
from uuid import uuid4

from investment_steward_core.domain import Evidence
from investment_steward_core.research import select_relevant_evidence


def evidence(summary, relation="unknown", **updates):
    return Evidence(evidence_id=uuid4(), tenant_id=uuid4(), subject_refs=["synthetic-company"],
                    evidence_type="announcement", source_name="synthetic-feed", summary=summary,
                    content_hash="synthetic-hash-123456", relation=relation, **updates)


def test_english_cash_flow_search_keeps_supporting_and_contradicting_evidence():
    supporting = evidence("Operating cash flow increased this period", "supporting")
    opposing = evidence("Operating cash flow declined after customer cancellations", "contradicting")
    unrelated = evidence("A new factory opened overseas")
    result = select_relevant_evidence("operating cash flow", [supporting, opposing, unrelated])
    assert [row.evidence_id for row in result] == [supporting.evidence_id, opposing.evidence_id]


def test_word_boundaries_do_not_create_a_false_compound_match():
    unrelated = evidence("Cash flow declined")
    assert select_relevant_evidence("cashflow", [unrelated]) == []


def test_chinese_search_and_inactive_evidence_policy_remain_intact():
    current = evidence("订单取消导致经营现金流下降", "contradicting")
    inactive = evidence("经营现金流增长", status="retracted")
    assert select_relevant_evidence("经营现金流", [current, inactive]) == [current]
