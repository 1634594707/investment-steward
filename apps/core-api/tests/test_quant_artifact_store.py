"""量化制品存储测试（路线图 3.2,v23):SQLite 索引 + 内容哈希文件 + 旧 JSON 导入。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from investment_steward_core import quant_pool
from investment_steward_core.storage import artifact_store
from investment_steward_core.storage.database import Database
from investment_steward_core.storage.paths import StorageLayout
from test_quant_pool import _bars, _db, _lay


def test_artifact_store_atomic_write_and_integrity_check(tmp_path):
    layout = _lay(tmp_path)
    file_name, content_hash = artifact_store.save_artifact(
        layout, artifact_store.KIND_PARAMETER_SETS, "ps-demo.json", {"hello": "制品"}
    )
    assert file_name == "ps-demo.json"
    on_disk = json.loads((layout.artifacts / "parameter_sets" / file_name).read_text(encoding="utf-8"))
    assert on_disk == {"hello": "制品"}
    # 临时文件已清理,只留最终制品
    assert [p.name for p in (layout.artifacts / "parameter_sets").iterdir()] == ["ps-demo.json"]

    payload = artifact_store.read_artifact(layout, artifact_store.KIND_PARAMETER_SETS, file_name, content_hash)
    assert payload == {"hello": "制品"}

    # 哈希不一致 → 拒绝读取
    with pytest.raises(artifact_store.ArtifactIntegrityError):
        artifact_store.read_artifact(layout, artifact_store.KIND_PARAMETER_SETS, file_name, "0" * 64)


def test_publish_persists_to_artifact_file_and_db_index(tmp_path):
    layout = _lay(tmp_path)
    db = _db(tmp_path)
    entry = quant_pool.publish_parameter_set(
        db, layout, _bars(120), name="测试", symbol="510300", formula_tokens=["ma_ratio", "tanh"],
    )
    artifact_file = layout.artifacts / "parameter_sets" / f"{entry['artifact_id']}.json"
    assert artifact_file.exists()
    row = db.get_quant_artifact(entry["artifact_id"])
    assert row is not None
    assert row["kind"] == "parameter_sets" and row["symbol"] == "510300"
    # 本体不进 SQLite:数据库里只有索引与哈希
    assert "formula_tokens" not in json.dumps(row["payload_meta"], ensure_ascii=False)
    # 列表从索引 + 文件重组完整条目
    listed = quant_pool.list_parameter_sets(db, layout)
    assert listed[0]["formula_tokens"] == ["ma_ratio", "tanh"]


def test_legacy_json_imported_once_and_kept_as_bak(tmp_path):
    layout = _lay(tmp_path)
    legacy_entry = {
        "artifact_id": "ps-legacy0000000001",
        "name": "旧参数集", "symbol": "510300",
        "formula_tokens": ["ma_ratio", "tanh"], "formula": "ma_ratio tanh",
        "metrics": {"train_ic": 0.1, "valid_ic": 0.05, "samples": 120},
        "type": "parameter_set", "stage": "A", "author": "local",
        "parent_id": None, "note": "", "as_of": None,
        "dataset_version": "local:510300:2026-01-01",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    layout.quant_parameter_sets_file.parent.mkdir(parents=True, exist_ok=True)
    layout.quant_parameter_sets_file.write_text(
        json.dumps({"sets": [legacy_entry]}, ensure_ascii=False), encoding="utf-8"
    )
    db = Database(tmp_path / "steward.sqlite3")

    imported = quant_pool.import_legacy_pool(db, layout)
    assert imported == 1
    # 原文件改名保留(不删除)
    assert not layout.quant_parameter_sets_file.exists()
    bak = layout.artifacts / "quant_parameter_sets.json.imported.bak"
    assert bak.exists()

    entries = quant_pool.list_parameter_sets(db, layout)
    assert [e["artifact_id"] for e in entries] == ["ps-legacy0000000001"]
    assert entries[0]["name"] == "旧参数集"

    # 幂等:再次调用(新库或同库)不会重复报错
    assert quant_pool.import_legacy_pool(db, layout) == 0
