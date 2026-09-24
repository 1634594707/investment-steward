"""Worker 调度循环（D4）：对 Core 主动巡查，把每次调用记为结构化工具调用入账。

- D4 定时巡检：日报（每日一次）、证据新鲜度（按小时桶）、通知评估（按小时桶）。
- D3 工具调用：每步经 `checkpoint.record_tool_call` 入账，重启可从账本续看。

研究执行已按用户定案改为全手动（F2，2026-09-05）：worker 不再消费 `/research/runs`
队列，也不自动推进研究；研究 run 由用户在前端点「运行」触发 `POST /research/runs/{id}/run`。

去重策略（D2）：日报/巡检/通知按时间桶 `key` 去重，标记已落则本桶不再触发。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from investment_steward_agent_worker.checkpoint import PersistentCheckpoint, ToolCallRecord
from investment_steward_agent_worker.client import CoreClient, CoreClientError


def _day(now: datetime) -> str:
    return now.date().isoformat()


def _hour_bucket(now: datetime) -> str:
    return now.strftime("%Y-%m-%d-%H")


def _run_safe(
    client: object,
    checkpoint: PersistentCheckpoint,
    tool: str,
    target: str,
    call: Callable[[], Any],
) -> tuple[ToolCallRecord, Any]:
    """执行一次工具调用并记账；异常记为失败调用，不中断其他任务。"""
    try:
        result = call()
    except CoreClientError as exc:
        record = ToolCallRecord(tool=tool, target=target, run_id=None, ok=False, note=str(exc))
        checkpoint.record_tool_call(record)
        return record, None
    except Exception as exc:  # noqa: BLE001 - one broken task must not kill the worker loop
        record = ToolCallRecord(
            tool=tool,
            target=target,
            run_id=None,
            ok=False,
            note=f"未分类任务异常：{type(exc).__name__}: {exc}",
        )
        checkpoint.record_tool_call(record)
        return record, None
    record = ToolCallRecord(tool=tool, target=target, run_id=None, ok=True, note="")
    checkpoint.record_tool_call(record)
    return record, result


def run_tick(
    client: CoreClient,
    checkpoint: PersistentCheckpoint,
    now: datetime | None = None,
) -> list[ToolCallRecord]:
    """执行一次完整的主动巡查：返回本 tick 记录的工具调用（也已写入 checkpoint 账本）。"""
    now = now or datetime.now(UTC)
    performed: list[ToolCallRecord] = []

    def record(entry: ToolCallRecord) -> None:
        performed.append(entry)

    # --- D4 日报：每日一次，按天去重 ---
    brief_key = f"brief:{_day(now)}"
    if not checkpoint.is_done(brief_key):
        rec, result = _run_safe(
            client, checkpoint, "brief.today.generate", "/brief/today/generate",
            client.generate_today_brief,
        )
        record(rec)
        if result is not None:
            checkpoint.mark_done(brief_key, now)
    else:
        record(ToolCallRecord(tool="brief.today.skip", target="/brief/today/generate", ok=True, note="本日已生成"))

    # --- D4 证据新鲜度巡检：按小时桶 ---
    patrol_bucket = f"patrol:{_hour_bucket(now)}"
    if not checkpoint.is_done(patrol_bucket):
        rec, result = _run_safe(
            client, checkpoint, "evidence.freshness.patrol", "/evidence/freshness/patrol",
            client.patrol_evidence,
        )
        record(rec)
        if result is not None:
            checkpoint.mark_done(patrol_bucket, now)

    # --- D4 通知评估：按小时桶 ---
    notif_bucket = f"notification:{_hour_bucket(now)}"
    if not checkpoint.is_done(notif_bucket):
        rec, result = _run_safe(
            client, checkpoint, "notification.evaluate", "/notifications/evaluate",
            client.evaluate_notifications,
        )
        record(rec)
        if result is not None:
            checkpoint.mark_done(notif_bucket, now)

    return performed
