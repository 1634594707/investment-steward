"""策略分享池阶段 A 测试:发布/列表/回放/Fork/谱系/非法公式 422。"""

from __future__ import annotations

import pytest

from investment_steward_core import market_feed, quant_pool
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
