"""横截面因子研究测试（QL11）:cs 算子语义、逐日 rank IC、重叠抽样 t 检验、分组中性化、确定性挖掘、空榜诚实。"""

from __future__ import annotations

import math

import pytest

from investment_steward_core import quant_cross, quant_factors, quant_panel
from test_quant_panel import SYMBOLS, synth_dates

DATES = synth_dates(90)  # 三段切分（50/25/25）要求每段 ≥ MIN_DATES_FOR_IC(=20) 个可用交易日


def _engineered_panel(
    symbols: list[str],
    dates: list[str],
    *,
    sign,
    amplitude: float = 0.002,
    jitter: float = 0.0,
    jitter2: float = 0.0,
    base: float = 10.0,
):
    """构造**解析已知**的截面:日收益 = ``amplitude × (截面序 − 中位) × sign(i) + 旋转扰动``。

    ``ret_1`` 与 1 日前瞻收益的逐日 rank IC 符号完全由 ``sign`` 决定:
    ``sign ≡ +1`` → 动量（IC 恒正）;``sign`` 逐日交替 → 反转（IC 恒负）。这就是「方向复现常识」的可判定版本。

    ``jitter``/``jitter2`` 是**跨标的相位不同、随日期旋转**的小扰动（幅度远小于标的间基准间距）:
    它让截面排序逐日轻微洗牌,IC 高但**方差非零** —— 否则 IC 恒为 ±1、标准差为 0,t 检验无法进行
    （零方差序列属于退化输入,只能如实报「无法估计显著性」）。真实横截面本来也不会逐日不变。
    """
    offset = (len(symbols) - 1) / 2
    per_symbol: dict[str, list[dict[str, object]]] = {}
    for index, symbol in enumerate(symbols):
        drift = amplitude * (index - offset)
        price = base + index * 0.05
        bars: list[dict[str, object]] = []
        for step, day in enumerate(dates):
            wobble = jitter * math.sin(step * 0.7 + index * 2.3) + jitter2 * math.cos(step * 0.13 + index * 5.1)
            previous = price
            price = max(0.5, price * (1.0 + drift * sign(step) + wobble))
            bars.append({
                "timestamp": f"{day}T00:00:00+00:00",
                "open": previous,
                "high": max(previous, price),
                "low": min(previous, price),
                "close": price,
                "volume": 1000.0 + index * 10 + step,
            })
        per_symbol[symbol] = bars
    return quant_panel.assemble_panel(per_symbol, symbols=symbols, window=len(dates))


def momentum_panel() -> dict[str, object]:
    """零扰动动量面板:IC 恒为 +1,用于判方向（不作显著性主张）。"""
    return _engineered_panel(SYMBOLS, DATES, sign=lambda _step: 1)


def reversal_panel() -> dict[str, object]:
    """零扰动反转面板:IC 恒为 −1,用于判方向。"""
    return _engineered_panel(SYMBOLS, DATES, sign=lambda step: 1 if step % 2 == 0 else -1)


def noisy_momentum_panel(dates: list[str] | None = None) -> dict[str, object]:
    """带旋转扰动的动量面板:IC 高但方差非零,用于跑完整挖掘链路（t 检验可算）。"""
    return _engineered_panel(
        SYMBOLS, dates or DATES, sign=lambda _step: 1, jitter=0.0009, jitter2=0.0006,
    )


# --------------------------------------------------------------------------- #
# 横截面算子
# --------------------------------------------------------------------------- #
def test_cs_rank_semantics_with_ties():
    # 并列取平均秩(0 基)后映射到 [−1,1]:秩 r → 2r/(n−1) − 1
    assert quant_cross.cs_rank([1.0, 2.0, 3.0, 4.0]) == pytest.approx([-1.0, -1 / 3, 1 / 3, 1.0])
    # 并列位次 0/1 取平均 0.5 → 2×0.5/3 − 1 = −2/3;最大位次 3 → +1
    assert quant_cross.cs_rank([5.0, 1.0, 1.0, 9.0]) == pytest.approx([1 / 3, -2 / 3, -2 / 3, 1.0])
    # 全体并列 → 全部落在中位 0（不是 −1/+1 这种虚假分化）
    assert quant_cross.cs_rank([7.0, 7.0, 7.0]) == pytest.approx([0.0, 0.0, 0.0])
    # 缺失值原样留 0,不参与取秩
    assert quant_cross.cs_rank([float("nan"), 1.0, 2.0])[0] == 0.0


