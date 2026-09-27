"""后台巡查的到期调度与业务日历（A05，架构改进路线图 2026-09-25）。

旧实现（`scheduler.run_tick` + `__main__` 里的固定 sleep）有三件说不清的事：

1. 实际两轮间隔 = 任务耗时 + 等待时间，不是固定周期；「下一次什么时候跑」无人能回答；
2. 一次 tick 里三项任务串行，前一项没结束后面不会开始；没有单项超时、没有重试；
3. 日桶直接取 UTC 日期——产品说的「每日」到底是哪个时区的每日，代码里没有答案。

现在把这些显式化：

- `BusinessCalendar` 定义业务日历：日桶 / 小时桶 / 到期时刻；
- `TaskRuntime` 逐任务记录到期时间、最近开始与结束、最近成功与失败、连续失败次数、下次重试；
- `ScheduleStore` 把上述状态原子落盘，进程重启后重试计划不丢；
- 失败按 `RETRY_BACKOFF_SECONDS` 有界重试，用尽后等下一个自然到期时刻（不无限重试）；
- 任何一项失败都不影响其它到期任务（逐任务独立执行、独立记账）。

业务日历取值见 `docs/agent-worker-schedule-decision-2026-09-25.md`：
默认 **UTC+8（北京时间）**、日报到期 **本地 08:00（盘前）**。中国自 1991 年起不实行
夏令时，因此固定偏移与 IANA `Asia/Shanghai` 在业务日界上完全等价；用固定偏移还能避开
`tzdata` 依赖（Windows 的 Python 不自带 IANA 数据库，`zoneinfo` 会直接抛
`ZoneInfoNotFoundError`）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

# 业务时区：北京时间（UTC+8）。
BUSINESS_UTC_OFFSET_MINUTES = 480
# 日报到期时刻（业务本地时间）：盘前 08:00。
DAILY_BRIEF_DUE_HOUR = 8
# 失败后的重试退避（秒）。用尽后不再重试，等下一个自然到期时刻。
RETRY_BACKOFF_SECONDS = (60, 300, 900)
# 单次巡查任务的默认超时（秒）；调用方可按任务覆盖。
DEFAULT_TASK_TIMEOUT_SECONDS = 60.0


def as_utc(value: datetime) -> datetime:
    """统一成带时区的 UTC 时刻：naive 输入按 UTC 解释，避免比较时抛 TypeError。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class BusinessCalendar:
    """业务日历：把「每日 / 每小时」翻译成确定的到期时刻与去重桶 key。"""

    utc_offset_minutes: int = BUSINESS_UTC_OFFSET_MINUTES
    daily_due_hour: int = DAILY_BRIEF_DUE_HOUR

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(minutes=self.utc_offset_minutes))

    def local(self, now: datetime) -> datetime:
        return as_utc(now).astimezone(self.tz)

    def bucket_key(self, period: str, now: datetime) -> str:
        """去重桶 key 的日期部分：日报按业务日，小时任务按业务小时。"""
        local = self.local(now)
        if period == "daily":
            return local.date().isoformat()
        return local.strftime("%Y-%m-%d-%H")

    def due_at(self, period: str, now: datetime) -> datetime:
        """当前业务周期内的到期时刻（UTC）。日报为本地 daily_due_hour:00，小时任务为整点。"""
        local = self.local(now)
        if period == "daily":
            anchor = local.replace(hour=self.daily_due_hour, minute=0, second=0, microsecond=0)
        else:
            anchor = local.replace(minute=0, second=0, microsecond=0)
        return anchor.astimezone(UTC)

    def next_due_at(self, period: str, now: datetime) -> datetime:
        """下一个业务周期的到期时刻（UTC）。"""
        local = self.local(now)
        if period == "daily":
            anchor = (local + timedelta(days=1)).replace(
                hour=self.daily_due_hour, minute=0, second=0, microsecond=0
            )
        else:
            anchor = (local + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return anchor.astimezone(UTC)

    def is_due(self, period: str, now: datetime) -> bool:
        """当前周期是否已到期。

        休眠/长时间停顿后只认**当前**周期：不回补错过的历史桶——一小时级的巡查补跑
        只会堆出一串无意义的过期请求，而日报补跑多天更是重复消耗模型调用。
        """
        return as_utc(now) >= self.due_at(period, now)


class TaskRuntime(BaseModel):
    """单个巡查任务的运行状态（可落盘、可呈现「最近成功 / 最近失败 / 下次运行」）。"""

    task: str
    period: str
    due_at: datetime | None = None
    next_run_at: datetime | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_ok: bool | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    retry_at: datetime | None = None
    runs_ok: int = 0
    runs_failed: int = 0


class ScheduleStore:
    """调度状态的持久化：`<name>.schedule.json`，原子覆写。

    与 checkpoint 的分工：checkpoint 存「哪些桶已经跑过」（幂等去重），
    store 存「什么时候该跑、上次结果如何、要不要重试」。进程重启后两者都要恢复，
    因此重试计划不会因为一次重启而被丢掉或无限重复。
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, TaskRuntime] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, dict):
            return
        for name, item in tasks.items():
            try:
                self._tasks[name] = TaskRuntime.model_validate(item)
            except ValueError:
                continue

    def get(self, task: str, period: str) -> TaskRuntime:
        runtime = self._tasks.get(task)
        if runtime is None:
            runtime = TaskRuntime(task=task, period=period)
            self._tasks[task] = runtime
        return runtime

    def save(self) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "tasks": {
                name: runtime.model_dump(mode="json") for name, runtime in self._tasks.items()
            },
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def snapshot(self) -> dict[str, TaskRuntime]:
        return dict(self._tasks)


def retry_delay(consecutive_failures: int) -> int | None:
    """第 n 次连续失败后的重试等待秒数；超出退避表返回 None（等下一个自然到期时刻）。"""
    if consecutive_failures <= 0 or consecutive_failures > len(RETRY_BACKOFF_SECONDS):
        return None
    return RETRY_BACKOFF_SECONDS[consecutive_failures - 1]
