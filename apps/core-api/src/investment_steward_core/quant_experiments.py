"""本地量化实验（路线图 §7.2 / 阶段 3+7）。

设计约束：
- 确定性：同一实验配置 + 同一数据快照 → 结果逐位一致，结果哈希可复算。
- 口径冻结：SPEC v2 在 v1（手续费单边 bps、滑点 bps、仓位上限、换手上限）之上
  补齐停牌（volume<=0 冻结）、涨跌停（按前一日涨幅判定，禁止逆限价方向开仓）、
  调仓周期（rebalance_every）、复权因子（adj_factors，默认数据源已前复权）；
  收益按「今日收盘建仓、次日收盘结算」的闭 K 线口径，与 quant_pool.replay 一致。
- 样本外：70/30 拆分真实分段统计（train / out_of_sample 各自权益与回撤），不再只是元数据。
- 数据快照：参与实验的 K 线按 canonical JSON 落制品目录（内容寻址），
  元数据只登记 SHA-256；快照哈希写进实验记录。
- 基线比较：每次实验同时输出买入持有基线，避免只看绝对收益。
- 复现：reproduce 用登记的快照哈希重跑并比对结果哈希，不一致即报出（不静默）；
  SPEC v1 旧实验按 v1 口径复现（legacy_v1），保证历史登记不因升级失真。
- 泄漏检查：detect_lookahead_bias（全量打分 vs 截断打分逐点比对）+
  check_disclosure_alignment（披露日错位报告）。
- 组合回测：见 quant_portfolio.py（权重/行业上限 + 组合风险指标）。
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_steward_core.storage import artifact_store
from investment_steward_core.storage.database import Database
from investment_steward_core.storage.paths import StorageLayout

SNAPSHOT_KIND = "data_snapshots"

# 回测口径冻结（改动任何一项都必须递增 BACKTEST_SPEC_VERSION）
BACKTEST_SPEC_VERSION = 2
COMMISSION_BPS = 2.5      # 单边手续费
SLIPPAGE_BPS = 5.0        # 单边滑点
MAX_POSITION = 1.0        # 仓位上限（tanh 输出再截断）
MAX_TURNOVER_PER_BAR = 1.0  # 单日换手上限（仓位变动绝对值）
REBALANCE_EVERY = 1       # 调仓周期（每 N 根 K 线应用一次信号）
PRICE_LIMIT_TOLERANCE = 0.995  # 涨跌停判定容差（防浮点误判）
OOS_RATIO = 0.7           # 训练/样本外拆分比例（前 70% 为 train）


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_bars(layout: StorageLayout, symbol: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    """K 线快照落制品目录（内容寻址），返回登记信息。"""
    payload = {"symbol": symbol, "bar_count": len(bars), "bars": bars}
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    file_name = f"{digest}.json"
    _, content_hash = artifact_store.save_artifact(layout, SNAPSHOT_KIND, file_name, payload)
    as_of = str(bars[-1]["timestamp"])[:10] if bars else None
    return {
        "snapshot_hash": digest,
        "file_name": file_name,
        "content_hash": content_hash,
        "symbol": symbol,
        "bar_count": len(bars),
        "as_of": as_of,
    }


def load_snapshot(layout: StorageLayout, content_hash: str, file_name: str) -> list[dict[str, Any]]:
    payload = artifact_store.read_artifact(layout, SNAPSHOT_KIND, file_name, content_hash)
    bars = payload.get("bars")
    if not isinstance(bars, list):
        raise ValueError("数据快照结构异常:缺少 bars")
    return bars


def _cost_factor(side_bps: float) -> float:
    """单边成本（bps）转净系数：买入 (1+slip)/(1+fee) 的简化线性近似,
    冻结在 SPEC v1:成本按价格比例直接扣减。"""
    return side_bps / 10_000.0


def default_price_limit(symbol: str) -> float | None:
    """A 股涨跌停幅度启发式：688/300 开头 20%，其余 10%；非 A 股形态返回 None（不限）。"""
    code = symbol.split(".")[0]
    if code.startswith(("688", "300")):
        return 0.20
    if len(code) == 6 and code.isdigit():
        return 0.10
    return None


def _segment_stats(curve: list[dict[str, Any]], start: int, end: int) -> dict[str, Any]:
    """曲线区间 [start, end) 的分段统计：权益、最大回撤、胜率、天数。"""
    segment = curve[start:end]
    if not segment:
        return {"bars": 0, "equity": 1.0, "max_drawdown": 0.0, "win_rate": None}
    equity = 1.0
    peak = 1.0
    max_dd = 1.0
    wins = 0
    for point in segment:
        equity *= 1.0 + point["return"]
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, equity / peak)
        if point["return"] > 0:
            wins += 1
    return {
        "bars": len(segment),
        "equity": round(equity, 8),
        "max_drawdown": round(1.0 - max_dd, 6),
        "win_rate": round(wins / len(segment), 6),
    }


def run_backtest(values: list[float], closes: list[float], *, commission_bps: float = COMMISSION_BPS,
                 slippage_bps: float = SLIPPAGE_BPS, max_position: float = MAX_POSITION,
                 max_turnover: float = MAX_TURNOVER_PER_BAR, rebalance_every: int = REBALANCE_EVERY,
                 price_limit_pct: float | None = None, volumes: list[float] | None = None,
                 adj_factors: list[float] | None = None, legacy_v1: bool = False) -> dict[str, Any]:
    """确定性成本口径回测：tanh 仓位（截断+换手约束）× 次日收益 − 双边成本。

    交易时点口径（SPEC v2,冻结）:今日信号按今日收盘价执行（含滑点）,
    持有至次日收盘结算;仓位变动超过换手上限时截断到允许幅度。
    v2 增量（全部因果,无未来数据）:
    - 停牌:volumes[i]<=0 视为停牌,当日不交易、次日收益冻结（净值不动）。
    - 涨跌停:bar i 自身涨跌幅触及 price_limit_pct（含容差）时,收盘不可逆限价方向开仓
      （涨停禁买入、跌停禁卖出;只许减仓方向）。
    - 调仓:仅当 i % rebalance_every == 0 应用信号,其余 bar 持仓不动。
    - 复权:adj_factors 给出时收益与基线按 close*factor 计算;默认数据源已前复权(qfq)。
    legacy_v1=True 时按 SPEC v1 语义输出（用于历史实验逐位复现）。
    """
    fee = _cost_factor(commission_bps)
    slip = _cost_factor(slippage_bps)
    if legacy_v1:
        spec_version = 1
        rebalance_every = 1
        price_limit_pct = None
        volumes = None
        adj_factors = None
    else:
        spec_version = BACKTEST_SPEC_VERSION
    if rebalance_every < 1:
        raise ValueError(f"rebalance_every 必须 >= 1,收到 {rebalance_every}")
    if price_limit_pct is not None and not (0 < price_limit_pct <= 0.5):
        raise ValueError(f"price_limit_pct 必须在 (0, 0.5],收到 {price_limit_pct}")

    eff = [c * (adj_factors[i] if adj_factors and i < len(adj_factors) else 1.0)
           for i, c in enumerate(closes)]
    suspended = [bool(volumes and i < len(volumes) and volumes[i] <= 0) for i in range(len(closes))]

    equity = 1.0
    baseline = 1.0
    prev_position = 0.0
    returns: list[float] = []
    curve: list[dict[str, Any]] = []
    turnovers: list[float] = []
    limit_blocked = 0
    suspended_bars = 0
    for i in range(len(closes) - 1):
        prev_eff, next_eff = eff[i], eff[i + 1]
        gross = (next_eff - prev_eff) / prev_eff if prev_eff else 0.0
        if suspended[i + 1] or suspended[i]:
            # 停牌:净值与基线冻结,持仓不动,不产生收益记录
            suspended_bars += 1
            curve.append({"step": i, "position": round(prev_position, 6), "cost": 0.0,
                          "return": 0.0, "equity": round(equity, 8), "suspended": True})
            continue
        baseline *= 1.0 + gross
        raw = math.tanh(values[i]) if i < len(values) else 0.0
        target = max(-max_position, min(max_position, raw))
        delta = target - prev_position
        if abs(delta) > max_turnover:
            delta = max_turnover if delta > 0 else -max_turnover
        if i % rebalance_every != 0:
            delta = 0.0
        if delta != 0.0 and price_limit_pct is not None and i > 0 and eff[i - 1]:
            bar_move = (eff[i] - eff[i - 1]) / eff[i - 1]
            if abs(bar_move) >= price_limit_pct * PRICE_LIMIT_TOLERANCE:
                # 涨停禁买、跌停禁卖;只允许减仓/平仓方向
                if (bar_move > 0 and delta > 0) or (bar_move < 0 and delta < 0):
                    delta = 0.0
                    limit_blocked += 1
        position = prev_position + delta
        cost = abs(delta) * (fee + slip)
        day_return = position * gross - cost
        equity *= 1.0 + day_return
        returns.append(day_return)
        turnovers.append(round(abs(delta), 6))
        prev_position = position
        curve.append({"step": i, "position": round(position, 6), "cost": round(cost, 6),
                      "return": round(day_return, 8), "equity": round(equity, 8)})
    wins = sum(1 for r in returns if r > 0)
    max_dd = 1.0
    peak = 1.0
    for point in curve:
        peak = max(peak, point["equity"])
        if peak > 0:
            max_dd = min(max_dd, point["equity"] / peak)
    result: dict[str, Any] = {
        "spec_version": str(spec_version),
        "costs": {"commission_bps": commission_bps, "slippage_bps": slippage_bps,
                  "max_position": max_position, "max_turnover_per_bar": max_turnover},
        "equity": round(equity, 8),
        "baseline_buy_hold": round(baseline, 8),
        "excess_vs_baseline": round(equity - baseline, 8),
        "win_rate": round(wins / len(returns), 6) if returns else None,
        "max_drawdown": round(1.0 - max_dd, 6),
        "total_turnover": round(sum(turnovers), 6),
        "bars": len(returns),
        "curve": curve,
    }
    if legacy_v1:
        # 严格 v1 键集与类型（历史实验逐位复现;旧记录 spec_version 为 int 1）
        result["spec_version"] = 1
        return result
    result["costs"] = result["costs"] | {"rebalance_every": rebalance_every,
                                         "price_limit_pct": price_limit_pct}
    result["suspension"] = {"suspended_bars": suspended_bars,
                            "limit_blocked_entries": limit_blocked,
                            "adjust": "adj_factors" if adj_factors else "qfq(source)"}
    split = int(len(curve) * OOS_RATIO)
    result["splits"] = {"ratio": OOS_RATIO,
                        "train": _segment_stats(curve, 0, split),
                        "out_of_sample": _segment_stats(curve, split, len(curve))}
    return result


def _software_versions() -> dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "backtest_spec_version": str(BACKTEST_SPEC_VERSION),
    }


def _experiment_id(snapshot_hash: str, config: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        _canonical({"snapshot": snapshot_hash, "config": config}).encode("utf-8")
    ).hexdigest()
    return f"exp-{digest[:16]}"


def run_experiment(
    db: Database,
    layout: StorageLayout,
    user_id: UUID,
    bars: list[dict[str, Any]],
    *,
    symbol: str,
    score_fn_name: str,
    score_fn,
    artifact_id: str = "",
    label: str = "",
    rebalance_every: int = REBALANCE_EVERY,
    price_limit_pct: float | None | str = "auto",
) -> dict[str, Any]:
    """运行实验:快照 → 打分 → 成本口径回测 → 结果哈希登记。

    config 冻结 score_fn 名与 artifact_id(复现时据此重建打分函数);
    score_fn 为当前代码内的确定性打分函数(如公式求值/线性打分)。
    price_limit_pct="auto" 时按 default_price_limit(symbol) 推断(688/300→20%,其余 A 股→10%,
    非A股形态→None 不限);显式传 float/None 则冻结显式值。
    """
    if len(bars) < 35:
        raise ValueError(f"样本不足:回测至少需要 35 根 K 线,收到 {len(bars)}")
    resolved_limit: float | None
    if isinstance(price_limit_pct, str):
        if price_limit_pct != "auto":
            raise ValueError(f"price_limit_pct 仅支持 'auto' 或数值/None,收到 {price_limit_pct!r}")
        resolved_limit = default_price_limit(symbol)
    else:
        resolved_limit = price_limit_pct
    volumes = [float(bar.get("volume") or 0.0) for bar in bars]
    config = {"score_fn": score_fn_name, "symbol": symbol, "artifact_id": artifact_id,
              "rebalance_every": rebalance_every, "price_limit_pct": resolved_limit}
    snap = snapshot_bars(layout, symbol, bars)
    experiment_id = _experiment_id(snap["snapshot_hash"], config)
    existing = db.get_quant_experiment(experiment_id)
    if existing is not None:
        # 同快照同配置幂等返回已登记结果（内容寻址,不重复计算）
        return existing["result"] | {
            "experiment_id": experiment_id,
            "data_snapshot": existing["data_snapshot"],
            "result_hash": existing["result_hash"],
        }

    values = score_fn(bars)
    closes = [float(bar["close"]) for bar in bars]
    split = int(len(closes) * 0.7)
    result = run_backtest(values, closes, rebalance_every=rebalance_every,
                          price_limit_pct=resolved_limit, volumes=volumes)
    result["split"] = {"train_bars": split, "out_of_sample_bars": len(closes) - 1 - split}
    result_hash = hashlib.sha256(_canonical(result).encode("utf-8")).hexdigest()
    record = {
        "experiment_id": experiment_id,
        "symbol": symbol,
        "label": label,
        "config": config,
        "data_snapshot": {k: snap[k] for k in ("snapshot_hash", "file_name", "content_hash", "bar_count", "as_of")},
        "software": _software_versions(),
        "result_hash": result_hash,
        "result": {k: v for k, v in result.items() if k != "curve"},
        "created_at": datetime.now(UTC).isoformat(),
        "user_id": str(user_id),
    }
    db.insert_quant_experiment(record)
    return record["result"] | {"experiment_id": experiment_id, "data_snapshot": record["data_snapshot"],
                               "result_hash": result_hash}


def reproduce_experiment(
    db: Database,
    layout: StorageLayout,
    experiment_id: str,
    score_builder: Any,
) -> dict[str, Any]:
    """复现:从登记的数据快照重跑,逐位比对结果哈希。不一致抛 ValueError。

    score_builder(config) -> 打分函数(或 None 表示无法重建,如代码已变更)。
    """
    record = db.get_quant_experiment(experiment_id)
    if record is None:
        raise ValueError(f"实验不存在:{experiment_id}")
    stored = record["result"]
    snap = record["data_snapshot"]
    bars = load_snapshot(layout, snap["content_hash"], snap["file_name"])
    config = record["config"]
    score_fn = score_builder(config)
    if score_fn is None:
        raise ValueError(f"打分函数 {config['score_fn']} 无法重建(制品缺失或代码已变更),无法复现")
    values = score_fn(bars)
    closes = [float(bar["close"]) for bar in bars]
    # SPEC v1 历史记录按 v1 口径逐位复现;v2 记录带停牌/涨跌停/调仓参数重跑
    legacy = str(record["software"].get("backtest_spec_version")) == "1"
    volumes = [float(bar.get("volume") or 0.0) for bar in bars]
    rerun = run_backtest(
        values, closes,
        rebalance_every=config.get("rebalance_every", REBALANCE_EVERY) if not legacy else 1,
        price_limit_pct=config.get("price_limit_pct") if not legacy else None,
        volumes=volumes if not legacy else None,
        legacy_v1=legacy,
    )
    rerun["split"] = stored.get("split", {})
    rerun_hash = hashlib.sha256(_canonical(rerun).encode("utf-8")).hexdigest()
    stored_hash = record["result_hash"]
    return {
        "experiment_id": experiment_id,
        "match": rerun_hash == stored_hash,
        "expected_result_hash": stored_hash,
        "actual_result_hash": rerun_hash,
        "snapshot_hash": snap["snapshot_hash"],
        "as_of": snap["as_of"],
        "spec_version": record["software"].get("backtest_spec_version"),
        "detail": "同快照同代码逐位复现" if rerun_hash == stored_hash else "结果哈希不一致:代码或口径已变更",
    }


def walk_forward(values: list[float], closes: list[float], *, folds: int = 3,
                 volumes: list[float] | None = None,
                 price_limit_pct: float | None = None) -> dict[str, Any]:
    """Walk-forward 滚动样本外评估（§7.2）。

    无参数拟合（打分函数固定）,因此折间一致性即稳健性检验:
    expanding 窗口切折,每折只在测试段跑成本口径回测,报告逐折与聚合中位数。
    测试段切片带前一根 K 线（保证首日收益可算）,无重叠、无未来数据。
    """
    n = len(closes)
    if folds < 1:
        raise ValueError(f"folds 必须 >= 1,收到 {folds}")
    chunk = n // (folds + 1)
    if chunk < 10:
        raise ValueError(f"样本不足:每折至少 10 根 K 线,n={n},folds={folds}")
    fold_reports: list[dict[str, Any]] = []
    for f in range(folds):
        start = chunk * (f + 1) - 1
        end = chunk * (f + 2)
        seg_values = values[start:end]
        seg_closes = closes[start:end]
        seg_volumes = volumes[start:end] if volumes is not None else None
        bt = run_backtest(seg_values, seg_closes, volumes=seg_volumes,
                          price_limit_pct=price_limit_pct)
        fold_reports.append({
            "fold": f + 1,
            "test_start": start,
            "test_end": end,
            "bars": bt["bars"],
            "equity": bt["equity"],
            "baseline_buy_hold": bt["baseline_buy_hold"],
            "excess_vs_baseline": bt["excess_vs_baseline"],
            "max_drawdown": bt["max_drawdown"],
            "win_rate": bt["win_rate"],
        })
    equities = sorted(r["equity"] for r in fold_reports)
    excesses = [r["excess_vs_baseline"] for r in fold_reports]
    positive_excess = sum(1 for e in excesses if e > 0)
    return {
        "spec_version": str(BACKTEST_SPEC_VERSION),
        "folds": folds,
        "fold_reports": fold_reports,
        "median_equity": round(equities[len(equities) // 2], 8),
        "median_excess_vs_baseline": round(sorted(excesses)[len(excesses) // 2], 8),
        "positive_excess_folds": positive_excess,
        "oos_consistency": round(positive_excess / folds, 6),
        "honest_note": "打分函数固定无参数拟合,折间一致性=稳健性检验,非超参搜索",
    }


def detect_lookahead_bias(score_fn, bars: list[dict[str, Any]], *, probes: int = 4) -> dict[str, Any]:
    """未来数据泄漏检查（§7.2）:全量打分 vs 截断打分逐点比对。

    若打分函数逐点因果（values[i] 只依赖 bars[:i+1]）,则 bars[:k] 的前缀打分
    必须与全量打分在相同下标处逐位一致;任何不一致 = 用了未来数据。
    探针点取窗口末端/中段/浅段等多个位置,probes 控制探针数量。
    """
    n = len(bars)
    if n < 36:
        raise ValueError(f"样本不足:泄漏检查至少 36 根 K 线,收到 {n}")
    full = score_fn(bars)
    if len(full) != n:
        return {"uses_future_data": True, "detail": f"打分长度 {len(full)} != K 线数 {n}",
                "checked_points": [], "mismatches": []}
    candidates = sorted({n - 1, n - 5, n // 2, max(36, n // 4)})
    points = candidates[:max(1, probes)]
    mismatches: list[dict[str, Any]] = []
    for k in points:
        prefix = score_fn(bars[:k])
        for i in range(min(len(prefix), k)):
            if prefix[i] != full[i]:
                mismatches.append({"index": i, "probe": k,
                                   "full": full[i], "prefix": prefix[i]})
    return {
        "uses_future_data": bool(mismatches),
        "checked_points": points,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:10],
        "detail": "前缀打分与全量打分逐位一致,未检出未来数据" if not mismatches
        else f"检出 {len(mismatches)} 处前缀不一致,打分函数使用了未来数据",
    }


def check_disclosure_alignment(bars: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    """披露日错位报告（§7.2）:事件的可用 K 线起点与违规使用检查。

    events 元素:{"event_id", "disclosure_date": "YYYY-MM-DD"[, "used_from_bar": int]}。
    used_from_bar 给出时校验该 bar 日期 >= 披露日（违规即错位）;未给出则只报告可用起点。
    """
    bar_dates = [str(bar["timestamp"])[:10] for bar in bars]
    reports: list[dict[str, Any]] = []
    violations = 0
    for event in events:
        disclosure = str(event.get("disclosure_date", ""))
        if not disclosure:
            reports.append({"event_id": event.get("event_id"), "status": "missing_disclosure_date"})
            continue
        first_usable = next((i for i, d in enumerate(bar_dates) if d >= disclosure), None)
        report: dict[str, Any] = {
            "event_id": event.get("event_id"),
            "disclosure_date": disclosure,
            "first_usable_bar": first_usable,
            "first_usable_date": bar_dates[first_usable] if first_usable is not None else None,
        }
        used_from = event.get("used_from_bar")
        if used_from is not None:
            if first_usable is None or used_from < first_usable:
                report["violation"] = True
                report["detail"] = f"used_from_bar={used_from} 早于披露可用点 {first_usable}"
                violations += 1
            else:
                report["violation"] = False
        reports.append(report)
    return {
        "events_checked": len(events),
        "violations": violations,
        "reports": reports,
        "detail": "未检出披露日错位" if violations == 0 else f"检出 {violations} 处披露日错位",
    }
