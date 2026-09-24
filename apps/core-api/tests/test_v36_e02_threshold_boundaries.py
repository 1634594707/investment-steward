"""E02（v36，2026-09-14 路线图 §7）：阈值边界稳定性。

对分界值附近的板块做**阈值扰动测试**：分界点上下各取一档，断言分类正确翻转，
且翻转后的判定依据文本**正确反映翻转的那一维**（不是笼统换标签）。

覆盖四个分界：
- PB 分位 70%（高位门槛，含/不含）= VALUATION_HIGH_PB_PERCENTILE_MIN
- PB 分位 40%（便宜区间上界，含/不含）= VALUATION_CHEAP_PB_PERCENTILE_MAX
- 亏损面 40% / 30%（深跌触发 / 低估准入）= VALUATION_LOSS_RATIO_DEEP_FALL_MIN / _LOW_MAX
- 背离 40pp（ROE 塌陷签名）= VALUATION_ROE_COLLAPSE_GAP_PP
- PE 分位 10% 与 PB 分位 30%（盈利周期顶双条件）

边界板块落入空档时（无结论）→ 断言分类为「待定」且依据文本**披露原因与缺失维度**。
"""

from __future__ import annotations

import pytest

from investment_steward_core import direction_research as dr


def _board(**kw) -> dr.BoardValuationInput:
    base = dict(
        board_code="BK_TEST",
        board_name="测试板块",
        pb_percentile_median=20.0,
        pe_percentile_median=20.0,
        loss_ratio=0.0,
        trend=None,
        pb_percentile_sample_count=5,
        loss_count=0,
        loss_total=100,
        trade_date="2026-09-11",
    )
    base.update(kw)
    return dr.BoardValuationInput(**base)


def _label(**kw) -> str:
    return dr.classify_board_valuation(_board(**kw))[0]


def _rationale(**kw) -> str:
    return dr.classify_board_valuation(_board(**kw))[1]


# ---------------------------------------------------------------------------
# 1. 高位门槛：PB 分位 70%
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pb,expected",
    [
        (69.9, dr.VALUATION_JUDGMENT_PENDING),  # 差 0.1pp 未过 → 空档
        (70.0, dr.VALUATION_JUDGMENT_HIGH),     # 恰好等于（≥ 含边界）→ 高位
        (70.1, dr.VALUATION_JUDGMENT_HIGH),
    ],
)
def test_e02_high_threshold_pb70(pb, expected):
    assert _label(pb_percentile_median=pb) == expected


def test_e02_high_threshold_rationale_names_pb_dimension():
    """高位翻转时，依据文本必须引用维度1（PB 分位）与门槛值，而非笼统表述。"""
    text = _rationale(pb_percentile_median=70.0)
    assert "PB 分位中位数 70.0%" in text
    assert "70%" in text
    assert "高位" in text
    # 边界值恰等 → 必须走「≥」一侧
    assert "≥" in text


# ---------------------------------------------------------------------------
# 2. 便宜区间上界：PB 分位 40%（深跌 / 低估候选的共同前置）
# ---------------------------------------------------------------------------


def test_e02_cheap_upper_bound_pb40_inclusive_deep_fall():
    """PB 恰好 40% 且亏损面过阈值 → 深跌未反转（≤ 含边界）。"""
    assert _label(pb_percentile_median=40.0, loss_ratio=0.45) == dr.VALUATION_JUDGMENT_DEEP_FALL


def test_e02_cheap_upper_bound_pb40_inclusive_low_candidate():
    """PB 恰好 40% 且亏损面低 → 低估候选。"""
    assert _label(pb_percentile_median=40.0, loss_ratio=0.10) == dr.VALUATION_JUDGMENT_LOW


def test_e02_cheap_upper_bound_pb40_1_falls_into_gap():
    """PB 40.1% 已出便宜区间，且未达高位门槛 → 待定（空档）。"""
    label = _label(pb_percentile_median=40.1, loss_ratio=0.45)
    assert label == dr.VALUATION_JUDGMENT_PENDING
    text = _rationale(pb_percentile_median=40.1, loss_ratio=0.45)
    assert "PB 分位 40.1%" in text
    assert "未落入任一判定区间" in text


