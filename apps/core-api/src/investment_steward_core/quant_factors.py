"""确定性因子挖掘(quant_factors):token 序列公式 + 栈式求值 + 三段式枚举搜索。

许可说明:AlphaMaster(AGPL-3.0)仅作架构思想参考(公式化因子表达、position = tanh 约束),
本模块为独立原创实现(stdlib),未移植其任何代码。对齐分享池阶段 A:公式/参数集为纯 JSON,可复算。

- 特征(feature):对 OHLCV 序列逐 bar 可计算的原子值;
- 公式:逆波兰 token 序列(如 ["ret_5","1","sub","tanh"]),JSON 可序列化;
- 求值:栈式逐 bar 计算因子值序列;除零/溢出安全(返回 0.0);
- 挖掘:枚举深度受限的公式空间,按**三段式切分**（train 选模 / valid 调阈值 / test 只确认一次）
  以 test 段 IC × 逐段同号一致性排序,并做多重检验校正（QL01—QL04）。

统计口径见 quant_stats（IC_SPEC_VERSION）;本模块的挖掘契约见 MINE_SPEC_VERSION。
"""

from __future__ import annotations

import math
from itertools import product
from typing import Any

from investment_steward_core import quant_stats

FEATURE_NAMES = (
    # —— 原始 6 个（旧口径,公式向后兼容必须保留）——
    "ret_1",
    "ret_5",
    "vol_ratio",
    "ma_ratio",
    "range_ratio",
    "rsi_14",
    # —— QL06 扩充（全部 stdlib、逐 bar 因果:只用 t 及之前的 bar）——
    "gap",
    "close_pos",
    "hl_range",
    "high_60_dist",
    "low_60_dist",
    "vol_change",
    "vp_corr_20",
    "vol_ratio_60",
    "ma_gap_5_20",
    "up_ratio_20",
)
# 旧口径原子集合:exhaustive 搜索与历史计数口径只认这 6 个
LEGACY_FEATURE_NAMES = FEATURE_NAMES[:6]
BINARY_OPS = ("add", "sub", "mul", "div")
UNARY_OPS = ("abs", "tanh", "neg")
MAX_DEPTH = 4
FORWARD_DAYS = 5
MIN_VALID_SAMPLES = 30

# —— 公式语法版本（新增 token 必须递增；旧 token 语义不得变）——
FORMULA_SPEC_VERSION = 2
# 时序算子（QL06）:窗口参数固定 5/10/20/60,编码为 `<op>_<window>` 单 token（如 ts_mean_20）
TS_WINDOW_OPS = ("ts_mean", "ts_std", "ts_rank", "ts_zscore", "delay", "delta")
TS_WINDOWS = (5, 10, 20, 60)
TS_TOKENS = tuple(f"{op}_{window}" for op in TS_WINDOW_OPS for window in TS_WINDOWS)

# 统计口径版本由 quant_stats 拥有,此处转发便于调用方单点引用
IC_SPEC_VERSION = quant_stats.IC_SPEC_VERSION

# —— 挖掘契约版本（改动切分/排序/校正口径必须递增）——
MINE_SPEC_VERSION = 3
IC_SUITE_SPEC_VERSION = 1

# —— 确定性束搜索（QL06）：全枚举不可行（16 原子 + 7 算子 + 24 时序 token,
#    长度 ≤5 时组合数为 47^5 ≈ 2.3 亿）,改为固定宽度、无随机性的层式束搜索。——
BEAM_WIDTH = 16          # 每层保留的束宽（进响应,复算必须用同一宽度）
BEAM_LEVELS = 3          # 扩展层数
BEAM_PAIR_POOL = 8       # 束内两两组合的候选池大小（top-8 × top-8 × 4 算子）
SEARCH_MODES = ("beam", "exhaustive")
DEFAULT_SEARCH = "beam"
# 公式复杂度惩罚（AIC/BIC 式）:每观测惩罚 = coef × complexity × ln(n) / n,
# 排序键 = |test_IC| × 逐段同号一致性 × (1 − 惩罚)。长度不再只是 tie-break。
COMPLEXITY_PENALTY_COEF = 1.0

# 三段式切分比例（按可用样本量自适应，见 three_way_split）
SPLIT_TRAIN_RATIO = 0.5
SPLIT_VALID_RATIO = 0.25
MIN_SEGMENT_SAMPLES = 30

DEFAULT_MIN_TRAIN_IC = 0.05
DEFAULT_MIN_VALID_IC = 0.02
IC_DECAY_HORIZONS = (1, 5, 10, 20)
PERIOD_IC_MIN_FOLD_SAMPLES = 25
GROUP_RETURN_GROUPS = 5
# 置换校准的确定性等距抽样步长（覆盖整个枚举顺序,不按观测值挑头部候选）
CALIBRATION_STRIDE = 8
# 相关性去重（QL08）:|Spearman| > 阈值的候选归入同一簇,每簇只留排序最优者
DEDUP_CORRELATION_THRESHOLD = 0.7
DEDUP_SCAN_LIMIT = 200
DEDUP_MERGED_SAMPLE = 10  # 簇内被合并的公式只回传前若干条（size 给出完整计数）

# 历史窗口档位（QL09）：250 ≈ 一年 / 750 ≈ 三年 / 1250 ≈ 五年日线
WINDOW_TIERS = (250, 750, 1250)
DEFAULT_WINDOW_BARS = 750
# 探针实测（510300 日线，2026-09-22，逐源直拉、绕过 TTL 缓存；原始记录
# docs/evidence/ql09-window-probe-2026-09-22.json、脚本 .runtime/ql09_window_probe.py）：
#   腾讯   250→250 / 750→750 / 1250→**641**(首日 2024-01-30) / 2000→**641** → 该源硬上限 641 根；
#   东财   250→250 / 750→750 / 1250→1250(首日 2021-07-29) / 2000→2000(首日 2018-06-29) → 2000 未截断；
#   耗时 82—185 ms/次，序列化体积 31 KB(250) / 94 KB(750) / 156 KB(1250) / 249 KB(2000)。
# 默认取 750:这是**两个源都能足量供给**的最大档位 → 同一请求在不同网络环境下拿到同一根数，
# 结果可跨机复现;1250 只有东财能给（腾讯硬截 641），一旦东财不可达就会静默少 49% 样本,
# 故留在显式可选档并在响应里标注截断风险。
DEFAULT_WINDOW_REASON = (
    "默认 750 根（约三年）:探针实测（docs/evidence/ql09-window-probe-2026-09-22.json）"
    "腾讯与东财对 250/750 根均足量返回且逐根一致,对 1250 根则腾讯硬截到 641 根"
    "（首日前移到 2024-01-30）——750 是两源一致的最大档位,跨网络环境结果可复现;"
    "1250 需东财供给,属显式可选档。单次耗时 82—185 ms,750 根序列化约 94 KB。"
)


