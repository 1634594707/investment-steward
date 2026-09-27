"""确定性线性模型(quant_models):分享池阶段 D 的本机限定形态。

许可与边界说明:权重为纯 JSON 线性模型(特征 → 收益的线性打分),由官方内核解释执行,
与阶段 A 参数集同一信任级——不含代码、无需推理沙箱;训练为闭式岭回归(高斯消元,stdlib),
同输入同结果,训练快照(dataset_version/样本/切分/lambda/特征序/标准化统计量)完整记录可复算。
通用 NN 权重(需推理沙箱 + 训练集群)维持未开放,见 strategy-sharing-pool-plan 阶段 D。

- 打分:score = intercept + Σ w_i · z_i,z_i = (feature_i − mean_i) / std_i(确定性);
- **标准化(QL07)**:特征的量纲差 3 个数量级(ret_1 ~1e-2 vs rsi_14 ~1e1),
  不做标准化时 λ 的相对惩罚只落在小量纲特征上,岭回归退化为「几乎只拟合 RSI」。
  标准化统计量**只在 train 段估计并冻结**,valid/test 段复用(否则即为泄漏)。
- **λ 选择(QL07)**:λ 网格 × walk-forward(train∪valid 前缀的分折样本外)选择,
  选择过程与逐 λ 结果全部落训练快照,可复算;显式给 λ 时跳过选择。
- 仓位:沿用 tanh 约束 position = tanh(score),回放口径与参数集一致;
- 评价:三段式切分(train 选模 / valid 选 λ / test 只确认一次),IC = 打分值与未来收益秩相关。

向后兼容:旧模型权重的 ``standardization`` 缺失时按恒等变换打分（旧制品逐位可复现）。
"""

from __future__ import annotations

import math
from typing import Any

from investment_steward_core import quant_factors

MODEL_FEATURES = ("ret_1", "ret_5", "vol_ratio", "ma_ratio", "range_ratio", "rsi_14")
MODEL_FORWARD_DAYS = 5
MODEL_MIN_BARS = 60
MODEL_WARMUP = 30
MODEL_SPEC_VERSION = 2
MODEL_MIN_TRAIN_ROWS = 20
MODEL_MIN_SEGMENT = 15
SELECTION_FOLDS = 3
DEFAULT_LAMBDA_GRID = (0.0, 0.01, 0.1, 1.0, 10.0, 100.0)


def design_matrix(bars: list[dict[str, Any]]) -> list[list[float]]:
    """逐 bar 特征行(截去 MODEL_WARMUP 根预热,特征展开窗口未稳掉的头部)。确定性。"""
    series = {name: quant_factors._feature_series(bars, name) for name in MODEL_FEATURES}
    return [
        [series[name][i] for name in MODEL_FEATURES]
        for i in range(MODEL_WARMUP, len(bars))
    ]


def standardize_fit(rows: list[list[float]]) -> dict[str, list[float]]:
    """在给定行上估计标准化统计量(总体均值/标准差);零方差列 std 置 1.0 以免除零。"""
    if not rows:
        raise ValueError("标准化统计量需要至少 1 行样本")
    width = len(rows[0])
    count = len(rows)
    means = [sum(row[j] for row in rows) / count for j in range(width)]
    stds: list[float] = []
    for j in range(width):
        var = sum((row[j] - means[j]) ** 2 for row in rows) / count
        std = math.sqrt(var)
        stds.append(std if std > 1e-12 else 1.0)
    return {"mean": means, "std": stds}


def standardize_apply(rows: list[list[float]], stats: dict[str, list[float]] | None) -> list[list[float]]:
    """按冻结统计量做 z-score;stats 为 None 时原样返回（旧制品兼容）。"""
    if not stats:
        return rows
    means = stats["mean"]
    stds = stats["std"]
    return [
        [(row[j] - means[j]) / stds[j] for j in range(len(row))]
        for row in rows
    ]


