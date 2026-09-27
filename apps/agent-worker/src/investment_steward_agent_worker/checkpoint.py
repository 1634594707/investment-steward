"""Worker 调度器检查点与审计账本：崩溃后可从断点续跑不重发。

A04（架构改进路线图 2026-09-25）把两件性质完全不同的事拆成两份存储：

- **去重状态**（`done` 标记：哪个时间桶已经跑过）→ 快照文件 `<name>.state.json`，
  原子覆写。它决定「重启后不重发」，必须小而可靠，且**与账本解耦**。
- **历史调用记录**（每次工具调用一行）→ 追加式 JSONL `<name>.jsonl`，按大小轮换、
  按保留期清理；可回溯，但不需要全量常驻内存。

为什么拆：旧实现把两者塞进同一个追加文件，启动时逐行回放整个账本，且 `tool_calls`
列表随运行时间无界增长——托盘驻留越久，启动回放量越大、内存越高。拆开后：

- 完成标记写在独立快照里，**账本轮换/清理永远不会弄丢完成标记**；
- 内存只保留 `MAX_RECENT_RECORDS` 条近期记录（`tool_calls`），全量历史走
  `query_tool_calls` 按需读文件；
- 启动成本被轮换上限（`LEDGER_MAX_BYTES`）封顶，与累计历史量无关。

保留期限：超过 `DONE_RETENTION_DAYS` 天的去重标记可安全丢弃——时间桶 key 由日期 /
小时构造，过期桶不会复现，丢弃不改变任何去重判定。

兼容既有安装：首次运行时若快照不存在（或不可读），从既有账本回放 `done` 行做一次性
迁移并写出快照；此后 `done` 只进快照，账本只追加 `tool_call` 行。

末行损坏（崩溃时写到一半）时整行丢弃，不影响既有权重回放。
"""
from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# 内存中的近期工具调用窗口（有界）：够诊断「刚才发生了什么」，不随运行时长增长。
MAX_RECENT_RECORDS = 200
# 活动账本超过此字节数即轮换，保证启动回放量封顶。
LEDGER_MAX_BYTES = 1_048_576
# 已轮换账本的保留期（天）。
LEDGER_RETENTION_DAYS = 7
# 去重标记保留期（天）：更早的时间桶不会复现，可安全丢弃。
DONE_RETENTION_DAYS = 30


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
        # 去重状态快照与账本同目录、同前缀，便于整体迁移/备份。
        self.state_path = path.with_name(f"{path.stem}.state.json")
        self._done: dict[str, datetime] = {}
        self._recent: deque[ToolCallRecord] = deque(maxlen=MAX_RECENT_RECORDS)
        # 本进程见到过的工具调用条数（启动时活动账本内 + 运行期追加），仅用于状态展示。
        self.recorded_total = 0
        self._load()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def _load(self) -> None:
        # 先读快照；读不到才去回放账本里的 done 行（一次性迁移/降级回退）。
        state_ok = self._load_state()
        if self.path.exists():
            for record in _iter_records(self.path):
                kind = record.get("type")
                if kind == "done":
                    if not state_ok:
                        self._apply_done(record)
                elif kind == "tool_call":
                    self.recorded_total += 1
                    self._append_recent(record)
        # 顺序是刻意的：先把完成标记落到快照，再谈轮换与清理。
        # 账本可以丢、可以轮换，完成标记不能丢（A04 验收门槛）。
        self._persist_state(force=not state_ok)
        self._rotate_if_needed()
        self._prune_rotated()

    def _load_state(self) -> bool:
        """读取去重状态快照；返回是否成功（失败时调用方回退到账本回放）。"""
        if not self.state_path.exists():
            return False
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        done = payload.get("done") if isinstance(payload, dict) else None
        if not isinstance(done, dict):
            return False
        for key, at in done.items():
            if isinstance(key, str) and at is not None:
                self._done[key] = _coerce_dt(at)
        return True

    def _apply_done(self, record: dict[str, Any]) -> None:
        key = record.get("key")
        at = record.get("at")
        if isinstance(key, str) and at is not None:
            self._done[key] = _coerce_dt(at)

    def _append_recent(self, record: dict[str, Any]) -> None:
        payload = record.get("rec")
        if not isinstance(payload, dict):
            return
        try:
            self._recent.append(ToolCallRecord.model_validate(payload))
        except ValueError:  # 单行损坏丢掉即可，不阻断恢复
            return

    # ------------------------------------------------------------------
    # 去重状态
    # ------------------------------------------------------------------

    def is_done(self, key: str) -> bool:
        return key in self._done

    def mark_done(self, key: str, at: datetime | None = None) -> None:
        at = at or _now()
        self._done[key] = at
        # 原子覆写快照：完成标记与账本解耦，账本轮换/清理不会影响「重启不重发」。
        self._persist_state(force=True)

    def _prune_done(self) -> bool:
        cutoff = _now() - timedelta(days=DONE_RETENTION_DAYS)
        stale = [key for key, at in self._done.items() if at < cutoff]
        for key in stale:
            del self._done[key]
        return bool(stale)

    def _persist_state(self, *, force: bool = False) -> None:
        pruned = self._prune_done()
        if not force and not pruned:
            return
        payload = {
            "version": 1,
            "updated_at": _now().isoformat(),
            "done": {key: at.isoformat() for key, at in sorted(self._done.items())},
        }
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        # os.replace 在同一目录内是原子的：崩溃时要么旧快照、要么新快照，不会写坏。
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------
    # 历史调用记录
    # ------------------------------------------------------------------

    @property
    def tool_calls(self) -> list[ToolCallRecord]:
        """内存中的近期工具调用（有界窗口）。全量历史见 `query_tool_calls`。"""
        return list(self._recent)

    def record_tool_call(self, record: ToolCallRecord) -> None:
        self._recent.append(record)
        self.recorded_total += 1
        self._append_line({"type": "tool_call", "rec": record.model_dump(mode="json")})
        self._rotate_if_needed()

    def query_tool_calls(self, limit: int = 100) -> list[ToolCallRecord]:
        """按时间倒序读取历史工具调用，覆盖活动账本与已轮换文件。

        「历史记录可查询」不依赖内存常驻：需要时按文件读，因此长期驻留不会推高内存。
        """
        records: list[ToolCallRecord] = []
        for source in [self.path, *self.rotated_files()]:
            if not source.exists():
                continue
            for record in _iter_records(source):
                if record.get("type") != "tool_call":
                    continue
                payload = record.get("rec")
                if not isinstance(payload, dict):
                    continue
                try:
                    records.append(ToolCallRecord.model_validate(payload))
                except ValueError:
                    continue
        records.sort(key=lambda item: item.at, reverse=True)
        return records[:limit]

    def rotated_files(self) -> list[Path]:
        """已轮换的账本文件，按文件名倒序（新→旧）。"""
        pattern = f"{self.path.stem}.*.jsonl"
        files = [item for item in self.path.parent.glob(pattern) if item != self.path]
        files.sort(key=lambda item: item.name, reverse=True)
        return files

    def _append_line(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            if hasattr(handle, "fileno"):
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < LEDGER_MAX_BYTES:
            return
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.stem}.{stamp}.jsonl")
        suffix = 0
        while target.exists():
            suffix += 1
            target = self.path.with_name(f"{self.path.stem}.{stamp}-{suffix}.jsonl")
        try:
            # 单次 rename：中断只会让活动账本保持原样，完成标记已先在快照里。
            self.path.replace(target)
        except OSError:
            # 轮换失败不阻断追加；下一次写入后重试。
            return

    def _prune_rotated(self) -> None:
        cutoff = (_now() - timedelta(days=LEDGER_RETENTION_DAYS)).timestamp()
        for candidate in self.rotated_files():
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue


def _iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读取 JSONL；损坏行直接跳过（崩溃时写到一半的末行属预期）。"""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            record = _parse_line(line)
            if record is not None:
                yield record


def _parse_line(line: str) -> dict[str, Any] | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def _coerce_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    # 早期写入可能缺时区；统一按 UTC 解释，避免与 _now() 比较时抛 TypeError。
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
