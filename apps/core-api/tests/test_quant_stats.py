"""统计口径测试（量化研究实验室质量提升路线图 QL01/QL03）。

覆盖：平均秩 / 尾部标签剔除 / 重叠修正 t 值与 p 值校准 / BH-FDR / 块置换零分布尺度。
"""

from __future__ import annotations

import math
import random

from investment_steward_core import quant_stats


def test_average_ranks_shares_ties():
    # 并列值共享平均秩（旧实现在这里按顺序秩给出 1,2,3）
    assert quant_stats.average_ranks([5.0, 5.0, 5.0, 9.0, 9.0]) == [1.0, 1.0, 1.0, 3.5, 3.5]
    assert quant_stats.average_ranks([1.0, 2.0, 3.0]) == [0.0, 1.0, 2.0]


def test_spearman_matches_hand_computed_value_with_ties():
    """QL01(b) 验收:含并列序列的 rank_ic 与标准 Spearman 口径一致。

    手算：a=[1..5], b=[1,1,1,2,2] → b 的平均秩 = [1.5,1.5,1.5,4.5,4.5]
    cov=9.0, var_a=10, var_b=10.8 → ρ = 9/sqrt(108) = 0.8660254…
    """
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [1.0, 1.0, 1.0, 2.0, 2.0]
    expected = 9.0 / math.sqrt(108.0)
    assert abs(quant_stats.spearman(a, b) - expected) < 1e-9

    # 顺序秩(旧口径)会给出明显不同的值 —— 证明这次修复不是无操作
    def sequential_ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for rank, index in enumerate(order):
            out[index] = float(rank)
        return out

    legacy = quant_stats.pearson(sequential_ranks(a), sequential_ranks(b))
    assert abs(legacy - expected) > 0.05


def test_default_is_fdr_and_alpha_is_configurable():
    assert quant_stats.DEFAULT_ALPHA == 0.05
    assert isinstance(quant_stats.DEFAULT_SEED, int)


def test_benjamini_hochberg_rejects_exactly_the_planted_alternatives():
    p_values = [0.0001] * 5 + [0.9] * 95
    rejected = quant_stats.benjamini_hochberg(p_values, alpha=0.05)
    assert sum(1 for flag in rejected if flag) == 5
    assert rejected[:5] == [True] * 5
    assert not any(rejected[5:])


def test_benjamini_hochberg_monotone_in_alpha():
    p_values = [0.001, 0.01, 0.02, 0.03, 0.2]
    strict = sum(1 for flag in quant_stats.benjamini_hochberg(p_values, alpha=0.01) if flag)
    loose = sum(1 for flag in quant_stats.benjamini_hochberg(p_values, alpha=0.2) if flag)
    assert strict <= loose


def test_bh_threshold_is_never_below_alpha_over_m():
    p_values = [0.4, 0.5, 0.6]
    assert quant_stats.bh_threshold(p_values, alpha=0.05) == 0.05 / 3


def test_student_t_two_sided_p_known_values():
    # 双侧 5% 临界值:df=10 → t≈2.228
    assert abs(quant_stats.student_t_two_sided_p(2.228138852, 10) - 0.05) < 1e-6
    assert quant_stats.student_t_two_sided_p(0.0, 10) == 1.0
    assert quant_stats.student_t_two_sided_p(1.0, 0) == 1.0


def test_ic_statistics_effective_samples_uses_overlap():
    values = [float(i) for i in range(100)]
    forward: list[float | None] = [float(i) for i in range(100)]
    stat = quant_stats.ic_statistics(values, forward, overlap=5)
    assert stat["samples"] == 100
    assert abs(stat["effective_samples"] - 20.0) < 1e-9
    assert abs(stat["ic"] - 1.0) < 1e-12
    # 完全不相关时 p 应接近 1；单调关系下 t 为有限值
    assert 0.0 <= stat["p_value"] <= 1.0


def test_ic_statistics_p_value_is_calibrated_under_independence():
    """QL01(c) 验收:随机因子在 75 根样本上的 |IC| 分布与报告的 t 值自洽(p 值校准)。"""
    rng = random.Random(20260922)
    rejections = 0
    draws = 400
    for _ in range(draws):
        values = [rng.gauss(0.0, 1.0) for _ in range(75)]
        forward = [rng.gauss(0.0, 1.0) for _ in range(75)]
        stat = quant_stats.ic_statistics(values, forward, overlap=1)
        if stat["p_value"] < 0.05:
            rejections += 1
    rate = rejections / draws
    assert 0.01 <= rate <= 0.12, f"名义 5% 检验的实际拒绝率 {rate:.3f} 偏离过大"


def test_aligned_pairs_drops_missing_labels_and_non_finite_values():
    values = [1.0, 2.0, float("nan"), 4.0, 5.0]
    forward: list[float | None] = [0.1, None, 0.3, 0.4, None]
    xs, ys = quant_stats.aligned_pairs(values, forward)
    assert xs == [1.0, 4.0]
    assert ys == [0.1, 0.4]