def test_cs_zscore_is_pooled_standardisation():
    out = quant_cross.cs_zscore([1.0, 2.0, 3.0, 4.0])
    assert out == pytest.approx([-1.3416, -0.4472, 0.4472, 1.3416], abs=1e-4)
    assert sum(out) == pytest.approx(0.0, abs=1e-9)
    # 零方差 → 全 0（不做除零幻数）
    assert quant_cross.cs_zscore([3.0, 3.0, 3.0]) == [0.0, 0.0, 0.0]


def test_validate_tokens_accepts_cs_and_ts_ops():
    quant_cross.validate_tokens(["ret_1", "cs_rank"])
    quant_cross.validate_tokens(["vol_ratio_60", "ts_mean_20", "sub", "cs_zscore"])
    quant_cross.validate_tokens(["ret_5", "0.5", "mul"])
    with pytest.raises(ValueError, match="未知 token"):
        quant_cross.validate_tokens(["ret_1", "not_an_op"])


def test_evaluate_matrix_reuses_series_feature_definition():
    panel = momentum_panel()
    features = quant_cross.feature_matrix(panel)
    # 截面求值与逐标的时序求值必须同源（逐位一致）,否则「同一定义」是空话
    bars = quant_panel.bars_of(panel, SYMBOLS[0])
    assert features["ret_1"][SYMBOLS[0]] == pytest.approx(quant_factors._feature_series(bars, "ret_1"), abs=1e-12)
    assert features["ma_gap_5_20"][SYMBOLS[0]] == pytest.approx(
        quant_factors._feature_series(bars, "ma_gap_5_20"), abs=1e-12
    )
    # 16 个原子全部可用
    assert len(features) == len(quant_factors.FEATURE_NAMES) == 16


def test_evaluate_matrix_applies_cs_rank_per_date():
    panel = momentum_panel()
    features = quant_cross.feature_matrix(panel)
    raw = quant_cross.evaluate_matrix(["ret_1"], features, SYMBOLS, len(DATES))
    ranked = quant_cross.evaluate_matrix(["ret_1", "cs_rank"], features, SYMBOLS, len(DATES))
    # 截面秩化后每一天的值集合都是 {−1,…,1} 的等距排列
    row = sorted(ranked[symbol][40] for symbol in SYMBOLS)
    assert row[0] == pytest.approx(-1.0) and row[-1] == pytest.approx(1.0)
    # 时序算子与截面算子可组合（先时序均值再截面秩化）
    combo = quant_cross.evaluate_matrix(["ret_1", "ts_mean_5", "cs_zscore"], features, SYMBOLS, len(DATES))
    assert all(abs(sum(combo[symbol][50] for symbol in SYMBOLS)) < 1e-9 for _ in [0])
    assert raw[SYMBOLS[0]] != ranked[SYMBOLS[0]]


def test_evaluate_matrix_rejects_stack_underflow():
    panel = momentum_panel()
    features = quant_cross.feature_matrix(panel)
    with pytest.raises(ValueError, match="栈下溢"):
        quant_cross.evaluate_matrix(["ts_mean_20", "ret_5", "sub"], features, SYMBOLS, len(DATES))
    with pytest.raises(ValueError, match="栈残留"):
        quant_cross.evaluate_matrix(["ret_1", "ret_5"], features, SYMBOLS, len(DATES))


