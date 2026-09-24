from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    COLLECTING_EVIDENCE = "collecting_evidence"
    ANALYZING = "analyzing"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPOSING = "composing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class AgentRunState(BaseModel):
    run_id: UUID
    user_id: UUID
    status: RunStatus = RunStatus.CREATED
    attempt: int = Field(default=1, ge=1)
    input_snapshot_refs: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    checkpoint: str | None = None
    error_code: str | None = None


ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.PLANNING, RunStatus.CANCELLED},
    RunStatus.PLANNING: {RunStatus.COLLECTING_EVIDENCE, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.COLLECTING_EVIDENCE: {RunStatus.ANALYZING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.ANALYZING: {
        RunStatus.WAITING_CONFIRMATION,
        RunStatus.COMPOSING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.WAITING_CONFIRMATION: {RunStatus.COMPOSING, RunStatus.CANCELLED, RunStatus.STALE},
    RunStatus.COMPOSING: {RunStatus.COMPLETED, RunStatus.FAILED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.STALE: set(),
}


def transition(state: AgentRunState, target: RunStatus) -> AgentRunState:
    if target not in ALLOWED_TRANSITIONS[state.status]:
        raise ValueError(f"invalid AgentRun transition: {state.status} -> {target}")
    return state.model_copy(update={"status": target})
