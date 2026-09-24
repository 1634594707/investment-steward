"""J03（桌面端升级路线图 2026-09-18）：核心结论判定阈值化的闸门测试。

背景（样报 002468 §1.5）：旧判定 `supported > 0` 让「有支撑（有支撑 4/6）」与 1/6 显示完全
相同——非核心条数也能撑住「有支撑」。J03 之后：核心判断（importance=high）全数支撑才算
「有支撑」，部分支撑带分母（N/M）；模型未标 importance 时全部判断视为核心（保守兜底）。
"""

from __future__ import annotations

from investment_steward_core import report_quality

ALLOWED = {"S1", "S2", "S4"}


def _claim(claim_id: str, *, sources: list[str], requires: list[str], importance: str | None = None) -> dict:
    claim = {"claim_id": claim_id, "text": f"判断 {claim_id}", "sources": sources, "requires": requires}
    if importance is not None:
        claim["importance"] = importance
    return claim


def _supported_claim(claim_id: str, importance: str | None = None) -> dict:
    # S4 引用 + 无缺口 requires → supported（B 级达核心结论门槛）。
    return _claim(claim_id, sources=["S4"], requires=["net_income"], importance=importance)


def _downgraded_claim(claim_id: str, importance: str | None = None) -> dict:
    # 引 S2 却要求 S4 才覆盖的字段 → 覆盖缺口 → downgraded。
    return _claim(claim_id, sources=["S2"], requires=["goodwill"], importance=importance)


def test_one_of_six_is_partial_with_denominator():
    claims = [_supported_claim("C1")] + [_downgraded_claim(f"C{i}") for i in range(2, 7)]
    result = report_quality.check_claims(claims, allowed=ALLOWED)
    assert result["core_conclusion_state"] == "partial"
    assert result["core_support_counts"] == {"supported": 1, "total": 6}
    # 旧布尔口径同步收紧：1/6 不再是 True。
    assert result["core_conclusion_supported"] is False


def test_four_of_six_differs_from_one_of_six():
    four = report_quality.check_claims(
        [_supported_claim(f"C{i}") for i in range(1, 5)] + [_downgraded_claim("C5"), _downgraded_claim("C6")],
        allowed=ALLOWED,
    )
    one = report_quality.check_claims(
        [_supported_claim("C1")] + [_downgraded_claim(f"C{i}") for i in range(2, 7)],
        allowed=ALLOWED,
    )
    assert four["core_conclusion_state"] == "partial"
    assert one["core_conclusion_state"] == "partial"
    assert four["core_support_counts"] == {"supported": 4, "total": 6}
    assert one["core_support_counts"] == {"supported": 1, "total": 6}
    assert four["core_support_counts"] != one["core_support_counts"], "4/6 与 1/6 的判定信息必须可区分"


def test_non_core_claims_cannot_hold_up_or_drag_down():
    """4 条核心全支撑、2 条非核心失败 → 有支撑（4/4）；非核心既不撑住也不拖垮判定。"""
    claims = (
        [_supported_claim(f"K{i}", importance="high") for i in range(1, 5)]
        + [_downgraded_claim("L1", importance="low"), _downgraded_claim("L2", importance="low")]
    )
    result = report_quality.check_claims(claims, allowed=ALLOWED)
    assert result["core_conclusion_state"] == "supported"
    assert result["core_conclusion_supported"] is True
    assert result["core_support_counts"] == {"supported": 4, "total": 4}


def test_downgraded_core_claim_blocks_supported():
    """核心判断被降级时判定不得为「有支撑」：1 核心 downgraded、5 非核心 supported。"""
    claims = [_downgraded_claim("K1", importance="high")] + [
        _supported_claim(f"L{i}", importance="low") for i in range(2, 7)
    ]
    result = report_quality.check_claims(claims, allowed=ALLOWED)
    assert result["core_conclusion_state"] == "unsupported"
    assert result["core_conclusion_supported"] is False
    assert result["core_downgraded"] == ["K1"]


def test_no_claims_stays_undetermined():
    result = report_quality.check_claims([], allowed=ALLOWED)
    assert result["core_conclusion_state"] is None
    assert result["core_conclusion_supported"] is None