# --------------------------------------------------------------------------- #
# 逐日横截面 IC:方向复现常识
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("factory", "expected_sign", "label"),
    [(momentum_panel, 1.0, "动量"), (reversal_panel, -1.0, "短期反转")],
)
def test_daily_rank_ic_direction_matches_known_style(factory, expected_sign, label):
    """已知风格因子（动量 / 短期反转）的 IC 方向必须复现常识,不能靠人肉读图。"""
    panel = factory()
    features = quant_cross.feature_matrix(panel)
    forward = quant_cross.forward_return_matrix(panel, 1)
    daily = quant_cross.daily_rank_ic(features["ret_1"], forward, symbols=SYMBOLS, dates=DATES)
    # 第 0 根 bar 没有前收 → ret_1 全截面并列,该日的 IC 无信息量,按要求剔除后应当是**精确** ±1
    series = [point for point in daily["series"] if point["date"] != DATES[0]]
    summary = quant_cross.ic_series_summary(series, overlap=1)
    assert summary["mean"] == pytest.approx(expected_sign, abs=1e-9), f"{label} 方向不符:mean IC={summary['mean']}"
    assert summary["std"] == pytest.approx(0.0, abs=1e-9)
    assert summary["positive_ratio"] == (1.0 if expected_sign > 0 else 0.0)
    assert summary["samples"] >= len(DATES) - 3  # 仅首根与尾部 1 日无标签被剔除


def test_reversal_candidate_with_neg_token_flips_sign():
    panel = reversal_panel()
    features = quant_cross.feature_matrix(panel)
    forward = quant_cross.forward_return_matrix(panel, 1)
    negated = quant_cross.evaluate_matrix(["ret_1", "neg"], features, SYMBOLS, len(DATES))
    summary = quant_cross.ic_series_summary(
        quant_cross.daily_rank_ic(negated, forward, symbols=SYMBOLS, dates=DATES)["series"], overlap=1
    )
    assert summary["mean"] > 0.95  # 反转 → 取负后变正,这才是「反转因子」的正确写法


def test_daily_rank_ic_skips_thin_cross_sections_without_padding():
    panel = momentum_panel()
    features = quant_cross.feature_matrix(panel)
    forward = quant_cross.forward_return_matrix(panel, 1)
    # 只给 3 只（< MIN_SYMBOLS_PER_DATE=8）→ 每个交易日都跳过并计数,不报 IC
    daily = quant_cross.daily_rank_ic(features["ret_1"], forward, symbols=SYMBOLS[:3], dates=DATES)
    assert daily["series"] == []
    assert daily["skipped_total"] == len(DATES)
    assert "截面样本 3" in daily["skipped"][0]["reason"]


# --------------------------------------------------------------------------- #
# IC 序列统计:重叠标签抽样
# --------------------------------------------------------------------------- #
def test_ic_series_summary_strides_overlapping_labels():
    series = [{"date": f"2025-01-{index + 1:02d}", "ic": 0.1 + index * 0.001, "samples": 12} for index in range(50)]
    full = quant_cross.ic_series_summary(series, overlap=1)
    strided = quant_cross.ic_series_summary(series, overlap=5)
    assert full["samples"] == strided["samples"] == 50  # 均值/ICIR 口径不变
    assert full["mean"] == strided["mean"]
    assert full["effective_samples"] == 50
    assert strided["effective_samples"] == 10  # 每 5 日取一个近似独立样本
    assert strided["overlap"] == 5
    # 重叠不折算会高估显著性 → 抽样后的 |t| 必须更小(更保守)
    assert abs(strided["t_stat"]) < abs(full["t_stat"])


def test_ic_series_summary_stride_can_exhaust_the_variance():
    """抽样到「样本内无方差」时必须如实退化,不能编出一个 t 值。"""
    series = [{"date": f"2025-01-{index + 1:02d}", "ic": 0.05 if index % 2 == 0 else -0.05, "samples": 12}
              for index in range(20)]
    full = quant_cross.ic_series_summary(series, overlap=1)
    strided = quant_cross.ic_series_summary(series, overlap=4)  # 与周期对齐 → 抽样后全部同号同值
    assert full["t_stat"] is not None
    assert strided["effective_samples"] == 5
    assert strided["t_stat"] is None and strided["p_value"] == 1.0
    assert "未报 t 值" in strided["degraded_reason"]


def test_ic_series_summary_degrades_below_min_dates():
    series = [{"date": "2025-01-02", "ic": 0.2, "samples": 12}] * 5
    summary = quant_cross.ic_series_summary(series, overlap=1)
    assert summary["mean"] is None and summary["t_stat"] is None and summary["p_value"] is None
    assert summary["effective_samples"] == 5
    assert "不足即不报显著性" in summary["degraded_reason"]


