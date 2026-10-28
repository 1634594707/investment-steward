"""不作估值判断的枚举不是低估结论，旁边的真实结论仍须告警。"""
import pytest
from investment_steward_core import report_quality as rq


@pytest.mark.parametrize("text", [
    "因此不给「偏贵／合理／偏便宜」的判断，也不以盈利数据自行推算市盈率冒充来源数据[S4]。",
    "不给出偏便宜判断。",
    "不给「偏贵/合理/偏便宜」的估值判断。",
])
def test_withheld_judgement_does_not_conflict_with_undetermined_valuation(text):
    assert rq.valuation_conclusion_hits(text, "undetermined") == []
    aligned, changes = rq.align_valuation_language(text, "undetermined", "")
    assert aligned == text and changes == []


@pytest.mark.parametrize("tail,word", [
    ("，但PS提供安全垫", "安全垫"),
    ("，估值偏便宜", "偏便宜"),
    ("；PS被低估", "被低估"),
])
def test_refusal_does_not_hide_an_independent_conclusion(tail, word):
    text = "不给「偏贵／合理／偏便宜」的判断" + tail + "。"
    hits = rq.valuation_conclusion_hits(text, "undetermined")
    assert hits and any(word in reason for hit in hits for reason in hit["reasons"])


def test_positive_enumeration_is_still_a_claim():
    assert rq.valuation_conclusion_hits("给出「偏便宜」的判断。", "undetermined")
