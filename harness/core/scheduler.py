"""Global delegate-request admission control for one Harness process."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from .config import RuntimeConfig


@dataclass(frozen=True)
class SchedulerLease:
    """Observable admission facts for one API request, with no media payload."""

    queue_wait_ms: int
    in_flight: int
    waiting: int


class DelegateScheduler:
    """Bound total external API work across all live conversation sessions.

    Per-session ordering belongs to ``DelegateSessionRuntime``. This scheduler
    only enforces process-wide admission, so future provider-specific quotas or
    priority queues can be added here without changing media policy or adapters.
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self._semaphore = asyncio.Semaphore(config.max_concurrent_delegate_requests)
        self._state_lock = asyncio.Lock()
        self._waiting = 0
        self._in_flight = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[SchedulerLease]:
        queued_at = time.perf_counter_ns()
        acquired = False
        async with self._state_lock:
            self._waiting += 1
        try:
            await self._semaphore.acquire()
            acquired = True
            async with self._state_lock:
                self._waiting -= 1
                self._in_flight += 1
                lease = SchedulerLease(
                    queue_wait_ms=int(
                        (time.perf_counter_ns() - queued_at) // 1_000_000
                    ),
                    in_flight=self._in_flight,
                    waiting=self._waiting,
                )
            yield lease
        finally:
            if acquired:
                async with self._state_lock:
                    self._in_flight -= 1
                self._semaphore.release()
            else:
                # Cancellation while queued must not leave the metric state
                # inconsistent or reduce capacity for later callers.
                async with self._state_lock:
                    self._waiting -= 1
