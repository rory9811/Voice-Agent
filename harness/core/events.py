"""Non-blocking lifecycle event delivery shared by all session layers."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Awaitable, Callable, Optional

from .models import HarnessEvent

EventListener = Callable[[HarnessEvent], Optional[Awaitable[None]]]
Clock = Callable[[], int]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class EventEmitter:
    """Build session events and keep observers outside the request path."""

    def __init__(self, session_id: str, listener: Optional[EventListener]) -> None:
        self._session_id = session_id
        self._listener = listener

    def __call__(
        self,
        event_type: str,
        at_ms: int,
        *,
        work_id: Optional[str] = None,
        details=None,
    ) -> None:
        if self._listener is None:
            return
        event = HarnessEvent(
            type=event_type,
            session_id=self._session_id,
            at_ms=at_ms,
            work_id=work_id,
            details=details or {},
        )
        asyncio.get_running_loop().call_soon(self._notify, event)

    def _notify(self, event: HarnessEvent) -> None:
        try:
            outcome = self._listener(event) if self._listener is not None else None
            if inspect.isawaitable(outcome):
                asyncio.create_task(self._await_listener(outcome))
        except Exception:
            return

    @staticmethod
    async def _await_listener(outcome: Awaitable[None]) -> None:
        try:
            await outcome
        except Exception:
            return
