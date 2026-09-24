"""确定性线性模型(quant_models):分享池阶段 D 的本机限定形态。

许可与边界说明:权重为纯 JSON 线性模型(特征 → 收益的线性打分),由官方内核解释执行,
与阶段 A 参数集同一信任级——不含代码、无需推理沙箱;训练为闭式岭回归(高斯消元,stdlib),
同输入同结果,训练快照(dataset_version/样本/切分/lambda/特征序)完整记录可复算。
通用 NN 权重(需推理沙箱 + 训练集群)维持未开放,见 strategy-sharing-pool-plan 阶段 D。

- 打分:score = intercept + Σ w_i · feature_i(特征与 quant_factors 同一套,确定性);
- 仓位:沿用 tanh 约束 position = tanh(score),回放口径与参数集一致;
- 评价:训练/验证 70/30 切分,IC = 打分值与未来 forward_days 日收益的秩相关。
"""

from __future__ import annotations

import math
from typing import Any

from investment_steward_core import quant_factors

MODEL_FEATURES = ("ret_1", "ret_5", "vol_ratio", "ma_ratio", "range_ratio", "rsi_14")
MODEL_FORWARD_DAYS = 5
MODEL_MIN_BARS = 60
MODEL_WARMUP = 30


def design_matrix(bars: list[dict[str, Any]]) -> list[list[float]]:
    """逐 bar 特征行(截去 MODEL_WARMUP 根预热,特征展开窗口未稳掉的头部)。确定性。"""
    series = {name: quant_factors._feature_series(bars, name) for name in MODEL_FEATURES}
    return [
        [series[name][i] for name in MODEL_FEATURES]
        for i in range(MODEL_WARMUP, len(bars))
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
    """闭式岭回归,返回 [intercept, w_1..w_n](coefs 与 MODEL_FEATURES 对齐)。"""
    if len(rows) != len(targets) or len(rows) < MODEL_MIN_BARS - MODEL_WARMUP:
        raise ValueError(f"训练样本不足:{len(rows)} < {MODEL_MIN_BARS - MODEL_WARMUP}")
    n = len(MODEL_FEATURES)
    # 设计矩阵增广截距列;X^T X + λI 时截距项不正则(λ 只作用于斜率)。
    width = n + 1
    xtx = [[0.0] * width for _ in range(width)]
    xty = [0.0] * width
    for row, target in zip(rows, targets):
        augmented = [1.0, *row]
        for i in range(width):
            xty[i] += augmented[i] * target
            for j in range(width):
                xtx[i][j] += augmented[i] * augmented[j]
    for j in range(1, width):
        xtx[j][j] += lambda_
    return _solve_linear_system(xtx, xty)


def score_series(weights: list[float], bars: list[dict[str, Any]]) -> list[float]:
    """对每根 bar 求线性打分;weights = [intercept, *coefs]。确定性、无隐藏状态。"""
    series = {name: quant_factors._feature_series(bars, name) for name in MODEL_FEATURES}
    intercept = weights[0]
    coefs = weights[1:]
    out: list[float] = []
    for i in range(len(bars)):
        value = intercept + sum(coefs[j] * series[name][i] for j, name in enumerate(MODEL_FEATURES))
        out.append(value if math.isfinite(value) else 0.0)
    return out


def train(
    bars: list[dict[str, Any]],
    *,
    lambda_: float = 1.0,
    forward_days: int = MODEL_FORWARD_DAYS,
) -> dict[str, Any]:
    """训练线性模型:闭式岭回归 + 训练/验证 IC。同输入同结果(确定性)。"""
    if lambda_ < 0:
        raise ValueError("lambda 不能为负")
    if len(bars) < MODEL_MIN_BARS:
        raise ValueError(f"样本不足:{len(bars)} < {MODEL_MIN_BARS} 根 K 线")
    closes = [float(bar["close"]) for bar in bars]
    forward = quant_factors.forward_returns(closes, forward_days)
    rows = design_matrix(bars)
    targets = forward[MODEL_WARMUP:]
    split = int(len(rows) * 0.7)
    weights = ridge_fit(rows[:split], targets[:split], lambda_)
    scores = score_series(weights, bars)
    aligned_rows = MODEL_WARMUP
    train_ic = quant_factors.rank_ic(
        scores[aligned_rows : aligned_rows + split],
        forward[aligned_rows : aligned_rows + split],
    )
    valid_ic = quant_factors.rank_ic(scores[aligned_rows + split :], forward[aligned_rows + split :])
    weights = [round(value, 8) for value in weights]
    return {
        "weights": weights,
        "feature_order": list(MODEL_FEATURES),
        "metrics": {
            "train_ic": round(train_ic, 4),
            "valid_ic": round(valid_ic, 4),
            "samples": len(bars),
        },
        "training": {
            "lambda": lambda_,
            "forward_days": forward_days,
            "split_index": split,
            "train_rows": split,
            "valid_rows": len(rows) - split,
            "warmup": MODEL_WARMUP,
            "as_of": str(bars[-1]["timestamp"]) if bars else None,
        },
        "note": "确定性线性模型:闭式岭回归 + 验证集 IC;打分由官方内核解释执行,仅作研究背景,不构成买卖建议",
    }