# --------------------------------------------------------------------------- #
# 中性化
# --------------------------------------------------------------------------- #
def test_neutralise_rank_is_within_group_and_reports_unassigned():
    values = [10.0, 11.0, 12.0, 1.0, 2.0, 3.0]
    groups = ["A", "A", "A", "B", "B", "B"]
    ranked = quant_cross.neutralise(values, groups, mode="rank")
    assert ranked["groups_used"] == 2
    assert ranked["unassigned"] == 0 and ranked["unassigned_ratio"] == 0.0
    assert ranked["group_sizes"] == {"A": 3, "B": 3}
    # 组内秩化:两组各自 −1/0/1,组间量级差异被消除
    assert ranked["values"] == pytest.approx([-1.0, 0.0, 1.0, -1.0, 0.0, 1.0])

    demeaned = quant_cross.neutralise(values, groups, mode="demean")
    assert demeaned["values"] == pytest.approx([-1.0, 0.0, 1.0, -1.0, 0.0, 1.0])


def test_neutralise_leaves_unassigned_intact_and_counts_the_gap():
    values = [10.0, 11.0, 12.0, 7.0]
    groups = ["A", "A", "A", None]
    result = quant_cross.neutralise(values, groups, mode="rank")
    assert result["unassigned"] == 1 and result["unassigned_ratio"] == 0.25
    assert result["values"][3] == 7.0  # 未分配标的保留原值,不塞进任何组


def test_neutralise_rejects_unknown_mode_and_length_mismatch():
    with pytest.raises(ValueError, match="neutralise 仅支持"):
        quant_cross.neutralise([1.0], ["A"], mode="magic")
    with pytest.raises(ValueError, match="长度必须一致"):
        quant_cross.neutralise([1.0, 2.0], ["A"], mode="rank")


def test_neutralise_matrix_is_a_per_date_operation():
    panel = momentum_panel()
    features = quant_cross.feature_matrix(panel)
    matrix = {symbol: features["ret_1"][symbol] for symbol in SYMBOLS}
    groups = {symbol: ("前段" if index < 6 else "后段") for index, symbol in enumerate(SYMBOLS)}
    out = quant_cross.neutralise_matrix(
        matrix, groups, symbols=SYMBOLS, date_count=len(DATES), mode="rank"
    )
    assert out["mode"] == "rank" and out["groups_used"] == 2 and out["unassigned_ratio"] == 0.0
    for index in range(1, len(DATES)):  # 跳过第 0 根（全截面并列 → 全 0）
        row = sorted(out["matrix"][symbol][index] for symbol in SYMBOLS)
        assert row[0] == pytest.approx(-1.0) and row[-1] == pytest.approx(1.0)
    # 全部未分组时如实报 1.0,不假装做过
    none_assigned = quant_cross.neutralise_matrix(
        matrix, {}, symbols=SYMBOLS, date_count=len(DATES), mode="rank"
    )
    assert none_assigned["unassigned_ratio"] == 1.0


# --------------------------------------------------------------------------- #
# 横截面挖掘
# --------------------------------------------------------------------------- #
def test_cross_section_mine_is_deterministic():
    panel = noisy_momentum_panel()
    first = quant_cross.cross_section_mine(panel, forward_days=1, beam_width=6, beam_levels=2)
    second = quant_cross.cross_section_mine(panel, forward_days=1, beam_width=6, beam_levels=2)
    assert first == second  # 同输入同结果（含公式顺序与数值）
    assert first["cs_spec_version"] == quant_cross.CS_SPEC_VERSION == 1
    assert first["search"]["strategy"] == "beam"
    assert first["search"]["selection_segment"] == "train"


def test_cross_section_mine_surfaces_the_engineered_signal():
    panel = noisy_momentum_panel()
    board = quant_cross.cross_section_mine(panel, forward_days=1, beam_width=6, beam_levels=2)
    assert board["available"] is True
    assert board["top"], "解析已知的强信号应当被挖出来"
    first = board["top"][0]
    for key in ("formula", "formula_tokens", "train_ic", "valid_ic", "test_ic", "test_icir",
                "test_positive_ratio", "test_p_value", "sign_consistency", "complexity_shrink",
                "rank_score_adjusted", "test_significant"):
        assert key in first
    assert first["train_ic"] is not None and first["valid_ic"] is not None
    assert first["rank_score_adjusted"] == pytest.approx(first["rank_score"] * first["complexity_shrink"])
    # 三段 IC 与全部展示
    assert board["split"]["train_end"] > 0 and board["split"]["planned"]["test"] > 0
    assert board["multiple_testing"]["method"] == "fdr"
    assert board["ic_definition"]["kind"] == "daily_cross_sectional_spearman"