def resolve_window(bars: int | None) -> dict[str, Any]:
    """解析历史窗口档位（QL09）:缺省档、请求值、实际档位与理由全部显式返回。

    请求值向上取到最近的档位（内存/请求量按档位收敛）;超出最大档位时取最大档。
    """
    minimum = MIN_SEGMENT_SAMPLES * 3 + FORWARD_DAYS
    if bars is None:
        return {
            "requested": None,
            "bars": DEFAULT_WINDOW_BARS,
            "tier": DEFAULT_WINDOW_BARS,
            "tiers": list(WINDOW_TIERS),
            "reason": DEFAULT_WINDOW_REASON,
        }
    requested = int(bars)
    if requested < minimum:
        raise ValueError(f"window 至少 {minimum} 根（三段切分每段 ≥ {MIN_SEGMENT_SAMPLES}）收到 {requested}")
    if requested > WINDOW_TIERS[-1]:
        return {
            "requested": requested,
            "bars": WINDOW_TIERS[-1],
            "tier": WINDOW_TIERS[-1],
            "tiers": list(WINDOW_TIERS),
            "reason": f"请求 {requested} 超出最大档位,已收敛到 {WINDOW_TIERS[-1]} 根",
            "clamped": True,
        }
    tier = next(value for value in WINDOW_TIERS if value >= requested)
    return {
        "requested": requested,
        "bars": tier,
        "tier": tier,
        "tiers": list(WINDOW_TIERS),
        "reason": f"按档位收敛到 {tier} 根（请求 {requested}）",
    }


# 各源实测行数上限（QL09）:超过此值的请求会被上游**静默截断**（不报错、不告知）
SOURCE_ROW_CAP = {"tencent": 641, "eastmoney": None}
WINDOW_TRUNCATION_NOTE = (
    "上游可能静默截断:腾讯对 >641 根的请求只回 641 根且不报错;"
    "取回行数少于请求档位时在此标注,不把少给的样本当足量样本用。"
)


def window_provenance(resolved: dict[str, Any], bars: list[dict[str, Any]], source: str) -> dict[str, Any]:
    """把「请求档位 vs 实际拿到」写进响应（QL09 验收:响应标注窗口档位）。

    ``truncated`` 为真时 ``reason`` 显式说明少给了多少,调用方不得把实际行数当档位用。
    """
    actual = len(bars)
    requested = int(resolved["bars"])
    truncated = actual < requested
    report = {
        **resolved,
        "bars_actual": actual,
        "source": source,
        "truncated": truncated,
        "source_row_cap": SOURCE_ROW_CAP.get(source),
        "first_bar": str(bars[0]["timestamp"])[:10] if bars else None,
        "last_bar": str(bars[-1]["timestamp"])[:10] if bars else None,
    }
    if truncated:
        report["truncation_note"] = (
            f"请求 {requested} 根,{source} 实回 {actual} 根（少 {requested - actual} 根）;"
            + WINDOW_TRUNCATION_NOTE
        )
    return report


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


def _rolling_mean(values: list[float], window: int) -> list[float]:
    """滚动均值（头部用扩张窗口,与既有特征口径一致）:O(n) 前缀和。"""
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    return [
        (prefix[i + 1] - prefix[max(0, i - window + 1)]) / (i - max(0, i - window + 1) + 1)
        for i in range(len(values))
    ]


def _rolling_std(values: list[float], window: int) -> list[float]:
    """滚动**样本**标准差（n−1;窗口内不足 2 个样本记 0.0）,头部用扩张窗口。"""
    prefix = [0.0]
    prefix_sq = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
        prefix_sq.append(prefix_sq[-1] + value * value)
    out: list[float] = []
    for i in range(len(values)):
        lo = max(0, i - window + 1)
        count = i - lo + 1
        if count < 2:
            out.append(0.0)
            continue
        total = prefix[i + 1] - prefix[lo]
        total_sq = prefix_sq[i + 1] - prefix_sq[lo]
        variance = (total_sq - total * total / count) / (count - 1)
        out.append(math.sqrt(variance) if variance > 0.0 else 0.0)
    return out


def _rolling_corr(a: list[float], b: list[float], window: int) -> list[float]:
    """滚动 Pearson 相关（头部扩张窗口,窗口内不足 3 个点记 0.0）:O(n) 前缀和。"""
    n = len(a)
    sa, sb, sa2, sb2, sab = [0.0], [0.0], [0.0], [0.0], [0.0]
    for i in range(n):
        sa.append(sa[-1] + a[i])
        sb.append(sb[-1] + b[i])
        sa2.append(sa2[-1] + a[i] * a[i])
        sb2.append(sb2[-1] + b[i] * b[i])
        sab.append(sab[-1] + a[i] * b[i])
    out: list[float] = []
    for i in range(n):
        lo = max(0, i - window + 1)
        count = i - lo + 1
        if count < 3:
            out.append(0.0)
            continue
        ma = (sa[i + 1] - sa[lo]) / count
        mb = (sb[i + 1] - sb[lo]) / count
        va = (sa2[i + 1] - sa2[lo]) / count - ma * ma
        vb = (sb2[i + 1] - sb2[lo]) / count - mb * mb
        cov = (sab[i + 1] - sab[lo]) / count - ma * mb
        denom = math.sqrt(va * vb) if va > 0.0 and vb > 0.0 else 0.0
        out.append(cov / denom if denom > 1e-12 else 0.0)
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
    # —— QL06 扩充原子（全部只用 t 及之前的 bar,逐 bar 因果）——
    if name == "gap":
        opens = [float(bar.get("open") or bar["close"]) for bar in bars]
        return [safe_div(opens[i] - closes[i - 1], closes[i - 1]) if i else 0.0 for i in range(len(closes))]
    if name == "close_pos":
        return [
            safe_div(closes[i] - lows[i], highs[i] - lows[i]) if highs[i] > lows[i] else 0.5
            for i in range(len(closes))
        ]
    if name == "hl_range":
        return [safe_div(highs[i] - lows[i], closes[i]) if closes[i] else 0.0 for i in range(len(closes))]
    if name == "high_60_dist":
        out = []
        for i in range(len(closes)):
            peak = max(highs[max(0, i - 59) : i + 1])
            out.append(safe_div(closes[i], peak) - 1.0 if peak else 0.0)
        return out
    if name == "low_60_dist":
        out = []
        for i in range(len(closes)):
            trough = min(lows[max(0, i - 59) : i + 1])
            out.append(safe_div(closes[i], trough) - 1.0 if trough else 0.0)
        return out
    if name == "vol_change":
        ret1 = [safe_div(closes[i] - closes[i - 1], closes[i - 1]) if i else 0.0 for i in range(len(closes))]
        short = _rolling_std(ret1, 5)
        long = _rolling_std(ret1, 20)
        return [safe_div(short[i], long[i]) - 1.0 if long[i] else 0.0 for i in range(len(closes))]
    if name == "vp_corr_20":
        ret1 = [safe_div(closes[i] - closes[i - 1], closes[i - 1]) if i else 0.0 for i in range(len(closes))]
        vol_delta = [safe_div(volumes[i] - volumes[i - 1], volumes[i - 1]) if i else 0.0 for i in range(len(closes))]
        return _rolling_corr(ret1, vol_delta, 20)
    if name == "vol_ratio_60":
        avg60 = _rolling_mean(volumes, 60)
        return [safe_div(volumes[i], avg60[i]) if avg60[i] else 0.0 for i in range(len(closes))]
    if name == "ma_gap_5_20":
        ma5 = _rolling_mean(closes, 5)
        ma20 = _rolling_mean(closes, 20)
        return [safe_div(ma5[i], ma20[i]) - 1.0 if ma20[i] else 0.0 for i in range(len(closes))]
    if name == "up_ratio_20":
        flags = [1.0 if (i and closes[i] > closes[i - 1]) else 0.0 for i in range(len(closes))]
        return _rolling_mean(flags, 20)
    raise ValueError(f"未知特征:{name}")


