"""确定性线性模型测试(阶段 D):训练确定性/权重形态/回放分支/样本不足报错。"""

from __future__ import annotations

import json

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
    assert snapshot["lambda"] == 1.0 and snapshot["forward_days"] == 5
    assert snapshot["as_of"] and json.dumps(first, ensure_ascii=False)


def test_train_insufficient_bars_raises():
    import pytest

    with pytest.raises(ValueError):
        quant_models.train(_bars(50))


def test_negative_lambda_raises():
    import pytest

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
    )
    assert entry["artifact_id"].startswith("mw-")
    assert entry["type"] == "model_weights" and entry["stage"] == "D"
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