def test_block_permute_preserves_multiset_and_is_seed_deterministic():
    labels = [float(i) for i in range(23)]
    first = quant_stats.block_permute(labels, block=5, rng=random.Random(7))
    second = quant_stats.block_permute(labels, block=5, rng=random.Random(7))
    assert first == second
    assert sorted(first) == sorted(labels)
    # 块整体搬运：first 必须是原块按某种顺序的串联（块长不等，末块可能被搬到前面）
    blocks = [list(labels[i : i + 5]) for i in range(0, 23, 5)]
    remaining = [list(block) for block in blocks]
    cursor = 0
    while cursor < len(first):
        matched = next(
            (block for block in remaining if first[cursor : cursor + len(block)] == block), None
        )
        assert matched is not None, f"下标 {cursor} 起的片段不属于任何原块:{first[cursor:]}"
        remaining.remove(matched)
        cursor += len(matched)
    assert not remaining and cursor == len(first)
    different = quant_stats.block_permute(labels, block=5, rng=random.Random(8))
    assert different != first


def test_shuffle_labels_keeps_missing_slots_in_place():
    forward: list[float | None] = [0.1, 0.2, 0.3, 0.4, 0.5, None, None]
    shuffled = quant_stats.shuffle_labels(forward, seed=11, block=3)
    assert shuffled[5] is None and shuffled[6] is None
    assert sorted(value for value in shuffled if value is not None) == [0.1, 0.2, 0.3, 0.4, 0.5]


def test_null_scale_recovers_textbook_value_for_iid_labels():
    """iid 标签下,块置换零分布应复现教科书口径:sd ≈ 1/√(n−1),95% 临界 ≈ 1.96/√(n−1)。

    这条验证的是**校准机制本身没跑偏**:真实重叠标签下同一机制会给出更宽的零分布,
    因此解析口径（按 n/overlap 折算）是否保守,由 QL01c 的因子级测试另行验证。
    n=80、40 个候选、200 次置换:样本量足够让经验值与理论值落在 20% 以内。
    """
    rng = random.Random(3)
    candidates = [[rng.gauss(0.0, 1.0) for _ in range(80)] for _ in range(40)]
    forward = [rng.gauss(0.0, 1.0) for _ in range(80)]
    result = quant_stats.null_scale_by_permutation(
        candidates, forward, overlap=5, permutations=200, seed=5
    )
    naive_sd = 1.0 / math.sqrt(80 - 1)
    assert abs(result["null_sigma"] - naive_sd) < 0.2 * naive_sd
    assert abs(result["empirical_critical_95"] - 1.96 * naive_sd) < 0.2 * (1.96 * naive_sd)
    assert result["pool_size"] == 40 * 200
    assert result["calibration_candidates"] == 40

    pool = result["abs_pool_ascending"]
    # 池最大值之上只可能拿到分辨率下界；池内取值应给出更大的 p
    assert quant_stats.p_value_from_null_pool(pool[-1] * 2.0, pool) == 1.0 / (len(pool) + 1)
    assert quant_stats.p_value_from_null_pool(0.0, pool) == 1.0


def test_null_scale_is_seed_deterministic():
    rng = random.Random(9)
    candidates = [[rng.gauss(0.0, 1.0) for _ in range(60)] for _ in range(10)]
    forward = [rng.gauss(0.0, 1.0) for _ in range(60)]
    first = quant_stats.null_scale_by_permutation(candidates, forward, overlap=5, permutations=50, seed=42)
    second = quant_stats.null_scale_by_permutation(candidates, forward, overlap=5, permutations=50, seed=42)
    assert first == second
    other = quant_stats.null_scale_by_permutation(candidates, forward, overlap=5, permutations=50, seed=43)
    assert other["abs_pool_ascending"] != first["abs_pool_ascending"]


def test_null_scale_degrades_loudly_without_candidates():
    result = quant_stats.null_scale_by_permutation([], [], overlap=5, permutations=10, seed=1)
    assert result["null_sigma"] is None and result["pool_size"] == 0
    assert result["degraded_reason"]


def test_greedy_decorrelate_treats_mirrored_series_as_one_cluster():
    """QL08 原语:反号（|ρ| = 1）也算同一簇,且按给定顺序保留最优代表。"""
    base = [float(i % 7) for i in range(50)]
    mirrored = [-value for value in base]
    other = [float((i * 3) % 11) for i in range(50)]
    result = quant_stats.greedy_decorrelate([base, mirrored, other], threshold=0.7)
    assert result["kept"] == [0, 2]
    assert result["clusters"][0]["merged"] == [1]
    assert result["clusters"][0]["max_abs_correlation"] == 1.0
    assert result["scanned"] == 3


def test_greedy_decorrelate_stops_at_keep():
    base = [float(i % 7) for i in range(50)]
    other = [float((i * 3) % 11) for i in range(50)]
    result = quant_stats.greedy_decorrelate([base, other], threshold=0.7, keep=1)
    assert result["kept"] == [0] and result["scanned"] == 1
    assert quant_stats.greedy_decorrelate([base, other], threshold=0.7, keep=0)["kept"] == []
