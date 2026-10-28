"""In-memory ownership and lookup for background work state."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from .models import Work, WorkState


class WorkManager:
    """Own work records, completion events, and deterministic queue positions.

    This class deliberately contains no model calls, response creation, or event
    emission. The runtime remains responsible for orchestration while work-state
    storage and selection rules stay independently testable.
    """

    CANCELLABLE_STATES = frozenset(
        {
            WorkState.QUEUED,
            WorkState.RUNNING,
            WorkState.CANCELLING,
            WorkState.COMPLETED,
            WorkState.DELIVERING,
        }
    )
    ACTIVE_STATES = CANCELLABLE_STATES

    def __init__(self) -> None:
        self.works: dict[str, Work] = {}
        self.done_events: dict[str, asyncio.Event] = {}

    def register(self, work: Work) -> asyncio.Event:
        if work.work_id in self.works:
            raise ValueError(f"任务已存在: {work.work_id}")
        self.works[work.work_id] = work
        done = asyncio.Event()
        self.done_events[work.work_id] = done
        return done

    def get(self, work_id: str) -> Work | None:
        return self.works.get(work_id)

    def require(self, work_id: str) -> Work:
        try:
            return self.works[work_id]
        except KeyError as exc:
            raise KeyError(f"任务不存在: {work_id}") from exc

    def done_event(self, work_id: str) -> asyncio.Event:
        return self.done_events.setdefault(work_id, asyncio.Event())

    def latest_cancellable(self, work_id: str | None = None) -> Work | None:
        if work_id:
            return self.works.get(work_id)
        return next(
            (
                work
                for work in reversed(tuple(self.works.values()))
                if work.state in self.CANCELLABLE_STATES
            ),
            None,
        )

    def snapshots(
        self,
        *,
        active_only: bool = False,
        newest_first: bool = False,
    ) -> list[dict[str, object]]:
        values = tuple(self.works.values())
        works: Iterable[Work] = reversed(values) if newest_first else values
        if active_only:
            works = (work for work in works if work.state in self.ACTIVE_STATES)
        return [work.snapshot() for work in works]

    def queued_count(self) -> int:
        return sum(work.state == WorkState.QUEUED for work in self.works.values())

    def refresh_queue_positions(self) -> None:
        position = 1
        for work in self.works.values():
            if work.state == WorkState.QUEUED:
                work.queue_position = position
                position += 1
            else:
                work.queue_position = None


__all__ = ["WorkManager"]