def test_cross_section_mine_returns_empty_board_on_shuffled_labels():
    """总体验收:标签置换后不得再挖出有效因子（空榜或全部标注不显著）。"""
    panel = noisy_momentum_panel()
    board = quant_cross.cross_section_mine(panel, forward_days=1, beam_width=6, beam_levels=2, label_shuffle_seed=7)
    assert board["label_shuffle"]["seed"] == 7
    assert board["label_shuffle"]["values_permuted"] > 0
    assert board["top"] == []
    assert board["available"] is False
    assert all(row["test_significant"] is False for row in board["marginal"])
    # 置换确定性:同 seed 同结果
    again = quant_cross.cross_section_mine(panel, forward_days=1, beam_width=6, beam_levels=2, label_shuffle_seed=7)
    assert again["top"] == board["top"] == []


def test_shuffle_cross_labels_preserves_coverage_and_is_seeded():
    panel = momentum_panel()
    forward = quant_cross.forward_return_matrix(panel, 1)
    first = quant_cross.shuffle_cross_labels(forward, SYMBOLS, seed=3)
    second = quant_cross.shuffle_cross_labels(forward, SYMBOLS, seed=3)
    other = quant_cross.shuffle_cross_labels(forward, SYMBOLS, seed=4)
    assert first == second
    assert first["forward"] != other["forward"]
    # 每日覆盖结构不变（None 位置不搬移）→ IC 序列样本数不变
    for symbol in SYMBOLS:
        assert [v is None for v in first["forward"][symbol]] == [v is None for v in forward[symbol]]
    # 值集合不变(只是换了标的)
    date_index = 30
    assert sorted(first["forward"][symbol][date_index] for symbol in SYMBOLS) == pytest.approx(
        sorted(forward[symbol][date_index] for symbol in SYMBOLS)
    )


def test_cross_section_mine_requires_data_and_degrades_without_neutralisation():
    empty = {"available": False, "degraded_reason": "面板不可用（未构建或标的/日期不足）"}
    board = quant_cross.cross_section_mine(empty)
    assert board["available"] is False and board["top"] == []
    assert board["cs_spec_version"] == quant_cross.CS_SPEC_VERSION
    assert board["degraded_reason"] == "面板不可用（未构建或标的/日期不足）"


def test_cross_section_mine_reports_raw_and_neutralised_ic_side_by_side():
    panel = noisy_momentum_panel()
    groups = {symbol: ("前段" if index < 6 else "后段") for index, symbol in enumerate(SYMBOLS)}
    board = quant_cross.cross_section_mine(
        panel, forward_days=1, beam_width=6, beam_levels=2,
        neutralise_mode="rank", groups_by_symbol=groups,
    )
    assert board["neutralise"]["mode"] == "rank"
    assert board["neutralise"]["groups_used"] == 2
    assert board["neutralise"]["unassigned_ratio"] == 0.0
    comparison = board["neutralise"]["comparison"]
    assert comparison["pairs"] >= 1
    # 中性化（量级差异被组内秩化吃掉）与原始口径并列给出,差值如实
    assert comparison["raw_test_ic_mean"] is not None
    assert comparison["delta_test_ic_mean"] == pytest.approx(
        comparison["neutralised_test_ic_mean"] - comparison["raw_test_ic_mean"], abs=1e-6
    )
    for row in board["top"]:
        assert row["neutralise_mode"] == "rank"
        assert row["raw_test_ic"] is not None
        assert row["neutralisation"]["mode"] == "rank"


def test_cross_section_mine_does_not_claim_neutralisation_without_groups():
    panel = noisy_momentum_panel()
    board = quant_cross.cross_section_mine(
        panel, forward_days=1, beam_width=4, beam_levels=1, neutralise_mode="rank", groups_by_symbol=None,
    )
    assert board["neutralise"]["mode"] == "rank"  # 口径如实保留
    assert board["neutralise"]["unassigned_ratio"] == 1.0  # 但缺口 100%,不冒充做过中性化
    assert board["neutralise"]["groups_used"] == 0


