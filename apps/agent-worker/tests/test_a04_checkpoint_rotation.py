"""A04（架构改进路线图 2026-09-25）：账本轮换与完成状态快照的验收。

改造前去重状态与历史调用记录同处一个追加文件：启动逐行回放整本账，`tool_calls`
列表随运行时长无界增长。改造后两者分离，本文件锁定四条性质：

1. **完成标记不随账本走**——轮换、轮换中断、进程重启后，`is_done` 判定不变；
2. **内存有界**——记录再多，`tool_calls` 窗口也不超过 `MAX_RECENT_RECORDS`；
3. **历史可查询**——全量历史跨轮换文件按需读取，不需要常驻内存；
4. **既有安装可迁移**——老账本里的 `done` 行一次性迁入快照，迁移后不再追加。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from investment_steward_agent_worker import checkpoint as checkpoint_module
from investment_steward_agent_worker.checkpoint import (
    MAX_RECENT_RECORDS,
    PersistentCheckpoint,
    ToolCallRecord,
)

BRIEF_KEY = "brief:2026-09-25"


def _record(index: int) -> ToolCallRecord:
    return ToolCallRecord(tool=f"task.{index}", target=f"/x/{index}", ok=True)


def _done_line(key: str, at: str) -> str:
    return json.dumps({"type": "done", "key": key, "at": at})


def _line_count(path) -> int:
    return len(path.read_text(encoding="utf-8").strip().splitlines())


def test_memory_window_bounded_and_history_queryable(tmp_path, monkeypatch) -> None:
    """内存只保留有界近期记录；全量历史仍可跨轮换文件查询。"""
    monkeypatch.setattr(checkpoint_module, "LEDGER_MAX_BYTES", 4096)
    checkpoint = PersistentCheckpoint(tmp_path / "c.jsonl")
    total = MAX_RECENT_RECORDS * 12

    for index in range(total):
        checkpoint.record_tool_call(_record(index))

    assert len(checkpoint.tool_calls) == MAX_RECENT_RECORDS, "内存窗口必须封顶"
    assert checkpoint.recorded_total == total
    assert checkpoint.rotated_files(), "超过字节阈值后应产生轮换文件"

    history = checkpoint.query_tool_calls(limit=total)
    assert len(history) == total, "轮换后的历史必须仍可查询"
    assert [item.at for item in history] == sorted(
        (item.at for item in history), reverse=True
    ), "历史查询按时间倒序"


def test_done_markers_survive_repeated_rotation(tmp_path, monkeypatch) -> None:
    """账本反复轮换后，完成标记仍可从快照恢复（重启不重发）。"""
    monkeypatch.setattr(checkpoint_module, "LEDGER_MAX_BYTES", 2048)
    path = tmp_path / "c.jsonl"
    checkpoint = PersistentCheckpoint(path)
    checkpoint.mark_done(BRIEF_KEY, datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    for index in range(400):
        checkpoint.record_tool_call(_record(index))
    assert checkpoint.rotated_files()

    restarted = PersistentCheckpoint(path)
    assert restarted.is_done(BRIEF_KEY)


def test_done_markers_survive_manual_half_rotation(tmp_path) -> None:
    """轮换只做了一半（账本被移走、新账本未建立）也不能丢完成标记。"""
    path = tmp_path / "c.jsonl"
    checkpoint = PersistentCheckpoint(path)
    checkpoint.mark_done(BRIEF_KEY, datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    checkpoint.record_tool_call(_record(1))

    # 模拟「rename 完成但进程随即被杀」：活动账本已不在原位。
    path.replace(tmp_path / "c.20260925-080000.jsonl")
    assert not path.exists()

    restarted = PersistentCheckpoint(path)
    assert restarted.is_done(BRIEF_KEY)
    assert restarted.rotated_files()
    # 新活动账本可继续追加，不因缺文件而中断。
    restarted.record_tool_call(_record(2))
    assert [item.tool for item in restarted.tool_calls] == ["task.2"]


def test_rotation_failure_does_not_lose_markers(tmp_path, monkeypatch) -> None:
    """轮换本身失败（文件被占用/权限）时，追加与完成标记都不受影响。"""
    monkeypatch.setattr(checkpoint_module, "LEDGER_MAX_BYTES", 1024)
    path = tmp_path / "c.jsonl"
    checkpoint = PersistentCheckpoint(path)
    checkpoint.mark_done(BRIEF_KEY, datetime(2026, 9, 25, 8, 0, tzinfo=UTC))

    original_replace = type(path).replace

    def failing_replace(self, target):  # type: ignore[no-untyped-def]
        if str(self).endswith(".jsonl"):
            raise OSError("rotate interrupted")
        return original_replace(self, target)

    monkeypatch.setattr(type(path), "replace", failing_replace)
    for index in range(200):
        checkpoint.record_tool_call(_record(index))
    assert not checkpoint.rotated_files(), "轮换失败不应留下半成品轮换文件"

    monkeypatch.setattr(type(path), "replace", original_replace)
    restarted = PersistentCheckpoint(path)
    assert restarted.is_done(BRIEF_KEY)
    assert restarted.recorded_total == 200, "轮换失败不得吞掉已追加的记录"


def test_legacy_ledger_migrates_done_markers_once(tmp_path) -> None:
    """老格式账本（done 与 tool_call 混在一起）一次性迁移，迁移后 done 不再进账本。"""
    path = tmp_path / "c.jsonl"
    path.write_text(
        _done_line("brief:2026-09-24", "2026-09-24T08:00:00+00:00")
        + "\n"
        + json.dumps(
            {
                "type": "tool_call",
                "rec": {
                    "tool": "old.task",
                    "target": "/x",
                    "ok": True,
                    "at": "2026-09-24T08:00:01+00:00",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    checkpoint = PersistentCheckpoint(path)
    assert checkpoint.is_done("brief:2026-09-24"), "迁移必须带回既有完成标记"
    assert checkpoint.state_path.exists(), "迁移应写出独立状态快照"
    assert [item.tool for item in checkpoint.tool_calls] == ["old.task"]

    lines_before = _line_count(path)
    checkpoint.mark_done(BRIEF_KEY, datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    assert _line_count(path) == lines_before, "完成标记不得再写进历史账本"
    assert PersistentCheckpoint(path).is_done(BRIEF_KEY)


def test_corrupt_snapshot_falls_back_to_ledger(tmp_path) -> None:
    """快照损坏时回退到账本回放，不静默丢完成标记。"""
    path = tmp_path / "c.jsonl"
    path.write_text(
        _done_line("brief:2026-09-24", "2026-09-24T08:00:00+00:00") + "\n", encoding="utf-8"
    )
    checkpoint = PersistentCheckpoint(path)
    assert checkpoint.is_done("brief:2026-09-24")

    checkpoint.state_path.write_text("{ 这不是 JSON", encoding="utf-8")
    restarted = PersistentCheckpoint(path)
    assert restarted.is_done("brief:2026-09-24")


def test_stale_done_markers_pruned_recent_kept(tmp_path, monkeypatch) -> None:
    """保留期外的去重标记可丢弃；近期标记必须保留。"""
    monkeypatch.setattr(checkpoint_module, "DONE_RETENTION_DAYS", 7)
    path = tmp_path / "c.jsonl"
    checkpoint = PersistentCheckpoint(path)
    checkpoint.mark_done("brief:2026-01-01", datetime(2026, 1, 1, tzinfo=UTC))
    checkpoint.mark_done("patrol:recent", datetime.now(UTC) - timedelta(hours=1))

    restarted = PersistentCheckpoint(path)
    assert not restarted.is_done("brief:2026-01-01")
    assert restarted.is_done("patrol:recent")
