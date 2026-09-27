"""确定性因子挖掘测试:StackVM 求值/健壮性/确定性挖掘/参数集形态。

并覆盖《量化研究实验室质量提升任务路线图》(2026-09-22) 的 P0/P1 验收:
QL01 标签与秩口径、QL02 三段式切分与排序键、QL03 多重检验校正、QL04 泄漏检查进主流程、
QL05 IC 评估套件、QL06 公式空间扩展与确定性束搜索。
"""

from __future__ import annotations

import json
import statistics

import pytest

from investment_steward_core import quant_factors, quant_stats


def _bars(count: int = 120) -> list[dict[str, object]]:
    bars: list[dict[str, object]] = []
    for i in range(count):
        phase = i % 16
        price = 10.0 + phase * 0.25 if phase < 8 else 12.0 - (phase - 8) * 0.25
        bars.append({
            "timestamp": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
            "open": round(price - 0.02, 3),
            "high": round(price + 0.06, 3),
            "low": round(price - 0.06, 3),
            "close": round(price, 3),
            "volume": 1000.0 + i,
        })
    return bars


def _mean_reverting_bars(count: int = 250) -> list[dict[str, object]]:
    """确定性均值回复序列:价格隔日 +0.1 / −0.1 交替 → 当日涨则次日跌（短期反转的干净锚）。"""
    bars: list[dict[str, object]] = []
    for i in range(count):
        price = round(10.0 + 0.1 * (i % 2), 4)
        bars.append({
            "timestamp": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
            "open": price,
            "high": round(price + 0.05, 4),
            "low": round(price - 0.05, 4),
            "close": price,
            "volume": 1000.0,
        })
    return bars


# --------------------------------------------------------------------------- #
# 求值内核
# --------------------------------------------------------------------------- #
def test_evaluate_tokens_tanh_formula():
    values = quant_factors.evaluate_tokens(["ret_5", "tanh"], _bars(40))
    assert len(values) == 40
    assert all(-1 <= v <= 1 for v in values)


def test_evaluate_tokens_div_by_zero_safe():
    values = quant_factors.evaluate_tokens(["ret_1", "0", "div"], _bars(30))
    assert all(v == 0.0 for v in values)


def test_evaluate_tokens_illegal_formula_raises():
    with pytest.raises(ValueError):
        quant_factors.evaluate_tokens(["add"], _bars(30))
    with pytest.raises(ValueError):
        quant_factors.evaluate_tokens(["no_such_feature"], _bars(30))


def test_rpn_arity_filter_matches_evaluability_and_keeps_enumeration_order():
    """元数预检必须与「能真正求值」等价,且枚举集合与顺序不变（确定性前提）。"""
    enumerated = list(quant_factors._valid_formulas(3))
    assert len(enumerated) == 1680  # 路线图实测计数(depth=3)
    bars = _bars(30)
    for tokens in enumerated[:200]:
        assert quant_factors._rpn_arity_ok(tokens)
        quant_factors.evaluate_tokens(tokens, bars)  # 不应抛错
    # 元数不合法的公式不会被枚举
    assert not quant_factors._rpn_arity_ok(["add"])
    assert not quant_factors._rpn_arity_ok(["ret_1", "ret_5"])
    assert not quant_factors._rpn_arity_ok([])


# --------------------------------------------------------------------------- #
# QL01 标签与秩口径
# --------------------------------------------------------------------------- #
def test_forward_returns_tail_is_missing_not_zero():
    """QL01(a):尾部 forward 窗口越界的样本标记缺失,不再填 0.0 与真实平盘混淆。"""
    closes = [100.0 + (i % 7) for i in range(60)]
    forward = quant_factors.forward_returns(closes, 5)
    assert len(forward) == len(closes)
    assert forward[-5:] == [None] * 5
    assert any(value is not None for value in forward[:55])


def test_tail_samples_do_not_enter_ic():
    """QL01(a) 验收:合成数据下尾部样本不参与 IC——样本数去掉尾部,且与手工配对结果一致。"""
    closes = [100.0 + (i % 7) for i in range(60)]
    factor = [float(c) for c in closes]
    forward = quant_factors.forward_returns(closes, 5)
    stat = quant_factors.ic_statistics(factor, forward, overlap=5)
    assert stat["samples"] == 55  # 60 − 5,显著性分母不再含无效样本

    xs, ys = quant_stats.aligned_pairs(factor, forward)
    assert len(xs) == 55
    assert abs(stat["ic"] - quant_stats.spearman(xs, ys)) < 1e-12

    # 旧口径（尾部填 0.0）会给出不同的 IC —— 证明这次修复不是无操作
    legacy = [
        (closes[i + 5] - closes[i]) / closes[i] if i + 5 < len(closes) and closes[i] else 0.0
        for i in range(len(closes))
    ]
    assert abs(quant_stats.spearman(factor, legacy) - stat["ic"]) > 1e-9


