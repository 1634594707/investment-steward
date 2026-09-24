"""Worker 调度器检查点与审计账本：追加式 JSONL，崩溃后可从断点续跑不重发。

每个事件一行 JSON，追加写 + fsync。启动时按行回放重建状态：
- `done` 行：任务完成标记（key → 时间），用于幂等/去重。
- `tool_call` 行：结构化工具调用（D3 审记得卷），可平铺审计"每一步"。
渲染"kill worker 重启不丢运行、不重发"验收：已完成标记在重启后仍生效。
末行损坏（崩溃时写到一半）时整行丢弃，不影响既有权重回放。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class ToolCallRecord(BaseModel):
    """一次对 Core 的工具调用（D3）：可追到调了什么、针对哪个运行、结果如何。"""

    tool: str
    target: str
    run_id: str | None = None
    ok: bool
    note: str = ""
    at: datetime = Field(default_factory=_now)


class PersistentCheckpoint:
    """代理 worker 的有状态账本。线程安全由调度循环串行访问保证（单 worker 单循环）。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._done: dict[str, datetime] = {}
        self.tool_calls: list[ToolCallRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if not line:
                    continue
                record = _parse_line(line)
                if record is None:
                    continue
                self._apply(record)

    def _apply(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "done":
            key = record.get("key")
            at = record.get("at")
            if isinstance(key, str) and at is not None:
                self._done[key] = _coerce_dt(at)
        elif kind == "tool_call":
            payload = record.get("rec")
            if isinstance(payload, dict):
                try:
                    self.tool_calls.append(ToolCallRecord.model_validate(payload))
                except ValueError:  # 单行损坏丢掉即可，不阻断恢复
                    return

    def is_done(self, key: str) -> bool:
        return key in self._done

    def mark_done(self, key: str, at: datetime | None = None) -> None:
        at = at or _now()
        self._done[key] = at
        self._append({"type": "done", "key": key, "at": at.isoformat()})

    def record_tool_call(self, record: ToolCallRecord) -> None:
        self.tool_calls.append(record)
        self._append({"type": "tool_call", "rec": record.model_dump(mode="json")})

    def _append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            if hasattr(handle, "fileno"):
                import os

                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass


def _parse_line(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _coerce_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)