def test_cross_section_mine_degrades_to_three_equal_segments_on_medium_history():
    medium_dates = synth_dates(70)  # usable=69:50/25/25 → 34/17/18 有段低于 20,退化到等分 23/23/23
    panel = noisy_momentum_panel(medium_dates)
    board = quant_cross.cross_section_mine(panel, forward_days=1, beam_width=4, beam_levels=1)
    split = board["split"]
    assert split["degraded"] is True and split["feasible"] is True
    assert "退化为三段等分" in split["degraded_reason"]
    assert split["planned"]["train"] == split["planned"]["valid"] == split["planned"]["test"]
    assert board["available"] is True  # 等分后每段都达标 → 照常出结果,并把口径说明带上
    assert "切分口径:" in board["degraded_reason"]


def test_cross_section_mine_refuses_to_conclude_on_insufficient_dates():
    """连三段等分都达不到显著性地板时:如实拒绝并说明缺口,绝不静默给空榜。"""
    panel = _engineered_panel(SYMBOLS, synth_dates(40), sign=lambda _step: 1)
    board = quant_cross.cross_section_mine(panel, forward_days=1, beam_width=4, beam_levels=1)
    assert board["available"] is False and board["top"] == []
    assert board["split"]["feasible"] is False
    assert "拒绝在不足样本上下结论" in board["degraded_reason"]
    assert board["split"]["planned"]["train"] < quant_cross.MIN_DATES_FOR_IC


# --------------------------------------------------------------------------- #
# QL12:长空组合报告卡
# --------------------------------------------------------------------------- #
def test_quantile_members_split_is_deterministic_and_covers_everyone():
    column = [0.3, -0.1, 0.9, 0.0, 0.5, 0.7, -0.4, 0.2, 0.1, 0.6, -0.2, 0.4]
    groups = quant_cross._quantile_members(column, SYMBOLS, 4)
    assert len(groups) == 4
    flat = [symbol for group in groups for symbol in group]
    assert sorted(flat) == sorted(SYMBOLS) and len(flat) == len(SYMBOLS)  # 不丢标的
    assert groups == quant_cross._quantile_members(column, SYMBOLS, 4)  # 确定性
    # 组内因子值单调:最低组的第一名因子值必须最小
    assert column[SYMBOLS.index(groups[0][0])] == min(column)


def test_long_short_report_is_reproducible_and_挂_full_caliber():
    panel = noisy_momentum_panel()
    first = quant_cross.long_short_report(panel, ["ret_1"], quantiles=4, folds=3)
    second = quant_cross.long_short_report(panel, ["ret_1"], quantiles=4, folds=3)
    assert first == second  # 同快照同参数 → 逐位一致,report_hash 可复算
    assert first["available"] is True
    assert first["formula"] == "ret_1" and first["formula_tokens"] == ["ret_1"]
    assert len(first["report_hash"]) == 64
    assert first["caliber"]["backtest_spec_version"] == quant_cross.BACKTEST_SPEC_VERSION
    assert first["caliber"]["cs_spec_version"] == quant_cross.CS_SPEC_VERSION
    assert first["caliber"]["annual_trading_days"] == 252
    for key in ("commission_bps", "slippage_bps", "weighting", "turnover_definition",
                "portfolio_kind", "annualisation", "quantiles", "forward_days"):
        assert first["caliber"][key] not in (None, "")


def test_long_short_report_ic_and_quantile_structure_agree_with_known_signal():
    panel = noisy_momentum_panel()
    report = quant_cross.long_short_report(panel, ["ret_1"], quantiles=4, folds=2)
    # 已知动量信号:IC 强正;最高分位的前瞻收益应显著高于最低分位
    assert report["ic"]["mean"] > 0.8
    quantile_means = report["long_short"]["quantile_mean_forward_returns"]
    assert len(quantile_means) == 4
    assert quantile_means[-1] > quantile_means[0]
    assert report["long_short"]["gross_mean"] > 0


