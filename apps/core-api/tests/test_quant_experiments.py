"""本地量化实验测试（阶段 3 / §7.2):成本口径、确定性、快照、结果哈希复现。"""

from __future__ import annotations

import hashlib
import json

import pytest

from investment_steward_core import quant_experiments
from investment_steward_core.storage.database import Database
from investment_steward_core.storage.paths import StorageLayout
from test_quant_pool import _bars, _db, _lay

USER = _user = __import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, "test-user")


def _score_fn(bars):
    """确定性打分:收盘价相对窗口均值的位置（避免依赖行情因子模块）。"""
    closes = [float(b["close"]) for b in bars]
    out = []
    for i, close in enumerate(closes):
        window = closes[max(0, i - 7):i + 1]
        mean = sum(window) / len(window)
        out.append((close - mean) / mean if mean else 0.0)
    return out


def test_backtest_is_deterministic_and_costs_applied():
    closes = [float(b["close"]) for b in _bars(120)]
    values = _score_fn(_bars(120))
    first = quant_experiments.run_backtest(values, closes)
    second = quant_experiments.run_backtest(values, closes)
    assert first == second  # 同输入同结果
    assert first["equity"] != first["baseline_buy_hold"]  # 与买入持有基线分离
    assert first["costs"]["commission_bps"] > 0 and first["costs"]["slippage_bps"] > 0
    assert first["bars"] == len(closes) - 1


def test_experiment_snapshot_and_result_hash(tmp_path):
    layout, db = _lay(tmp_path), _db(tmp_path)
    bars = _bars(120)
    result = quant_experiments.run_experiment(
        db, layout, USER, bars, symbol="510300",
        score_fn_name="mean_revert_test", score_fn=_score_fn, artifact_id="ps-demo",
    )
    assert result["experiment_id"].startswith("exp-")
    # 快照登记与实际文件一致
    snap = result["data_snapshot"]
    payload = quant_experiments.load_snapshot(layout, snap["content_hash"], snap["file_name"])
    assert len(payload) == 120 and payload[-1] == bars[-1]
    digest = hashlib.sha256(json.dumps(
        {"symbol": "510300", "bar_count": 120, "bars": bars},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert snap["snapshot_hash"] == digest
    # 同快照同配置幂等:不产生第二条
    again = quant_experiments.run_experiment(
        db, layout, USER, bars, symbol="510300",
        score_fn_name="mean_revert_test", score_fn=_score_fn, artifact_id="ps-demo",
    )
    assert again["experiment_id"] == result["experiment_id"]
    assert len(db.list_quant_experiments(USER)) == 1


def test_reproduce_matches_then_detects_tamper(tmp_path):
    layout, db = _lay(tmp_path), _db(tmp_path)
    bars = _bars(120)
    result = quant_experiments.run_experiment(
        db, layout, USER, bars, symbol="510300",
        score_fn_name="mean_revert_test", score_fn=_score_fn,
    )
    eid = result["experiment_id"]

    def builder(config):
        return _score_fn

    report = quant_experiments.reproduce_experiment(db, layout, eid, builder)
    assert report["match"] is True and report["as_of"] == result["data_snapshot"]["as_of"]

    # 篡改快照文件 → 哈希校验拒绝(不静默复现)
    snap = result["data_snapshot"]
    path = layout.artifacts / "data_snapshots" / snap["file_name"]
    path.write_text(json.dumps({"symbol": "510300", "bar_count": 1, "bars": []}), encoding="utf-8")
    with pytest.raises(Exception, match="哈希不一致|缺失"):
        quant_experiments.reproduce_experiment(db, layout, eid, builder)


def test_experiment_rejects_insufficient_bars(tmp_path):
    with pytest.raises(ValueError, match="样本不足"):
        quant_experiments.run_experiment(
            _db(tmp_path), _lay(tmp_path), USER, _bars(20),
            symbol="510300", score_fn_name="x", score_fn=_score_fn,
        )
