"""确定性统计口径(quant_stats):因子评估所需的秩相关 / 显著性 / 多重检验原语。

口径冻结(改动任何一项必须递增 IC_SPEC_VERSION,旧制品按旧口径回放):

1. **秩口径**:Spearman 秩相关,并列值取**平均秩**(fractional rank)。
   旧实现用顺序秩(`enumerate(sorted order)`),离散因子(如 tanh 饱和后的取值、
   分组型特征)会因并列顺序被人为抖动,秩相关失真。平均秩是该口径的标准定义。
2. **标签口径**:未来 N 日收益在尾部窗口越界时**不填 0.0**,而是标记为缺失并剔除。
   旧实现填 0.0,与真实「平盘」样本混淆,且把 5 个无效样本算进显著性分母。
3. **重叠修正**:未来 N 日收益逐日重叠(自相关窗口长 N),独立样本数远少于名义样本数。
   有效样本数按 ``n_eff ≈ n / overlap`` 折算,t 值按 n_eff 的自由度计算:
   ``t = ρ · sqrt((n_eff - 2) / (1 - ρ²))``,p 值取 Student-t 双侧。
   这是**保守近似**(不精确建模重叠带来的自相关结构),但不做修正则必然高估显著性。
4. **多重检验**:枚举式挖掘天然是多重检验(winner's curse)。
   默认 Benjamini-Hochberg FDR(对**全部进入检验的候选**做),
   可选块置换检验(block permutation,块长 = overlap,固定种子,仅作用于初筛通过的候选)。
   两者都把「候选总数 / 通过数 / 方法 / seed」写进响应。

全部实现为 stdlib(不引入 numpy/scipy):同输入同结果、逐位可复算。
"""

from __future__ import annotations

import bisect
import math
import random
from typing import Any, Sequence

IC_SPEC_VERSION = 1
DEFAULT_ALPHA = 0.05
DEFAULT_SEED = 20260922
DEFAULT_PERMUTATIONS = 200


