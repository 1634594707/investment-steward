"""R03: evidence existence and completed factor analysis are separate judgments."""

import pytest

from investment_steward_core import jev_client, report_quality as rq


def _rows():
    return rq.normalize_earnings_growth([
        {"factor": "volume", "effect": "未提供产销量，无法验证销量贡献", "basis": "营收增加，但缺产销量",
         "sources": ["S4"], "analysis_state": "analyzed"},
        {"factor": "price", "effect": "单价下降拖累收入", "basis": "同口径单价下降，业务量不变",
         "sources": ["S4"], "contribution_pct": None},
    ], allowed_sources={"S4"})


def test_review_requires_factor_analysis_not_merely_a_supported_statement_about_missing_data():
    def judge(state, questions):
        assert "未提供产销量" in state
        assert len(questions) == 2
        assert "无法验证" in str(questions["e1"])
        return {"model": "test-review", "answers": {
            "e1": jev_client.JevAnswer(question_id="e1", type="choice", choice="insufficient"),
            "e2": jev_client.JevAnswer(question_id="e2", type="choice", choice="analyzed"),
        }}
    rows = rq.review_earnings_growth(_rows(), source_texts={"S4": "营收增加，单价下降，业务量不变。"}, judge=judge)
    assert rows[0]["analysis_state"] == "insufficient"
    assert rows[1]["analysis_state"] == "analyzed"
    assert rows[1]["contribution_pct"] is None  # 有证据的定性分析不必伪造贡献百分比。
    assert rows[1]["analysis_model"] == "test-review"


@pytest.mark.parametrize("sources", [{}, {"S4": ""}])
def test_missing_actual_evidence_is_not_sent_for_review(sources):
    def judge(*_):
        pytest.fail("缺少实际证据不应调用核验服务")
    rows = rq.review_earnings_growth(_rows(), source_texts=sources, judge=judge)
    assert all(row["analysis_state"] == "insufficient" for row in rows)


@pytest.mark.parametrize("mode", ["disabled", "failure", "missing_answer", "invalid_answer"])
def test_unavailable_review_never_accepts_model_authored_completion(mode):
    def judge(*_):
        if mode == "failure":
            raise RuntimeError("offline")
        return {"answers": {"e1": jev_client.JevAnswer(question_id="e1", type="choice", choice="wrong")}
                if mode == "invalid_answer" else {}}
    rows = rq.review_earnings_growth(_rows(), source_texts={"S4": "真实证据内容"},
                                    judge=None if mode == "disabled" else judge)
    assert all(row["analysis_state"] == "unverified" for row in rows)
    assert all("不计为完成分析" in row["analysis_note"] for row in rows)


@pytest.mark.parametrize("choice,expected", [("insufficient", "partial"), ("analyzed", "done")])
def test_only_reviewed_distinct_factors_complete_analysis_without_inventing_percentages(choice, expected):
    raw = [{"factor": key, "effect": "作用说明", "basis": "因素依据", "sources": ["S4"]}
           for key in ("volume", "price", "cost_efficiency")]
    def judge(state, questions):
        return {"answers": {key: jev_client.JevAnswer(question_id=key, type="choice", choice=choice)
                            for key in questions}}
    rows = rq.review_earnings_growth(rq.normalize_earnings_growth(raw, allowed_sources={"S4"}),
                                    source_texts={"S4": "合成核验证据"}, judge=judge)
    item = next(row for row in rq.research_completeness({"earnings_growth": rows})["items"]
                if row["key"] == "earnings_decomposition")
    assert item["state"] == expected
    assert len(item["covered"]) == (3 if choice == "analyzed" else 0)
    assert all(row["contribution_pct"] is None for row in rows)
