"""跨研究、日报和通知共用的证据有效性策略。"""

from __future__ import annotations

from datetime import UTC, datetime

from investment_steward_core.domain import Evidence, EvidenceStatus


def is_current_evidence(item: Evidence, *, now: datetime | None = None) -> bool:
    """只有 active 且未超过 valid_until 的证据可作为当前决策依据。"""
    if item.status != EvidenceStatus.ACTIVE:
        return False
    moment = now or datetime.now(UTC)
    valid_until = item.valid_until
    if valid_until is not None:
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=UTC)
        if valid_until < moment:
            return False
    return True


def as_utc(value: datetime) -> datetime:
    """将历史无时区时间按 UTC 解释，避免跨模块比较时抛 TypeError。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
