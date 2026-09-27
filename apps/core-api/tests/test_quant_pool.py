"""策略分享池阶段 A 测试:发布/列表/回放/Fork/谱系/非法公式 422。"""

from __future__ import annotations

import pytest

from investment_steward_core import market_feed, quant_factors, quant_pool
from investment_steward_core.storage.database import Database
from investment_steward_core.storage.paths import StorageLayout


def _lay(tmp_path) -> StorageLayout:
    return StorageLayout.from_user_data(tmp_path)


def _db(tmp_path) -> Database:
    return Database(tmp_path / "steward.sqlite3")


def _bars(count: int = 120) -> list[dict[str, object]]:
    bars: list[dict[str, object]] = []
    for i in range(count):
        phase = i % 16
        price = 10.0 + phase * 0.25 if phase < 8 else 12.0 - (phase - 8) * 0.25
        bars.append({
            "timestamp": f"2026-06-{(i % 28) + 1:02d}T00:00:00+00:00",
            "open": round(price - 0.02, 3),
            "high": round(price + 0.06, 3),
            "low": round(price - 0.06, 3),
            "close": round(price, 3),
            "volume": 1000.0 + i,
        })
    return bars


def test_publish_list_replay_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(market_feed, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(120), "eastmoney"))
    entry = quant_pool.publish_parameter_set(
        _db(tmp_path), _lay(tmp_path), _bars(120),
        name="测试参数集", symbol="510300",
        formula_tokens=["ma_ratio", "tanh"], note="",
    )
    assert entry["artifact_id"].startswith("ps-")
    assert entry["type"] == "parameter_set"
    listed = quant_pool.list_parameter_sets(_db(tmp_path), _lay(tmp_path))
    assert [s["artifact_id"] for s in listed] == [entry["artifact_id"]]
    replay = quant_pool.replay(_db(tmp_path), _lay(tmp_path), entry["artifact_id"], _bars(120))
    assert replay is not None and replay["bars"] == 119 and 0 < replay["equity"] < 100
    chain = quant_pool.lineage(_db(tmp_path), _lay(tmp_path), entry["artifact_id"])
    assert [c["artifact_id"] for c in chain] == [entry["artifact_id"]]


def test_fork_creates_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(market_feed, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(120), "eastmoney"))
    parent = quant_pool.publish_parameter_set(
        _db(tmp_path), _lay(tmp_path), _bars(120), name="父", symbol="510300", formula_tokens=["ma_ratio", "tanh"],
    )
    child = quant_pool.fork_parameter_set(_db(tmp_path), _lay(tmp_path), parent["artifact_id"], name="子", note="调参试验")
    assert child["parent_id"] == parent["artifact_id"]
    chain = quant_pool.lineage(_db(tmp_path), _lay(tmp_path), child["artifact_id"])
    assert [c["artifact_id"] for c in chain] == [child["artifact_id"], parent["artifact_id"]]


def test_publish_illegal_formula_raises(tmp_path):
    with pytest.raises(ValueError):
        quant_pool.publish_parameter_set(
            _db(tmp_path), _lay(tmp_path), _bars(120), name="坏公式", symbol="510300", formula_tokens=["add"],
        )


def test_published_metrics_equal_mining_ic_on_the_same_snapshot(tmp_path):
    """路线图 §7 总体验收:同一数据快照下,挖掘时点 IC 与发布后内核重算 IC 的差异必须为 0。

    这依赖两条链路共用同一套三段式切分与同一套 IC 统计(quant_factors.three_way_split /
    ic_statistics);任何一侧改了口径而不对齐,本测试即失败。
    """
    bars = _bars(250)
    board = quant_factors.mine(bars)
    assert board["top"], "合成周期数据应能挖到显著公式"
    row = board["top"][0]
    entry = quant_pool.publish_parameter_set(
        _db(tmp_path), _lay(tmp_path), bars,
        name="对齐校验", symbol="510300", formula_tokens=row["formula_tokens"],
    )
    metrics = entry["metrics"]
    assert metrics["train_ic"] == row["train_ic"]
    assert metrics["valid_ic"] == row["valid_ic"]
    assert metrics["test_ic"] == row["test_ic"]
    assert metrics["segment_samples"] == row["segment_samples"]
    assert metrics["split"]["train_end"] == board["split"]["train_end"]
    assert metrics["split"]["valid_end"] == board["split"]["valid_end"]
    # 口径版本随制品冻结,检索时能看出该制品按哪版口径产出
    caliber = metrics["caliber"]
    assert caliber["mine_spec_version"] == quant_factors.MINE_SPEC_VERSION
    assert caliber["ic_spec_version"] == quant_factors.IC_SPEC_VERSION
    assert caliber["ic_suite_spec_version"] == quant_factors.IC_SUITE_SPEC_VERSION
    assert caliber["forward_days"] == quant_factors.FORWARD_DAYS


def test_published_parameter_set_carries_ic_suite(tmp_path):
    """QL05:评估套件随参数集制品入库并挂口径版本。"""
    entry = quant_pool.publish_parameter_set(
        _db(tmp_path), _lay(tmp_path), _bars(250),
        name="套件校验", symbol="510300", formula_tokens=["ma_ratio", "tanh"],
    )
    suite = entry["metrics"]["ic_suite"]
    assert suite["spec_version"] == quant_factors.IC_SUITE_SPEC_VERSION
    assert suite["period_ic"]["folds"] >= 2
    assert [point["days"] for point in suite["ic_decay"]] == sorted(quant_factors.IC_DECAY_HORIZONS)
    assert suite["group_returns"]["groups"] == quant_factors.GROUP_RETURN_GROUPS
    # 制品本体可读取且含同一份 metrics（内容寻址不可变）
    loaded = quant_pool.get_parameter_set(_db(tmp_path), _lay(tmp_path), entry["artifact_id"])
    assert loaded is not None
    assert loaded["metrics"]["ic_suite"] == suite
