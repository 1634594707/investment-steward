"""确定性因子挖掘(quant_factors):token 序列公式 + 栈式求值 + 枚举搜索。

许可说明:AlphaMaster(AGPL-3.0)仅作架构思想参考(公式化因子表达、position = tanh 约束),
本模块为独立原创实现(stdlib),未移植其任何代码。对齐分享池阶段 A:公式/参数集为纯 JSON,可复算。

- 特征(feature):对 OHLCV 序列逐 bar 可计算的原子值;
- 公式:逆波兰 token 序列(如 ["ret_5","1","sub","tanh"]),JSON 可序列化;
- 求值:栈式逐 bar 计算因子值序列;除零/溢出安全(返回 0.0);
- 挖掘:枚举深度受限的公式空间,以验证集 IC(因子值与未来 5 日收益秩相关)排序。
"""

from __future__ import annotations

import math
from itertools import product
from typing import Any

FEATURE_NAMES = ("ret_1", "ret_5", "vol_ratio", "ma_ratio", "range_ratio", "rsi_14")
BINARY_OPS = ("add", "sub", "mul", "div")
UNARY_OPS = ("abs", "tanh", "neg")
MAX_DEPTH = 4
FORWARD_DAYS = 5
MIN_VALID_SAMPLES = 30


def _rma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    avg: float | None = None
    for i, value in enumerate(values):
        if i < period:
            window = values[: i + 1]
            avg = sum(window) / len(window)
        else:
            avg = ((avg or 0.0) * (period - 1) + value) / period
        out.append(avg if i + 1 >= period else None)
    return out


def _feature_series(bars: list[dict[str, Any]], name: str) -> list[float]:
    closes = [float(bar["close"]) for bar in bars]
    volumes = [float(bar.get("volume") or 0.0) for bar in bars]
    highs = [float(bar["high"]) for bar in bars]
    lows = [float(bar["low"]) for bar in bars]

    def safe_div(a: float, b: float) -> float:
        return a / b if b else 0.0

    if name == "ret_1":
        return [safe_div(closes[i] - closes[i - 1], closes[i - 1]) if i else 0.0 for i in range(len(closes))]
    if name == "ret_5":
        return [safe_div(closes[i] - closes[i - 5], closes[i - 5]) if i >= 5 else 0.0 for i in range(len(closes))]
    if name == "vol_ratio":
        avg20 = [sum(volumes[max(0, i - 20) : i]) / max(1, min(20, i)) if i else 0.0 for i in range(len(volumes))]
        return [safe_div(volumes[i], avg20[i]) if avg20[i] else 0.0 for i in range(len(volumes))]
    if name == "ma_ratio":
        ma20 = [sum(closes[max(0, i - 19) : i + 1]) / min(20, i + 1) for i in range(len(closes))]
        return [safe_div(closes[i], ma20[i]) if ma20[i] else 0.0 for i in range(len(closes))]
    if name == "range_ratio":
        ranges = [max(highs[i], closes[i]) - min(lows[i], closes[i]) if i else highs[0] - lows[0] for i in range(len(closes))]
        atr14 = _rma(ranges, 14)
        return [safe_div(ranges[i], atr14[i]) if atr14[i] else 0.0 for i in range(len(closes))]
    if name == "rsi_14":
        gains, losses = [], []
        for i in range(len(closes)):
            if i == 0:
                gains.append(0.0)
                losses.append(0.0)
                continue
            change = closes[i] - closes[i - 1]
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        out = []
        for i in range(len(closes)):
            window_g = gains[max(0, i - 13) : i + 1]
            window_l = losses[max(0, i - 13) : i + 1]
            avg_g = sum(window_g) / len(window_g)
            avg_l = sum(window_l) / len(window_l)
            out.append(100.0 - 100.0 / (1.0 + avg_g / avg_l) if avg_l else 100.0)
        return out
    raise ValueError(f"未知特征:{name}")


def _apply_op(op: str, stack: list[float]) -> None:
    if op in BINARY_OPS:
        if len(stack) < 2:
            raise ValueError(f"栈下溢:{op}")
        b = stack.pop()
        a = stack.pop()
        if op == "add":
            stack.append(a + b)
        elif op == "sub":
            stack.append(a - b)
        elif op == "mul":
            stack.append(a * b)
        else:
            stack.append(a / b if abs(b) > 1e-12 else 0.0)
    elif op in UNARY_OPS:
        if not stack:
            raise ValueError(f"栈下溢:{op}")
        a = stack.pop()
        if op == "abs":
            stack.append(abs(a))
        elif op == "tanh":
            stack.append(math.tanh(a))
        else:
            stack.append(-a)
    else:
        raise ValueError(f"未知 token:{op}")


