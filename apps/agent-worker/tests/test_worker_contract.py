"""D4 worker 契约测试：崩溃恢复续跑不重发、审计完整。"""
from __future__ import annotations

from datetime import UTC, datetime

from investment_steward_agent_worker.checkpoint import PersistentCheckpoint
from investment_steward_agent_worker.scheduler import run_tick
from test_worker_scheduler import FakeClient

DAY_1 = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def test_crash_recovery_resume_without_replay(tmp_path) -> None:
    """崩溃前已落账；重启后构建新实例，同 tick 不重发日报、不重跑巡检。"""
    client = FakeClient()
    path = tmp_path / "c.jsonl"

    first = PersistentCheckpoint(path)
    run_tick(client, first, DAY_1)
    assert client.counts["brief"] == 1
    assert client.counts["patrol"] == 1

    # 模拟 kill + 重启：同一 checkpoint 文件重建实例，审计可续看（上一进程记录完整带出）
    restarted = PersistentCheckpoint(path)
    carried = {entry.tool for entry in restarted.tool_calls}
    assert "brief.today.generate" in carried
    assert "evidence.freshness.patrol" in carried

    run_tick(client, restarted, DAY_1)
    assert client.counts["brief"] == 1  # 日报不重发
    assert client.counts["patrol"] == 1  # 巡检不重跑


def test_audit_completeness_every_step_recorded(tmp_path) -> None:
    """一次完整 tick：日报、巡检、通知 全部入账。"""
    client = FakeClient()
    checkpoint = PersistentCheckpoint(tmp_path / "c.jsonl")

    run_tick(client, checkpoint, DAY_1)

    tools = {entry.tool for entry in checkpoint.tool_calls}
    assert {
        "brief.today.generate",
        "evidence.freshness.patrol",
        "notification.evaluate",
    } <= tools