def _solve_linear_system(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """高斯消元(部分主元)解 (X^T X + λI) w = X^T y。确定性,stdlib。"""
    n = len(rhs)
    augmented = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            raise ValueError("设计矩阵奇异(特征零方差或样本不足),拒绝训练")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        pivot_value = augmented[col][col]
        for r in range(n + 1):
            augmented[col][r] /= pivot_value
        for r in range(n):
            if r == col:
                continue
            factor = augmented[r][col]
            if factor:
                for c in range(col, n + 1):
                    augmented[r][c] -= factor * augmented[col][c]
    return [augmented[i][n] for i in range(n)]


def ridge_fit(rows: list[list[float]], targets: list[float], lambda_: float) -> list[float]:
    """闭式岭回归,返回 [intercept, w_1..w_n](coefs 与 MODEL_FEATURES 对齐)。

    训练段方差为 0 的特征（常数列,如合成数据里恒为 1 的 ``range_ratio``）标准化后整列为 0,
    与截距列共线、系数**不可识别**:这类列先整体剔除再解（对应系数记 0.0）,
    否则 λ=0 时 X^T X 出现全零行列必奇异。剔除后仍奇异才报错。
    """
    if len(rows) != len(targets) or len(rows) < MODEL_MIN_TRAIN_ROWS:
        raise ValueError(f"训练样本不足:{len(rows)} < {MODEL_MIN_TRAIN_ROWS}")
    width = len(rows[0]) if rows else 0
    columns = list(zip(*rows))
    active = [j for j, column in enumerate(columns) if len(set(column)) > 1]
    # 设计矩阵增广截距列;X^T X + λI 时截距项不正则(λ 只作用于斜率)。
    matrix_width = len(active) + 1
    xtx = [[0.0] * matrix_width for _ in range(matrix_width)]
    xty = [0.0] * matrix_width
    for row, target in zip(rows, targets):
        augmented = [1.0, *[row[j] for j in active]]
        for i in range(matrix_width):
            xty[i] += augmented[i] * target
            for j in range(matrix_width):
                xtx[i][j] += augmented[i] * augmented[j]
    for j in range(1, matrix_width):
        xtx[j][j] += lambda_
    solved = _solve_linear_system(xtx, xty)
    weights = [0.0] * (width + 1)
    weights[0] = solved[0]
    for position, column in enumerate(active):
        weights[column + 1] = solved[position + 1]
    return weights


def score_series(
    weights: list[float],
    bars: list[dict[str, Any]],
    *,
    standardization: dict[str, list[float]] | None = None,
) -> list[float]:
    """对每根 bar 求线性打分;weights = [intercept, *coefs]。确定性、无隐藏状态。

    standardization 给出时先按冻结统计量做 z-score（新模型）;为 None 时按原始量纲打分,
    与旧制品口径逐位一致。
    """
    series = {name: quant_factors._feature_series(bars, name) for name in MODEL_FEATURES}
    intercept = weights[0]
    coefs = weights[1:]
    means = standardization["mean"] if standardization else None
    stds = standardization["std"] if standardization else None
    out: list[float] = []
    for i in range(len(bars)):
        value = intercept
        for j, name in enumerate(MODEL_FEATURES):
            raw = series[name][i]
            if means is not None and stds is not None:
                raw = (raw - means[j]) / stds[j]
            value += coefs[j] * raw
        out.append(value if math.isfinite(value) else 0.0)
    return out


def _select_lambda(
    bars: list[dict[str, Any]],
    closes: list[float],
    train_rows_z: list[list[float]],
    train_targets: list[float],
    *,
    prefix_end: int,
    lambda_grid: tuple[float, ...],
    standardization: dict[str, list[float]] | None,
) -> dict[str, Any]:
    """λ 网格 × walk-forward 选择:在 train∪valid 前缀上做分折样本外,取中位超额最优者。

    Q1（用户视角路线图 2026-09-26）：`standardization` **必须**传入，且必须是训练集
    `standardize_fit(train_rows)` 算出的那一份。理由是数学上的，不是风格问题：

    - `ridge_fit` 的入参 `train_rows_z` 已经是标准化后的特征，得到的 `weights` 因此
      处在 z 空间的系数域；
    - 本模块规格（`score = intercept + Σ w_i · z_i`）也要求用 z 打分；
    - 而出厂模型的 `score_series(weights, bars, standardization=stats)` 就是这么做的。

    改造前这里漏传，打分用的是**原始特征**：把 z 空间的系数套到量纲各异的原始值上，
    6 个特征里凡量纲大的（成交量、price）会压过量纲小的。标准化是仿射变换，
    它虽然不改变单个特征的线性单调性，但**会改变各候选 λ 之间的相对排序**，
    于是 walk-forward 选出的是一个用户拿不到、也无法复算的 λ——读
    `training.lambda_selection` 复算必然得到不同结果。
    """
    from investment_steward_core import quant_experiments

    reports: list[dict[str, Any]] = []
    for candidate in lambda_grid:
        try:
            weights = ridge_fit(train_rows_z, train_targets, candidate)
            values = score_series(
                weights, bars[:prefix_end], standardization=standardization
            )
            wf = quant_experiments.walk_forward(values, closes[:prefix_end], folds=SELECTION_FOLDS)
        except ValueError as error:
            reports.append({"lambda": candidate, "available": False, "degraded_reason": str(error)})
            continue
        reports.append({
            "lambda": candidate,
            "available": True,
            "median_equity": wf["median_equity"],
            "median_excess_vs_baseline": wf["median_excess_vs_baseline"],
            "oos_consistency": wf["oos_consistency"],
            "positive_excess_folds": wf["positive_excess_folds"],
        })
    usable = [item for item in reports if item.get("available")]
    if not usable:
        fallback = lambda_grid[len(lambda_grid) // 2]
        return {"method": "walk_forward_grid", "grid": list(lambda_grid), "selected": fallback,
                "reports": reports, "criterion": "全部 λ 均无法折分,回退中位值",
                "folds": SELECTION_FOLDS, "prefix_bars": prefix_end}
    best = sorted(
        usable,
        key=lambda item: (
            -item["median_excess_vs_baseline"],
            -item["oos_consistency"],
            item["lambda"],
        ),
    )[0]
    return {
        "method": "walk_forward_grid",
        "criterion": "中位超额收益 → 折间一致性 → 较小 λ（确定性 tie-break）",
        "grid": list(lambda_grid),
        "selected": best["lambda"],
        "reports": reports,
        "folds": SELECTION_FOLDS,
        "prefix_bars": prefix_end,
    }


def train(
    bars: list[dict[str, Any]],
    *,
    lambda_: float | None = None,
    forward_days: int = MODEL_FORWARD_DAYS,
    lambda_grid: tuple[float, ...] = DEFAULT_LAMBDA_GRID,
) -> dict[str, Any]:
    """训练线性模型:特征标准化 + (λ 网格 walk-forward 选择) + 闭式岭回归。同输入同结果。

    lambda_ 显式给出时跳过选择（快照 lambda 即该值）;为 None 时按 lambda_grid 选择。
    """
    if lambda_ is not None and lambda_ < 0:
        raise ValueError("lambda 不能为负")
    if len(bars) < MODEL_MIN_BARS:
        raise ValueError(f"样本不足:{len(bars)} < {MODEL_MIN_BARS} 根 K 线")
    closes = [float(bar["close"]) for bar in bars]
    forward = quant_factors.forward_returns(closes, forward_days)
    rows = design_matrix(bars)
    # 只保留有有效标签的行（尾部 forward 窗口越界者剔除，不再当 0.0 平盘）
    usable = len(rows) - forward_days
    if usable < MODEL_MIN_TRAIN_ROWS * 3:
        raise ValueError(f"有效样本不足:{usable} 行（每段至少 {MODEL_MIN_TRAIN_ROWS} 行）")
    aligned_rows = rows[:usable]
    # design_matrix 截去了 MODEL_WARMUP 根预热,故行 i 对应 bar (MODEL_WARMUP + i) 的特征与标签
    aligned_targets = [float(value) for value in forward[MODEL_WARMUP : MODEL_WARMUP + usable]]
    split = quant_factors.three_way_split(usable, 0, min_segment=MODEL_MIN_SEGMENT)
    train_end = int(split["train_end"])
    valid_end = int(split["valid_end"])
    train_rows = aligned_rows[:train_end]
    train_targets = aligned_targets[:train_end]
    stats = standardize_fit(train_rows)
    train_rows_z = standardize_apply(train_rows, stats)

    if lambda_ is None:
        prefix_end = min(len(bars), MODEL_WARMUP + valid_end + 1)
        selection = _select_lambda(
            bars, closes, train_rows_z, train_targets,
            prefix_end=prefix_end, lambda_grid=tuple(lambda_grid),
            # Q1：与出厂打分（下方 score_series(..., standardization=stats)）同一口径。
            standardization=stats,
        )
        chosen = float(selection["selected"])
    else:
        selection = {"method": "fixed", "selected": float(lambda_), "grid": None,
                     "criterion": "调用方显式指定 λ,未做选择"}
        chosen = float(lambda_)

    weights = ridge_fit(train_rows_z, train_targets, chosen)
    scores = score_series(weights, bars, standardization=stats)
    aligned_scores = scores[MODEL_WARMUP : MODEL_WARMUP + usable]

    def segment_ic(start: int, end: int) -> dict[str, Any]:
        return quant_factors.ic_statistics(
            aligned_scores[start:end], aligned_targets[start:end], overlap=forward_days
        )

    train_stat = segment_ic(0, train_end)
    valid_stat = segment_ic(train_end, valid_end)
    test_stat = segment_ic(valid_end, usable)
    rounded_weights = [round(value, 8) for value in weights]
    evaluation = quant_factors._rounded_suite(
        quant_factors.factor_evaluation(scores, closes, forward_days=forward_days)
    )
    return {
        "model_spec_version": MODEL_SPEC_VERSION,
        "weights": rounded_weights,
        "feature_order": list(MODEL_FEATURES),
        "standardization": {"mean": [round(v, 10) for v in stats["mean"]],
                            "std": [round(v, 10) for v in stats["std"]]},
        "metrics": {
            "train_ic": round(train_stat["ic"], 4),
            "valid_ic": round(valid_stat["ic"], 4),
            "test_ic": round(test_stat["ic"], 4),
            "test_t_stat": round(test_stat["t_stat"], 4) if test_stat["t_stat"] is not None else None,
            "test_p_value": round(test_stat["p_value"], 8),
            "samples": len(bars),
            "segment_samples": {"train": train_stat["samples"], "valid": valid_stat["samples"],
                                "test": test_stat["samples"]},
            "ic_suite": evaluation,
        },
        "training": {
            "lambda": chosen,
            "lambda_selection": selection,
            "standardized": True,
            "forward_days": forward_days,
            "split": split,
            "train_rows": train_end,
            "valid_rows": valid_end - train_end,
            "test_rows": usable - valid_end,
            "warmup": MODEL_WARMUP,
            "rows_with_valid_label": usable,
            "as_of": str(bars[-1]["timestamp"]) if bars else None,
        },
        "note": "确定性线性模型:特征标准化(训练段统计量冻结) + λ 网格 walk-forward 选择 + 闭式岭回归;"
                "三段式切分 train/valid/test;打分由官方内核解释执行,仅作研究背景,不构成买卖建议",
    }
