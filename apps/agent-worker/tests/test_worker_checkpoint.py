"""D2 检查点落盘与恢复：追加式 JSONL，重启续跑不重发、崩溃不丢既有状态。"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from investment_steward_agent_worker.checkpoint import PersistentCheckpoint, ToolCallRecord


def _path(tmp_path) -> object:
    return tmp_path / "checkpoint.jsonl"


def test_append_and_reload_reconstructs_state(tmp_path) -> None:
    path = _path(tmp_path)
    first = PersistentCheckpoint(path)
    first.mark_done("brief:2026-09-04", datetime(2026, 9, 4, 8, 0, tzinfo=UTC))
    first.record_tool_call(ToolCallRecord(tool="test.ping", target="x", ok=True))
    assert first.is_done("brief:2026-09-04")

    # 模拟进程重启：同一路径新建实例，状态应完整重建
    second = PersistentCheckpoint(path)
    assert second.is_done("brief:2026-09-04")
    assert [t.tool for t in second.tool_calls] == ["test.ping"]


def test_torn_last_line_ignored_without_crash(tmp_path) -> None:
    path = _path(tmp_path)
    path.write_text(
        json.dumps({"type": "done", "key": "brief:2026-09-03", "at": "2026-09-03T08:00:00+00:00"})
        + "\n"
        + '{"type":"done","key":"brief:2026-09-04","at":"2026-09-04T08:00:00'  # 写到一半被 kill
        + "\n",
        encoding="utf-8",
    )
    checkpoint = PersistentCheckpoint(path)  # 不应抛异常
    assert checkpoint.is_done("brief:2026-09-03")
    # 半行不阻断续跑
    assert not checkpoint.is_done("brief:2026-09-04")


def test_missing_file_starts_empty(tmp_path) -> None:
    checkpoint = PersistentCheckpoint(_path(tmp_path))
    assert not checkpoint.is_done("anything")
    assert checkpoint.tool_calls == []