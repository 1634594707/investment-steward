"""确定性线性模型测试(阶段 D):训练确定性/权重形态/回放分支/样本不足报错。

并覆盖《量化研究实验室质量提升任务路线图》(2026-09-22) QL07 验收:
特征标准化（训练段统计量冻结）+ λ 网格 walk-forward 选择 + 旧制品向后兼容。
"""

from __future__ import annotations

import json
import random

import pytest

from investment_steward_core import quant_models, quant_pool
from investment_steward_core.storage.database import Database
from investment_steward_core.storage.paths import StorageLayout
from test_quant_pool import _bars, _db


def _lay(tmp_path) -> StorageLayout:
    return StorageLayout.from_user_data(tmp_path)


def test_train_is_deterministic_and_snapshot_complete():
    first = quant_models.train(_bars(250))
    second = quant_models.train(_bars(250))
    assert first == second  # 同输入同结果(确定性)
    assert len(first["weights"]) == len(quant_models.MODEL_FEATURES) + 1  # 截距 + 6 系数
    assert first["feature_order"] == list(quant_models.MODEL_FEATURES)
    snapshot = first["training"]
    assert snapshot["forward_days"] == 5
    assert snapshot["as_of"] and json.dumps(first, ensure_ascii=False)
    assert first["model_spec_version"] == quant_models.MODEL_SPEC_VERSION


def test_train_selects_lambda_by_walk_forward_and_records_the_process():
    """QL07 验收:λ 选择过程可复算 —— 网格与逐 λ 分折结果全落训练快照。"""
    result = quant_models.train(_bars(250))
    selection = result["training"]["lambda_selection"]
    assert selection["method"] == "walk_forward_grid"
    assert selection["grid"] == list(quant_models.DEFAULT_LAMBDA_GRID)
    assert selection["folds"] == quant_models.SELECTION_FOLDS
    assert result["training"]["lambda"] in selection["grid"]
    assert selection["selected"] == result["training"]["lambda"]
    assert any(report.get("available") for report in selection["reports"])
    for report in selection["reports"]:
        if report.get("available"):
            assert report["median_excess_vs_baseline"] is not None
            assert 0.0 <= report["oos_consistency"] <= 1.0

    # 选择按「中位超额 → 折间一致性 → 较小 λ」确定性 tie-break:重跑必须同结论
    assert quant_models.train(_bars(250))["training"]["lambda"] == result["training"]["lambda"]


def test_explicit_lambda_is_frozen_without_selection():
    result = quant_models.train(_bars(250), lambda_=1.0)
    assert result["training"]["lambda"] == 1.0
    assert result["training"]["lambda_selection"]["method"] == "fixed"
    assert result["training"]["lambda_selection"]["grid"] is None


def test_standardization_stats_are_frozen_on_train_segment_only():
    """QL07:标准化统计量只在 train 段估计;valid/test 复用（否则即泄漏）。"""
    bars = _bars(250)
    result = quant_models.train(bars, lambda_=1.0)
    stats = result["standardization"]
    assert len(stats["mean"]) == len(quant_models.MODEL_FEATURES)
    assert len(stats["std"]) == len(quant_models.MODEL_FEATURES)
    assert all(std > 0 for std in stats["std"])
    assert result["training"]["standardized"] is True

    usable = result["training"]["rows_with_valid_label"]
    split = result["training"]["split"]
    rows = quant_models.design_matrix(bars)[:usable]
    train_rows = rows[: int(split["train_end"])]
    recomputed = quant_models.standardize_fit(train_rows)
    assert recomputed["mean"] == pytest.approx(stats["mean"], abs=1e-9)
    assert recomputed["std"] == pytest.approx(stats["std"], abs=1e-9)


def test_standardization_balances_ridge_penalty_across_scales():
    """QL07 验收:标准化后各特征系数量级可比（原始量纲下小量纲特征被过度压缩）。"""
    rng = random.Random(20260922)
    rows: list[list[float]] = []
    # 两份量纲差 3 个数量级的特征,在 z 空间里对同一目标等权
    for _ in range(120):
        rows.append([rng.gauss(0.0, 1e-2), rng.gauss(0.0, 1e1)])
    stats = quant_models.standardize_fit(rows)
    z_rows = quant_models.standardize_apply(rows, stats)
    targets = [row[0] / stats["std"][0] + row[1] / stats["std"][1] for row in rows]  # z 空间等权真值

    standardized = quant_models.ridge_fit(z_rows, targets, 1.0)
    # z 空间等权 → 标准化后两个系数应几乎相等
    assert abs(standardized[1] - standardized[2]) < 0.1 * max(abs(standardized[1]), abs(standardized[2]))

    raw = quant_models.ridge_fit(rows, targets, 1.0)
    # 原始量纲真值系数 = 1/σ(小特征≈100、大特征≈0.1);岭惩罚对大幅值系数更狠,
    # 故小量纲特征的系数被明显压缩,与真值严重不成比例。
    true_small = 1.0 / stats["std"][0]
    true_large = 1.0 / stats["std"][1]
    assert abs(raw[1] / raw[2]) < 0.5 * abs(true_small / true_large)


