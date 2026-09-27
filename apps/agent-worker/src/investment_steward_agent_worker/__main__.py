from __future__ import annotations

import argparse
import json
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path

from investment_steward_agent_worker.checkpoint import PersistentCheckpoint
from investment_steward_agent_worker.client import CoreClient
from investment_steward_agent_worker.schedule import (
    BUSINESS_UTC_OFFSET_MINUTES,
    DAILY_BRIEF_DUE_HOUR,
    BusinessCalendar,
    ScheduleStore,
    as_utc,
)
from investment_steward_agent_worker.scheduler import TASKS, run_tick

# 唤醒精度上限：即使下一项任务还很久，也至少每分钟醒一次，
# 这样 SIGTERM/配置变更/系统休眠恢复都能被及时感知。
MAX_SLEEP_SECONDS = 60.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Investment Steward Agent Worker")
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--core-url", type=str, required=True)
    parser.add_argument("--session-token", type=str, required=True)
    parser.add_argument("--tick-seconds", type=int, default=60)
    # A05：业务日历显式化（见 docs/agent-worker-schedule-decision-2026-09-25.md）。
    parser.add_argument(
        "--business-utc-offset-minutes",
        type=int,
        default=BUSINESS_UTC_OFFSET_MINUTES,
        help="业务时区相对 UTC 的分钟偏移（默认 480 = 北京时间；中国无夏令时，固定偏移即精确）",
    )
    parser.add_argument(
        "--daily-due-hour",
        type=int,
        default=DAILY_BRIEF_DUE_HOUR,
        help="日报在业务本地时间的到期小时（默认 8 = 盘前）",
    )
    args = parser.parse_args()

    stop_waiter = threading.Event()

    def stop(_signal: int, _frame: object) -> None:
        stop_waiter.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    args.status_file.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = PersistentCheckpoint(args.status_file.with_name("agent-worker.checkpoint.jsonl"))
    store = ScheduleStore(args.status_file.with_name("agent-worker.schedule.json"))
    calendar = BusinessCalendar(
        utc_offset_minutes=args.business_utc_offset_minutes,
        daily_due_hour=args.daily_due_hour,
    )
    # 默认超时仍保留 15s 作为兜底；每项巡查任务在 scheduler.TASKS 里自带更贴合的超时预算。
    client = CoreClient(args.core_url, args.session_token, timeout=15)

    def write_status(kind: str, **extra: object) -> None:
        payload = {
            "status": kind,
            "version": "0.2.0",
            "ts": datetime.now(UTC).isoformat(),
            # A04：`tool_calls` 是内存里的近期窗口（有界），`ledger_records` 是活动账本
            # 内的条数。二者都不再随运行时长无界增长；全量历史走 checkpoint.query_tool_calls。
            "tool_calls": len(checkpoint.tool_calls),
            "ledger_records": checkpoint.recorded_total,
            # A05：业务日历显式呈现，避免「每日」到底按哪个时区算无人能答。
            "business_calendar": {
                "utc_offset_minutes": calendar.utc_offset_minutes,
                "daily_due_hour": calendar.daily_due_hour,
            },
            "tasks": {
                name: runtime.model_dump(mode="json")
                for name, runtime in store.snapshot().items()
            },
            **extra,
        }
        args.status_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def seconds_until_next_due(now: datetime) -> float:
        """到最近一项任务到期时刻的秒数；已到期返回 0。"""
        waits = []
        for spec in TASKS:
            runtime = store.get(spec.name, spec.period)
            target = runtime.next_run_at or runtime.retry_at or calendar.due_at(spec.period, now)
            waits.append(max((as_utc(target) - now).total_seconds(), 0.0))
        return min(waits) if waits else MAX_SLEEP_SECONDS

    write_status("running", reason="start")
    while not stop_waiter.is_set():
        now = datetime.now(UTC)
        performed = run_tick(client, checkpoint, now, calendar=calendar, store=store)
        write_status(
            "running",
            reason="tick",
            ran=len(performed),
            ok=sum(1 for entry in performed if entry.ok),
            last_at=now.isoformat(),
            # 唤醒点按「最近一项到期时刻」算，而不是固定 60s：实际间隔不再等于任务耗时加等待。
            next_wake_in_seconds=round(seconds_until_next_due(now), 1),
        )
        # 睡到下一个到期时刻（或上限），而不是睡满固定周期。
        stop_waiter.wait(timeout=max(min(seconds_until_next_due(datetime.now(UTC)), float(args.tick_seconds)), 1.0))

    write_status("stopped")
    stop_waiter.set()


if __name__ == "__main__":
    main()