# --------------------------------------------------------------------------- #
# 秩与相关
# --------------------------------------------------------------------------- #
def average_ranks(values: Sequence[float]) -> list[float]:
    """平均秩(并列值共享同一秩,取并列位次均值);0 基。确定性。"""
    n = len(values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        pivot = values[order[i]]
        while j + 1 < n and values[order[j + 1]] == pivot:
            j += 1
        shared = (i + j) / 2.0
        for k in range(i, j + 1):
            out[order[k]] = shared
        i = j + 1
    return out


def pearson(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson 相关;长度不等/样本不足/零方差返回 0.0。"""
    n = len(a)
    if n != len(b) or n < 2:
        return 0.0
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = 0.0
    var_a = 0.0
    var_b = 0.0
    for x, y in zip(a, b):
        dx = x - mean_a
        dy = y - mean_b
        cov += dx * dy
        var_a += dx * dx
        var_b += dy * dy
    if var_a <= 0.0 or var_b <= 0.0:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


def spearman(a: Sequence[float], b: Sequence[float]) -> float:
    """Spearman 秩相关(并列取平均秩);长度不等/零方差返回 0.0。"""
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    return pearson(average_ranks(a), average_ranks(b))


def aligned_pairs(
    values: Sequence[float], forward: Sequence[float | None]
) -> tuple[list[float], list[float]]:
    """对齐 (因子值, 未来收益):剔除标签缺失(None)与因子非有限值。确定性保序。"""
    xs: list[float] = []
    ys: list[float] = []
    for v, f in zip(values, forward):
        if f is None:
            continue
        if not math.isfinite(v):
            continue
        xs.append(float(v))
        ys.append(float(f))
    return xs, ys


# --------------------------------------------------------------------------- #
# Student-t 分布(正则化不完全 Beta 函数,数值食谱口径)
# --------------------------------------------------------------------------- #
def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    max_iter = 300
    eps = 3.0e-16
    fpmin = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b),确定性(连分式收敛判据固定)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _beta_continued_fraction(a, b, x) / a
    return 1.0 - bt * _beta_continued_fraction(b, a, 1.0 - x) / b


def student_t_two_sided_p(t_stat: float, degrees_of_freedom: float) -> float:
    """P(|T| > |t|),自由度 df;**双侧** p 值。df<=0 或无 t 时返回 1.0。"""
    if degrees_of_freedom <= 0.0 or not math.isfinite(t_stat):
        return 1.0
    x = degrees_of_freedom / (degrees_of_freedom + t_stat * t_stat)
    if x <= 0.0:
        return 0.0
    return regularized_incomplete_beta(degrees_of_freedom / 2.0, 0.5, x)


# --------------------------------------------------------------------------- #
# IC 统计量
# --------------------------------------------------------------------------- #
def ic_statistics(
    values: Sequence[float],
    forward: Sequence[float | None],
    *,
    overlap: int = 1,
    min_samples: int = 2,
) -> dict[str, Any]:
    """秩相关 + 重叠修正的显著性。

    overlap 为标签的前视窗口长度(未来 N 日收益则 overlap=N);
    n_eff = n / overlap,t = ρ·sqrt((n_eff-2)/(1-ρ²)),p 为 t 双侧 p。
    """
    xs, ys = aligned_pairs(values, forward)
    n = len(xs)
    ic = spearman(xs, ys) if n >= min_samples else 0.0
    span = max(1, int(overlap))
    n_eff = n / span
    df = n_eff - 2.0
    if n < min_samples or df <= 0.0 or abs(ic) >= 1.0:
        t_stat = 0.0 if n < min_samples or df <= 0.0 else math.copysign(math.inf, ic)
        p_value = 1.0
    else:
        t_stat = ic * math.sqrt(df / (1.0 - ic * ic))
        p_value = student_t_two_sided_p(t_stat, df)
    return {
        "ic": ic,
        "samples": n,
        "overlap": span,
        "effective_samples": round(n_eff, 6),
        "t_stat": t_stat if math.isfinite(t_stat) else None,
        "p_value": p_value,
    }


# --------------------------------------------------------------------------- #
# 多重检验
# --------------------------------------------------------------------------- #
def benjamini_hochberg(p_values: Sequence[float], *, alpha: float = DEFAULT_ALPHA) -> list[bool]:
    """BH-FDR:返回每个候选是否被拒绝(通过)。p 值相等时按原序稳定排序,确定性。"""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    k_max = 0
    for rank, index in enumerate(order, start=1):
        if p_values[index] <= alpha * rank / m:
            k_max = rank
    rejected = [False] * m
    for rank, index in enumerate(order, start=1):
        if rank <= k_max:
            rejected[index] = True
    return rejected


def bh_threshold(p_values: Sequence[float], *, alpha: float = DEFAULT_ALPHA) -> float:
    """BH 实际生效的 p 阈值(最大被拒绝候选取 max(alpha·k/m, p_(k)))。"""
    m = len(p_values)
    if m == 0:
        return 0.0
    order = sorted(range(m), key=lambda i: p_values[i])
    k_max = 0
    for rank, index in enumerate(order, start=1):
        if p_values[index] <= alpha * rank / m:
            k_max = rank
    if k_max == 0:
        return alpha / m
    return max(alpha * k_max / m, p_values[order[k_max - 1]])


def block_permute(labels: Sequence[float], *, block: int, rng: random.Random) -> list[float]:
    """块置换:按 block 长度切块并打乱块序(保留块内重叠结构),确定性由 rng 决定。"""
    n = len(labels)
    if n == 0:
        return []
    span = max(1, block)
    blocks = [list(labels[i : i + span]) for i in range(0, n, span)]
    rng.shuffle(blocks)
    out: list[float] = []
    for chunk in blocks:
        out.extend(chunk)
    return out[:n]


def shuffle_labels(
    forward: Sequence[float | None], *, seed: int, block: int
) -> list[float | None]:
    """随机标签（过拟合自检）:把**有效标签**做确定性块置换,缺失位保持缺失。

    用途:把同一批因子值配到打乱的标签上重跑一整条挖掘链路——若榜单仍能「挖到」显著公式,
    说明统计口径在噪声上也能造出显著性。这是防过拟合的直接证据(路线图 §7 总体验收)。

    注意:打乱 K 线顺序**不能**当零假设(特征与标签共享当根 close,存在真实的机械耦合,
    会稳定产出伪显著);只有置换标签才是干净的零假设。
    """
    indexes = [i for i, value in enumerate(forward) if value is not None]
    values = [float(forward[i]) for i in indexes]  # type: ignore[arg-type]
    permuted = block_permute(values, block=block, rng=random.Random(seed))
    out: list[float | None] = list(forward)
    for position, index in enumerate(indexes):
        out[index] = permuted[position]
    return out


def null_scale_by_permutation(
    candidate_values: Sequence[Sequence[float]],
    forward: Sequence[float | None],
    *,
    overlap: int,
    permutations: int = DEFAULT_PERMUTATIONS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """块置换估计**零分布尺度**:对标签做确定性块置换,汇总候选的秩相关得到经验零分布。

    用于把解析 p 值换成「经验零分布校准」的 p 值:``p = erfc(|IC| / (σ̂√2))``。
    为什么不用逐候选经验 p:经验 p 的分辨率是 1/(池大小),而 BH 在候选数 m=1680 时阈值 ~α·k/m
    可低至 1e-5,分辨率不够会退化成「永远不显著」;尺度校准既保留了置换的分布信息,
    又给出连续 p 值。**候选池必须覆盖全部候选**（此处用确定性等距抽样）——只看按观测 |IC|
    选出的头部候选会引入选择偏差,反而把噪声判成显著。

    实现要点:因子秩与候选无关地固定,逐置换只需重算标签秩,再用中心化秩向量点积求相关,
    避免 O(置换×候选) 次排序。
    """
    usable = [list(values) for values in candidate_values]
    if not usable or permutations <= 0:
        return {"null_sigma": None, "null_mean": None, "pool_size": 0, "permutations": permutations,
                "seed": seed, "block": max(1, int(overlap)), "degraded_reason": "无候选或置换次数为 0"}
    _, labels = aligned_pairs(usable[0], forward)
    if len(labels) < 3:
        return {"null_sigma": None, "null_mean": None, "pool_size": 0, "permutations": permutations,
                "seed": seed, "block": max(1, int(overlap)), "degraded_reason": "有效标签不足"}

    factor_ranks: list[tuple[list[float], float]] = []
    for values in usable:
        xs, _ = aligned_pairs(values, forward)
        if len(xs) != len(labels):
            continue
        ranks = average_ranks(xs)
        mean = sum(ranks) / len(ranks)
        centered = [r - mean for r in ranks]
        norm = math.sqrt(sum(r * r for r in centered))
        factor_ranks.append((centered, norm))

    rng = random.Random(seed)
    pool: list[float] = []
    for _ in range(permutations):
        permuted = block_permute(labels, block=overlap, rng=rng)
        label_ranks = average_ranks(permuted)
        mean = sum(label_ranks) / len(label_ranks)
        centered = [r - mean for r in label_ranks]
        norm = math.sqrt(sum(r * r for r in centered))
        if norm <= 0.0:
            continue
        for centered_factor, factor_norm in factor_ranks:
            if factor_norm <= 0.0:
                continue
            dot = 0.0
            for a, b in zip(centered_factor, centered):
                dot += a * b
            pool.append(dot / (factor_norm * norm))
    if len(pool) < 2:
        return {"null_sigma": None, "null_mean": None, "pool_size": len(pool), "permutations": permutations,
                "seed": seed, "block": max(1, int(overlap)), "degraded_reason": "零分布样本不足"}
    mean = sum(pool) / len(pool)
    sd = math.sqrt(sum((value - mean) ** 2 for value in pool) / len(pool))
    ordered = sorted(abs(value) for value in pool)
    index = min(len(ordered) - 1, max(0, int(math.ceil(0.95 * len(ordered))) - 1))
    return {
        "null_mean": mean,
        "null_sigma": sd,
        "empirical_critical_95": ordered[index],
        "abs_pool_ascending": ordered,
        "pool_size": len(pool),
        "calibration_candidates": len(factor_ranks),
        "permutations": permutations,
        "seed": seed,
        "block": max(1, int(overlap)),
    }


def p_value_from_null_pool(ic: float, abs_pool_ascending: Sequence[float]) -> float:
    """以经验零分布(|IC| 升序池)换算双侧 p 值:超过池中最多值者取最小可达 p。

    用经验分位而非正态近似:BH 工作在极尾部,而正态尾部过薄会把噪声判成显著。
    代价是分辨率 = 1/(池大小+1),随池大小收敛;该分辨率随校正报告一并披露。
    """
    size = len(abs_pool_ascending)
    if size == 0 or not math.isfinite(ic):
        return 1.0
    position = bisect.bisect_left(abs_pool_ascending, abs(ic))
    return (size - position + 1) / (size + 1)


def greedy_decorrelate(
    series_list: Sequence[Sequence[float]],
    *,
    threshold: float = 0.7,
    keep: int | None = None,
) -> dict[str, Any]:
    """按给定顺序贪心相关性去重:与已选代表 |Spearman| > threshold 者归入该簇。

    用途:枚举式挖掘的榜单前几名常是同一信号的变体（x / neg x / abs x / tanh x）,
    只看 |IC| 排序会让一簇信号占满榜单。按序列顺序（调用方保证已是排序键降序）贪心,
    每簇保留第一个 = 排序最优的那个。
    """
    kept: list[int] = []
    clusters: list[dict[str, Any]] = []
    if keep is not None and keep <= 0:
        return {"kept": [], "clusters": [], "threshold": threshold, "scanned": 0}
    scanned = 0
    for index, series in enumerate(series_list):
        if keep is not None and len(kept) >= keep:
            break
        scanned = index + 1
        for position, representative_index in enumerate(kept):
            correlation = abs(spearman(series_list[representative_index], series))
            if correlation > threshold:
                clusters[position]["merged"].append(index)
                clusters[position]["max_abs_correlation"] = max(
                    clusters[position]["max_abs_correlation"], correlation
                )
                break
        else:
            kept.append(index)
            clusters.append({"representative": index, "merged": [], "max_abs_correlation": 0.0})
    return {
        "kept": kept,
        "clusters": clusters,
        "threshold": threshold,
        "scanned": scanned,
    }


# --------------------------------------------------------------------------- #
# 因子评估套件 (QL05)
# --------------------------------------------------------------------------- #
def period_ic_series(
    values: Sequence[float], forward: Sequence[float | None], *, folds: int, min_samples: int = 20
) -> dict[str, Any]:
    """逐期 IC 序列:样本顺序切成 folds 个不重叠折,逐折算秩相关。

    时序口径的「逐期」= 顺序折;P2 横截面口径下同样的容器换成逐日截面 IC。
    """
    xs, ys = aligned_pairs(values, forward)
    n = len(xs)
    if folds < 2 or n < folds * 2:
        return {"folds": 0, "series": [], "mean": None, "std": None, "icir": None, "positive_ratio": None}
    span = n // folds
    if span < min_samples:
        return {"folds": 0, "series": [], "mean": None, "std": None, "icir": None, "positive_ratio": None,
                "degraded_reason": f"每折样本 {span} < {min_samples},逐期 IC 不可靠"}
    series: list[float] = []
    for f in range(folds):
        start = f * span
        end = n if f == folds - 1 else start + span
        series.append(spearman(xs[start:end], ys[start:end]))
    mean = sum(series) / len(series)
    var = sum((s - mean) ** 2 for s in series) / len(series)
    std = math.sqrt(var)
    positives = sum(1 for s in series if s > 0)
    return {
        "folds": len(series),
        "fold_samples": span,
        "series": series,
        "mean": mean,
        "std": std,
        "icir": (mean / std) if std > 0 else None,
        "positive_ratio": positives / len(series),
    }


def rank_autocorrelation(values: Sequence[float], *, lag: int = 1) -> float:
    """因子值自身的秩自相关(换手代理):lag=1 的秩相关;取值高 → 变化慢 → 换手低。"""
    span = max(1, int(lag))
    if len(values) <= span + 1:
        return 0.0
    return spearman(list(values[:-span]), list(values[span:]))


def quantile_group_returns(
    values: Sequence[float], forward: Sequence[float | None], *, groups: int = 5
) -> dict[str, Any]:
    """按因子值分位数分组的事件式平均未来收益(时序口径)。

    返回各组平均未来收益与 Q_top−Q_bottom 价差;分组按因子值排序后等分,确定性。
    """
    xs, ys = aligned_pairs(values, forward)
    n = len(xs)
    if groups < 2 or n < groups * 3:
        return {"groups": 0, "mean_forward_return": [], "spread_top_bottom": None}
    order = sorted(range(n), key=lambda i: xs[i])
    size = n // groups
    means: list[float] = []
    for g in range(groups):
        start = g * size
        end = n if g == groups - 1 else start + size
        bucket = [ys[i] for i in order[start:end]]
        means.append(sum(bucket) / len(bucket) if bucket else 0.0)
    return {
        "groups": groups,
        "group_samples": size,
        "mean_forward_return": means,
        "spread_top_bottom": means[-1] - means[0],
    }


def ic_decay(
    values: Sequence[float], forwards_by_horizon: dict[int, Sequence[float | None]]
) -> list[dict[str, Any]]:
    """IC 衰减曲线:同一因子在各前视窗口(1/5/10/20 日)下的 IC 与 t 值。

    每个窗口的 overlap 即该窗口天数(标签重叠长度),因此 t 值口径逐窗口自适应。
    """
    curve: list[dict[str, Any]] = []
    for days in sorted(forwards_by_horizon):
        stat = ic_statistics(values, forwards_by_horizon[days], overlap=int(days))
        curve.append({
            "days": int(days),
            "ic": round(stat["ic"], 4),
            "t_stat": round(stat["t_stat"], 4) if stat["t_stat"] is not None else None,
            "p_value": round(stat["p_value"], 6),
            "samples": stat["samples"],
        })
    return curve
