"""State models shared by the background job runtime."""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class WorkState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(slots=True)
class Work:
    work_id: str
    objective: str
    session_id: str = ""
    language: str = "zh"
    snapshot_id: str = ""
    capability: str = "general"
    state: WorkState = WorkState.QUEUED
    result: dict[str, Any] | None = None
    error: str | None = None
    delivery_attempts: int = 0
    queue_position: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    parent_work_id: str | None = None
    lineage_id: str = ""
    revision: int = 0
    execution_outcome: str = "not_started"
    delivery_status: str = "none"
    delivery_error: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    fault: dict[str, Any] | None = None
    feedback_records: list[dict[str, Any]] = field(default_factory=list)
    last_feedback_at: float | None = None
    phase: str = "queued"
    agent_events: list[dict[str, Any]] = field(default_factory=list)

    def set_state(self, state: WorkState) -> None:
        now = time.time()
        self.state = state
        self.phase = str(state)
        self.updated_at = now
        if state == WorkState.RUNNING and self.started_at is None:
            self.started_at = now
        if state != WorkState.QUEUED:
            self.queue_position = None
        if (
            state
            in {
                WorkState.COMPLETED,
                WorkState.DELIVERED,
                WorkState.CANCELLED,
                WorkState.FAILED,
            }
            and self.completed_at is None
        ):
            self.completed_at = now

    def snapshot(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "session_id": self.session_id,
            "language": self.language,
            "snapshot_id": self.snapshot_id,
            "state": self.state,
            "parent_work_id": self.parent_work_id,
            "lineage_id": self.lineage_id or self.work_id,
            "phase": self.phase,
            "revision": self.revision,
            "execution_outcome": self.execution_outcome,
            "delivery_status": self.delivery_status,
            "delivery_error": self.delivery_error,
            "progress": deepcopy(self.progress),
            "fault": deepcopy(self.fault),
            "last_feedback_at": self.last_feedback_at,
            "feedback_records": deepcopy(self.feedback_records),
            "objective": self.objective,
            "capability": self.capability,
            "error": self.error,
            "delivery_attempts": self.delivery_attempts,
            "queue_position": self.queue_position,
            "result_available": self.result is not None,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "updated_at": self.updated_at,
        }
