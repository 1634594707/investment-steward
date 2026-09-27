"""A05（架构改进路线图 2026-09-25）：后台调度语义的验收。

改造前：一个 while 循环「先跑 tick、再等 tick_seconds」，实际间隔 = 任务耗时 + 等待时间；
三项任务串行互堵；没有单项超时与重试；日桶取 UTC 日期，「每日」的业务定义无人回答。
本文件锁定改造后的六条性质：

1. **业务日历**：日报按业务日（默认北京时间）去重，到期时刻为本地 08:00；
2. **跨日**：跨业务日必再触发，同一业务日内不重复；
3. **休眠恢复**：长时间停顿后只跑当前桶，不回补历史桶（不产生补跑风暴）；
4. **单项超时**：一项超时/失败不影响其它到期任务，且按失败记账可重试；
5. **有界重试**：退避用尽后等下一个自然到期时刻，不无限重试；
6. **进程重启**：去重标记与重试计划都能从磁盘恢复；研究任务始终不在自动巡查范围内。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from investment_steward_agent_worker.checkpoint import PersistentCheckpoint
from investment_steward_agent_worker.client import CoreClientError
from investment_steward_agent_worker.schedule import (
    RETRY_BACKOFF_SECONDS,
    BusinessCalendar,
    ScheduleStore,
)
from investment_steward_agent_worker.scheduler import TASKS, run_tick

CAL = BusinessCalendar()  # 默认 UTC+8、日报本地 08:00 到期


class FakeClient:
    def __init__(self) -> None:
        self.counts = {"brief": 0, "patrol": 0, "notif": 0}
        self.fail: set[str] = set()
        self.timeout: set[str] = set()
        self.timeouts_seen: list[float | None] = []

    def generate_today_brief(self, *, timeout: float | None = None) -> dict[str, Any]:
        self.counts["brief"] += 1
        self.timeouts_seen.append(timeout)
        if "brief" in self.timeout:
            raise CoreClientError("/brief/today/generate -> 超时（180.0s）", timed_out=True)
        if "brief" in self.fail:
            raise CoreClientError("/brief/today/generate -> HTTP 500", status=500)
        return {"brief_id": "b1"}

    def patrol_evidence(self, *, timeout: float | None = None) -> dict[str, Any]:
        self.counts["patrol"] += 1
        self.timeouts_seen.append(timeout)
        if "patrol" in self.timeout:
            raise CoreClientError("/evidence/freshness/patrol -> 超时（60.0s）", timed_out=True)
        if "patrol" in self.fail:
            raise CoreClientError("/evidence/freshness/patrol -> HTTP 503", status=503)
        return {"checked": 1, "stale": 0, "stale_ids": []}

    def evaluate_notifications(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        self.counts["notif"] += 1
        self.timeouts_seen.append(timeout)
        if "notif" in self.timeout:
            raise CoreClientError("/notifications/evaluate -> 超时（60.0s）", timed_out=True)
        if "notif" in self.fail:
            raise CoreClientError("/notifications/evaluate -> HTTP 503", status=503)
        return []


@pytest.fixture()
def env(tmp_path):
    """一套独立的 checkpoint + schedule store（同一目录，模拟真实部署布局）。"""
    checkpoint = PersistentCheckpoint(tmp_path / "agent-worker.checkpoint.jsonl")
    store = ScheduleStore(tmp_path / "agent-worker.schedule.json")
    return tmp_path, checkpoint, store


def _tick(client, checkpoint, store, now):
    return run_tick(client, checkpoint, now, calendar=CAL, store=store)


# ---------------------------------------------------------------------------
# 1~2. 业务日历与跨日
# ---------------------------------------------------------------------------


def test_brief_follows_business_day_not_utc_day(env):
    """日报按业务日（北京时间）去重，而不是 UTC 日期。"""
    _tmp, checkpoint, store = env
    client = FakeClient()

    # UTC 2026-09-24 23:30 = 北京 2026-09-25 07:30：未到本地 08:00，不应触发。
    _tick(client, checkpoint, store, datetime(2026, 9, 24, 23, 30, tzinfo=UTC))
    assert client.counts["brief"] == 0, "未到业务到期时刻不应生成日报"

    # UTC 2026-09-25 00:05 = 北京 08:05：已到期，触发一次。
    _tick(client, checkpoint, store, datetime(2026, 9, 25, 0, 5, tzinfo=UTC))
    assert client.counts["brief"] == 1

    # 同一业务日内的后续 tick 不重复。
    _tick(client, checkpoint, store, datetime(2026, 9, 25, 10, 0, tzinfo=UTC))
    assert client.counts["brief"] == 1

    # 跨业务日（北京 09-26 08:05）再触发一次。
    _tick(client, checkpoint, store, datetime(2026, 9, 26, 0, 5, tzinfo=UTC))
    assert client.counts["brief"] == 2


def test_hourly_tasks_follow_local_hour_buckets(env):
    _tmp, checkpoint, store = env
    client = FakeClient()

    _tick(client, checkpoint, store, datetime(2026, 9, 25, 0, 5, tzinfo=UTC))
    assert client.counts["patrol"] == 1 and client.counts["notif"] == 1

    # 同一业务小时（北京 08:xx）不重复
    _tick(client, checkpoint, store, datetime(2026, 9, 25, 0, 40, tzinfo=UTC))
    assert client.counts["patrol"] == 1 and client.counts["notif"] == 1

    # 跨到下一业务小时（北京 09:xx）
    _tick(client, checkpoint, store, datetime(2026, 9, 25, 1, 5, tzinfo=UTC))
    assert client.counts["patrol"] == 2 and client.counts["notif"] == 2


# ---------------------------------------------------------------------------
# 3. 休眠恢复
# ---------------------------------------------------------------------------


def test_long_suspend_runs_only_current_bucket(env):
    """系统休眠 26 小时后恢复：只跑当前桶，不补跑错过的历史桶。"""
    _tmp, checkpoint, store = env
    client = FakeClient()

    _tick(client, checkpoint, store, datetime(2026, 9, 25, 0, 5, tzinfo=UTC))
    assert (client.counts["brief"], client.counts["patrol"], client.counts["notif"]) == (1, 1, 1)

    # 26 小时无 tick（休眠/进程停摆）
    _tick(client, checkpoint, store, datetime(2026, 9, 26, 2, 5, tzinfo=UTC))
    assert (client.counts["brief"], client.counts["patrol"], client.counts["notif"]) == (2, 2, 2), (
        "休眠恢复只应补当前桶一次，不能按错过的小时数补跑"
    )


# ---------------------------------------------------------------------------
# 4. 单项失败 / 超时不吞掉其它任务
# ---------------------------------------------------------------------------


def test_task_failure_does_not_block_other_due_tasks(env):
    _tmp, checkpoint, store = env
    client = FakeClient()
    client.fail = {"brief", "patrol"}

    performed = _tick(client, checkpoint, store, datetime(2026, 9, 25, 0, 5, tzinfo=UTC))

    assert client.counts["brief"] == 1 and client.counts["patrol"] == 1
    assert client.counts["notif"] == 1, "前两项失败不得阻止第三项到期任务"
    by_tool = {entry.tool: entry for entry in performed}
    assert by_tool["brief.today.generate"].ok is False
    assert by_tool["evidence.freshness.patrol"].ok is False
    assert by_tool["notification.evaluate"].ok is True


def test_per_task_timeout_recorded_as_timeout_failure(env):
    """单项超时按超时归类（可重试），且不影响其它任务；超时预算按任务传入。"""
    _tmp, checkpoint, store = env
    client = FakeClient()
    client.timeout = {"brief"}

    performed = _tick(client, checkpoint, store, datetime(2026, 9, 25, 0, 5, tzinfo=UTC))

    brief = next(entry for entry in performed if entry.tool == "brief.today.generate")
    assert brief.ok is False and "超时" in brief.note
    assert client.counts["patrol"] == 1 and client.counts["notif"] == 1
    # 每项任务带自己的超时预算：日报 180s，其余 60s（旧实现是全局 15s）。
    assert 180.0 in client.timeouts_seen and 60.0 in client.timeouts_seen


# ---------------------------------------------------------------------------
# 5. 有界重试
# ---------------------------------------------------------------------------


def test_retry_backoff_then_wait_for_next_due(env):
    """失败按退避重试；退避用尽后等下一个自然到期时刻，不无限重试。"""
    _tmp, checkpoint, store = env
    client = FakeClient()
    client.fail = {"brief"}
    base = datetime(2026, 9, 25, 0, 5, tzinfo=UTC)

    runtime = store.get("brief", "daily")
    for attempt in range(1, len(RETRY_BACKOFF_SECONDS) + 1):
        _tick(client, checkpoint, store, base + timedelta(seconds=attempt * 4000))
        assert runtime.consecutive_failures == attempt
        assert runtime.retry_at is not None, f"第 {attempt} 次失败后应安排重试"

    # 第 len+1 次失败：退避表用尽 → 不再安排重试，等下一个自然到期时刻。
    _tick(client, checkpoint, store, base + timedelta(days=1))
    assert runtime.consecutive_failures == len(RETRY_BACKOFF_SECONDS) + 1
    assert runtime.retry_at is None, "退避用尽后不应继续安排重试"
    assert runtime.next_run_at is not None

    # 重试成功后连续失败计数归零。
    client.fail = set()
    _tick(client, checkpoint, store, base + timedelta(days=2))
    assert runtime.consecutive_failures == 0
    assert runtime.last_success_at is not None
    assert runtime.last_error == ""


def test_retry_wait_skips_without_calling_core(env):
    """退避窗口内不重复打 Core，但状态里能看出「在等重试」。"""
    _tmp, checkpoint, store = env
    client = FakeClient()
    client.fail = {"brief"}
    base = datetime(2026, 9, 25, 0, 5, tzinfo=UTC)

    _tick(client, checkpoint, store, base)
    assert client.counts["brief"] == 1
    performed = _tick(client, checkpoint, store, base + timedelta(seconds=5))
    assert client.counts["brief"] == 1, "退避窗口内不得重复调用"
    assert any(entry.tool == "brief.today.retry_wait" for entry in performed)


# ---------------------------------------------------------------------------
# 6. 进程重启
# ---------------------------------------------------------------------------


def test_retry_plan_and_done_markers_survive_restart(env):
    _tmp, checkpoint, store = env
    client = FakeClient()
    client.fail = {"brief"}
    base = datetime(2026, 9, 25, 0, 5, tzinfo=UTC)
    _tick(client, checkpoint, store, base)
    retry_at = store.get("brief", "daily").retry_at
    assert retry_at is not None

    # 模拟进程重启：同一目录重建两个存储实例。
    checkpoint2 = PersistentCheckpoint(_tmp / "agent-worker.checkpoint.jsonl")
    store2 = ScheduleStore(_tmp / "agent-worker.schedule.json")
    assert store2.get("brief", "daily").retry_at == retry_at, "重试计划必须跨重启保留"

    client2 = FakeClient()
    # 重启后仍在退避窗口内 → 不重发。
    _tick(client2, checkpoint2, store2, base + timedelta(seconds=10))
    assert client2.counts["brief"] == 0

    # 退避窗口过后 → 按计划重试，这次成功。
    client2.fail = set()
    _tick(client2, checkpoint2, store2, base + timedelta(seconds=120))
    assert client2.counts["brief"] == 1
    assert store2.get("brief", "daily").last_ok is True

    # 日报已成功过的桶在重启后依然不重发。
    client3 = FakeClient()
    checkpoint3 = PersistentCheckpoint(_tmp / "agent-worker.checkpoint.jsonl")
    store3 = ScheduleStore(_tmp / "agent-worker.schedule.json")
    _tick(client3, checkpoint3, store3, base + timedelta(hours=6))
    assert client3.counts["brief"] == 0, "已完成的桶重启后不得重发"


def test_schedule_store_survives_corrupt_file(tmp_path):
    """调度状态文件损坏时按空状态起步，不阻断 worker 启动。"""
    path = tmp_path / "agent-worker.schedule.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    store = ScheduleStore(path)
    assert store.snapshot() == {}
    store.get("brief", "daily")
    store.save()
    assert ScheduleStore(path).get("brief", "daily").task == "brief"


# ---------------------------------------------------------------------------
# 研究仍由用户手动启动
# ---------------------------------------------------------------------------


def test_no_research_task_in_automatic_patrol():
    """自动巡查范围里不得出现研究任务（F2 定案：研究全手动）。"""
    targets = [spec.target for spec in TASKS]
    assert targets == ["/brief/today/generate", "/evidence/freshness/patrol", "/notifications/evaluate"]
    assert not any("research" in target for target in targets)


def test_status_snapshot_exposes_last_success_failure_and_next_run(env):
    """状态快照必须能回答「最近成功 / 最近失败 / 下次运行」。"""
    _tmp, checkpoint, store = env
    client = FakeClient()
    client.fail = {"patrol"}
    now = datetime(2026, 9, 25, 0, 5, tzinfo=UTC)
    _tick(client, checkpoint, store, now)

    snapshot = store.snapshot()
    assert set(snapshot) == {"brief", "patrol", "notifications"}
    brief = snapshot["brief"]
    assert brief.last_success_at is not None and brief.last_ok is True
    assert brief.next_run_at is not None
    patrol = snapshot["patrol"]
    assert patrol.last_failure_at is not None and patrol.last_ok is False
    assert "503" in patrol.last_error
    assert patrol.next_run_at is not None