# ---------------------------------------------------------------------------
# 3. 亏损面分界：40%（深跌触发）与 30%（低估准入）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "loss,expected",
    [
        (0.399, dr.VALUATION_JUDGMENT_PENDING),   # 39.9%：>30% 不过低估准入、<40% 不触发深跌 → 空档
        (0.400, dr.VALUATION_JUDGMENT_DEEP_FALL), # 恰好 40% → 深跌（≥ 含边界）
        (0.401, dr.VALUATION_JUDGMENT_DEEP_FALL),
    ],
)
def test_e02_loss_ratio_deep_fall_threshold(loss, expected):
    assert _label(pb_percentile_median=20.0, loss_ratio=loss) == expected


def test_e02_loss_ratio_gap_between_30_and_40_is_pending():
    """亏损面落在 30-40% 空档：既不入低估候选，也不触发深跌 → 待定，须披露原因。"""
    label = _label(pb_percentile_median=20.0, loss_ratio=0.35)
    assert label == dr.VALUATION_JUDGMENT_PENDING
    text = _rationale(pb_percentile_median=20.0, loss_ratio=0.35)
    assert "亏损面 35.0%" in text
    assert "未落入任一判定区间" in text


@pytest.mark.parametrize(
    "loss,expected",
    [
        (0.300, dr.VALUATION_JUDGMENT_LOW),       # 恰好 30% → 准入通过
        (0.301, dr.VALUATION_JUDGMENT_PENDING),   # 30.1% 过准入线但不触发深跌 → 空档
    ],
)
def test_e02_loss_ratio_low_candidate_threshold(loss, expected):
    assert _label(pb_percentile_median=20.0, loss_ratio=loss) == expected


def test_e02_deep_fall_rationale_names_loss_dimension():
    """亏损面触发深跌时，依据文本引用维度3（亏损面）与该维数值。"""
    text = _rationale(pb_percentile_median=20.0, loss_ratio=0.40)
    assert "亏损面 40.0%" in text
    assert "40%" in text


# ---------------------------------------------------------------------------
# 4. 背离分界：40pp（ROE 塌陷签名）
# ---------------------------------------------------------------------------
# 注意：低 PB 分位 + 亏损面低 + 背离未达线 = 低估候选（合法），不是空档。
# 背离分界的「下方」表现是**回落为低估候选**，「上方」才是深跌未反转。


@pytest.mark.parametrize(
    "pe,pb,expected,note",
    [
        (59.9, 20.0, dr.VALUATION_JUDGMENT_LOW, "背离 39.9pp 未达线且亏损面 0 → 低估候选"),
        (60.0, 20.0, dr.VALUATION_JUDGMENT_DEEP_FALL, "恰好 40.0pp → 深跌（≥ 含边界）"),
        (60.1, 20.0, dr.VALUATION_JUDGMENT_DEEP_FALL, "40.1pp"),
        (40.0, 20.0, dr.VALUATION_JUDGMENT_LOW, "背离 20.0pp 未达线 → 低估候选"),
    ],
)
def test_e02_roe_gap_threshold(pe, pb, expected, note):
    assert _label(pb_percentile_median=pb, pe_percentile_median=pe, loss_ratio=0.0) == expected, note


def test_e02_roe_gap_rationale_shows_arithmetic():
    """背离触发时，依据文本给出算式（PE 分位 − PB 分位 = 背离）与门槛值。"""
    text = _rationale(pb_percentile_median=20.0, pe_percentile_median=60.0, loss_ratio=0.0)
    assert "PE 分位 60.0% − PB 分位 20.0% = 背离 40.0pp" in text
    assert "40pp" in text
    assert "ROE 塌陷签名" in text


