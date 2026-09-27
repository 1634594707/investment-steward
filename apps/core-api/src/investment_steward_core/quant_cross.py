"""横截面因子研究（QL11）:逐日 cross-sectional rank IC、IC 序列/ICIR、cs_rank/cs_zscore、分组中性化。

与 ``quant_factors``（单标的时序口径）的关系:
- 复用同一套 token 语法与公式空间（``quant_factors.FEATURE_NAMES`` / ``_expand_candidates`` /
  ``_ts_series`` / ``_feature_series``）,只是求值对象从「一条序列」换成「日期 × 标的矩阵」;
- 新增两个**横截面算子**:``cs_rank`` / ``cs_zscore``（逐日在截面内取秩 / 标准化,时序上不可分解）;
- 统计口径:逐日 rank IC 序列 → 均值 / 标准差 / ICIR / >0 占比 / t 检验。重叠标签（forward_days > 1）下
  逐日 IC 自相关,直接用 T 个观测会高估显著性 → **按 forward_days 等距抽样**成近似独立样本再检验,
  这与单标的侧「重叠折算有效样本」是同一件事的两种等价做法。

诚实红线:截面样本不足的交易日**跳过并计数**,不用 0 填充;中性化取不到行业/市值就如实标注缺口,
不假装做过中性化。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from typing import Any, Callable

from investment_steward_core import quant_factors, quant_panel, quant_stats

# —— 口径版本（改动 IC 定义/抽样规则/中性化口径必须递增）——
CS_SPEC_VERSION = 1
CS_RANK_OPS = ("cs_rank", "cs_zscore")
MIN_SYMBOLS_PER_DATE = 8       # 单日截面至少 8 只才有意义（取秩相关的最小样本）
MIN_DATES_FOR_IC = 20          # IC 序列至少 20 个有效交易日
NEUTRALISE_MODES = ("none", "demean", "rank")
DEFAULT_NEUTRALISE = "none"


def _canonical_matrix(matrix: dict[str, list[float | None]], symbols: list[str]) -> dict[str, Any]:
    return {"symbols": symbols, "values": {symbol: matrix.get(symbol, []) for symbol in symbols}}


# --------------------------------------------------------------------------- #
# 横截面算子
# --------------------------------------------------------------------------- #
def cs_rank(values: list[float]) -> list[float]:
    """截面百分位秩（并列取平均秩,映射到 [−1,1]）。

    **非有限值（NaN/Inf）不参与取秩,原样返回 0.0** —— 不能让它被排成最小值:那会把「缺数据」
    伪装成「截面最低」,是最隐蔽的一类因子污染。有效样本 < 2 时全返回 0.0。
    """
    out = [0.0] * len(values)
    usable = [index for index, value in enumerate(values) if math.isfinite(value)]
    if len(usable) < 2:
        return out
    ranks = quant_stats.average_ranks([values[index] for index in usable])
    span = len(usable) - 1
    for position, index in enumerate(usable):
        out[index] = 2.0 * (ranks[position] / span) - 1.0
    return out


def cs_zscore(values: list[float]) -> list[float]:
    """截面 z-score（总体标准差;非有限值不参与统计并原样返回 0.0,有效样本 <2 或零方差记 0.0）。"""
    out = [0.0] * len(values)
    usable = [value for value in values if math.isfinite(value)]
    if len(usable) < 2:
        return out
    mean = sum(usable) / len(usable)
    variance = sum((value - mean) ** 2 for value in usable) / len(usable)
    if variance <= 0.0:
        return out
    std = math.sqrt(variance)
    return [
        (value - mean) / std if math.isfinite(value) else 0.0
        for value in values
    ]


def _cross_sectional(
    matrix: dict[str, list[float]], symbols: list[str], date_count: int, op: str
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {symbol: [0.0] * date_count for symbol in symbols}
    for index in range(date_count):
        column = [matrix[symbol][index] for symbol in symbols]
        values = cs_rank(column) if op == "cs_rank" else cs_zscore(column)
        for position, symbol in enumerate(symbols):
            out[symbol][index] = values[position]
    return out


# --------------------------------------------------------------------------- #
# 矩阵求值（token 语法与单标的侧完全一致）
# --------------------------------------------------------------------------- #
def feature_matrix(panel: dict[str, Any], names: list[str] | None = None) -> dict[str, dict[str, list[float]]]:
    """按面板逐标的构造原子特征矩阵:``{feature_name: {symbol: [值 per date]}}``。

    逐标的只还原一次 bar 列表（``_panel_bars``）,不缓存跨调用状态——求值路径必须无隐藏状态。
    """
    symbols = list(panel.get("symbols") or [])
    bars_cache = {symbol: _panel_bars(panel, symbol) for symbol in symbols}
    matrix: dict[str, dict[str, list[float]]] = {}
    for name in names or quant_factors.FEATURE_NAMES:
        matrix[name] = {
            symbol: quant_factors._feature_series(bars_cache[symbol], name) for symbol in symbols
        }
    return matrix


def _panel_bars(panel: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    """把列式面板还原成 bar 列表（``quant_panel.bars_of`` 的薄包装）。"""
    from investment_steward_core import quant_panel

    return quant_panel.bars_of(panel, symbol)


def evaluate_matrix(
    tokens: list[str],
    features: dict[str, dict[str, list[float]]],
    symbols: list[str],
    date_count: int,
) -> dict[str, list[float]]:
    """在矩阵上求值一条公式:横截面算子逐日作用,时序算子逐标的沿日期作用。"""
    stack: list[dict[str, list[float]]] = []
    for token in tokens:
        series = features.get(token)
        if series is not None:
            stack.append(series)
            continue
        if token in CS_RANK_OPS:
            if not stack:
                raise ValueError(f"栈下溢:{token}")
            stack.append(_cross_sectional(stack.pop(), symbols, date_count, token))
            continue
        parsed = quant_factors.parse_ts_token(token)
        if parsed is not None:
            if not stack:
                raise ValueError(f"栈下溢:{token}")
            source = stack.pop()
            base, window = parsed
            stack.append({
                symbol: quant_factors._ts_series(base, window, source[symbol], date_count) for symbol in symbols
            })
            continue
        if token in quant_factors.BINARY_OPS:
            if len(stack) < 2:
                raise ValueError(f"栈下溢:{token}")
            right = stack.pop()
            left = stack.pop()
            out: dict[str, list[float]] = {}
            for symbol in symbols:
                a = left[symbol]
                b = right[symbol]
                if token == "add":
                    out[symbol] = [x + y for x, y in zip(a, b)]
                elif token == "sub":
                    out[symbol] = [x - y for x, y in zip(a, b)]
                elif token == "mul":
                    out[symbol] = [x * y for x, y in zip(a, b)]
                else:
                    out[symbol] = [(x / y if abs(y) > 1e-12 else 0.0) for x, y in zip(a, b)]
            stack.append(out)
            continue
        if token in quant_factors.UNARY_OPS:
            if not stack:
                raise ValueError(f"栈下溢:{token}")
            source = stack.pop()
            out = {}
            for symbol in symbols:
                values = source[symbol]
                if token == "abs":
                    out[symbol] = [abs(v) for v in values]
                elif token == "tanh":
                    out[symbol] = [math.tanh(v) for v in values]
                else:
                    out[symbol] = [-v for v in values]
            stack.append(out)
            continue
        try:
            constant = float(token)
        except ValueError as error:
            raise ValueError(f"未知 token:{token}") from error
        stack.append({symbol: [constant] * date_count for symbol in symbols})
    if len(stack) != 1:
        raise ValueError(f"公式栈残留 {len(stack)} 项")
    result = stack[0]
    return {
        symbol: [(value if math.isfinite(value) else 0.0) for value in result[symbol]] for symbol in symbols
    }


def validate_tokens(tokens: list[str]) -> None:
    """公式合法性预检（含横截面算子）。"""
    for token in tokens:
        if (
            token in quant_factors.FEATURE_NAMES
            or token in quant_factors.BINARY_OPS
            or token in quant_factors.UNARY_OPS
            or token in CS_RANK_OPS
            or quant_factors.parse_ts_token(token) is not None
        ):
            continue
        try:
            float(token)
        except ValueError as error:
            raise ValueError(f"未知 token:{token}") from error


# --------------------------------------------------------------------------- #
# 标签矩阵与逐日横截面 IC
# --------------------------------------------------------------------------- #
def forward_return_matrix(panel: dict[str, Any], days: int = quant_factors.FORWARD_DAYS) -> dict[str, list[float | None]]:
    """逐标的未来 N 日收益（尾部越界记 None,与单标的侧口径一致）。"""
    out: dict[str, list[float | None]] = {}
    for symbol in panel.get("symbols") or []:
        closes = (panel.get("columns") or {}).get(symbol, {}).get("close")
        if not closes:
            out[symbol] = []
            continue
        out[symbol] = quant_factors.forward_returns([float(v) for v in closes], days)
    return out


def daily_rank_ic(
    factor: dict[str, list[float]],
    forward: dict[str, list[float | None]],
    *,
    symbols: list[str],
    dates: list[str],
    min_symbols: int = MIN_SYMBOLS_PER_DATE,
) -> dict[str, Any]:
    """逐日横截面 Spearman（并列取平均秩）;截面样本不足的交易日跳过并计入 ``skipped``。"""
    series: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, day in enumerate(dates):
        xs: list[float] = []
        ys: list[float] = []
        for symbol in symbols:
            values = factor.get(symbol)
            labels = forward.get(symbol)
            if values is None or labels is None or index >= len(values) or index >= len(labels):
                continue
            label = labels[index]
            if label is None:
                continue
            xs.append(float(values[index]))
            ys.append(float(label))
        if len(xs) < min_symbols:
            skipped.append({"date": day, "samples": len(xs), "reason": f"截面样本 {len(xs)} < {min_symbols}"})
            continue
        ic = quant_stats.spearman(xs, ys)
        series.append({"date": day, "ic": round(ic, 6), "samples": len(xs)})
    return {"series": series, "skipped": skipped, "skipped_total": len(skipped)}


def ic_series_summary(series: list[dict[str, Any]], *, overlap: int = 1) -> dict[str, Any]:
    """IC 序列统计:均值 / 标准差 / ICIR / >0 占比 / 抽样后的 t 与 p。

    重叠标签下逐日 IC 自相关 → 按 ``overlap`` 等距抽样,用近似独立样本做 t 检验（不为显著性注水）。
    """
    values = [float(point["ic"]) for point in series]
    count = len(values)
    if count < MIN_DATES_FOR_IC:
        return {
            "samples": count,
            "mean": None,
            "std": None,
            "icir": None,
            "positive_ratio": None,
            "t_stat": None,
            "p_value": None,
            "effective_samples": count,
            "overlap": overlap,
            "degraded_reason": f"有效交易日仅 {count} 个（至少 {MIN_DATES_FOR_IC}）——不足即不报显著性",
        }
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / (count - 1) if count > 1 else 0.0
    std = math.sqrt(variance) if variance > 0.0 else 0.0
    sampled = values[:: max(1, int(overlap))]
    effective = len(sampled)
    t_stat = None
    p_value = None
    if effective >= 2 and std > 0.0:
        pooled = sum(sampled) / effective
        pooled_variance = sum((value - pooled) ** 2 for value in sampled) / (effective - 1)
        pooled_std = math.sqrt(pooled_variance) if pooled_variance > 0.0 else 0.0
        if pooled_std > 0.0:
            t_stat = pooled / (pooled_std / math.sqrt(effective))
            p_value = quant_stats.student_t_two_sided_p(t_stat, effective - 1)
    return {
        "samples": count,
        "mean": round(mean, 6),
        "std": round(std, 6),
        "icir": round(mean / std, 6) if std > 0.0 else None,
        "positive_ratio": round(sum(1 for value in values if value > 0) / count, 6),
        "t_stat": round(t_stat, 6) if t_stat is not None else None,
        "p_value": round(p_value, 8) if p_value is not None else 1.0,
        "effective_samples": effective,
        "overlap": overlap,
        "degraded_reason": None if t_stat is not None else "抽样后独立样本不足,未报 t 值",
    }


# --------------------------------------------------------------------------- #
# 中性化（行业 / 市值分组内）
# --------------------------------------------------------------------------- #
def neutralise(
    values: list[float],
    groups: list[str | None],
    *,
    mode: str = DEFAULT_NEUTRALISE,
) -> dict[str, Any]:
    """组内中性化（QL11）:``demean`` 组内去均值,``rank`` 组内取秩后映射到 [−1,1]。

    分组缺失（``None`` / 空串）的标的进 ``unassigned`` 并保留原值,同时如实报告缺口比例——
    不把它们默认塞进某一组假装做过中性化。
    """
    if mode not in NEUTRALISE_MODES:
        raise ValueError(f"neutralise 仅支持 {' / '.join(NEUTRALISE_MODES)},收到 {mode!r}")
    if mode == "none":
        return {"mode": "none", "values": list(values), "groups_used": 0, "unassigned": 0, "unassigned_ratio": 0.0}
    if len(values) != len(groups):
        raise ValueError("values 与 groups 长度必须一致")

    buckets: dict[str, list[int]] = {}
    unassigned = 0
    for index, group in enumerate(groups):
        if group is None or str(group).strip() == "":
            unassigned += 1
            continue
        buckets.setdefault(str(group), []).append(index)

    out = list(values)
    for members in buckets.values():
        bucket_values = [values[index] for index in members]
        if mode == "demean":
            mean = sum(bucket_values) / len(bucket_values)
            for index in members:
                out[index] = values[index] - mean
        else:  # rank
            ranks = cs_rank(bucket_values)
            for position, index in enumerate(members):
                out[index] = ranks[position]
    return {
        "mode": mode,
        "values": out,
        "groups_used": len(buckets),
        "unassigned": unassigned,
        "unassigned_ratio": round(unassigned / max(1, len(values)), 6),
        "group_sizes": {name: len(members) for name, members in sorted(buckets.items())},
    }


def neutralise_matrix(
    factor: dict[str, list[float]],
    groups_by_symbol: dict[str, str | None],
    *,
    symbols: list[str],
    date_count: int,
    mode: str = DEFAULT_NEUTRALISE,
) -> dict[str, Any]:
    """逐日做组内中性化:每个交易日对截面内各分组分别去均值/取秩。"""
    if mode == "none":
        return {"mode": "none", "matrix": factor, "unassigned_ratio": 0.0, "groups_used": 0}
    result: dict[str, list[float]] = {symbol: [0.0] * date_count for symbol in symbols}
    unassigned_total = 0
    groups_used = 0
    for index in range(date_count):
        column = [factor[symbol][index] for symbol in symbols]
        groups = [groups_by_symbol.get(symbol) for symbol in symbols]
        neutralised = neutralise(column, groups, mode=mode)
        unassigned_total += int(neutralised["unassigned"])
        groups_used = max(groups_used, int(neutralised["groups_used"]))
        for position, symbol in enumerate(symbols):
            result[symbol][index] = neutralised["values"][position]
    cells = max(1, date_count * len(symbols))
    return {
        "mode": mode,
        "matrix": result,
        "unassigned_ratio": round(unassigned_total / cells, 6),
        "groups_used": groups_used,
    }


# --------------------------------------------------------------------------- #
# 横截面挖掘（确定性束搜索,选择只用 train 日期）
# --------------------------------------------------------------------------- #
def _date_split(date_count: int, overlap: int, *, min_segment: int = MIN_DATES_FOR_IC) -> dict[str, Any]:
    """按**日期**做三段式切分（train 选模 / valid 调阈值 / test 只确认一次）。

    **每段都必须达到显著性地板（``MIN_DATES_FOR_IC``）**——否则该段的 ``ic_series_summary`` 会退化
    成「不报显著性」,整条链路会静默产出空榜,看起来像「没挖到因子」而其实是样本不够。
    因此:50/25/25 不满足地板时退化为三段等分;三段等分仍不满足地板时 ``feasible=False``,
    由调用方如实拒绝而不是静默给空结果。
    """
    usable = max(0, date_count - overlap)
    train_end = int(usable * quant_factors.SPLIT_TRAIN_RATIO)
    valid_end = train_end + int(usable * quant_factors.SPLIT_VALID_RATIO)
    degraded = False
    reason = None
    if min(train_end, valid_end - train_end, usable - valid_end) < min_segment:
        third = usable // 3
        train_end = third
        valid_end = 2 * third
        degraded = True
        reason = f"可用交易日 {usable} 不足以按 50/25/25 切分（每段至少 {min_segment}）,已退化为三段等分"
    segments = {"train": train_end, "valid": valid_end - train_end, "test": usable - valid_end}
    feasible = min(segments.values()) >= min_segment if usable else False
    if not feasible:
        degraded = True
        reason = (
            f"可用交易日 {usable} 连三段等分都达不到每段 {min_segment} 个（现状 "
            f"train/valid/test = {segments['train']}/{segments['valid']}/{segments['test']}）——"
            "样本不足以做可判定的三段验证,拒绝在不足样本上下结论;请拉长面板窗口再试。"
        )
    return {
        "train_end": train_end,
        "valid_end": valid_end,
        "usable_dates": usable,
        "planned": segments,
        "degraded": degraded,
        "degraded_reason": reason,
        "min_segment_dates": min_segment,
        "feasible": feasible,
    }


def shuffle_cross_labels(
    forward: dict[str, list[float | None]],
    symbols: list[str],
    *,
    seed: int,
) -> dict[str, Any]:
    """逐日**标的维度**置换标签（QL11 过拟合自检）:破坏「因子 ↔ 未来收益」的对应关系。

    只在当日有标签的标的之间置换（保留各日覆盖结构），因此 IC 序列的样本数不变——
    若置换后仍能挖出高 IC 榜单，问题出在搜索/多重检验而非数据。确定性与 seed 绑定。
    """
    rng = random.Random(int(seed))
    out: dict[str, list[float | None]] = {symbol: list(forward[symbol]) for symbol in symbols}
    date_count = len(next(iter(forward.values()))) if forward else 0
    moved = 0
    for index in range(date_count):
        holders = [symbol for symbol in symbols if forward[symbol][index] is not None]
        if len(holders) < 2:
            continue
        values = [forward[symbol][index] for symbol in holders]
        rng.shuffle(values)
        for position, symbol in enumerate(holders):
            out[symbol][index] = values[position]
        moved += len(holders)
    return {"seed": int(seed), "dates_permuted": date_count, "values_permuted": moved, "forward": out}


def cross_section_mine(
    panel: dict[str, Any],
    *,
    forward_days: int = quant_factors.FORWARD_DAYS,
    top_n: int = 5,
    beam_width: int = 6,
    beam_levels: int = 2,
    alpha: float = quant_stats.DEFAULT_ALPHA,
    min_train_ic: float = 0.01,
    min_valid_ic: float = 0.005,
    neutralise_mode: str = DEFAULT_NEUTRALISE,
    groups_by_symbol: dict[str, str | None] | None = None,
    marginal_n: int = 5,
    label_shuffle_seed: int | None = None,
    on_step: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """在面板上做横截面挖掘:逐日 rank IC 打分 + 三段式切分（按日期）+ BH 校正 + 相关性去重。

    ``label_shuffle_seed`` 给出时先把标签按**标的维度**逐日确定性置换再跑整条链路（过拟合自检:
    随机标签下榜单应为空）。置换记录落响应 ``label_shuffle``,seed 可复算。
    """
    if not panel.get("available"):
        return {
            "available": False,
            "top": [],
            "marginal": [],
            "label_shuffle": None,
            "cs_spec_version": CS_SPEC_VERSION,
            "degraded_reason": panel.get("degraded_reason") or "面板不可用（未构建或标的/日期不足）",
        }
    symbols = list(panel["symbols"])
    dates = list(panel["dates"])
    date_count = len(dates)
    forward = forward_return_matrix(panel, forward_days)
    label_shuffle: dict[str, Any] | None = None
    if label_shuffle_seed is not None:
        shuffled = shuffle_cross_labels(forward, symbols, seed=int(label_shuffle_seed))
        forward = shuffled["forward"]
        label_shuffle = {key: value for key, value in shuffled.items() if key != "forward"}
    features = feature_matrix(panel)
    split = _date_split(date_count, forward_days)
    if not split["feasible"]:
        return {
            "available": False,
            "top": [],
            "marginal": [],
            "split": split,
            "label_shuffle": None,
            "cs_spec_version": CS_SPEC_VERSION,
            "ic_definition": {
                "kind": "daily_cross_sectional_spearman",
                "min_symbols_per_date": MIN_SYMBOLS_PER_DATE,
                "forward_days": forward_days,
                "effective_sampling": f"每 {forward_days} 个交易日取一个近似独立样本做 t 检验",
            },
            "degraded_reason": split["degraded_reason"],
        }
    train_end = int(split["train_end"])
    valid_end = int(split["valid_end"])
    train_dates = dates[:train_end]

    def score(tokens: list[str], upto: int, *, apply_neutralise: bool = True) -> dict[str, Any] | None:
        try:
            values = evaluate_matrix(tokens, features, symbols, date_count)
        except ValueError:
            return None
        if apply_neutralise and neutralise_mode != "none":
            values = neutralise_matrix(
                values,
                groups_by_symbol or {},
                symbols=symbols,
                date_count=date_count,
                mode=neutralise_mode,
            )["matrix"]
        segment_dates = dates[:upto]
        daily = daily_rank_ic(
            {symbol: values[symbol][:upto] for symbol in symbols},
            {symbol: forward[symbol][:upto] for symbol in symbols},
            symbols=symbols,
            dates=segment_dates,
        )
        return {"values": values, "daily": daily, "summary": ic_series_summary(daily["series"], overlap=forward_days)}

    # —— 束搜索:只在 train 日期上选留 ——
    evaluated = 0
    seen: set[tuple[str, ...]] = set()
    pool: list[dict[str, Any]] = []

    def rank_key(item: dict[str, Any]) -> tuple[float, int, str]:
        mean_ic = item["train_summary"]["mean"]
        return (-abs(mean_ic or 0.0), len(item["tokens"]), " ".join(item["tokens"]))

    frontier: list[dict[str, Any]] = []
    for atom in [*quant_factors.FEATURE_NAMES, *CS_RANK_OPS]:
        tokens = [atom]
        result = score(tokens, train_end)
        if result is None:
            continue
        evaluated += 1
        frontier.append({"tokens": tokens, "train_summary": result["summary"]})
        pool.append({"tokens": tokens, "train_summary": result["summary"]})
        if on_step is not None:
            on_step(evaluated, 0)
    frontier.sort(key=rank_key)
    frontier = frontier[:beam_width]
    pool = sorted(pool, key=rank_key)[: max(beam_width * 2, top_n)]

    for _level in range(beam_levels):
        batch: list[tuple[str, ...]] = []
        for item in frontier:
            for candidate in quant_factors._expand_candidates(item["tokens"]):
                key = tuple(candidate)
                if key in seen:
                    continue
                seen.add(key)
                batch.append(key)
            for cs_op in CS_RANK_OPS:
                key = tuple([*item["tokens"], cs_op])
                if key in seen:
                    continue
                seen.add(key)
                batch.append(key)
        scored: list[dict[str, Any]] = []
        for key in batch:
            result = score(list(key), train_end)
            if result is None:
                continue
            evaluated += 1
            scored.append({"tokens": list(key), "train_summary": result["summary"]})
            if on_step is not None:
                on_step(evaluated, 0)
        if not scored:
            break
        scored.sort(key=rank_key)
        frontier = scored[:beam_width]
        pool.extend(frontier)
        pool.sort(key=rank_key)
        pool = pool[: max(beam_width * 2, top_n)]

    # —— 完整三段统计 + BH 校正 ——
    rows: list[dict[str, Any]] = []
    p_values: list[float] = []
    for item in pool:
        tokens = item["tokens"]
        ranked = score(tokens, date_count)
        if ranked is None:
            continue
        train_summary = ic_series_summary(
            daily_rank_ic(
                {s: ranked["values"][s][:train_end] for s in symbols},
                {s: forward[s][:train_end] for s in symbols},
                symbols=symbols,
                dates=dates[:train_end],
            )["series"],
            overlap=forward_days,
        )
        valid_summary = ic_series_summary(
            daily_rank_ic(
                {s: ranked["values"][s][train_end:valid_end] for s in symbols},
                {s: forward[s][train_end:valid_end] for s in symbols},
                symbols=symbols,
                dates=dates[train_end:valid_end],
            )["series"],
            overlap=forward_days,
        )
        test_daily = daily_rank_ic(
            {s: ranked["values"][s][valid_end:] for s in symbols},
            {s: forward[s][valid_end:] for s in symbols},
            symbols=symbols,
            dates=dates[valid_end:],
        )
        test_summary = ic_series_summary(test_daily["series"], overlap=forward_days)
        p_values.append(float(test_summary["p_value"] if test_summary["p_value"] is not None else 1.0))
        mean_ic = train_summary["mean"]
        valid_ic = valid_summary["mean"]
        if mean_ic is None or valid_ic is None or abs(mean_ic) < min_train_ic or abs(valid_ic) < min_valid_ic:
            continue
        test_ic = test_summary["mean"] or 0.0
        consistency = 0.0
        if test_ic != 0.0:
            reference_sign = 1.0 if test_ic > 0 else -1.0
            matches = sum(
                1
                for value in (mean_ic, valid_ic, test_ic)
                if value != 0.0 and (1.0 if value > 0 else -1.0) == reference_sign
            )
            consistency = matches / 3.0
        shrink = quant_factors._complexity_shrink(len(tokens), max(1, test_summary["samples"]))
        # 中性化前后并列:同一公式在**未中性化**口径下的 valid/test IC（mode=none 时两者恒等）
        raw_line: dict[str, Any] = {"raw_valid_ic": None, "raw_test_ic": None}
        if neutralise_mode != "none":
            raw_ranked = score(tokens, date_count, apply_neutralise=False)
            if raw_ranked is not None:
                raw_valid = ic_series_summary(
                    daily_rank_ic(
                        {s: raw_ranked["values"][s][train_end:valid_end] for s in symbols},
                        {s: forward[s][train_end:valid_end] for s in symbols},
                        symbols=symbols,
                        dates=dates[train_end:valid_end],
                    )["series"],
                    overlap=forward_days,
                )
                raw_test = ic_series_summary(
                    daily_rank_ic(
                        {s: raw_ranked["values"][s][valid_end:] for s in symbols},
                        {s: forward[s][valid_end:] for s in symbols},
                        symbols=symbols,
                        dates=dates[valid_end:],
                    )["series"],
                    overlap=forward_days,
                )
                raw_line = {
                    "raw_valid_ic": raw_valid["mean"],
                    "raw_test_ic": raw_test["mean"],
                    "raw_test_t_stat": raw_test["t_stat"],
                    "ic_delta_test": (
                        round(test_ic - float(raw_test["mean"]), 6) if raw_test["mean"] is not None else None
                    ),
                }
        rows.append({
            "formula": " ".join(tokens),
            "formula_tokens": tokens,
            "complexity": len(tokens),
            "train_ic": mean_ic,
            "valid_ic": valid_ic,
            "test_ic": round(test_ic, 6),
            "train_icir": train_summary["icir"],
            "test_icir": test_summary["icir"],
            "test_positive_ratio": test_summary["positive_ratio"],
            "test_t_stat": test_summary["t_stat"],
            "test_p_value": test_summary["p_value"],
            "test_effective_samples": test_summary["effective_samples"],
            "sign_consistency": round(consistency, 6),
            "rank_score": round(abs(test_ic) * consistency, 10),
            "complexity_shrink": round(shrink, 10),
            "rank_score_adjusted": round(abs(test_ic) * consistency * shrink, 10),
            "test_dates": test_summary["samples"],
            **raw_line,
            "neutralise_mode": neutralise_mode,
            "_index": len(p_values) - 1,
            "_values": ranked["values"],
        })

    rows.sort(key=lambda row: (-row["rank_score_adjusted"], row["complexity"], row["formula"]))
    rejected = quant_stats.benjamini_hochberg(p_values, alpha=alpha) if p_values else []
    rejected_indexes = {index for index, flag in enumerate(rejected) if flag}
    for row in rows:
        row["test_significant"] = row["_index"] in rejected_indexes
    significant = [row for row in rows if row["test_significant"]]
    marginal_rows = [row for row in rows if not row["test_significant"]]

    dedup = quant_stats.greedy_decorrelate(
        [_flatten(row["_values"], symbols) for row in significant[: quant_factors.DEDUP_SCAN_LIMIT]],
        threshold=quant_factors.DEDUP_CORRELATION_THRESHOLD,
        keep=top_n,
    )
    scan = significant[: quant_factors.DEDUP_SCAN_LIMIT]
    top = [scan[index] for index in dedup["kept"]]

    def present(row: dict[str, Any], *, with_values: bool) -> dict[str, Any]:
        item = {key: value for key, value in row.items() if key not in ("_values", "_index")}
        item["significance_note"] = (
            None if item["test_significant"] else "test 段横截面 IC 未达统计显著（BH 校正后），不可当作有效因子"
        )
        if with_values:
            item["neutralisation"] = {
                "mode": neutralise_mode,
                "groups_used": len({g for g in (groups_by_symbol or {}).values() if g}),
                "unassigned_ratio": _unassigned_ratio(groups_by_symbol, symbols),
            }
        return item

    return {
        "available": bool(top),
        "cs_spec_version": CS_SPEC_VERSION,
        "top": [present(row, with_values=True) for row in top],
        "marginal": [present(row, with_values=False) for row in marginal_rows[:marginal_n]],
        "split": split,
        "multiple_testing": {
            "method": "fdr",
            "alpha": alpha,
            "candidates_evaluated": evaluated,
            "candidates_tested": len(p_values),
            "passed_screen": len(rows),
            "rejected_total": len(rejected_indexes),
            "threshold": round(quant_stats.bh_threshold(p_values, alpha=alpha), 10) if p_values else None,
        },
        "dedup": {
            "threshold": quant_factors.DEDUP_CORRELATION_THRESHOLD,
            "scanned": dedup["scanned"],
            "merged_total": sum(len(cluster["merged"]) for cluster in dedup["clusters"]),
        },
        "search": {
            "strategy": "beam",
            "beam_width": beam_width,
            "beam_levels": beam_levels,
            "selection_segment": "train",
            "candidates_evaluated": evaluated,
            "cs_ops": list(CS_RANK_OPS),
        },
        "neutralise": {
            "mode": neutralise_mode,
            "unassigned_ratio": _unassigned_ratio(groups_by_symbol, symbols),
            "groups_used": len({group for group in (groups_by_symbol or {}).values() if group}),
            "comparison": _neutralise_comparison(rows),
        },
        "ic_definition": {
            "kind": "daily_cross_sectional_spearman",
            "min_symbols_per_date": MIN_SYMBOLS_PER_DATE,
            "forward_days": forward_days,
            "effective_sampling": f"每 {forward_days} 个交易日取一个近似独立样本做 t 检验",
        },
        "dates": {"total": date_count, "first": dates[0] if dates else None, "last": dates[-1] if dates else None},
        "label_shuffle": label_shuffle,
        "degraded_reason": _degraded_lines(top, split, label_shuffle),
    }


def _degraded_lines(top: list[Any], split: dict[str, Any], label_shuffle: dict[str, Any] | None) -> str | None:
    """把「没结果的真实原因」与「口径退化的说明」都写出来,不靠留空让人猜。"""
    lines: list[str] = []
    if not top:
        if label_shuffle is not None:
            lines.append(
                f"过拟合自检:标签已按标的维度确定性置换(seed={label_shuffle['seed']}),"
                "未挖到显著公式——这是期望结果。"
            )
        else:
            lines.append("没有任何公式通过横截面 IC 的多重检验校正（不编造结果）")
    if split.get("degraded_reason"):
        lines.append(f"切分口径:{split['degraded_reason']}")
    return " ".join(lines) or None


def _neutralise_comparison(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """中性化前后 IC 的并列汇总（QL11 验收:差异如实展示,不挑好看的那一个报）。"""
    paired = [row for row in rows if row.get("raw_test_ic") is not None]
    if not paired:
        return {"pairs": 0, "neutralised_test_ic_mean": None, "raw_test_ic_mean": None,
                "delta_test_ic_mean": None, "note": "没有中性化口径的配对样本（mode=none 或原始口径求值失败）"}
    neutralised_mean = sum(float(row["test_ic"]) for row in paired) / len(paired)
    raw_mean = sum(float(row["raw_test_ic"]) for row in paired) / len(paired)
    return {
        "pairs": len(paired),
        "neutralised_test_ic_mean": round(neutralised_mean, 6),
        "raw_test_ic_mean": round(raw_mean, 6),
        "delta_test_ic_mean": round(neutralised_mean - raw_mean, 6),
        "note": "中性化会吃掉与分组共线的部分:I 差值为负属正常,不代表公式变差",
    }


def _flatten(matrix: dict[str, list[float]], symbols: list[str]) -> list[float]:
    out: list[float] = []
    for symbol in symbols:
        out.extend(matrix[symbol])
    return out


def _unassigned_ratio(groups_by_symbol: dict[str, str | None] | None, symbols: list[str]) -> float:
    if not groups_by_symbol:
        return 1.0
    missing = sum(1 for symbol in symbols if not groups_by_symbol.get(symbol))
    return round(missing / max(1, len(symbols)), 6)


# --------------------------------------------------------------------------- #
# 长空组合报告卡（QL12:挖掘 → 实验闭环）
# --------------------------------------------------------------------------- #
BACKTEST_SPEC_VERSION = 2      # 与 quant_experiments.BACKTEST_SPEC_VERSION 同口径
ANNUAL_TRADING_DAYS = 252
DEFAULT_QUANTILES = 5


def _quantile_members(column: list[float], symbols: list[str], quantiles: int) -> list[list[str]]:
    """按因子值升序切成 ``quantiles`` 组（等数量切分;并列按代码稳定排序,保证确定性）。"""
    paired = sorted(zip(column, symbols), key=lambda item: (item[0], item[1]))
    size = len(paired) // quantiles
    if size < 1:
        return []
    buckets: list[list[str]] = []
    for index in range(quantiles):
        start = index * size
        end = len(paired) if index == quantiles - 1 else start + size
        buckets.append([symbol for _value, symbol in paired[start:end]])
    return buckets


def _close_matrix(panel: dict[str, Any]) -> dict[str, list[float]]:
    return {symbol: [float(v) for v in panel["columns"][symbol]["close"]] for symbol in panel["symbols"]}


def _leg_return(closes: dict[str, list[float]], members: list[str], index: int) -> float:
    values = [
        closes[symbol][index + 1] / closes[symbol][index] - 1.0
        for symbol in members
        if closes[symbol][index] > 0.0
    ]
    return sum(values) / len(values) if values else 0.0


def _turnover(previous: list[list[str]], current: list[list[str]]) -> float:
    """换手代理 = 多空两腿「新进名单占并集比例」之和（名单进出比例,非权重漂移）。"""
    total = 0.0
    for previous_leg, current_leg in ((previous[-1], current[-1]), (previous[0], current[0])):
        before, after = set(previous_leg), set(current_leg)
        union = before | after
        total += len(union - before) / len(union) if union else 0.0
    return total


def _quantile_forward_means(
    panel: dict[str, Any], values: dict[str, list[float]], quantiles: int, dates: list[str]
) -> list[float | None]:
    """各分位的**前瞻 1 日收益均值**（分位结构证据:按因子值升序切组,越靠前 = 因子值越低）。"""
    symbols = list(panel["symbols"])
    closes = _close_matrix(panel)
    totals = [0.0] * quantiles
    counts = [0] * quantiles
    for index in range(max(0, len(dates) - 1)):
        groups = _quantile_members([values[symbol][index] for symbol in symbols], symbols, quantiles)
        if len(groups) != quantiles:
            continue
        for bucket_index, members in enumerate(groups):
            values_at = [
                closes[symbol][index + 1] / closes[symbol][index] - 1.0
                for symbol in members
                if closes[symbol][index] > 0.0
            ]
            if values_at:
                totals[bucket_index] += sum(values_at) / len(values_at)
                counts[bucket_index] += 1
    return [round(totals[i] / counts[i], 8) if counts[i] else None for i in range(quantiles)]


def long_short_report(
    panel: dict[str, Any],
    tokens: list[str],
    *,
    quantiles: int = DEFAULT_QUANTILES,
    commission_bps: float = 2.5,
    slippage_bps: float = 5.0,
    folds: int = 3,
    adv_participation: float = 0.05,
    volume_unit: float = 100.0,
) -> dict[str, Any]:
    """把一条横截面公式做成一张**可复算**的报告卡（QL12）。

    组成:IC 套件（逐日截面 rank IC）+ 分位结构 + 成本后长空净值 + 分段 fold 稳定性 + 容量量级提示。
    口径全部显式挂载:成本按多空两腿的名单进出换手双边计费、建仓日按满仓换手（不假装免费入场）;
    年化用 252 个交易日;容量只用「成交量 × 收盘价」当成交额代理并明确标注这是代理口径。

    ``report_hash`` 由「面板 digest + 公式 + 全部口径参数」派生 —— 同一快照 + 同一参数必得同一哈希,
    这是「全部数字可从快照复算」的可核对凭据。
    """
    if not panel.get("available"):
        return {"available": False, "degraded_reason": panel.get("degraded_reason") or "面板不可用"}
    symbols = list(panel["symbols"])
    dates = list(panel["dates"])
    if not 2 <= int(quantiles) <= len(symbols):
        raise ValueError(f"quantiles 必须在 2—{len(symbols)},收到 {quantiles}")
    quantiles = int(quantiles)

    values = evaluate_matrix(tokens, feature_matrix(panel), symbols, len(dates))
    forward = forward_return_matrix(panel, 1)
    ic = ic_series_summary(daily_rank_ic(values, forward, symbols=symbols, dates=dates)["series"], overlap=1)

    closes = _close_matrix(panel)
    cost_rate = (float(commission_bps) + float(slippage_bps)) / 10000.0
    equity = 1.0
    gross_curve: list[float] = []
    net_curve: list[float] = []
    turnover_curve: list[float] = []
    equity_curve: list[dict[str, Any]] = []
    previous: list[list[str]] | None = None
    step = max(0, len(dates) - 1)
    for index in range(step):
        groups = _quantile_members([values[symbol][index] for symbol in symbols], symbols, quantiles)
        if len(groups) != quantiles:
            continue
        spread = _leg_return(closes, groups[-1], index) - _leg_return(closes, groups[0], index)
        turnover = 1.0 if previous is None else _turnover(previous, groups)  # 建仓日满仓计费
        net = spread - cost_rate * turnover
        equity *= 1.0 + net
        gross_curve.append(spread)
        net_curve.append(net)
        turnover_curve.append(turnover)
        equity_curve.append({"date": dates[index + 1], "gross": round(spread, 8), "net": round(net, 8),
                             "turnover": round(turnover, 6), "equity": round(equity, 8)})
        previous = groups

    if not net_curve:
        return {
            "available": False,
            "formula": " ".join(tokens),
            "formula_tokens": list(tokens),
            "ic": ic,
            "degraded_reason": "没有任何可计算的调仓日（面板日期数不足）",
            "caliber": _report_caliber(quantiles, commission_bps, slippage_bps),
        }

    mean_net = sum(net_curve) / len(net_curve)
    variance = sum((value - mean_net) ** 2 for value in net_curve) / (len(net_curve) - 1) if len(net_curve) > 1 else 0.0
    std = math.sqrt(variance)
    peak = 1.0
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, float(point["equity"]))
        max_drawdown = min(max_drawdown, float(point["equity"]) / peak - 1.0)
    gross_total = math.prod(1.0 + value for value in gross_curve) - 1.0
    long_short = {
        "samples": len(net_curve),
        "first_date": equity_curve[0]["date"],
        "last_date": equity_curve[-1]["date"],
        "gross_mean": round(sum(gross_curve) / len(gross_curve), 8),
        "gross_total": round(gross_total, 6),
        "net_mean": round(mean_net, 8),
        "net_total": round(equity - 1.0, 6),
        "cost_total": round(gross_total - (equity - 1.0), 6),
        "net_std": round(std, 8),
        "net_sharpe": round(mean_net / std * math.sqrt(ANNUAL_TRADING_DAYS), 6) if std > 0 else None,
        "net_annualised": round(mean_net * ANNUAL_TRADING_DAYS, 6),
        "max_drawdown": round(max_drawdown, 6),
        "turnover_mean": round(sum(turnover_curve) / len(turnover_curve), 6),
        "quantile_mean_forward_returns": _quantile_forward_means(panel, values, quantiles, dates),
        "equity_curve": equity_curve,
    }

    fold_reports: list[dict[str, Any]] = []
    if folds >= 2:
        size = max(1, len(dates) // folds)
        for index in range(folds):
            start = index * size
            end = len(dates) if index == folds - 1 else (index + 1) * size
            if end - start < 2:
                continue
            fold_daily = daily_rank_ic(
                {symbol: values[symbol][start:end] for symbol in symbols},
                {symbol: forward[symbol][start:end] for symbol in symbols},
                symbols=symbols,
                dates=dates[start:end],
            )
            fold_ic = ic_series_summary(fold_daily["series"], overlap=1)
            low, high = max(0, start - 1), max(0, end - 1)
            fold_net = [float(value) for value in net_curve[low:high]]
            fold_reports.append({
                "fold": index + 1,
                "first_date": dates[start],
                "last_date": dates[end - 1],
                "ic_mean": fold_ic["mean"],
                "ic_samples": fold_ic["samples"],
                "net_mean": round(sum(fold_net) / len(fold_net), 8) if fold_net else None,
                "net_total": round(math.prod(1.0 + value for value in fold_net) - 1.0, 6) if fold_net else None,
                "skipped_sections": fold_daily["skipped_total"],
            })

    return {
        "available": True,
        "formula": " ".join(tokens),
        "formula_tokens": list(tokens),
        "ic": ic,
        "quantiles": quantiles,
        "long_short": long_short,
        "folds": fold_reports,
        "capacity": _capacity_hint(panel, adv_participation=adv_participation, volume_unit=volume_unit),
        "caliber": _report_caliber(quantiles, commission_bps, slippage_bps),
        "report_hash": _report_hash(panel, tokens, quantiles, commission_bps, slippage_bps, folds),
        "degraded_reason": None,
    }


def _report_caliber(quantiles: int, commission_bps: float, slippage_bps: float) -> dict[str, Any]:
    return {
        "backtest_spec_version": BACKTEST_SPEC_VERSION,
        "cs_spec_version": CS_SPEC_VERSION,
        "commission_bps": commission_bps,
        "slippage_bps": slippage_bps,
        "annual_trading_days": ANNUAL_TRADING_DAYS,
        "quantiles": quantiles,
        "forward_days": 1,
        "weighting": "分位内等权,每交易日重排",
        "turnover_definition": "多空两腿「新进名单占并集比例」之和;建仓日按满仓换手计费",
        "portfolio_kind": "横截面分位多空（不是单标的择时）",
        "annualisation": "net_mean × 252;Sharpe = net_mean / net_std × √252",
    }


def _capacity_hint(panel: dict[str, Any], *, adv_participation: float, volume_unit: float) -> dict[str, Any]:
    """容量量级提示:面板只有 volume 没有 amount,故用「成交量 × 收盘价」当成交额代理并如实标注。"""
    advis: list[float] = []
    for symbol in panel["symbols"]:
        columns = panel["columns"][symbol]
        notionals = sorted(
            float(volume) * volume_unit * float(close)
            for volume, close in zip(columns["volume"], columns["close"])
            if float(close) > 0.0
        )
        if notionals:
            advis.append(notionals[len(notionals) // 2])
    if not advis:
        return {"available": False, "degraded_reason": "面板没有可用的成交量,无法给容量提示"}
    advis.sort()
    median = advis[len(advis) // 2]
    return {
        "available": True,
        "proxy": "成交量 × 收盘价 × volume_unit（面板无成交额字段,故用代理,非真实撮合容量）",
        "volume_unit": volume_unit,
        "universe_median_adv_cny": round(median, 2),
        "participation": adv_participation,
        "per_name_notional_cny": round(median * adv_participation, 2),
        "universe_notional_cny": round(median * adv_participation * len(advis), 2),
        "caveat": "只用于判断规模上限的**量级**,不含冲击成本与涨跌停约束。",
    }


def _report_hash(
    panel: dict[str, Any], tokens: list[str], quantiles: int,
    commission_bps: float, slippage_bps: float, folds: int,
) -> str:
    payload = {
        "panel_digest": quant_panel.panel_digest(panel),
        "tokens": list(tokens),
        "quantiles": quantiles,
        "commission_bps": commission_bps,
        "slippage_bps": slippage_bps,
        "folds": folds,
        "backtest_spec_version": BACKTEST_SPEC_VERSION,
        "cs_spec_version": CS_SPEC_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