def test_rank_ic_uses_average_ranks_on_discrete_factors():
    """QL01(b):并列值取平均秩——离散因子的 IC 不再被顺序秩抖动。"""
    forward = [float(i) for i in range(40)]
    values = [float(i // 14) for i in range(40)]  # 只有 3 个取值
    correct = quant_factors.rank_ic(values, forward)
    assert 0.90 < correct < 1.0

    def sequential_ranks(series: list[float]) -> list[float]:
        order = sorted(range(len(series)), key=lambda i: series[i])
        out = [0.0] * len(series)
        for rank, index in enumerate(order):
            out[index] = float(rank)
        return out

    legacy = quant_stats.pearson(sequential_ranks(values), sequential_ranks(forward))
    assert legacy == 1.0
    assert abs(legacy - correct) > 0.05


def test_ic_statistics_reports_overlap_adjusted_t_value():
    """QL01(c):IC 报告附 t 值,自由度按重叠窗口折算的有效样本数。"""
    closes = [100.0 + (i % 7) for i in range(60)]
    factor = [float(c) for c in closes]
    stat = quant_factors.ic_statistics(factor, quant_factors.forward_returns(closes, 5), overlap=5)
    assert stat["samples"] == 55
    assert abs(stat["effective_samples"] - 55 / 5) < 1e-9
    assert stat["t_stat"] is not None
    assert (stat["t_stat"] > 0) == (stat["ic"] > 0)
    assert 0.0 < stat["p_value"] <= 1.0
    # 样本不足时明确退化,而不是给出伪造的 t
    short = quant_factors.ic_statistics([1.0] * 10, [(1.0 if i < 5 else None) for i in range(10)], overlap=1)
    assert short["ic"] == 0.0 and short["t_stat"] is None and short["p_value"] == 1.0


# --------------------------------------------------------------------------- #
# QL02 三段式切分与排序键
# --------------------------------------------------------------------------- #
def test_three_way_split_adapts_to_sample_size():
    split = quant_factors.three_way_split(250, 5)
    assert split["train_end"] == 122 and split["valid_end"] == 183
    assert split["planned"] == {"train": 122, "valid": 61, "test": 62}
    assert split["degraded"] is False

    # 小样本退化为三段等分并显式标记（不静默）
    small = quant_factors.three_way_split(60, 5)
    assert small["degraded"] is True and small["degraded_reason"]


def test_mine_reports_three_segments_and_is_bitwise_deterministic():
    """QL02 验收:同一数据重挖两次排行逐位一致;三段 IC 全部展示。"""
    first = quant_factors.mine(_bars(250))
    second = quant_factors.mine(_bars(250))
    assert first == second  # 同输入同结果(确定性)
    assert first["mine_spec_version"] == quant_factors.MINE_SPEC_VERSION
    assert first["split"]["ratio"] == [0.5, 0.25, 0.25]
    assert first["top"], "合成周期数据应能挖到显著公式"
    for item in first["top"]:
        assert {"formula_tokens", "train_ic", "valid_ic", "test_ic", "sign_consistency"} <= set(item)
        assert item["test_significant"] is True
        assert item["significance_note"] is None
        assert item["segment_samples"]["train"] + item["segment_samples"]["valid"] == 183
        assert 0.0 <= item["sign_consistency"] <= 1.0
        # 排序键 = |test_ic| × 逐段同号一致性 × 复杂度收缩（QL06 加入复杂度惩罚）
        # rank_score 由未取整的 IC 派生,故与取整后的展示值有 1e-4 量级差异
        assert item["rank_score"] == pytest.approx(abs(item["test_ic"]) * item["sign_consistency"], abs=1e-3)
        assert item["rank_score_adjusted"] == pytest.approx(
            item["rank_score"] * item["complexity_shrink"], abs=1e-6
        )
    # 榜单按排序键降序
    scores = [item["rank_score_adjusted"] for item in first["top"]]
    assert scores == sorted(scores, reverse=True)
    json.dumps(first["top"], ensure_ascii=False)  # 参数集形态:JSON 可序列化


def test_mine_marks_insignificant_candidates_explicitly():
    """QL02:未达显著的公式进 marginal 并显式标注,不抹掉也不假装显著。"""
    board = quant_factors.mine(_bars(250))
    assert board["marginal"]
    for item in board["marginal"]:
        assert item["test_significant"] is False
        assert item["significance_note"] and "未达统计显著" in item["significance_note"]


# --------------------------------------------------------------------------- #
# QL03 多重检验校正
# --------------------------------------------------------------------------- #
def test_mine_reports_candidate_counts_and_correction_metadata():
    board = quant_factors.mine(_bars(250))
    report = board["multiple_testing"]
    assert report["method"] == "fdr"
    assert report["alpha"] == 0.05
    assert report["seed"] == quant_stats.DEFAULT_SEED
    # QL06:候选总数 = 束搜索实际生成并求值过的公式数（远大于旧口径 1680）
    assert report["candidates_total"] == report["candidates_evaluated"]
    assert report["candidates_total"] > 1680
    assert report["passed_screen"] <= report["candidates_tested"] <= report["candidates_total"]
    assert report["rejected_total"] >= len(board["top"])  # top 只是显著者的前 top_n 条
    assert board["thresholds"] == {"min_train_ic": 0.05, "min_valid_ic": 0.02}
    # 旧口径全枚举仍可复现历史计数
    legacy = quant_factors.mine(_bars(250), search="exhaustive")["multiple_testing"]
    assert legacy["candidates_total"] == legacy["candidates_evaluated"] == 1680
    assert legacy["candidates_tested"] <= 1680


def test_mine_on_shuffled_labels_yields_empty_board_fdr():
    """QL03 验收:随机标签输入下榜单为空（防过拟合的直接证据,seed 落响应可复算）。"""
    bars = _bars(250)
    rejected_total = 0
    for seed in (1, 2, 3, 4):
        board = quant_factors.mine(bars, label_shuffle_seed=seed)
        assert board["top"] == [], f"seed={seed} 时随机标签下仍挖出显著公式"
        assert board["label_shuffle"]["seed"] == seed
        rejected_total += board["multiple_testing"]["rejected_total"]
    assert rejected_total == 0


def test_mine_on_shuffled_labels_yields_empty_board_permutation():
    bars = _bars(250)
    for seed in (1, 3, 8):
        board = quant_factors.mine(bars, correction="permutation", permutations=60, label_shuffle_seed=seed)
        assert board["top"] == [], f"置换口径下 seed={seed} 仍挖出显著公式"


def test_mine_permutation_mode_reports_seed_and_calibration():
    board = quant_factors.mine(_bars(250), correction="permutation", permutations=60, seed=123)
    report = board["multiple_testing"]
    assert report["method"] == "permutation"
    assert report["correction"] == "benjamini_hochberg"
    assert report["seed"] == 123 and report["permutations"] == 60
    assert report["p_resolution"] is not None
    calibration = report["calibration"]
    assert calibration["seed"] == 123 and calibration["block"] == quant_factors.FORWARD_DAYS
    assert calibration["pool_size"] > 0 and calibration["null_sigma"] > 0
    assert "abs_pool_ascending" not in calibration  # 大数组不进响应
    assert board == quant_factors.mine(_bars(250), correction="permutation", permutations=60, seed=123)


def test_mine_rejects_unknown_correction_and_bad_alpha():
    with pytest.raises(ValueError):
        quant_factors.mine(_bars(250), correction="bonferroni")
    with pytest.raises(ValueError):
        quant_factors.mine(_bars(250), alpha=1.5)


def test_resolve_window_tiers_are_explicit():
    default = quant_factors.resolve_window(None)
    assert default["bars"] == quant_factors.DEFAULT_WINDOW_BARS
    assert default["tiers"] == list(quant_factors.WINDOW_TIERS)
    assert default["reason"]  # 默认档必须有明确理由
    assert quant_factors.resolve_window(600)["bars"] == 750
    assert quant_factors.resolve_window(9999)["bars"] == quant_factors.WINDOW_TIERS[-1]
    assert quant_factors.resolve_window(9999)["clamped"] is True
    with pytest.raises(ValueError):
        quant_factors.resolve_window(10)


# --------------------------------------------------------------------------- #
# QL04 泄漏检查进主流程
# --------------------------------------------------------------------------- #
def test_mine_runs_lookahead_check_on_top_rows():
    """QL04 验收:入选 top-N 的公式自动跑泄漏检查,正常公式全部通过。"""
    board = quant_factors.mine(_bars(250))
    assert board["top"]
    for item in board["top"]:
        assert item["lookahead"]["uses_future_data"] is False
        assert item["lookahead"]["mismatch_count"] == 0
        assert item["lookahead"]["checked_points"]


def test_lookahead_check_detects_future_dependent_score():
    """QL04 验收:故意用未来数据的打分函数能被检出。"""
    from investment_steward_core import quant_experiments

    bars = _bars(120)

    def leaking(candidate_bars: list[dict[str, object]]) -> list[float]:
        closes = [float(bar["close"]) for bar in candidate_bars]
        # 用了整段（含未来）的收口价:截断后必然不同,故探针一定能抓到
        return [closes[-1] - close for close in closes]

    report = quant_experiments.detect_lookahead_bias(leaking, bars)
    assert report["uses_future_data"] is True
    assert report["mismatch_count"] > 0

    assert quant_factors.lookahead_check(["ret_5", "tanh"], bars)["uses_future_data"] is False


def test_lookahead_check_degrades_loudly_when_sample_too_short():
    report = quant_factors.lookahead_check(["ret_1"], _bars(20))
    assert report["uses_future_data"] is None
    assert report["detail"]


# --------------------------------------------------------------------------- #
# QL05 IC 评估套件
# --------------------------------------------------------------------------- #
def test_ic_suite_shape_and_caliber_version():
    board = quant_factors.mine(_bars(250))
    assert board["ic_suite_spec_version"] == quant_factors.IC_SUITE_SPEC_VERSION
    for item in board["top"]:
        suite = item["evaluation"]
        assert suite["spec_version"] == quant_factors.IC_SUITE_SPEC_VERSION
        period = suite["period_ic"]
        assert period["folds"] >= 2 and len(period["series"]) == period["folds"]
        assert 0.0 <= period["positive_ratio"] <= 1.0
        assert [point["days"] for point in suite["ic_decay"]] == sorted(quant_factors.IC_DECAY_HORIZONS)
        assert f"lag_{quant_factors.FORWARD_DAYS}" in suite["autocorrelation"]
        assert suite["group_returns"]["groups"] == quant_factors.GROUP_RETURN_GROUPS
        assert len(suite["group_returns"]["mean_forward_return"]) == quant_factors.GROUP_RETURN_GROUPS


def test_ic_suite_reproduces_reversal_direction_on_mean_reverting_series():
    """QL05 验收:对反转类基准因子（取负的 1 日收益）套件输出的方向与已知常识一致。

    确定性均值回复序列上「当日涨→次日跌」,故 −ret_1 与未来收益应正相关（短期反转）。
    这是口径正确性的方向锚:方向反了说明秩/对齐出了问题。
    """
    bars = _mean_reverting_bars(250)
    closes = [float(bar["close"]) for bar in bars]
    long_forward = quant_factors.forward_returns(closes, 1)

    reversal = quant_factors.evaluate_tokens(["ret_1", "neg"], bars)
    reversal_stat = quant_factors.ic_statistics(reversal, long_forward, overlap=1)
    assert reversal_stat["ic"] > 0.9
    suite = quant_factors.factor_evaluation(reversal, closes, forward_days=1)
    assert suite["group_returns"]["spread_top_bottom"] > 0
    assert suite["period_ic"]["positive_ratio"] == 1.0

    raw_stat = quant_factors.ic_statistics(quant_factors.evaluate_tokens(["ret_1"], bars), long_forward, overlap=1)
    assert raw_stat["ic"] < -0.9


def test_ic_suite_is_odd_under_formula_negation():
    """套件方向自洽:公式取负后 IC / 分组价差 / 自相关应整体反号（数学上必然）。"""
    bars = _bars(250)
    closes = [float(bar["close"]) for bar in bars]
    values = quant_factors.evaluate_tokens(["ma_ratio", "ret_5", "abs", "sub"], bars)
    negated = [-value for value in values]
    positive_suite = quant_factors.factor_evaluation(values, closes)
    negative_suite = quant_factors.factor_evaluation(negated, closes)

    assert positive_suite["group_returns"]["spread_top_bottom"] == pytest.approx(
        -negative_suite["group_returns"]["spread_top_bottom"], abs=1e-9
    )
    # 自相关两侧同时取负 → 相关系数不变（这是不变量，不是反号）
    assert positive_suite["autocorrelation"]["lag_1"] == pytest.approx(
        negative_suite["autocorrelation"]["lag_1"], abs=1e-9
    )
    assert positive_suite["period_ic"]["mean"] == pytest.approx(-negative_suite["period_ic"]["mean"], abs=1e-9)
    assert [point["ic"] for point in positive_suite["ic_decay"]] == pytest.approx(
        [-point["ic"] for point in negative_suite["ic_decay"]], abs=1e-9
    )


def test_period_ic_degrades_loudly_when_folds_too_small():
    result = quant_stats.period_ic_series([float(i) for i in range(30)], [float(i) for i in range(30)], folds=5)
    assert result["folds"] == 0
    assert "degraded_reason" in result


def test_quantile_group_returns_requires_enough_samples():
    result = quant_stats.quantile_group_returns([1.0, 2.0], [0.1, 0.2], groups=5)
    assert result["groups"] == 0 and result["spread_top_bottom"] is None


# --------------------------------------------------------------------------- #
# QL08 top-N 相关性去重
# --------------------------------------------------------------------------- #
def test_dedup_merges_strongly_correlated_variants():
    """QL08 验收:构造强相关变体（x 与 neg tanh x）只出一个代表。

    直接喂 ``greedy_decorrelate`` 五条序列:同一信号的四种写法 + 一条无关因子;
    只有四种写法会被并成一簇,代表保留排序最靠前的那个。
    """
    bars = _bars(250)
    variants = [
        quant_factors.evaluate_tokens(["ret_1"], bars),
        quant_factors.evaluate_tokens(["ret_1", "neg"], bars),
        quant_factors.evaluate_tokens(["ret_1", "tanh"], bars),
        quant_factors.evaluate_tokens(["ret_1", "neg", "tanh"], bars),
    ]
    unrelated = quant_factors.evaluate_tokens(["up_ratio_20"], bars)
    for variant in variants:
        assert abs(quant_stats.spearman(variant, variants[0])) > 0.99
    result = quant_stats.greedy_decorrelate([*variants, unrelated], threshold=0.7, keep=5)
    assert result["scanned"] == 5
    clusters = [cluster for cluster in result["clusters"] if cluster["merged"]]
    assert len(clusters) == 1
    assert clusters[0]["representative"] == 0  # 保留排序最靠前者
    assert set(clusters[0]["merged"]) == {1, 2, 3}
    assert 4 in result["kept"]  # 无关因子单独成簇


def test_mine_dedups_correlated_top_rows_end_to_end():
    """QL08 集成:真实挖掘榜单上代表之间不再强相关,簇内被合并者列明。"""
    bars = _bars(250)
    board = quant_factors.mine(bars)
    assert board["top"]
    assert board["dedup"]["threshold"] == quant_factors.DEDUP_CORRELATION_THRESHOLD

    values = [quant_factors.evaluate_tokens(row["formula_tokens"], bars) for row in board["top"]]
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            assert abs(quant_stats.spearman(values[i], values[j])) <= quant_factors.DEDUP_CORRELATION_THRESHOLD

    # 簇信息只在 top 行出现;size 与 merged_formulas 自洽（大簇只回传前若干条）
    for item in board["top"]:
        cluster = item["cluster"]
        assert cluster is not None and cluster["size"] >= 1
        assert len(cluster["merged_formulas"]) == min(cluster["size"] - 1, quant_factors.DEDUP_MERGED_SAMPLE)
        if cluster["size"] - 1 > quant_factors.DEDUP_MERGED_SAMPLE:
            assert cluster["merged_truncated"] is True
    for item in board["marginal"]:
        assert item["cluster"] is None  # marginal 行不参与去重,不带簇信息


def test_dedup_can_be_disabled_by_raising_threshold():
    bars = _bars(250)
    loose = quant_factors.mine(bars, dedup_correlation=1.01)
    assert loose["dedup"]["merged_total"] == 0
    assert all(item["cluster"]["size"] == 1 for item in loose["top"])
    assert len(loose["top"]) >= len(quant_factors.mine(bars)["top"])


# --------------------------------------------------------------------------- #
# QL06 公式空间扩展 + 确定性束搜索
# --------------------------------------------------------------------------- #
def _legacy_per_bar_evaluate(tokens: list[str], bars: list[dict[str, object]]) -> list[float]:
    """QL06 之前的**逐 bar 栈求值**路径原文复刻（只覆盖旧 token,不含时序算子）。

    用途:证明「序列化求值改造」对旧公式是**逐位等价**的（旧参数集回放值不变）。
    旧实现每 bar 只往栈里放标量,因此它天然无法承载需要窗口的时序算子——这正是
    QL06 要把求值改成「按序列」的原因,时序算子的语义另由 ``_naive_series_evaluate`` 对照。
    """
    import math

    for token in tokens:
        assert quant_factors.parse_ts_token(token) is None, "该参照实现不支持时序算子"

    feature = {
        name: quant_factors._feature_series(bars, name)
        for name in quant_factors.FEATURE_NAMES
        if name in tokens
    }
    out: list[float] = []
    for i in range(len(bars)):
        stack: list[float] = []
        for token in tokens:
            if token in feature:
                stack.append(feature[token][i])
                continue
            if token in quant_factors.BINARY_OPS:
                b = stack.pop()
                a = stack.pop()
                if token == "add":
                    stack.append(a + b)
                elif token == "sub":
                    stack.append(a - b)
                elif token == "mul":
                    stack.append(a * b)
                else:
                    stack.append(a / b if abs(b) > 1e-12 else 0.0)
                continue
            if token in quant_factors.UNARY_OPS:
                a = stack.pop()
                stack.append(abs(a) if token == "abs" else (math.tanh(a) if token == "tanh" else -a))
                continue
            stack.append(float(token))
        assert len(stack) == 1
        value = stack[0]
        out.append(value if math.isfinite(value) else 0.0)
    return out


def _naive_series_evaluate(tokens: list[str], bars: list[dict[str, object]]) -> list[float]:
    """独立实现的**按序列**求值参照:窗口一律用朴素切片求和,不共享 ``_rolling_*``。

    与 ``quant_factors._evaluate_with_cache`` 的差别只有浮点舍入（前缀和 vs 直接求和）,
    因此用于断言时序算子的语义（含嵌套）。
    """
    import math

    n = len(bars)
    stack: list[list[float]] = []
    for token in tokens:
        if token in quant_factors.FEATURE_NAMES:
            stack.append(list(quant_factors._feature_series(bars, token)))
            continue
        parsed = quant_factors.parse_ts_token(token)
        if parsed is not None:
            base, window = parsed
            src = stack.pop()
            out: list[float] = []
            for i in range(n):
                lo = max(0, i - window + 1)
                window_values = src[lo : i + 1]
                if base == "delay":
                    out.append(src[i - window] if i >= window else 0.0)
                elif base == "delta":
                    out.append(src[i] - src[i - window] if i >= window else 0.0)
                elif base == "ts_mean":
                    out.append(sum(window_values) / len(window_values))
                elif base == "ts_std":
                    out.append(statistics.stdev(window_values) if len(window_values) >= 2 else 0.0)
                elif base == "ts_zscore":
                    std = statistics.stdev(window_values) if len(window_values) >= 2 else 0.0
                    mean = sum(window_values) / len(window_values)
                    out.append((src[i] - mean) / std if std > 1e-12 else 0.0)
                else:  # ts_rank
                    below = sum(1 for v in window_values if v < src[i])
                    ties = sum(1 for v in window_values if v == src[i])
                    out.append(2.0 * ((below + 0.5 * ties) / len(window_values)) - 1.0)
            stack.append(out)
            continue
        if token in quant_factors.BINARY_OPS:
            right = stack.pop()
            left = stack.pop()
            if token == "add":
                stack.append([a + b for a, b in zip(left, right)])
            elif token == "sub":
                stack.append([a - b for a, b in zip(left, right)])
            elif token == "mul":
                stack.append([a * b for a, b in zip(left, right)])
            else:
                stack.append([(a / b if abs(b) > 1e-12 else 0.0) for a, b in zip(left, right)])
            continue
        if token in quant_factors.UNARY_OPS:
            source = stack.pop()
            if token == "abs":
                stack.append([abs(value) for value in source])
            elif token == "tanh":
                stack.append([math.tanh(value) for value in source])
            else:
                stack.append([-value for value in source])
            continue
        stack.append([float(token)] * n)
    assert len(stack) == 1
    return [value if math.isfinite(value) else 0.0 for value in stack[0]]


def test_formula_grammar_is_the_single_source_of_truth():
    """QL06 契约:导出的语法必须与解释器一致——每个 token 都能被求值、每个模式都能被解析。

    这是「公式 JSON 契约同步」的可执行版本:前端与分享池阶段 A 只以这份导出为准,
    不再各自维护一份 token 清单。
    """
    grammar = quant_factors.formula_grammar()
    assert grammar["formula_spec_version"] == quant_factors.FORMULA_SPEC_VERSION
    assert grammar["atoms"] == list(quant_factors.FEATURE_NAMES)
    assert grammar["legacy_atoms"] == list(quant_factors.LEGACY_FEATURE_NAMES)
    assert len(grammar["atoms"]) == 16  # 路线图要求 12—18 个原子
    assert 12 <= len(grammar["atoms"]) <= 18
    assert grammar["time_series"]["windows"] == list(quant_factors.TS_WINDOWS)
    assert len(grammar["time_series"]["tokens"]) == len(quant_factors.TS_WINDOW_OPS) * len(quant_factors.TS_WINDOWS)

    bars = _bars(80)
    # 每个原子都能求值
    for atom in grammar["atoms"]:
        assert len(quant_factors.evaluate_tokens([atom], bars)) == 80
    # 每个时序 token 都能求值,且能被 parse 回 (base, window)
    for token in grammar["time_series"]["tokens"]:
        parsed = quant_factors.parse_ts_token(token)
        assert parsed is not None and parsed[0] in quant_factors.TS_WINDOW_OPS
        assert len(quant_factors.evaluate_tokens(["ret_1", token], bars)) == 80
    # 非时序 token 不会被误解析
    for token in ("ret_1", "add", "tanh", "ts_mean", "ts_mean_x", "ts_unknown_5", "60"):
        assert quant_factors.parse_ts_token(token) is None
    json.dumps(grammar, ensure_ascii=False)  # 契约必须是纯 JSON


def test_time_series_operators_have_correct_semantics():
    """QL06:六个时序算子的语义逐点对照朴素定义（delay/delta 精确,窗口算子按 1e-9 相对误差）。"""
    bars = _bars(60)
    source = [float(i % 7) + 0.1 * i for i in range(60)]

    # delay / delta 是精确的移位与差分（含头部 0.0）
    delayed = quant_factors._ts_series("delay", 5, source, 60)
    assert delayed[:5] == [0.0] * 5
    assert delayed[5:] == source[:55]
    delta = quant_factors._ts_series("delta", 5, source, 60)
    assert delta[:5] == [0.0] * 5
    assert delta[5:] == [source[i] - source[i - 5] for i in range(5, 60)]

    # ts_mean / ts_std 与 statistics 一致（含 n−1 样本标准差）
    mean5 = quant_factors._ts_series("ts_mean", 5, source, 60)
    std5 = quant_factors._ts_series("ts_std", 5, source, 60)
    for i in (0, 3, 20, 59):
        window = source[max(0, i - 4) : i + 1]
        assert mean5[i] == pytest.approx(sum(window) / len(window), rel=1e-12)
        expected_std = statistics.stdev(window) if len(window) >= 2 else 0.0
        assert std5[i] == pytest.approx(expected_std, rel=1e-9)

    # ts_zscore = (x − 均值) / 样本标准差,标准差为 0 时记 0.0
    zscore = quant_factors._ts_series("ts_zscore", 5, source, 60)
    assert zscore[0] == 0.0
    assert quant_factors._ts_series("ts_zscore", 5, [1.0] * 20, 20) == [0.0] * 20
    for i in (10, 40):
        window = source[max(0, i - 4) : i + 1]
        expected = (source[i] - sum(window) / len(window)) / statistics.stdev(window)
        assert zscore[i] == pytest.approx(expected, rel=1e-9)

    # ts_rank 落在 [−1,1] 且对窗口内取值单调
    rank5 = quant_factors._ts_series("ts_rank", 5, source, 60)
    assert all(-1.0 <= v <= 1.0 for v in rank5)
    assert rank5[0] == 0.0  # 窗口只有自己:百分位 0.5 → 0.0

    # 常量序列的窗口算子应稳定为 0.0 / 常量
    flat = [3.0] * 30
    assert quant_factors._ts_series("ts_std", 10, flat, 30) == [0.0] * 30
    assert quant_factors._ts_series("ts_mean", 10, flat, 30) == [3.0] * 30
    assert bars  # 保持夹具被使用（避免误删）


def test_new_atoms_are_causal_under_truncation():
    """QL06 因果性证明:把行情截断到前 k 根,新原子的前 k 个值必须逐位不变。

    任何用到未来 bar 的实现都会在这个测试下失败——这是比「跑一次泄漏探针」更强的证明。
    """
    bars = _bars(200)
    for name in quant_factors.FEATURE_NAMES:
        full = quant_factors._feature_series(bars, name)
        for k in (45, 90, 150):
            truncated = quant_factors._feature_series(bars[:k], name)
            assert truncated == full[:k], f"{name} 在截断到 {k} 根后变化 → 用到了未来数据"


def test_time_series_tokens_pass_lookahead_check():
    """QL06 验收:新算子全部通过未来数据泄漏检查。"""
    bars = _bars(250)
    for token in quant_factors.TS_TOKENS:
        report = quant_factors.lookahead_check(["ret_1", token], bars)
        assert report["uses_future_data"] is False, f"{token} 未通过泄漏检查:{report['detail']}"
        assert report["mismatch_count"] == 0 and report["checked_points"]


def test_series_evaluation_is_bitwise_equivalent_to_legacy_per_bar_evaluation():
    """QL06 向后兼容验收:旧公式（不含时序 token）的求值结果**逐位不变**。

    对照实现是 QL06 之前的逐 bar 栈式求值原文 → 断言用 ``==``（逐位一致）,
    这是「旧参数集回放结果不变」的直接证据。
    """
    bars = _bars(250)
    legacy_formulas = [
        ["ret_1"],
        ["ret_5", "tanh"],
        ["ret_5", "1", "sub", "tanh"],
        ["ret_1", "ret_5", "sub"],
        ["vol_ratio", "ma_ratio", "mul"],
        ["rsi_14", "0", "sub", "abs", "neg"],
        ["ret_1", "abs", "tanh"],
    ]
    for tokens in legacy_formulas:
        reference = _legacy_per_bar_evaluate(tokens, bars)
        assert quant_factors.evaluate_tokens(tokens, bars) == reference, f"{tokens} 不再逐位一致"


def test_time_series_formulas_match_naive_window_reference():
    """QL06:含时序算子的公式与独立朴素实现语义一致（含嵌套与时序叠加）。"""
    bars = _bars(250)
    ts_formulas = [
        ["ret_1", "ts_mean_5"],
        ["ret_1", "ts_std_20"],
        ["ret_1", "ts_rank_60"],
        ["ret_1", "ts_zscore_10"],
        ["ret_5", "delta_5"],
        ["ret_5", "delay_20", "ret_5", "sub"],
        ["ret_1", "ts_mean_5", "gap", "mul"],
        ["ret_1", "ts_mean_5", "ts_zscore_10"],  # 时序叠加时序
        ["close_pos", "ts_rank_20", "hl_range", "add"],
    ]
    for tokens in ts_formulas:
        reference = _naive_series_evaluate(tokens, bars)
        actual = quant_factors.evaluate_tokens(tokens, bars)
        assert actual == pytest.approx(reference, rel=1e-9, abs=1e-12), f"{tokens} 语义与朴素定义不一致"


def test_beam_search_is_deterministic_and_reports_geometry():
    """QL06 验收:同输入同结果;束宽/层数/候选数与选择段全部落响应可复算。"""
    bars = _bars(250)
    first = quant_factors.mine(bars)
    assert first == quant_factors.mine(bars)
    search = first["search"]
    assert search["strategy"] == "beam"
    assert search["beam_width"] == quant_factors.BEAM_WIDTH
    assert search["levels"] == quant_factors.BEAM_LEVELS
    assert search["selection_segment"] == "train"
    assert search["atoms"] == len(quant_factors.FEATURE_NAMES)
    assert search["ts_tokens"] == len(quant_factors.TS_TOKENS)
    assert search["candidates_evaluated"] == search["candidates_generated"] > 1680
    assert search["candidates_tested"] <= search["candidates_evaluated"]
    assert first["formula_spec_version"] == quant_factors.FORMULA_SPEC_VERSION
    assert first["grammar"]["atoms"] == list(quant_factors.FEATURE_NAMES)
    assert "确定性束搜索" in first["note"]
    # 束宽可配置（复算必须用同一宽度）
    narrow = quant_factors.mine(bars, beam_width=4)
    assert narrow["search"]["beam_width"] == 4
    assert narrow["search"]["candidates_tested"] <= 4 * (quant_factors.BEAM_LEVELS + 1)


def test_beam_search_selection_uses_train_segment_only():
    """QL06 口径:搜索属于选模,不得看 valid/test。

    证明方式:把 forward 的 valid/test 段全部替换成「缺失」,束搜索选出的候选集合必须完全不变。
    """
    bars = _bars(250)
    forward = quant_factors.forward_returns([float(bar["close"]) for bar in bars], quant_factors.FORWARD_DAYS)
    split = quant_factors.three_way_split(len(bars), quant_factors.FORWARD_DAYS)
    valid_end = int(split["valid_end"])

    intact, _ = quant_factors._beam_search(bars, forward, split=split, forward_days=quant_factors.FORWARD_DAYS)
    poisoned_forward = list(forward)
    for i in range(int(split["train_end"]), len(poisoned_forward)):
        poisoned_forward[i] = 999.0  # train 之外全部投毒
    poisoned, _ = quant_factors._beam_search(
        bars, poisoned_forward, split=split, forward_days=quant_factors.FORWARD_DAYS
    )
    assert [item["formula_tokens"] for item in intact] == [item["formula_tokens"] for item in poisoned]
    assert valid_end > int(split["train_end"])


def test_complexity_penalty_is_monotone_and_enters_the_sort():
    """QL06:复杂度惩罚（AIC/BIC 式）随长度单调加重,并真的进入排序键。"""
    assert quant_factors._complexity_shrink(3, 250) < 1.0
    assert quant_factors._complexity_shrink(3, 250) > quant_factors._complexity_shrink(9, 250)
    assert quant_factors._complexity_shrink(3, 250, coefficient=0.0) == 1.0
    assert quant_factors._complexity_shrink(1000, 250) == 0.0  # 截断到 ≥ 0
    # 长度相同时收缩一致;排序键 = 原始键 × 收缩
    shrunk = quant_factors._complexity_shrink(5, 100)
    ratio = shrunk / quant_factors._complexity_shrink(5, 250)
    assert ratio < 1.0  # 样本越少罚得越重


def _ts_signal_bars(count: int = 250) -> list[dict[str, object]]:
    """未来收益由**过去 5 日均值**反向决定:只有时序算子能表达的真结构。"""
    returns = [0.004 * (((i * 7919) % 23) - 11) for i in range(count)]
    for i in range(5, count):
        returns[i] = -0.9 * (sum(returns[i - 5 : i]) / 5.0) + 0.002 * (((i * 104729) % 17) - 8)
    bars: list[dict[str, object]] = []
    close = 100.0
    for i in range(count):
        close *= 1.0 + returns[i]
        bars.append({
            "timestamp": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
            "open": close * 0.999,
            "high": close * 1.006,
            "low": close * 0.994,
            "close": close,
            "volume": 1000.0 + (i * 37) % 500,
        })
    return bars


def test_beam_search_reaches_time_series_operators():
    """QL06 验收:扩空间后能挖出**时序算子公式**（旧 6 原子 7 算子空间表达不了的结构）。"""
    board = quant_factors.mine(_ts_signal_bars(250))
    displayed = [item["formula"] for item in board["top"]] + [item["formula"] for item in board["marginal"]]
    assert displayed, "时序信号夹具上不应是空榜"
    assert any("ts_" in formula for formula in displayed), f"未挖到时序算子公式:{displayed}"


def test_mine_rejects_unknown_search_mode():
    with pytest.raises(ValueError):
        quant_factors.mine(_bars(250), search="genetic")
    with pytest.raises(ValueError):
        quant_factors.mine(_bars(250), beam_width=0)


def test_exhaustive_search_keeps_legacy_board_contract():
    """QL06 向后兼容:``search="exhaustive"`` 仍产出旧口径形态的榜单（含 1680 计数）。"""
    board = quant_factors.mine(_bars(250), search="exhaustive")
    assert board["search"]["strategy"] == "exhaustive"
    assert board["multiple_testing"]["candidates_total"] == 1680
    for item in board["top"]:
        assert set(item["formula_tokens"]) <= set(quant_factors.LEGACY_FEATURE_NAMES) | set(
            quant_factors.BINARY_OPS
        ) | set(quant_factors.UNARY_OPS) | {"1", "0"}
