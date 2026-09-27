"""Worker 调度循环（D4 + A05）：对 Core 主动巡查，把每次调用记为结构化工具调用入账。

A05（架构改进路线图 2026-09-25）改造要点：

- **到期调度替代固定 sleep**：每项任务有自己的业务周期与到期时刻（`BusinessCalendar`），
  「下一次什么时候跑」是可计算的，不再等于「任务耗时 + 等待时间」；
- **逐任务独立执行**：一项失败（含超时）不影响其它到期任务，也不再串行互堵；
- **单项超时**：每项任务带自己的 `timeout_seconds`，超时按失败记账并可重试；
- **有界重试**：失败按 `RETRY_BACKOFF_SECONDS` 重试，用尽后等下一个自然到期时刻；
- **状态可呈现**：`ScheduleStore` 记录最近成功 / 最近失败 / 下次运行，落盘可跨重启。

研究执行仍按用户定案为全手动（F2，2026-09-05）：worker 不消费 `/research/runs` 队列，
也不自动推进研究；研究 run 由用户在前端点「运行」触发 `POST /research/runs/{id}/run`。
本模块的 `TASKS` 里不存在任何研究任务——这是有意为之，别往里加。

去重策略（D2）：日报/巡检/通知按业务时间桶 `key` 去重，标记已落则本桶不再触发。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from investment_steward_agent_worker.checkpoint import PersistentCheckpoint, ToolCallRecord
from investment_steward_agent_worker.client import CoreClient, CoreClientError
from investment_steward_agent_worker.schedule import (
    DEFAULT_TASK_TIMEOUT_SECONDS,
    BusinessCalendar,
    ScheduleStore,
    as_utc,
    retry_delay,
)


@dataclass(frozen=True)
class TaskSpec:
    """一项主动巡查任务：业务周期、到期规则、超时预算、要打的端点。"""

    name: str
    tool: str
    target: str
    period: str  # "daily" | "hourly"
    call: Callable[[CoreClient, float], Any]
    timeout_seconds: float = DEFAULT_TASK_TIMEOUT_SECONDS


# 顺序即执行顺序；三项互相独立，任何一项失败都不影响后面的。
TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        name="brief",
        tool="brief.today.generate",
        target="/brief/today/generate",
        period="daily",
        # 日报要跑模型，超时预算放宽（旧实现的全局 15s 对深研档明显不够）。
        call=lambda client, timeout: client.generate_today_brief(timeout=timeout),
        timeout_seconds=180.0,
    ),
    TaskSpec(
        name="patrol",
        tool="evidence.freshness.patrol",
        target="/evidence/freshness/patrol",
        period="hourly",
        call=lambda client, timeout: client.patrol_evidence(timeout=timeout),
    ),
    TaskSpec(
        name="notifications",
        tool="notification.evaluate",
        target="/notifications/evaluate",
        period="hourly",
        call=lambda client, timeout: client.evaluate_notifications(timeout=timeout),
    ),
)


def _classify(exc: BaseException) -> str:
    if isinstance(exc, CoreClientError):
        return f"超时：{exc}" if getattr(exc, "timed_out", False) else str(exc)
    return f"未分类任务异常：{type(exc).__name__}: {exc}"


def run_tick(
    client: CoreClient,
    checkpoint: PersistentCheckpoint,
    now: datetime | None = None,
    *,
    calendar: BusinessCalendar | None = None,
    store: ScheduleStore | None = None,
) -> list[ToolCallRecord]:
    """执行一次调度检查：只跑**已到期且本桶未跑过**的任务。

    返回本次实际发生（或明确跳过）的工具调用记录，用于状态文件展示。
    返回值含跳过项是刻意的：状态文件要能解释「这一轮为什么什么都没干」。
    """
    now = as_utc(now or datetime.now(UTC))
    calendar = calendar or BusinessCalendar()
    performed: list[ToolCallRecord] = []

    for spec in TASKS:
        runtime = store.get(spec.name, spec.period) if store is not None else None
        if runtime is not None:
            runtime.due_at = calendar.due_at(spec.period, now)

        bucket = f"{spec.name}:{calendar.bucket_key(spec.period, now)}"

        # 1) 本桶已完成 → 跳过（幂等去重，重启后依然生效）。
        if checkpoint.is_done(bucket):
            if runtime is not None:
                runtime.next_run_at = calendar.next_due_at(spec.period, now)
                runtime.retry_at = None
            performed.append(
                ToolCallRecord(
                    tool=f"{spec.tool.rsplit('.', 1)[0]}.skip",
                    target=spec.target,
                    ok=True,
                    note=f"本桶已完成（{bucket}）",
                    at=now,
                )
            )
            continue

        # 2) 未到期 → 跳过（业务日历决定，而不是「还没轮到循环」）。
        if not calendar.is_due(spec.period, now):
            if runtime is not None:
                runtime.next_run_at = calendar.due_at(spec.period, now)
            performed.append(
                ToolCallRecord(
                    tool=f"{spec.tool.rsplit('.', 1)[0]}.not_due",
                    target=spec.target,
                    ok=True,
                    note=f"未到到期时刻（{runtime.next_run_at if runtime else '—'}）",
                    at=now,
                )
            )
            continue

        # 3) 失败退避中 → 跳过（等重试时刻，不重复打 Core）。
        if runtime is not None and runtime.retry_at is not None and now < as_utc(runtime.retry_at):
            runtime.next_run_at = runtime.retry_at
            performed.append(
                ToolCallRecord(
                    tool=f"{spec.tool.rsplit('.', 1)[0]}.retry_wait",
                    target=spec.target,
                    ok=True,
                    note=f"等待重试（{runtime.retry_at}）",
                    at=now,
                )
            )
            continue

        # 4) 执行（独立记账；异常一律转成失败记录，不冒泡打断其它任务）。
        if runtime is not None:
            runtime.last_started_at = now
        try:
            spec.call(client, spec.timeout_seconds)
        except CoreClientError as exc:
            record = ToolCallRecord(
                tool=spec.tool, target=spec.target, ok=False, note=_classify(exc), at=now
            )
        except Exception as exc:  # noqa: BLE001 - 一项坏任务不能打死整个 worker 循环
            record = ToolCallRecord(
                tool=spec.tool, target=spec.target, ok=False, note=_classify(exc), at=now
            )
        else:
            record = ToolCallRecord(tool=spec.tool, target=spec.target, ok=True, note="", at=now)

        checkpoint.record_tool_call(record)
        performed.append(record)

        if runtime is not None:
            runtime.last_finished_at = now
            runtime.last_ok = record.ok
            if record.ok:
                runtime.last_success_at = now
                runtime.last_error = ""
                runtime.consecutive_failures = 0
                runtime.retry_at = None
                runtime.next_run_at = calendar.next_due_at(spec.period, now)
                runtime.runs_ok += 1
            else:
                runtime.last_failure_at = now
                runtime.last_error = record.note
                runtime.consecutive_failures += 1
                runtime.runs_failed += 1
                delay = retry_delay(runtime.consecutive_failures)
                if delay is None:
                    runtime.retry_at = None
                    runtime.next_run_at = calendar.next_due_at(spec.period, now)
                else:
                    runtime.retry_at = now.replace(microsecond=0) + timedelta(seconds=delay)
                    runtime.next_run_at = runtime.retry_at

        if record.ok:
            checkpoint.mark_done(bucket, now)

    if store is not None:
        store.save()
    return performed
