"""策略分享池阶段 A/D:参数集与线性模型的发布/展示/确定性回放/Fork 派生谱系。

存储形态（路线图 3.2,v23 起）:
- SQLite `quant_artifacts` 只存元数据索引(kind/stage/symbol/name/parent_id/created_at)
  与内容校验值(SHA-256);本体(公式 token/权重 JSON)存制品目录
  `{artifacts}/{kind}/{artifact_id}.json`,写入走临时文件 + 哈希校验 + 原子替换;
- artifact_id = 内容 sha256 前 16 位(内容寻址,不可变);
- Fork = 复制并记 parent_id(派生谱系);
- 回放 = quant_factors.evaluate_tokens 对最近 K 线逐 bar 求值 + tanh 仓位 → 确定性统计,不编造;
- 旧版 `quant_parameter_sets.json` 单文件在首次访问时自动导入(import_legacy_pool),
  原文件改名为 `.imported.bak` 保留,绝不删除。
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any

from investment_steward_core import quant_experiments, quant_factors, quant_models
from investment_steward_core.storage import artifact_store
from investment_steward_core.storage.artifact_store import KIND_MODEL_WEIGHTS, KIND_PARAMETER_SETS
from investment_steward_core.storage.database import Database
from investment_steward_core.storage.paths import StorageLayout

ARTIFACT_KIND_BY_TYPE = {"parameter_set": KIND_PARAMETER_SETS, "model_weights": KIND_MODEL_WEIGHTS}


def _artifact_id(tokens: list[str], symbol: str, created_at: str) -> str:
    digest = hashlib.sha256(json.dumps({"tokens": tokens, "symbol": symbol, "created_at": created_at}, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"ps-{digest[:16]}"


def _model_artifact_id(weights: list[float], symbol: str, created_at: str) -> str:
    digest = hashlib.sha256(json.dumps({"weights": weights, "symbol": symbol, "created_at": created_at}, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"mw-{digest[:16]}"


def _persist(db: Database, layout: StorageLayout, entry: dict[str, Any]) -> None:
    """本体落制品目录(原子 + 哈希),元数据索引写 SQLite。"""
    kind = ARTIFACT_KIND_BY_TYPE[str(entry["type"])]
    file_name, content_hash = artifact_store.save_artifact(
        layout, kind, f"{entry['artifact_id']}.json", entry
    )
    db.upsert_quant_artifact(
        artifact_id=str(entry["artifact_id"]),
        kind=kind,
        stage=str(entry.get("stage", "")),
        symbol=str(entry.get("symbol", "")),
        name=str(entry.get("name", "")),
        parent_id=entry.get("parent_id"),
        created_at=str(entry["created_at"]),
        file_name=file_name,
        content_hash=content_hash,
        payload_meta={
            "metrics": entry.get("metrics"),
            "note": entry.get("note"),
            "author": entry.get("author"),
            "as_of": entry.get("as_of"),
            "dataset_version": entry.get("dataset_version"),
        },
    )


def _load_entry(db: Database, layout: StorageLayout, artifact_id: str) -> dict[str, Any] | None:
    row = db.get_quant_artifact(artifact_id)
    if row is None:
        return None
    entry = artifact_store.read_artifact(layout, row["kind"], row["file_name"], row["content_hash"])
    entry["artifact_id"] = artifact_id  # 文件内容与登记一致性由哈希校验保证
    return entry


def import_legacy_pool(db: Database, layout: StorageLayout) -> int:
    """旧版单文件 JSON 一次性导入(幂等):逐条落制品文件 + 索引,原文件改名保留。"""
    legacy = layout.quant_parameter_sets_file
    if not legacy.exists():
        return 0
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    sets = data.get("sets") if isinstance(data, dict) else None
    if not isinstance(sets, list):
        return 0
    imported = 0
    for entry in sets:
        if not isinstance(entry, dict) or "artifact_id" not in entry or entry.get("type") not in ARTIFACT_KIND_BY_TYPE:
            continue
        if db.get_quant_artifact(str(entry["artifact_id"])) is not None:
            imported += 1
            continue
        _persist(db, layout, entry)
        imported += 1
    legacy.rename(legacy.with_suffix(".json.imported.bak"))
    return imported


def publish_parameter_set(
    db: Database,
    layout: StorageLayout,
    bars: list[dict[str, Any]],
    *,
    name: str,
    symbol: str,
    formula_tokens: list[str],
    note: str = "",
) -> dict[str, Any]:
    """发布参数集:公式先经全量求值验证(非法抛 ValueError),指标确定性计算,内容寻址入库。

    指标口径与挖掘主流程**逐位对齐**(同一数据快照下挖到的 IC 与发布后重算的 IC 必须相等):
    同一套三段式切分(``quant_factors.three_way_split``)+ 同一套 IC 统计(``ic_statistics``)
    + 同一套评估套件(``factor_evaluation``,口径版本挂进 metrics)。
    """
    values = quant_factors.evaluate_tokens(formula_tokens, bars)
    closes = [float(bar["close"]) for bar in bars]
    forward = quant_factors.forward_returns(closes)
    split = quant_factors.three_way_split(len(bars))
    train_end = int(split["train_end"])
    valid_end = int(split["valid_end"])
    train_stat = quant_factors.ic_statistics(values[:train_end], forward[:train_end], overlap=quant_factors.FORWARD_DAYS)
    valid_stat = quant_factors.ic_statistics(
        values[train_end:valid_end], forward[train_end:valid_end], overlap=quant_factors.FORWARD_DAYS
    )
    test_stat = quant_factors.ic_statistics(values[valid_end:], forward[valid_end:], overlap=quant_factors.FORWARD_DAYS)
    suite = quant_factors._rounded_suite(quant_factors.factor_evaluation(values, closes))
    created_at = datetime.now(UTC).isoformat()
    artifact_id = _artifact_id(formula_tokens, symbol, created_at)
    entry = {
        "artifact_id": artifact_id,
        "name": name or f"{symbol} 参数集",
        "symbol": symbol,
        "formula_tokens": formula_tokens,
        "formula": " ".join(formula_tokens),
        "metrics": {
            "train_ic": round(train_stat["ic"], 4),
            "valid_ic": round(valid_stat["ic"], 4),
            "test_ic": round(test_stat["ic"], 4),
            "test_t_stat": round(test_stat["t_stat"], 4) if test_stat["t_stat"] is not None else None,
            "test_p_value": round(test_stat["p_value"], 8),
            "samples": len(bars),
            "split": split,
            "segment_samples": {"train": train_stat["samples"], "valid": valid_stat["samples"],
                                "test": test_stat["samples"]},
            "ic_suite": suite,
            "caliber": {
                "mine_spec_version": quant_factors.MINE_SPEC_VERSION,
                "ic_spec_version": quant_factors.IC_SPEC_VERSION,
                "ic_suite_spec_version": quant_factors.IC_SUITE_SPEC_VERSION,
                "forward_days": quant_factors.FORWARD_DAYS,
            },
        },
        "type": "parameter_set",
        "stage": "A",
        "author": "local",
        "parent_id": None,
        "note": note,
        "as_of": bars[-1]["timestamp"] if bars else None,
        "dataset_version": f"local:{symbol}:{created_at[:10]}",
        "created_at": created_at,
    }
    _persist(db, layout, entry)
    return entry


def publish_model(
    db: Database,
    layout: StorageLayout,
    bars: list[dict[str, Any]],
    *,
    name: str,
    symbol: str,
    weights: list[float],
    feature_order: list[str],
    metrics: dict[str, Any],
    training: dict[str, Any],
    standardization: dict[str, Any] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """发布线性模型:权重已在训练时确定性求出,内容寻址入库(阶段 D 限定形态)。

    standardization 为训练段冻结的 z-score 统计量（新模型）;缺失即为旧口径原始量纲,
    旧制品按恒等变换解释执行,逐位不变。
    """
    created_at = datetime.now(UTC).isoformat()
    artifact_id = _model_artifact_id(weights, symbol, created_at)
    entry = {
        "artifact_id": artifact_id,
        "name": name or f"{symbol} 线性模型",
        "symbol": symbol,
        "type": "model_weights",
        "stage": "D",
        "author": "local",
        "parent_id": None,
        "weights": weights,
        "feature_order": feature_order,
        "standardization": standardization,
        "metrics": metrics,
        "training": training,
        "note": note,
        "as_of": training.get("as_of"),
        "dataset_version": f"local:{symbol}:{created_at[:10]}",
        "created_at": created_at,
    }
    _persist(db, layout, entry)
    return entry


def _entry_score_series(entry: dict[str, Any], bars: list[dict[str, Any]]) -> list[float]:
    """按制品类型求打分序列:parameter_set 用公式 token,model_weights 用线性权重。"""
    if entry.get("type") == "model_weights":
        return quant_models.score_series(
            [float(w) for w in entry["weights"]],
            bars,
            standardization=entry.get("standardization") or None,
        )
    return quant_factors.evaluate_tokens([str(t) for t in entry["formula_tokens"]], bars)


def list_parameter_sets(db: Database, layout: StorageLayout) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in db.list_quant_artifacts(kinds=(KIND_PARAMETER_SETS, KIND_MODEL_WEIGHTS)):
        try:
            entry = artifact_store.read_artifact(layout, row["kind"], row["file_name"], row["content_hash"])
        except artifact_store.ArtifactIntegrityError:
            # 单个制品损坏不拖垮整个列表;索引中仍可看到名字与标记,由 UI 提示修复。
            entries.append({
                "artifact_id": row["artifact_id"],
                "name": row["name"],
                "symbol": row["symbol"],
                "type": row["kind"],
                "stage": row["stage"],
                "created_at": row["created_at"],
                "parent_id": row["parent_id"],
                "integrity_error": True,
                **row["payload_meta"],
            })
            continue
        entries.append(entry)
    return entries


def get_parameter_set(db: Database, layout: StorageLayout, artifact_id: str) -> dict[str, Any] | None:
    return _load_entry(db, layout, artifact_id)


def fork_parameter_set(db: Database, layout: StorageLayout, artifact_id: str, *, name: str, note: str = "") -> dict[str, Any] | None:
    """Fork:复制制品并记 parent_id(派生谱系)。内容基于原集,created_at 新生成。"""
    parent = _load_entry(db, layout, artifact_id)
    if parent is None:
        return None
    created_at = datetime.now(UTC).isoformat()
    if parent.get("type") == "model_weights":
        forked_id = _model_artifact_id([float(w) for w in parent["weights"]], parent["symbol"], created_at)
    else:
        forked_id = _artifact_id([str(t) for t in parent["formula_tokens"]], parent["symbol"], created_at)
    forked = {
        **parent,
        "artifact_id": forked_id,
        "name": name or f"{parent['name']} Fork",
        "parent_id": artifact_id,
        "note": note or f"Fork 自 {artifact_id}",
        "created_at": created_at,
        "metrics": {**parent["metrics"], "forked_from": artifact_id},
    }
    _persist(db, layout, forked)
    return forked


def lineage(db: Database, layout: StorageLayout, artifact_id: str) -> list[dict[str, Any]]:
    """派生谱系:从指定参数集沿 parent_id 回溯至根。"""
    chain: list[dict[str, Any]] = []
    current = _load_entry(db, layout, artifact_id)
    guard = 0
    while current and guard < 50:
        chain.append(current)
        parent_id = current.get("parent_id")
        current = _load_entry(db, layout, parent_id) if parent_id else None
        guard += 1
    return chain


def replay(db: Database, layout: StorageLayout, artifact_id: str, bars: list[dict[str, Any]]) -> dict[str, Any] | None:
    """确定性回放:打分 → tanh 仓位 → 次日收益累计(闭 K 线口径),同输入同结果。"""
    entry = _load_entry(db, layout, artifact_id)
    if entry is None or not bars:
        return None
    values = _entry_score_series(entry, bars)
    closes = [float(bar["close"]) for bar in bars]
    dates = [str(bar["timestamp"])[:10] for bar in bars]
    equity = 1.0
    curve: list[dict[str, Any]] = []
    returns: list[float] = []
    for i in range(len(bars) - 1):
        position = math.tanh(values[i])
        day_return = (closes[i + 1] - closes[i]) / closes[i] if closes[i] else 0.0
        equity *= 1.0 + position * day_return
        returns.append(position * day_return)
        curve.append({"date": dates[i], "position": round(position, 4), "equity": round(equity, 6)})
    wins = sum(1 for r in returns if r > 0)
    return {
        "artifact_id": artifact_id,
        "equity": round(equity, 6),
        "curve": curve,
        "win_rate": round(wins / len(returns), 4) if returns else None,
        "bars": len(returns),
        "note": "确定性回放:tanh 仓位 × 次日收益,同输入同结果;仅作研究背景,不构成买卖建议",
        # 第四轮审计：同一份策略制品，本回放页与「实验」页会给**两个不同的净值**——
        # 本页是**毛收益**（零手续费、零滑点、无换手/涨跌停/停牌约束），
        # 而 `quant_experiments.run_backtest` 是**净收益**（单边 2.5+5.0 bps、仓位与
        # 换手上限、停牌冻结、涨跌停禁开仓）。差额全部来自成本与约束，
        # 而改造前两个响应里**都没有一句**说这件事，用户按回放页的净值判断策略、
        # 按实验页的净值下单，差额无从解释。口径与常量由 `quant_experiments` 单点定义，
        # 这里只引用不复制（避免两处各写一份而漂移）。
        "caliber": {
            "kind": "gross",
            "costs_applied": False,
            "detail": "毛收益：未计手续费、滑点、换手/仓位约束、涨跌停与停牌处理。",
            "compare_with": "quant_experiments.run_backtest（净收益，含成本与交易约束）",
            "net_spec_version": quant_experiments.BACKTEST_SPEC_VERSION,
            "net_costs": {
                "commission_bps": quant_experiments.COMMISSION_BPS,
                "slippage_bps": quant_experiments.SLIPPAGE_BPS,
                "max_position": quant_experiments.MAX_POSITION,
                "max_turnover_per_bar": quant_experiments.MAX_TURNOVER_PER_BAR,
            },
        },
    }