def test_long_short_report_charges_costs_and_reports_them():
    panel = noisy_momentum_panel()
    report = quant_cross.long_short_report(panel, ["ret_1"], quantiles=4, commission_bps=2.5, slippage_bps=5.0)
    leg = report["long_short"]
    # 建仓日按满仓换手计费 → 第一笔成本必须为正,净额必然低于毛额
    assert leg["equity_curve"][0]["turnover"] == 1.0
    assert leg["equity_curve"][0]["net"] < leg["equity_curve"][0]["gross"]
    assert leg["cost_total"] > 0
    assert leg["net_total"] < leg["gross_total"]
    assert leg["turnover_mean"] > 0
    # 零成本对照:成本归零时净值 == 毛值
    free = quant_cross.long_short_report(panel, ["ret_1"], quantiles=4, commission_bps=0.0, slippage_bps=0.0)
    assert free["long_short"]["net_total"] == pytest.approx(free["long_short"]["gross_total"], abs=1e-9)
    assert free["long_short"]["cost_total"] == pytest.approx(0.0, abs=1e-9)


def test_long_short_report_is_cost_sensitive_and_deterministic():
    panel = noisy_momentum_panel()
    cheap = quant_cross.long_short_report(panel, ["ret_1"], quantiles=4, commission_bps=1.0, slippage_bps=1.0)
    pricey = quant_cross.long_short_report(panel, ["ret_1"], quantiles=4, commission_bps=20.0, slippage_bps=30.0)
    assert pricey["long_short"]["net_total"] < cheap["long_short"]["net_total"]
    assert pricey["report_hash"] != cheap["report_hash"]  # 口径变了哈希必须变


def test_long_short_report_folds_cover_the_sample():
    panel = noisy_momentum_panel()
    report = quant_cross.long_short_report(panel, ["ret_1"], quantiles=4, folds=3)
    folds = report["folds"]
    assert len(folds) == 3
    assert [fold["fold"] for fold in folds] == [1, 2, 3]
    assert folds[0]["first_date"] == panel["dates"][0]
    assert folds[-1]["last_date"] == panel["dates"][-1]
    assert all(fold["ic_mean"] is not None for fold in folds)
    # fold 覆盖不重叠、首尾相接
    for previous, current in zip(folds, folds[1:]):
        assert previous["last_date"] < current["first_date"]


def test_long_short_report_capacity_is_an_explicit_proxy():
    panel = noisy_momentum_panel()
    capacity = quant_cross.long_short_report(panel, ["ret_1"])["capacity"]
    assert capacity["available"] is True
    assert capacity["volume_unit"] == 100.0
    assert "非真实撮合容量" in capacity["proxy"]
    assert capacity["per_name_notional_cny"] == pytest.approx(
        capacity["universe_median_adv_cny"] * capacity["participation"]
    )
    assert capacity["universe_notional_cny"] > capacity["per_name_notional_cny"]


def test_long_short_report_rejects_bad_quantiles_and_unavailable_panel():
    panel = noisy_momentum_panel()
    with pytest.raises(ValueError, match="quantiles 必须在"):
        quant_cross.long_short_report(panel, ["ret_1"], quantiles=1)
    empty = quant_cross.long_short_report({"available": False, "degraded_reason": "面板不可用"}, ["ret_1"])
    assert empty["available"] is False and empty["degraded_reason"] == "面板不可用"


def test_long_short_report_hash_binds_the_panel_snapshot(tmp_path):
    """report_hash 必须绑定面板内容:换一份快照 → 哈希必变（「可从快照复算」的凭据）。"""
    panel_a = noisy_momentum_panel()
    panel_b = noise_free_variant_panel()
    report_a = quant_cross.long_short_report(panel_a, ["ret_1"], quantiles=4)
    report_b = quant_cross.long_short_report(panel_b, ["ret_1"], quantiles=4)
    assert report_a["report_hash"] != report_b["report_hash"]
    assert quant_cross._report_hash(panel_a, ["ret_1"], 4, 2.5, 5.0, 3) == report_a["report_hash"]


def noise_free_variant_panel() -> dict[str, object]:
    """同一批日期、不同扰动的另一份面板（用于验证哈希绑定内容而不是参数）。"""
    return _engineered_panel(SYMBOLS, DATES, sign=lambda _step: 1, jitter=0.0012, jitter2=0.0002)