def _apply_op(op: str, stack: list[float]) -> None:
    """标量栈算子（保留给标量调用方;序列求值路径见 ``_evaluate_with_cache``）。

    唯一的标量使用点是"逐 bar 校验公式可求值性",已由 ``_rpn_arity_ok`` 的 token 级计数替代,
    因此这里只在**需要逐 token 语义文档**时保留;真正生效的算子在 ``_evaluate_with_cache``。
    """
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


def formula_grammar() -> dict[str, Any]:
    """把公式语法（原子/算子/时序 token/搜索口径）导出为**唯一可信来源**（QL06 契约同步）。

    前端契约（``packages/domain-contracts``）与分享池阶段 A 的解释器都以上述口径为准;
    测试逐 token 断言「导出的 token 都能被解释器求值、且旧 token 语义不变」。
    """
    return {
        "formula_spec_version": FORMULA_SPEC_VERSION,
        "atoms": list(FEATURE_NAMES),
        "legacy_atoms": list(LEGACY_FEATURE_NAMES),
        "binary_ops": list(BINARY_OPS),
        "unary_ops": list(UNARY_OPS),
        "time_series": {
            "bases": list(TS_WINDOW_OPS),
            "windows": list(TS_WINDOWS),
            "tokens": list(TS_TOKENS),
            "token_pattern": "^(" + "|".join(TS_WINDOW_OPS) + ")_(\\d+)$",
            "semantics": {
                "delay": "x[t−w]（不足 w 根记 0.0）",
                "delta": "x[t] − x[t−w]（不足 w 根记 0.0）",
                "ts_mean": "最近 w 根均值（头部扩张窗口）",
                "ts_std": "最近 w 根样本标准差（n−1,头部扩张窗口）",
                "ts_zscore": "(x[t] − 均值) / 样本标准差（标准差为 0 记 0.0）",
                "ts_rank": "x[t] 在最近 w 根中的百分位（并列取平均）,线性映射到 [−1,1]",
            },
        },
        "search": {
            "default": DEFAULT_SEARCH,
            "modes": list(SEARCH_MODES),
            "beam_width": BEAM_WIDTH,
            "beam_levels": BEAM_LEVELS,
            "beam_pair_pool": BEAM_PAIR_POOL,
            "exhaustive_atoms": list(LEGACY_FEATURE_NAMES),
            "exhaustive_max_depth": MAX_DEPTH,
        },
        "evaluation": {
            "label": f"close[t+{FORWARD_DAYS}]/close[t] − 1（尾部越界记缺失,不填 0）",
            "ic": "Spearman 秩相关（并列取平均秩）",
            "significance": "重叠修正 t 检验 + Benjamini-Hochberg FDR（或块置换校准）",
            "complexity_penalty": "rank_score × (1 − coef × complexity × ln(n) / n)",
        },
        "notes": [
            "公式是纯 JSON 的 token 序列（list[str]）,可用引号/编码安全地跨进程传递。",
            "新增 token 只做加法:旧公式（不含时序 token）语义与逐位结果不变。",
        ],
    }


def _rpn_arity_ok(tokens: list[str]) -> bool:
    """RPN 元数(arity)合法性:与逐 bar 栈求值的可求值性**等价**,但不建特征序列。

    栈式求值只在 bar 数 > 0 时才检查栈下溢/残留,因此旧版用 ``evaluate_tokens(tokens, [])``
    做预检是空转(全部组合都会通过),真正过滤发生在主循环里重复求值 → 18 倍无效开销。
    这里用纯 token 级元数计数替代:原子 +1、一元/时序算子需 1 且不增不减、二元需 2 且 -1,
    终态须为 1。
    """
    depth = 0
    for token in tokens:
        if token in BINARY_OPS:
            if depth < 2:
                return False
            depth -= 1
        elif token in UNARY_OPS:
            if depth < 1:
                return False
        elif parse_ts_token(token) is not None:
            if depth < 1:
                return False
        elif token in FEATURE_NAMES:
            depth += 1
        else:
            try:
                float(token)
            except ValueError:
                return False
            depth += 1
    return depth == 1


def parse_ts_token(token: str) -> tuple[str, int] | None:
    """解析时序算子 token（QL06）:``"ts_mean_20"`` → ``("ts_mean", 20)``;非时序 token 返回 None。

    语法是**单 token + 下划线窗口后缀**,因此公式仍是纯 ``list[str]``（分享池阶段 A 的
    JSON 契约形态不变,旧公式因为是旧 token 而天然向后兼容）。
    """
    base, _, suffix = token.rpartition("_")
    if base not in TS_WINDOW_OPS or not suffix.isdigit():
        return None
    window = int(suffix)
    if window <= 0:
        return None
    return base, window


