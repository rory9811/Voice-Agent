"""Frontend-neutral lifecycle for one session's delegate requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Optional, Tuple

from .backend import DelegateBackend
from .config import HarnessConfig
from .delivery import ResultDelivery
from .events import Clock
from .models import (
    DelegateCandidate,
    DelegateRelation,
    DelegateRequest,
    DelegateResult,
    RoutingDecision,
)
from .pipeline import DelegatePipeline
from .scheduler import DelegateScheduler
from .stages import EventSink


@dataclass
class _ActiveRequest:
    request: DelegateRequest
    order: int
    task: "asyncio.Task[None]"


class DelegateSessionRuntime:
    """Run explicit delegate requests without knowing any frontend protocol."""

    def __init__(
        self,
        *,
        session_id: str,
        config: HarnessConfig,
        backend: DelegateBackend,
        backend_name: str,
        scheduler: DelegateScheduler,
        clock: Clock,
        emit: EventSink,
    ) -> None:
        self.session_id = session_id
        self._config = config
        self._backend = backend
        self._clock = clock
        self._emit = emit
        self._lock = asyncio.Lock()
        self._order = 0
        self._active: dict[str, _ActiveRequest] = {}
        self._known_ids: set[str] = set()
        self._superseded: set[str] = set()
        self._closed = False
        self._delivery = ResultDelivery(
            config=config,
            clock=clock,
            emit=emit,
            is_current=self._is_current,
            validate_result=getattr(getattr(backend, "feedback", None), "valid", None),
        )
        self._pipeline = DelegatePipeline(
            config=config,
            backend=backend,
            backend_name=backend_name,
            scheduler=scheduler,
            clock=clock,
            emit=emit,
            publish_result=self._delivery.publish,
            candidate_provider=self._routing_candidates,
            apply_route=self._apply_routing_decision,
        )

    @property
    def delivery(self) -> ResultDelivery:
        return self._delivery

    async def submit(self, request: DelegateRequest) -> str:
        """Schedule one fully formed request and return its stable request ID."""

        if request.session_id != self.session_id:
            raise ValueError("delegate request belongs to another session")
        async with self._lock:
            self._ensure_open()
            if request.work_id in self._known_ids:
                raise ValueError(
                    "duplicate delegate work_id: {}".format(request.work_id)
                )
            accept = getattr(self._backend, "accept_work", None)
            if callable(accept):
                await accept(request)
            register = getattr(self._backend, "register_feedback_route", None)
            if register:
                register(request, self._delivery.publish)
            self._known_ids.add(request.work_id)
            self._order += 1
            task = asyncio.create_task(
                self._run(request),
                name="delegate-runtime-{}-{}".format(self.session_id, request.work_id),
            )
            self._active[request.work_id] = _ActiveRequest(request, self._order, task)
            bind = getattr(self._backend, "bind_work_task", None)
            if callable(bind):
                bind(request.work_id, task)
            self._emit(
                "delegate_queued", request.created_at_ms, work_id=request.work_id
            )
            return request.work_id

    async def next_result(self, timeout_s: Optional[float] = None) -> DelegateResult:
        return await self._delivery.next_result(timeout_s)

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            records = tuple(self._active.values())
            tasks = tuple(record.task for record in records)
            self._active.clear()
            for task in tasks:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        cancel = getattr(self._backend, "observe_cancelled", None)
        if callable(cancel):
            for record in records:
                await cancel(record.request.work_id)
        await self._delivery.aclose()

    async def _run(self, request: DelegateRequest) -> None:
        try:
            await self._pipeline.run(request)
        except asyncio.CancelledError:
            cancel = getattr(self._backend, "observe_cancelled", None)
            if callable(cancel):
                await cancel(request.work_id)
            return
        finally:
            async with self._lock:
                record = self._active.get(request.work_id)
                if record is not None and record.task is asyncio.current_task():
                    self._active.pop(request.work_id, None)

    async def _routing_candidates(self, work_id: str) -> tuple[DelegateCandidate, ...]:
        async with self._lock:
            record = self._active.get(work_id)
            if record is None:
                return ()
            earlier = sorted(
                (
                    item
                    for item in self._active.values()
                    if item.order < record.order
                    and self._is_current(item.request.work_id)
                ),
                key=lambda item: item.order,
            )
            limit = self._config.delegate.max_relation_candidates
            earlier = earlier[-limit:] if limit else []
            return tuple(
                DelegateCandidate(
                    work_id=item.request.work_id,
                    query=item.request.query,
                    created_at_ms=item.request.created_at_ms,
                )
                for item in earlier
            )

    async def _apply_routing_decision(
        self,
        request: DelegateRequest,
        decision: RoutingDecision,
    ) -> Optional[Tuple[DelegateRequest, RoutingDecision]]:
        async with self._lock:
            record = self._active.get(request.work_id)
            if record is None or not self._is_current(request.work_id):
                return None
            if decision.relation is DelegateRelation.UPDATE:
                previous = self._active.get(decision.supersedes_work_id or "")
                if previous is None or previous.order >= record.order:
                    decision = replace(
                        decision,
                        relation=DelegateRelation.INDEPENDENT,
                        supersedes_work_id=None,
                    )
                else:
                    self._supersede(previous, request)

            capability = self._config.capabilities.get(request.operation)
            selected_backend = (
                capability.backend_name
                if capability is not None and capability.backend_name
                else request.backend_name
            )
            request = replace(
                request,
                backend_name=selected_backend,
                allow_web_search=(
                    decision.requires_web_search
                    and self._config.backend_search_enabled.get(selected_backend, False)
                ),
            )
            record.request = request
            self._emit(
                "delegate_routed",
                self._clock(),
                work_id=request.work_id,
                details={
                    "input_mode": decision.input_mode.value,
                    "operation": decision.operation.value,
                    "web_search_requested": decision.requires_web_search,
                    "web_search_enabled": request.allow_web_search,
                    "relation": decision.relation.value,
                    "supersedes_work_id": decision.supersedes_work_id,
                    "fallback": decision.fallback,
                    "policy_override": decision.policy_override,
                },
            )
            return request, decision

    def _supersede(
        self, previous: _ActiveRequest, replacement: DelegateRequest
    ) -> None:
        previous_id = previous.request.work_id
        self._superseded.add(previous_id)
        previous.task.cancel()
        self._emit(
            "delegate_superseded",
            self._clock(),
            work_id=previous_id,
            details={"replacement_work_id": replacement.work_id},
        )

    def _is_current(self, work_id: str) -> bool:
        return work_id not in self._superseded

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("delegate runtime is closed")