def evaluate_tokens(tokens: list[str], bars: list[dict[str, Any]]) -> list[float]:
    """对每根 bar 求因子值序列;公式非法抛 ValueError。"""
    feature_cache: dict[str, list[float]] = {}
    for token in tokens:
        if token not in FEATURE_NAMES and token not in BINARY_OPS and token not in UNARY_OPS:
            try:
                float(token)
            except ValueError as error:
                raise ValueError(f"未知 token:{token}") from error
    for token in tokens:
        if token in FEATURE_NAMES and token not in feature_cache:
            feature_cache[token] = _feature_series(bars, token)
    out: list[float] = []
    for index in range(len(bars)):
        stack: list[float] = []
        for token in tokens:
            if token in feature_cache:
                stack.append(feature_cache[token][index])
            elif token in BINARY_OPS or token in UNARY_OPS:
                _apply_op(token, stack)
            else:
                stack.append(float(token))
        if len(stack) != 1:
            raise ValueError(f"公式栈残留 {len(stack)} 项")
        value = stack[0]
        out.append(value if math.isfinite(value) else 0.0)
    return out


def _valid_formulas(depth: int = MAX_DEPTH):
    """按确定顺序枚举可求值的 RPN 公式(长度 1..depth+1)。"""
    alphabet = (*FEATURE_NAMES, *BINARY_OPS, *UNARY_OPS)
    for length in range(1, depth + 2):
        for combo in product(alphabet, repeat=length):
            tokens = list(combo)
            try:
                evaluate_tokens(tokens, [])
            except ValueError:
                continue
            yield tokens


def rank_ic(factor_values: list[float], forward_returns: list[float]) -> float:
    """秩相关(对秩做 Pearson);样本不足或零方差返回 0.0。"""

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for rank, index in enumerate(order):
            out[index] = float(rank)
        return out

    if len(factor_values) != len(forward_returns) or len(factor_values) < MIN_VALID_SAMPLES:
        return 0.0
    ra, rb = ranks(factor_values), ranks(forward_returns)
    mean_a = sum(ra) / len(ra)
    mean_b = sum(rb) / len(rb)
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(ra, rb))
    var_a = sum((a - mean_a) ** 2 for a in ra)
    var_b = sum((b - mean_b) ** 2 for b in rb)
    if var_a <= 0 or var_b <= 0:
        return 0.0
    return cov / math.sqrt(var_a * var_b)


def forward_returns(closes: list[float], days: int = FORWARD_DAYS) -> list[float]:
    out: list[float] = []
    for i in range(len(closes)):
        j = i + days
        out.append((closes[j] - closes[i]) / closes[i] if j < len(closes) and closes[i] else 0.0)
    return out


def mine(bars: list[dict[str, Any]], *, depth: int = 3, top_n: int = 5) -> dict[str, Any]:
    """枚举公式空间,按验证集 IC 排序输出 top-N 参数集(确定性:同输入同结果)。"""
    closes = [float(bar["close"]) for bar in bars]
    forward = forward_returns(closes)
    split = int(len(bars) * 0.7)
    results: list[dict[str, Any]] = []
    for tokens in _valid_formulas(depth):
        try:
            values = evaluate_tokens(tokens, bars)
        except ValueError:
            continue
        train_ic = rank_ic(values[:split], forward[:split])
        if abs(train_ic) < 0.05:
            continue
        valid_ic = rank_ic(values[split:], forward[split:])
        if abs(valid_ic) < 0.02:
            continue
        results.append({
            "formula_tokens": tokens,
            "formula": " ".join(tokens),
            "train_ic": round(train_ic, 4),
            "valid_ic": round(valid_ic, 4),
            "samples": len(bars),
        })
    results.sort(key=lambda item: (-abs(item["valid_ic"]), len(item["formula_tokens"]), item["formula"]))
    return {
        "top": results[:top_n],
        "searched": True,
        "forward_days": FORWARD_DAYS,
        "note": "确定性枚举挖掘,同输入同结果;IC 为秩相关,仅作研究背景,不构成买卖建议",
    }