def _ts_series(op: str, window: int, values: list[float], length: int) -> list[float]:
    """时序算子作用于**序列**（QL06）:只用 t 及之前的 bar,逐 bar 因果。

    - ``delay_w`` = x[t−w]（不足 w 根记 0.0）;``delta_w`` = x[t] − x[t−w]（不足记 0.0）;
    - ``ts_mean_w`` / ``ts_std_w``（**样本**标准差,n−1）/ ``ts_zscore_w`` 头部用扩张窗口;
    - ``ts_rank_w`` = x[t] 在最近 w 个值中的百分位映射到 [−1,1]（并列取平均）。
    """
    if op == "delay":
        return [values[i - window] if i >= window else 0.0 for i in range(length)]
    if op == "delta":
        return [values[i] - values[i - window] if i >= window else 0.0 for i in range(length)]
    if op == "ts_mean":
        return _rolling_mean(values, window)
    if op == "ts_std":
        return _rolling_std(values, window)
    if op == "ts_zscore":
        mean = _rolling_mean(values, window)
        std = _rolling_std(values, window)
        return [((values[i] - mean[i]) / std[i]) if std[i] > 1e-12 else 0.0 for i in range(length)]
    if op == "ts_rank":
        out: list[float] = []
        for i in range(length):
            lo = max(0, i - window + 1)
            current = values[i]
            below = 0
            ties = 0
            for value in values[lo : i + 1]:
                if value < current:
                    below += 1
                elif value == current:
                    ties += 1
            count = i - lo + 1
            out.append(2.0 * ((below + 0.5 * ties) / count) - 1.0)
        return out
    raise ValueError(f"未知时序算子:{op}")


def _feature_cache_for(tokens: list[str], bars: list[dict[str, Any]]) -> dict[str, list[float]]:
    cache: dict[str, list[float]] = {}
    for token in tokens:
        if token in FEATURE_NAMES and token not in cache:
            cache[token] = _feature_series(bars, token)
    return cache


def _evaluate_with_cache(tokens: list[str], feature_cache: dict[str, list[float]], length: int) -> list[float]:
    """**按序列**栈式求值（QL06 性能改造）:每个 token 一次遍历整条序列,而不是每 bar 一次遍历全部 token。

    与旧版逐 bar 实现**逐位等价**（同样的运算、同样的顺序、同样的除零/有限性守卫）,
    但把 Python 层的内层循环从 n×L 次解释执行压成 L 次列表推导,长窗口（750/1250 根）下
    是束搜索能在秒级跑完的前提。
    """
    stack: list[list[float]] = []
    for token in tokens:
        series = feature_cache.get(token)
        if series is not None:
            stack.append(series)
            continue
        parsed = parse_ts_token(token)
        if parsed is not None:
            if not stack:
                raise ValueError(f"栈下溢:{token}")
            stack.append(_ts_series(parsed[0], parsed[1], stack.pop(), length))
            continue
        if token in BINARY_OPS:
            if len(stack) < 2:
                raise ValueError(f"栈下溢:{token}")
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
        if token in UNARY_OPS:
            if not stack:
                raise ValueError(f"栈下溢:{token}")
            source = stack.pop()
            if token == "abs":
                stack.append([abs(value) for value in source])
            elif token == "tanh":
                stack.append([math.tanh(value) for value in source])
            else:
                stack.append([-value for value in source])
            continue
        try:
            constant = float(token)
        except ValueError as error:
            raise ValueError(f"未知 token:{token}") from error
        stack.append([constant] * length)
    if len(stack) != 1:
        raise ValueError(f"公式栈残留 {len(stack)} 项")
    return [value if math.isfinite(value) else 0.0 for value in stack[0]]


def evaluate_tokens(tokens: list[str], bars: list[dict[str, Any]]) -> list[float]:
    """对每根 bar 求因子值序列;公式非法抛 ValueError。"""
    for token in tokens:
        if (
            token not in FEATURE_NAMES
            and token not in BINARY_OPS
            and token not in UNARY_OPS
            and parse_ts_token(token) is None
        ):
            try:
                float(token)
            except ValueError as error:
                raise ValueError(f"未知 token:{token}") from error
    return _evaluate_with_cache(tokens, _feature_cache_for(tokens, bars), len(bars))


def _valid_formulas(depth: int = MAX_DEPTH):
    """按确定顺序枚举**元数合法**的 RPN 公式(长度 1..depth+1)。

    **旧口径全枚举空间**（6 原子 × 7 算子,实测 1680 条）:只用于复现历史计数与
    ``search="exhaustive"``;QL06 之后的 16 原子 + 24 时序 token 空间由束搜索负责。
    """
    alphabet = (*LEGACY_FEATURE_NAMES, *BINARY_OPS, *UNARY_OPS)
    for length in range(1, depth + 2):
        for combo in product(alphabet, repeat=length):
            tokens = list(combo)
            if _rpn_arity_ok(tokens):
                yield tokens


# --------------------------------------------------------------------------- #
# 确定性束搜索（QL06）
# --------------------------------------------------------------------------- #
def _expand_candidates(tokens: list[str]):
    """由一条完整公式扩展出更长公式（顺序固定:一元 → 时序 → 与每个原子做二元）。"""
    for op in UNARY_OPS:
        yield [*tokens, op]
    for ts_token in TS_TOKENS:
        yield [*tokens, ts_token]
    for atom in FEATURE_NAMES:
        for op in BINARY_OPS:
            yield [*tokens, atom, op]
            yield [atom, *tokens, op]


