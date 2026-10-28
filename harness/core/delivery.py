"""Bounded result delivery with an optional real-time output gate."""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from .config import HarnessConfig
from .events import Clock
from .models import AudioChunk, DelegateResult
from .stages import EventSink, OutputGateComponent


class ResultDelivery:
    """Own result buffering and presentation timing, independent of task work."""

    def __init__(
        self,
        *,
        config: HarnessConfig,
        clock: Clock,
        emit: EventSink,
        is_current: Callable[[str], bool],
        validate_result=None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._emit = emit
        self._is_current = is_current
        self._validate_result = validate_result or (lambda result: True)
        self._lock = asyncio.Lock()
        self._results: "asyncio.Queue[DelegateResult]" = asyncio.Queue(
            maxsize=config.runtime.max_buffered_delegate_results,
        )
        self._gate = (
            OutputGateComponent(
                config.result_management,
                max_backlog=config.runtime.max_buffered_delegate_results,
            )
            if config.result_management.enabled
            else None
        )
        self._wakeup = asyncio.Event()
        self._closed = False
        self._manager_task = (
            asyncio.create_task(self._run_gate(), name="delegate-result-delivery")
            if self._gate is not None
            else None
        )

    async def publish(self, result: DelegateResult) -> None:
        async with self._lock:
            if (
                self._closed
                or not self._is_current(result.work_id)
                or not self._validate_result(result)
            ):
                return
            if self._gate is not None and (
                result.status != "pending" or result.feedback_id
            ):
                now = self._clock()
                dropped = self._gate.offer(result, now)
                self._emit(
                    "delegate_result_managed",
                    now,
                    work_id=result.work_id,
                    details={"backlog_size": self._gate.backlog_size},
                )
                if dropped is not None:
                    details = {
                        "reason": "result_manager_backlog_full",
                        "incoming_admitted": dropped.work_id != result.work_id,
                    }
                    if dropped.work_id != result.work_id:
                        details["replacement_work_id"] = result.work_id
                    self._emit(
                        "delegate_result_dropped",
                        now,
                        work_id=dropped.work_id,
                        details=details,
                    )
                self._wakeup.set()
                return
            if result.feedback_id:
                queued = list(self._results._queue)
                self._results._queue.clear()
                for item in queued:
                    if not (
                        item.work_id == result.work_id
                        and item.status == "pending"
                        and item.feedback_id
                    ):
                        self._results._queue.append(item)
            self._enqueue_locked(result)

    async def next_result(self, timeout_s: Optional[float] = None) -> DelegateResult:
        async def consume() -> DelegateResult:
            while True:
                result = await self._results.get()
                async with self._lock:
                    if self._is_current(result.work_id) and self._validate_result(
                        result
                    ):
                        if self._gate is not None and (
                            result.status != "pending" or result.feedback_id
                        ):
                            self._gate.mark_consumed(result.delivery_id, self._clock())
                            self._wakeup.set()
                        return result
                    if self._gate is not None and (
                        result.status != "pending" or result.feedback_id
                    ):
                        self._gate.discard_queued(result.delivery_id, self._clock())
                        self._wakeup.set()

        result = (
            await consume()
            if timeout_s is None
            else await asyncio.wait_for(consume(), timeout_s)
        )
        self._emit(
            "delegate_result_consumed",
            self._clock(),
            work_id=result.work_id,
            details={"status": result.status},
        )
        return result

    async def observe_user_audio(self, chunk: AudioChunk) -> None:
        async with self._lock:
            if self._gate is not None:
                self._gate.observe_user_audio(chunk, self._clock())
                self._wakeup.set()

    async def observe_frontend_text(self, text: str) -> None:
        if not text:
            return
        async with self._lock:
            if self._gate is not None:
                self._gate.observe_frontend_text(text, self._clock())
                self._wakeup.set()

    async def observe_tts_text(self, utterance_id: str, text: str) -> None:
        async with self._lock:
            if self._gate is not None:
                self._gate.observe_tts_text(utterance_id, text, self._clock())
                self._wakeup.set()

    async def acknowledge_playback(self, utterance_id: str, played_chars: int) -> None:
        async with self._lock:
            if self._gate is not None:
                self._gate.acknowledge_playback(
                    utterance_id, played_chars, self._clock()
                )
                self._wakeup.set()

    async def finish_frontend_turn(self, visible_text: str = "") -> None:
        async with self._lock:
            if self._gate is None:
                return
            if visible_text:
                self._gate.observe_frontend_text(visible_text, self._clock())
            self._gate.observe_frontend_turn_finished(self._clock())
            self._wakeup.set()

    async def discard_feedback(self, feedback_id):
        async with self._lock:
            if self._gate:
                self._gate.discard_feedback(feedback_id, self._clock())
                self._wakeup.set()

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            manager_task = self._manager_task
            if manager_task is not None:
                manager_task.cancel()
        if manager_task is not None:
            await asyncio.gather(manager_task, return_exceptions=True)

    async def _run_gate(self) -> None:
        assert self._gate is not None
        try:
            while True:
                async with self._lock:
                    if self._closed:
                        return
                    now = self._clock()
                    decision = self._gate.pop_releasable(now)
                    if decision is not None:
                        if self._is_current(decision.result.work_id):
                            self._emit(
                                "delegate_result_released",
                                now,
                                work_id=decision.result.work_id,
                                details={
                                    "ready_wait_ms": decision.ready_wait_ms,
                                    "estimated_speech_ms": decision.estimated_speech_ms,
                                    "laxity_ms": decision.laxity_ms,
                                    "backlog_size": decision.backlog_size,
                                },
                            )
                            # Emit before making the item consumable so an
                            # observer cannot receive the result and still miss
                            # its causally preceding release event.
                            self._enqueue_locked(decision.result)
                        else:
                            self._gate.discard_queued(decision.result.work_id, now)
                    self._wakeup.clear()
                    timed_work = self._gate.has_timed_work
                if decision is not None:
                    continue
                if not timed_work:
                    await self._wakeup.wait()
                    continue
                try:
                    await asyncio.wait_for(
                        self._wakeup.wait(),
                        timeout=self._config.result_management.manager_tick_ms / 1_000,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    def _enqueue_locked(self, result: DelegateResult) -> None:
        if self._gate is not None and self._results.full():
            buffered = []
            while True:
                try:
                    buffered.append(self._results.get_nowait())
                except asyncio.QueueEmpty:
                    break
            candidates = buffered + [result]
            drop_index = next(
                (
                    index
                    for index, candidate in enumerate(candidates)
                    if candidate.status == "pending"
                ),
                0,
            )
            dropped = candidates.pop(drop_index)
            for candidate in candidates:
                self._results.put_nowait(candidate)
            if dropped.status != "pending":
                self._gate.discard_queued(dropped.work_id, self._clock())
            details = {
                "reason": "result_queue_full",
                "incoming_admitted": dropped is not result,
            }
            if dropped is not result:
                details["replacement_work_id"] = result.work_id
            self._emit(
                "delegate_result_dropped",
                self._clock(),
                work_id=dropped.work_id,
                details=details,
            )
            return

        dropped = None
        while self._results.full():
            try:
                dropped = self._results.get_nowait()
            except asyncio.QueueEmpty:
                break
        if dropped is not None:
            if self._gate is not None and dropped.status != "pending":
                self._gate.discard_queued(dropped.work_id, self._clock())
            self._emit(
                "delegate_result_dropped",
                self._clock(),
                work_id=dropped.work_id,
                details={
                    "reason": "result_queue_full",
                    "replacement_work_id": result.work_id,
                },
            )
        self._results.put_nowait(result)