# ---------------------------------------------------------------------------
# 5. 盈利周期顶双条件：PE 分位 10% + PB 分位 30%
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pe,pb,expected,note",
    [
        (10.0, 30.0, dr.VALUATION_JUDGMENT_CYCLE_TOP, "双边界恰好命中"),
        (10.1, 30.0, dr.VALUATION_JUDGMENT_LOW, "PE 超 0.1pp 未达周期顶；PB≤40 且背离为负 → 低估候选"),
        (10.0, 29.9, dr.VALUATION_JUDGMENT_LOW, "PB 差 0.1pp 未达周期顶；仍在便宜区间 → 低估候选"),
        (0.0, 30.0, dr.VALUATION_JUDGMENT_CYCLE_TOP, "PE 极低端"),
        (10.0, 69.9, dr.VALUATION_JUDGMENT_CYCLE_TOP, "PB 高但未过高位门槛"),
    ],
)
def test_e02_cycle_top_double_condition(pe, pb, expected, note):
    assert _label(pb_percentile_median=pb, pe_percentile_median=pe, loss_ratio=0.0) == expected, note


def test_e02_cycle_top_near_miss_in_gap_band_is_pending():
    """周期顶双条件都不满足、且 PB 落在 40-70% 空档 → 待定，须披露四维数值。"""
    label = _label(pb_percentile_median=50.0, pe_percentile_median=30.0, loss_ratio=0.0)
    assert label == dr.VALUATION_JUDGMENT_PENDING
    text = _rationale(pb_percentile_median=50.0, pe_percentile_median=30.0, loss_ratio=0.0)
    assert "PB 分位 50.0%" in text
    assert "未落入任一判定区间" in text


def test_e02_cycle_top_rationale_cites_both_dimensions():
    text = _rationale(pb_percentile_median=30.0, pe_percentile_median=10.0, loss_ratio=0.0)
    assert "PE 分位中位数 10.0%" in text
    assert "PB 分位 30.0%" in text
    assert "盈利周期顶" in text


# ---------------------------------------------------------------------------
# 6. 判定顺序稳定性：多重命中时按冻结顺序归类
# ---------------------------------------------------------------------------


def test_e02_joint_high_and_cycle_top_prefers_high():
    """PB 96.6% + PE 0.1% 同时满足高位与周期顶 → 按顺序取高位。"""
    assert _label(pb_percentile_median=96.6, pe_percentile_median=0.1, loss_ratio=0.0) == (
        dr.VALUATION_JUDGMENT_HIGH
    )


def test_e02_joint_deep_fall_and_low_prefers_deep_fall():
    """PB 20% + 亏损面 45% 同时满足深跌触发与低分位 → 取深跌未反转。"""
    assert _label(pb_percentile_median=20.0, loss_ratio=0.45) == dr.VALUATION_JUDGMENT_DEEP_FALL


def test_e02_missing_pb_is_pending_with_missing_dimension_disclosed():
    """维度1（PB 分位）不可得 → 待定，且依据文本披露缺哪维（不填默认值）。"""
    label = _label(pb_percentile_median=None)
    assert label == dr.VALUATION_JUDGMENT_PENDING
    text = _rationale(pb_percentile_median=None)
    assert "不可得" in text
    assert "缺失维度" in text


def test_e02_missing_loss_does_not_silently_trigger_deep_fall():
    """亏损面不可得时**不**当作 0，也不当作触发——低分位但两触发维皆缺 → 不误判深跌。

    口径：`loss_triggers` / `gap_triggers` 均要求对应维度非 None，
    因此低分位 + 亏损面 None + 背离 <40pp → 落入空档待定（保守，不冒充低估候选）。
    """
    label = _label(pb_percentile_median=20.0, pe_percentile_median=20.0, loss_ratio=None)
    assert label == dr.VALUATION_JUDGMENT_PENDING


def test_e02_threshold_constants_are_the_frozen_m0_values():
    """E02 前置：扰动测试必须锚在 M0 冻结值上，常量被改动时这里先红。"""
    assert dr.VALUATION_HIGH_PB_PERCENTILE_MIN == 70.0
    assert dr.VALUATION_CHEAP_PB_PERCENTILE_MAX == 40.0
    assert dr.VALUATION_CYCLE_TOP_PE_PERCENTILE_MAX == 10.0
    assert dr.VALUATION_CYCLE_TOP_PB_PERCENTILE_MIN == 30.0
    assert dr.VALUATION_ROE_COLLAPSE_GAP_PP == 40.0
    assert dr.VALUATION_LOSS_RATIO_DEEP_FALL_MIN == 0.40
    assert dr.VALUATION_LOSS_RATIO_LOW_MAX == 0.30