def _beam_search(
    bars: list[dict[str, Any]],
    forward: list[float | None],
    *,
    split: dict[str, Any],
    forward_days: int,
    beam_width: int = BEAM_WIDTH,
    levels: int = BEAM_LEVELS,
    pair_pool: int = BEAM_PAIR_POOL,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """确定性层式束搜索:每层按**训练段**秩 IC 选留 ``beam_width`` 条,再扩展。

    关键口径:**选择只依据 train 段**——束搜索属于「选模」,不得看 valid/test;因此
    存活者集合是一个与 test 段独立的子集,BH 校正在其 test 段 p 值上仍然有效。
    搜索本身不使用随机性（遍历顺序固定）,``seed`` 只服务于置换检验。

    返回 ``(存活者, 报告)``;存活者带已求值的因子序列,避免主流程重复求值。
    """
    train_end = int(split["train_end"])
    feature_cache = _feature_cache_for(list(FEATURE_NAMES), bars)
    bar_count = len(bars)
    train_forward = forward[:train_end]

    generated_total = 0
    evaluated_total = 0
    seen: set[tuple[str, ...]] = set()

    def score(tokens: list[str]) -> tuple[list[float], float] | None:
        """求值 + 训练段秩 IC（不做显著性检验,那是主流程的事）。"""
        nonlocal evaluated_total
        try:
            values = _evaluate_with_cache(tokens, feature_cache, bar_count)
        except ValueError:
            return None
        evaluated_total += 1
        xs, ys = quant_stats.aligned_pairs(values[:train_end], train_forward)
        ic = quant_stats.spearman(xs, ys) if len(xs) >= MIN_VALID_SAMPLES else 0.0
        return values, ic

    def select(scored: list[tuple[float, list[str], list[float]]]) -> list[tuple[float, list[str], list[float]]]:
        # 确定性排序:先 |train_IC| 降序,再复杂度升序,最后 token 序列字典序
        scored.sort(key=lambda item: (-abs(item[0]), len(item[1]), item[1]))
        return scored[:beam_width]

    # 候选池 = 各层存活者的并集（去重、保序）:主流程对**整个池**做 test 段统计与 BH 校正,
    # 而不是只对最后一层存活者——只留最后一层会把「看过多少条」缩小到束宽,弱化多重检验。
    pool: list[tuple[float, list[str], list[float]]] = []
    pool_keys: set[tuple[str, ...]] = set()

    def add_to_pool(items: list[tuple[float, list[str], list[float]]]) -> None:
        for item in items:
            key = tuple(item[1])
            if key in pool_keys:
                continue
            pool_keys.add(key)
            pool.append(item)

    # 第 0 层:全部原子
    seed_scored: list[tuple[float, list[str], list[float]]] = []
    for atom in FEATURE_NAMES:
        result = score([atom])
        if result is not None:
            seed_scored.append((result[1], [atom], result[0]))
    generated_total += len(FEATURE_NAMES)
    survivors = select(list(seed_scored))
    add_to_pool(survivors)

    for level in range(levels):
        batch: list[tuple[str, ...]] = []
        for _score, tokens, _values in survivors:
            for candidate in _expand_candidates(tokens):
                key = tuple(candidate)
                if key in seen:
                    continue
                seen.add(key)
                batch.append(key)
        # 束内两两组合:束搜索真正的搜索力来源（只取池内前 pair_pool 条,限制组合数）
        pair_sources = [tokens for _score, tokens, _values in survivors[:pair_pool]]
        for left in pair_sources:
            for right in pair_sources:
                for op in BINARY_OPS:
                    candidate = tuple([*left, *right, op])
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    batch.append(candidate)
        generated_total += len(batch)

        level_scored: list[tuple[float, list[str], list[float]]] = []
        for key in batch:
            result = score(list(key))
            if result is None:
                continue
            level_scored.append((result[1], list(key), result[0]))
        if not level_scored:
            break
        survivors = select(level_scored)
        add_to_pool(survivors)

    tested = sorted(pool, key=lambda item: (-abs(item[0]), len(item[1]), item[1]))
    report = {
        "strategy": "beam",
        "beam_width": beam_width,
        "levels": levels,
        "pair_pool": pair_pool,
        "atoms": len(FEATURE_NAMES),
        "ts_tokens": len(TS_TOKENS),
        "candidates_generated": generated_total,
        "candidates_evaluated": evaluated_total,
        "candidates_tested": len(tested),
        "selection_segment": "train",
        "randomness": "无（层式遍历顺序固定;随机性仅出现在置换检验的 seed）",
        "meaning": (
            "束搜索只在 train 段上选留候选,valid/test 不参与选择;"
            "candidates_tested 是进入多重检验校正的候选池（各层存活者并集）,"
            "candidates_generated 是实际生成并求值过的公式总数。"
        ),
    }
    return (
        [{"formula_tokens": tokens, "values": values, "train_proxy_ic": ic} for ic, tokens, values in tested],
        report,
    )


# --------------------------------------------------------------------------- #
# 标签与 IC（QL01）
# --------------------------------------------------------------------------- #
def forward_returns(closes: list[float], days: int = FORWARD_DAYS) -> list[float | None]:
    """未来 N 日收益;尾部窗口越界的样本返回 **None**（QL01a）。

    旧实现填 0.0,与真实「平盘」样本混淆,并把无效样本算进显著性分母。
    调用方用 quant_stats.aligned_pairs 过滤 None 后统计。
    """
    out: list[float | None] = []
    for i in range(len(closes)):
        j = i + days
        if j < len(closes) and closes[i]:
            out.append((closes[j] - closes[i]) / closes[i])
        else:
            out.append(None)
    return out


def rank_ic(factor_values: list[float], forward: list[float | None]) -> float:
    """秩相关(并列取平均秩);样本不足或零方差返回 0.0（QL01b）。"""
    xs, ys = quant_stats.aligned_pairs(factor_values, forward)
    if len(xs) < MIN_VALID_SAMPLES:
        return 0.0
    return quant_stats.spearman(xs, ys)


def ic_statistics(factor_values: list[float], forward: list[float | None], *, overlap: int) -> dict[str, Any]:
    """IC + 重叠修正 t 值 / p 值（QL01c）;样本不足时 IC 归零、p 归 1。"""
    stat = quant_stats.ic_statistics(factor_values, forward, overlap=overlap, min_samples=MIN_VALID_SAMPLES)
    if stat["samples"] < MIN_VALID_SAMPLES:
        stat = {**stat, "ic": 0.0, "t_stat": None, "p_value": 1.0}
    return stat


# --------------------------------------------------------------------------- #
# 三段式切分（QL02）
# --------------------------------------------------------------------------- #
def three_way_split(n: int, forward_days: int = FORWARD_DAYS, *, min_segment: int = MIN_SEGMENT_SAMPLES) -> dict[str, Any]:
    """train / valid / test 三段切分（QL02）;比例按可用样本量自适应。

    只在**有有效标签**的 bar 上分配长度（可用样本 = n − forward_days,尾部窗口越界无标签）。
    若按 50/25/25 切分后 valid 或 test 段不足 min_segment,退化为三段等分并标记 degraded。
    """
    usable = max(0, n - forward_days)
    ratios = (SPLIT_TRAIN_RATIO, SPLIT_VALID_RATIO)
    train_len = int(usable * ratios[0])
    valid_len = int(usable * ratios[1])
    test_len = usable - train_len - valid_len
    degraded = False
    fallback_reason = None
    if valid_len < min_segment or test_len < min_segment or train_len < min_segment:
        third = usable // 3
        train_len = valid_len = third
        test_len = usable - 2 * third
        degraded = True
        fallback_reason = (
            f"样本量 {n} 不足以按 {int(ratios[0]*100)}/{int(ratios[1]*100)}/25 切分"
            f"（每段至少 {min_segment} 根）,已退化为三段等分"
        )
    train_end = train_len
    valid_end = train_len + valid_len
    return {
        "ratio": [ratios[0], ratios[1], 1.0 - ratios[0] - ratios[1]],
        "train_end": train_end,
        "valid_end": valid_end,
        "usable_samples": usable,
        "planned": {"train": train_len, "valid": valid_len, "test": test_len},
        "degraded": degraded,
        "degraded_reason": fallback_reason,
        "min_segment_samples": min_segment,
    }


def segment_views(values: list[float], forward: list[float | None], split: dict[str, Any]) -> dict[str, tuple[list[float], list[float | None]]]:
    """按切分索引取出三段 (因子值, 标签) 视图。"""
    train_end = int(split["train_end"])
    valid_end = int(split["valid_end"])
    return {
        "train": (values[:train_end], forward[:train_end]),
        "valid": (values[train_end:valid_end], forward[train_end:valid_end]),
        "test": (values[valid_end:], forward[valid_end:]),
    }


# --------------------------------------------------------------------------- #
# 因子评估套件（QL05）
# --------------------------------------------------------------------------- #
def factor_evaluation(
    values: list[float],
    closes: list[float],
    *,
    forward_days: int = FORWARD_DAYS,
) -> dict[str, Any]:
    """IC 评估套件:逐期 IC/ICIR、IC 衰减、因子自相关、分位分组收益（时序口径）。

    全部指标确定性、可复算;口径版本 IC_SUITE_SPEC_VERSION,挂进挖掘响应与制品 metrics。
    """
    usable = max(0, len(values) - forward_days)
    folds = max(0, min(10, usable // PERIOD_IC_MIN_FOLD_SAMPLES))
    forward_main = forward_returns(closes, forward_days)
    horizons = set(IC_DECAY_HORIZONS) | {forward_days}
    forwards_by_horizon = {h: forward_returns(closes, h) for h in sorted(horizons)}
    return {
        "spec_version": IC_SUITE_SPEC_VERSION,
        "forward_days": forward_days,
        "samples": len(values),
        "period_ic": quant_stats.period_ic_series(values, forward_main, folds=folds),
        "ic_decay": quant_stats.ic_decay(values, forwards_by_horizon),
        "autocorrelation": {
            "lag_1": quant_stats.rank_autocorrelation(values, lag=1),
            f"lag_{forward_days}": quant_stats.rank_autocorrelation(values, lag=forward_days),
        },
        "group_returns": quant_stats.quantile_group_returns(values, forward_main, groups=GROUP_RETURN_GROUPS),
    }


def _rounded_suite(suite: dict[str, Any]) -> dict[str, Any]:
    """把评估套件四舍五入成可入制品/响应的稳定形态（确定性）。"""

    def r(value: Any, digits: int = 4) -> Any:
        return round(value, digits) if isinstance(value, float) else value

    period = suite["period_ic"]
    group = suite["group_returns"]
    return {
        "spec_version": suite["spec_version"],
        "forward_days": suite["forward_days"],
        "samples": suite["samples"],
        "period_ic": {
            "folds": period["folds"],
            "fold_samples": period.get("fold_samples"),
            "series": [r(v) for v in period["series"]],
            "mean": r(period["mean"]),
            "std": r(period["std"]),
            "icir": r(period["icir"]),
            "positive_ratio": r(period["positive_ratio"], 6),
            **({"degraded_reason": period["degraded_reason"]} if "degraded_reason" in period else {}),
        },
        "ic_decay": [dict(point) for point in suite["ic_decay"]],
        "autocorrelation": {k: r(v) for k, v in suite["autocorrelation"].items()},
        "group_returns": {
            "groups": group["groups"],
            "group_samples": group.get("group_samples"),
            "mean_forward_return": [r(v, 6) for v in group["mean_forward_return"]],
            "spread_top_bottom": r(group["spread_top_bottom"], 6),
        },
    }


# --------------------------------------------------------------------------- #
# 泄漏检查（QL04）
# --------------------------------------------------------------------------- #
def lookahead_check(tokens: list[str], bars: list[dict[str, Any]], *, probes: int = 4) -> dict[str, Any]:
    """对单条公式跑未来数据泄漏检查（复用 quant_experiments.detect_lookahead_bias）。"""
    from investment_steward_core.quant_experiments import detect_lookahead_bias

    def score_fn(candidate_bars: list[dict[str, Any]]) -> list[float]:
        return evaluate_tokens(tokens, candidate_bars)

    if len(bars) < 36:
        return {"uses_future_data": None, "detail": f"样本不足:{len(bars)} < 36,未做泄漏检查",
                "checked_points": [], "mismatch_count": 0, "mismatches": []}
    report = detect_lookahead_bias(score_fn, bars, probes=probes)
    return {
        "uses_future_data": report["uses_future_data"],
        "checked_points": report["checked_points"],
        "mismatch_count": report["mismatch_count"],
        "mismatches": report["mismatches"],
        "detail": report["detail"],
    }


# --------------------------------------------------------------------------- #
# 挖掘主流程（QL02/QL03/QL04）
# --------------------------------------------------------------------------- #
def _sign_consistency(train_ic: float, valid_ic: float, test_ic: float) -> float:
    """逐段 IC 与 test 段同号比例（test 为 0 时记 0.0）。"""
    if test_ic == 0.0:
        return 0.0
    reference = 1.0 if test_ic > 0 else -1.0
    matches = sum(1 for ic in (train_ic, valid_ic, test_ic) if ic != 0.0 and (1.0 if ic > 0 else -1.0) == reference)
    return matches / 3.0


def _complexity_shrink(complexity: int, samples: int, coefficient: float = COMPLEXITY_PENALTY_COEF) -> float:
    """公式复杂度惩罚（QL06,AIC/BIC 式）:每观测惩罚 = coef × 长度 × ln(n) / n。

    返回乘在排序键上的收缩系数（1.0 表示不惩罚）。截断到 ≥ 0,过长公式不会因为符号翻转
    反而被抬高。
    """
    if coefficient <= 0.0 or samples <= 1:
        return 1.0
    penalty = coefficient * complexity * math.log(samples) / samples
    return max(0.0, 1.0 - penalty)


def mine(
    bars: list[dict[str, Any]],
    *,
    depth: int = 3,
    top_n: int = 5,
    forward_days: int = FORWARD_DAYS,
    alpha: float = quant_stats.DEFAULT_ALPHA,
    correction: str = "fdr",
    seed: int = quant_stats.DEFAULT_SEED,
    permutations: int = quant_stats.DEFAULT_PERMUTATIONS,
    min_train_ic: float = DEFAULT_MIN_TRAIN_IC,
    min_valid_ic: float = DEFAULT_MIN_VALID_IC,
    check_lookahead: bool = True,
    marginal_n: int = 5,
    label_shuffle_seed: int | None = None,
    dedup_correlation: float = DEDUP_CORRELATION_THRESHOLD,
    search: str = DEFAULT_SEARCH,
    beam_width: int = BEAM_WIDTH,
    beam_levels: int = BEAM_LEVELS,
    beam_pair_pool: int = BEAM_PAIR_POOL,
) -> dict[str, Any]:
    """搜索公式空间,三段式切分 + 多重检验校正 + 相关性去重,输出 top-N 参数集（确定性）。

    - 搜索:``search="beam"``（默认,QL06:16 原子 + 24 时序 token 的层式确定性束搜索,
      只在 train 段选留）或 ``"exhaustive"``（旧口径 6 原子 × 7 算子全枚举,用于复现历史计数）;
    - 排序键:``|test_ic| × 逐段同号一致性 × 复杂度收缩``（test 段只用于确认一次,不参与选模）;
    - 校正:``correction="fdr"``（BH,作用于候选池的 test 段 p 值）或
      ``"permutation"``（块置换校准零分布 + BH,固定 seed）;
    - 去重:排名相邻的公式若 |Spearman| > ``dedup_correlation`` 归入同一簇,
      每簇只留排序最优者,簇内被合并的公式在 ``cluster.merged_formulas`` 里列明（QL08）;
    - 榜单 ``top`` 只含校正后显著者;未达显著者进 ``marginal`` 并显式标注（不抹掉、不假装）;
    - ``label_shuffle_seed`` 给出时先把标签做确定性块置换再跑整条链路（过拟合自检:
      随机标签下榜单应为空;seed 写进响应,可复算）。
    """
    if correction not in ("fdr", "permutation"):
        raise ValueError(f"correction 仅支持 'fdr' / 'permutation',收到 {correction!r}")
    if search not in SEARCH_MODES:
        raise ValueError(f"search 仅支持 {' / '.join(SEARCH_MODES)},收到 {search!r}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha 必须在 (0,1),收到 {alpha}")
    if beam_width < 1 or beam_levels < 0:
        raise ValueError(f"beam 参数非法:beam_width={beam_width}, beam_levels={beam_levels}")

    closes = [float(bar["close"]) for bar in bars]
    forward = forward_returns(closes, forward_days)
    label_shuffle: dict[str, Any] | None = None
    if label_shuffle_seed is not None:
        forward = quant_stats.shuffle_labels(forward, seed=int(label_shuffle_seed), block=forward_days)
        label_shuffle = {
            "seed": int(label_shuffle_seed),
            "block": forward_days,
            "meaning": "标签已随机置换(过拟合自检):榜单应为空;非空即说明统计口径能从噪声里造出显著性",
        }
    split = three_way_split(len(bars), forward_days)
    train_end = int(split["train_end"])
    valid_end = int(split["valid_end"])

    enumerated = 0
    evaluated = 0
    rows: list[dict[str, Any]] = []
    # 全部特征序列只算一次,跨公式复用
    feature_cache = _feature_cache_for(list(FEATURE_NAMES), bars)
    bar_count = len(bars)
    # 候选池的 test 段统计量,BH 校正的分母
    tested_stats: list[dict[str, Any]] = []
    # 置换校准的候选池（exhaustive 用等距抽样覆盖枚举顺序;beam 用束存活者全集）
    calibration_values: list[list[float]] = []

    if search == "exhaustive":
        candidate_iter = ((tokens, None) for tokens in _valid_formulas(depth))
        search_report: dict[str, Any] = {
            "strategy": "exhaustive",
            "max_depth": depth,
            "atoms": list(LEGACY_FEATURE_NAMES),
            "randomness": "无（字典序枚举）",
            "selection_segment": "train+valid（旧口径,排序键只由 test 段决定）",
            "meaning": "旧口径全枚举空间,只用于复现历史计数;新挖掘默认走 beam。",
        }
    else:
        survivors, search_report = _beam_search(
            bars,
            forward,
            split=split,
            forward_days=forward_days,
            beam_width=beam_width,
            levels=beam_levels,
            pair_pool=beam_pair_pool,
        )
        candidate_iter = ((item["formula_tokens"], item["values"]) for item in survivors)

    for tokens, cached_values in candidate_iter:
        enumerated += 1
        if cached_values is None:
            try:
                values = _evaluate_with_cache(tokens, feature_cache, bar_count)
            except ValueError:
                continue
        else:
            values = cached_values
        if correction == "permutation" and (
            search == "beam" or evaluated % CALIBRATION_STRIDE == 0
        ):
            calibration_values.append(values[valid_end:])
        evaluated += 1
        train_stat = ic_statistics(values[:train_end], forward[:train_end], overlap=forward_days)
        valid_stat = ic_statistics(values[train_end:valid_end], forward[train_end:valid_end], overlap=forward_days)
        test_stat = ic_statistics(values[valid_end:], forward[valid_end:], overlap=forward_days)
        if test_stat["samples"] >= MIN_VALID_SAMPLES:
            tested_stats.append(test_stat)
        train_ic = train_stat["ic"]
        valid_ic = valid_stat["ic"]
        if abs(train_ic) < min_train_ic or abs(valid_ic) < min_valid_ic:
            continue
        test_ic = test_stat["ic"]
        consistency = _sign_consistency(train_ic, valid_ic, test_ic)
        shrink = _complexity_shrink(len(tokens), test_stat["samples"])
        rows.append({
            "formula_tokens": tokens,
            "formula": " ".join(tokens),
            "complexity": len(tokens),
            "train_ic": round(train_ic, 4),
            "valid_ic": round(valid_ic, 4),
            "test_ic": round(test_ic, 4),
            "sign_consistency": round(consistency, 6),
            "rank_score": round(abs(test_ic) * consistency, 10),
            "complexity_shrink": round(shrink, 10),
            "rank_score_adjusted": round(abs(test_ic) * consistency * shrink, 10),
            "samples": len(bars),
            "segment_samples": {
                "train": train_stat["samples"],
                "valid": valid_stat["samples"],
                "test": test_stat["samples"],
            },
            "test_effective_samples": test_stat["effective_samples"],
            "test_t_stat": round(test_stat["t_stat"], 4) if test_stat["t_stat"] is not None else None,
            "test_p_value": round(test_stat["p_value"], 8),
            "test_significant": False,
            "_test_index": len(tested_stats) - 1,
            "_values": values,
        })

    rows.sort(key=lambda item: (-item["rank_score_adjusted"], item["complexity"], item["formula"]))

    # —— 多重检验校正（QL03）——
    p_values = [stat["p_value"] for stat in tested_stats]
    correction_report: dict[str, Any] = {
        "method": correction,
        "alpha": alpha,
        "seed": seed,
        "candidates_total": (
            enumerated if search == "exhaustive" else search_report["candidates_generated"]
        ),
        "candidates_evaluated": (
            evaluated if search == "exhaustive" else search_report["candidates_evaluated"]
        ),
        "candidates_tested": len(p_values),
        "passed_screen": len(rows),
    }
    if correction == "fdr":
        rejected = quant_stats.benjamini_hochberg(p_values, alpha=alpha)
        rejected_indexes = {index for index, flag in enumerate(rejected) if flag}
        for row in rows:
            row["test_significant"] = row["_test_index"] in rejected_indexes
        correction_report["rejected_total"] = len(rejected_indexes)
        correction_report["threshold"] = round(quant_stats.bh_threshold(p_values, alpha=alpha), 10)
    else:
        # 置换校准:用块置换估计零分布尺度 σ̂,再对**全部候选**做 BH（避免只看头部候选的选择偏差）
        calibration = quant_stats.null_scale_by_permutation(
            calibration_values,
            forward[valid_end:],
            overlap=forward_days,
            permutations=permutations,
            seed=seed,
        )
        pool = calibration.get("abs_pool_ascending")
        if pool:
            # 合取(取较大 p):解析 p 是模型侧保守界;经验池 p 抓肥尾但忽略候选间异方差
            # (并列结构不同的因子零分布宽度不同)。两者都要求通过才判显著。
            effective_p = [
                max(stat["p_value"], quant_stats.p_value_from_null_pool(stat["ic"], pool))
                for stat in tested_stats
            ]
        else:
            effective_p = list(p_values)
        rejected = quant_stats.benjamini_hochberg(effective_p, alpha=alpha)
        rejected_indexes = {index for index, flag in enumerate(rejected) if flag}
        for row in rows:
            row["test_significant"] = row["_test_index"] in rejected_indexes
            row["test_p_value"] = round(effective_p[row["_test_index"]], 8)
        correction_report |= {
            "correction": "benjamini_hochberg",
            "permutations": permutations,
            "calibration": {k: v for k, v in calibration.items() if k != "abs_pool_ascending"},
            "calibration_stride": CALIBRATION_STRIDE,
            "p_resolution": round(1.0 / (len(pool) + 1), 12) if pool else None,
            "rejected_total": len(rejected_indexes),
            "threshold": round(quant_stats.bh_threshold(effective_p, alpha=alpha), 10),
        }

    significant_rows = [row for row in rows if row["test_significant"]]
    marginal_rows = [row for row in rows if not row["test_significant"]]

    # —— top-N 相关性去重（QL08）——
    scan = significant_rows[:DEDUP_SCAN_LIMIT]
    dedup = quant_stats.greedy_decorrelate(
        [row["_values"] for row in scan], threshold=dedup_correlation, keep=top_n
    )
    top = [scan[index] for index in dedup["kept"]]
    def cluster_view(cluster: dict[str, Any]) -> dict[str, Any]:
        merged = [scan[index]["formula"] for index in cluster["merged"]]
        view = {
            "size": 1 + len(merged),
            "merged_formulas": merged[:DEDUP_MERGED_SAMPLE],
            "max_abs_correlation": round(cluster["max_abs_correlation"], 4),
        }
        if len(merged) > DEDUP_MERGED_SAMPLE:
            view["merged_truncated"] = True
            view["merged_sample_size"] = DEDUP_MERGED_SAMPLE
        return view

    for cluster in dedup["clusters"]:
        scan[cluster["representative"]]["_cluster"] = cluster_view(cluster)
    dedup_report = {
        "threshold": dedup_correlation,
        "scanned": dedup["scanned"],
        "scan_limit": DEDUP_SCAN_LIMIT,
        "clusters": [
            {"representative": scan[cluster["representative"]]["formula"], **cluster_view(cluster)}
            for cluster in dedup["clusters"]
        ],
        "merged_total": sum(len(cluster["merged"]) for cluster in dedup["clusters"]),
    }
    marginal = [row for row in marginal_rows[:marginal_n]]

    def present(row: dict[str, Any], *, with_suite: bool) -> dict[str, Any]:
        item = {k: v for k, v in row.items() if k not in ("_values", "_test_index")}
        item["cluster"] = item.pop("_cluster", None)
        if not item["test_significant"]:
            item["significance_note"] = "test 段 IC 未达统计显著（多重检验校正后），不可当作有效因子"
        else:
            item["significance_note"] = None
        if with_suite:
            item["evaluation"] = _rounded_suite(
                factor_evaluation(row["_values"], closes, forward_days=forward_days)
            )
            item["lookahead"] = lookahead_check(row["formula_tokens"], bars) if check_lookahead else None
        return item

    top_view = [present(row, with_suite=True) for row in top]
    marginal_view = [present(row, with_suite=False) for row in marginal]

    notes: list[str] = [
        "三段式切分(train 选模/valid 调阈值/test 只确认一次):排序键 = "
        "|test_IC| × 逐段同号一致性 × 复杂度收缩(AIC/BIC 式);"
        "榜单仅含多重检验校正后显著者,未达显著者在 marginal 中显式标注。",
        f"IC 为秩相关(并列取平均秩),t 值按重叠窗口折算有效样本数(≈n/{forward_days});"
        "仅作研究背景,不构成买卖建议。",
    ]
    if search == "beam":
        notes.append(
            f"搜索:确定性束搜索(束宽 {beam_width},层数 {beam_levels}),"
            f"生成 {search_report['candidates_generated']} 条、求值 {search_report['candidates_evaluated']} 条,"
            f"只在 train 段选留 {search_report['candidates_tested']} 条进入显著性校正;搜索本身无随机性。"
        )
    else:
        notes.append(
            f"搜索:旧口径全枚举(depth={depth},{len(LEGACY_FEATURE_NAMES)} 原子),"
            "只用于复现历史计数,不参与新挖掘默认路径。"
        )
    if correction == "fdr":
        notes.append(
            f"FDR(BH)校正:候选 {correction_report['candidates_tested']} 条,"
            f"通过 {correction_report.get('rejected_total', 0)} 条,阈值 {correction_report.get('threshold')}。"
        )
    else:
        notes.append(
            f"块置换检验:块长 {forward_days},{permutations} 次置换,seed={seed},固定种子可复算。"
        )
    if split["degraded"]:
        notes.append(str(split["degraded_reason"]))
    if dedup_report["merged_total"]:
        notes.append(
            f"相关性去重(|Spearman| > {dedup_correlation}):合并 {dedup_report['merged_total']} 条同簇变体,"
            "簇内被合并的公式在各行 cluster.merged_formulas 中列明。"
        )

    return {
        "top": top_view,
        "marginal": marginal_view,
        "searched": True,
        "forward_days": forward_days,
        "mine_spec_version": MINE_SPEC_VERSION,
        "formula_spec_version": FORMULA_SPEC_VERSION,
        "ic_spec_version": quant_stats.IC_SPEC_VERSION,
        "ic_suite_spec_version": IC_SUITE_SPEC_VERSION,
        "split": split,
        "search": search_report,
        "multiple_testing": correction_report,
        "dedup": dedup_report,
        "thresholds": {"min_train_ic": min_train_ic, "min_valid_ic": min_valid_ic},
        "label_shuffle": label_shuffle,
        "grammar": formula_grammar(),
        "note": " ".join(notes),
    }
