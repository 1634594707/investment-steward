"""D4 调度去重与工具调用入账。"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_steward_agent_worker.checkpoint import PersistentCheckpoint
from investment_steward_agent_worker.client import CoreClientError
from investment_steward_agent_worker.scheduler import run_tick

DAY_1 = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
DAY_2 = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.counts = {"brief": 0, "patrol": 0, "notif": 0}
        self.fail: set[str] = set()

    def generate_today_brief(self) -> dict[str, Any]:
        self.counts["brief"] += 1
        if "brief" in self.fail:
            raise CoreClientError("/brief/today/generate -> HTTP 500", status=500)
        return {"brief_id": "b1"}

    def patrol_evidence(self) -> dict[str, Any]:
        self.counts["patrol"] += 1
        return {"checked": 1, "stale": 0, "stale_ids": []}

    def evaluate_notifications(self) -> list[dict[str, Any]]:
        self.counts["notif"] += 1
        return []


def _tools(records) -> list[str]:
    return [entry.tool for entry in records]


def test_daily_brief_once_per_day_and_rolls_over(tmp_path) -> None:
    client = FakeClient()
    checkpoint = PersistentCheckpoint(tmp_path / "c.jsonl")

    run_tick(client, checkpoint, DAY_1)
    assert client.counts["brief"] == 1
    assert "brief.today.generate" in _tools(checkpoint.tool_calls)

    # 同日本应去重：再次 tick 不应重复调用
    run_tick(client, checkpoint, DAY_1)
    assert client.counts["brief"] == 1

    # 跨天应再次触发
    run_tick(client, checkpoint, DAY_2)
    assert client.counts["brief"] == 2


def test_hourly_buckets_dedup_patrol_and_notification(tmp_path) -> None:
    client = FakeClient()
    checkpoint = PersistentCheckpoint(tmp_path / "c.jsonl")
    hour1 = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    hour2 = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)

    run_tick(client, checkpoint, hour1)
    assert client.counts["patrol"] == 1 and client.counts["notif"] == 1

    run_tick(client, checkpoint, hour1)
    assert client.counts["patrol"] == 1 and client.counts["notif"] == 1

    run_tick(client, checkpoint, hour2)
    assert client.counts["patrol"] == 2 and client.counts["notif"] == 2


def test_failed_call_recorded_but_other_tasks_proceed(tmp_path) -> None:
    client = FakeClient()
    client.fail = {"brief"}
    checkpoint = PersistentCheckpoint(tmp_path / "c.jsonl")

    run_tick(client, checkpoint, DAY_1)
    tools = _tools(checkpoint.tool_calls)
    assert client.counts["brief"] == 1
    brief_entry = next(e for e in checkpoint.tool_calls if e.tool == "brief.today.generate")
    assert brief_entry.ok is False
    # 失败不阻断巡检
    assert client.counts["patrol"] == 1
    assert "evidence.freshness.patrol" in tools