def test_ridge_fit_drops_constant_columns_instead_of_failing():
    """常数列（标准化后整列 0,与截距共线、系数不可识别）在 λ=0 下也不得让矩阵奇异。"""
    rows = [[1.0, float(i)] for i in range(40)]  # 第 0 列恒为 1
    targets = [float(i) for i in range(40)]
    weights = quant_models.ridge_fit(rows, targets, 0.0)
    assert weights[1] == 0.0  # 不可识别列记 0,不报错
    assert weights[2] != 0.0


def test_score_series_legacy_and_standardized_paths_differ_but_are_deterministic():
    bars = _bars(250)
    weights = [0.1, 1.0, -2.0, 0.5, 0.25, 0.75, -0.5]
    raw = quant_models.score_series(weights, bars)
    stats = quant_models.standardize_fit(quant_models.design_matrix(bars)[:150])
    standardized = quant_models.score_series(weights, bars, standardization=stats)
    assert raw == quant_models.score_series(weights, bars)
    assert standardized == quant_models.score_series(weights, bars, standardization=stats)
    assert raw != standardized


def test_train_insufficient_bars_raises():
    with pytest.raises(ValueError):
        quant_models.train(_bars(50))


def test_negative_lambda_raises():
    with pytest.raises(ValueError):
        quant_models.train(_bars(250), lambda_=-0.5)


def test_publish_model_and_replay(tmp_path):
    bars = _bars(250)
    result = quant_models.train(bars)
    entry = quant_pool.publish_model(
        _db(tmp_path), _lay(tmp_path),
        bars,
        name="510300 线性模型",
        symbol="510300",
        weights=result["weights"],
        feature_order=result["feature_order"],
        metrics=result["metrics"],
        training=result["training"],
        standardization=result["standardization"],
    )
    assert entry["artifact_id"].startswith("mw-")
    assert entry["type"] == "model_weights" and entry["stage"] == "D"
    assert entry["standardization"] == result["standardization"]
    listed = quant_pool.list_parameter_sets(_db(tmp_path), _lay(tmp_path))
    assert [item["artifact_id"] for item in listed] == [entry["artifact_id"]]
    replay = quant_pool.replay(_db(tmp_path), _lay(tmp_path), entry["artifact_id"], bars)
    assert replay is not None and replay["bars"] == len(bars) - 1
    assert replay["equity"] > 0
    # 回放确定性:同输入同权益
    again = quant_pool.replay(_db(tmp_path), _lay(tmp_path), entry["artifact_id"], bars)
    assert again == replay
    # Fork 对模型条目同样生效
    forked = quant_pool.fork_parameter_set(_db(tmp_path), _lay(tmp_path), entry["artifact_id"], name="Fork 模型")
    assert forked is not None and forked["parent_id"] == entry["artifact_id"]
    assert forked["weights"] == entry["weights"]


def test_legacy_model_without_standardization_still_scores_identically(tmp_path):
    """QL07 验收:旧模型权重制品（无 standardization）按恒等变换解释执行,逐位不变。"""
    bars = _bars(250)
    weights = [0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    entry = quant_pool.publish_model(
        _db(tmp_path), _lay(tmp_path), bars,
        name="旧口径模型", symbol="510300",
        weights=weights, feature_order=list(quant_models.MODEL_FEATURES),
        metrics={"train_ic": 0.0, "valid_ic": 0.0, "samples": len(bars)},
        training={"lambda": 1.0, "forward_days": 5, "as_of": str(bars[-1]["timestamp"])},
        standardization=None,
    )
    assert entry["standardization"] is None
    loaded = quant_pool.get_parameter_set(_db(tmp_path), _lay(tmp_path), entry["artifact_id"])
    assert loaded is not None and loaded["standardization"] is None
    # 与直接按原始量纲打分逐位一致
    expected = quant_models.score_series(weights, bars, standardization=None)
    assert quant_pool._entry_score_series(loaded, bars) == expected
