"""Explicit orchestration of routing, work, and oralization components."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Awaitable, Callable, Optional, Protocol, Tuple

from .backend import DelegateBackend
from .config import HarnessConfig
from .errors import ApiNotConfiguredError, HarnessError
from .models import (
    DelegateCandidate,
    DelegateRelation,
    DelegateRequest,
    DelegateResult,
    ResponseMode,
    RoutingDecision,
)
from .results import DelegateResultFactory
from .scheduler import DelegateScheduler
from .stages import (
    EventSink,
    OralizationComponent,
    OralizationOutput,
    RoutingComponent,
    WorkComponent,
)
from .workspace import WorkspaceNotConfiguredError, WorkspaceWriteError

Clock = Callable[[], int]
ResultPublisher = Callable[[DelegateResult], Awaitable[None]]
CandidateProvider = Callable[[str], Awaitable[Tuple[DelegateCandidate, ...]]]


class RouteApplier(Protocol):
    def __call__(
        self,
        request: DelegateRequest,
        decision: RoutingDecision,
    ) -> Awaitable[Optional[Tuple[DelegateRequest, RoutingDecision]]]: ...


class DelegatePipeline:
    """Run one delegate through explicit components and normalize its result."""

    def __init__(
        self,
        *,
        config: HarnessConfig,
        backend: DelegateBackend,
        backend_name: str,
        scheduler: DelegateScheduler,
        clock: Clock,
        emit: EventSink,
        publish_result: ResultPublisher,
        candidate_provider: CandidateProvider,
        apply_route: RouteApplier,
    ) -> None:
        self._clock = clock
        self._emit = emit
        self._backend = backend
        self._publish_result = publish_result
        self._candidate_provider = candidate_provider
        self._apply_route = apply_route
        self._results = DelegateResultFactory(
            config.delegate, backend_name=backend_name
        )
        self.routing = RoutingComponent(
            backend=backend,
            backend_name=backend_name,
            scheduler=scheduler,
            clock=clock,
            emit=emit,
        )
        self.work = WorkComponent(
            config=config,
            backend=backend,
            backend_name=backend_name,
            scheduler=scheduler,
            clock=clock,
            emit=emit,
            report_progress=self._report_progress,
        )
        self.oralization = OralizationComponent(
            config=config.delegate,
            backend=backend,
            backend_name=backend_name,
            scheduler=scheduler,
            clock=clock,
            emit=emit,
            report_progress=self._report_progress,
        )

    async def run(self, request: DelegateRequest) -> None:
        """Publish exactly one terminal result unless the request is superseded."""

        feedback = getattr(self._backend, "feedback", None)
        watcher = (
            asyncio.create_task(feedback.watch(request, self._publish_result))
            if feedback
            else None
        )
        try:
            await self._run_pipeline(request)
        finally:
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    async def _run_pipeline(self, request):
        result: Optional[DelegateResult] = None
        try:
            candidates = await self._candidate_provider(request.work_id)
            if request.routing_locked:
                decision = RoutingDecision(
                    input_mode=request.input_mode,
                    operation=request.operation,
                    relation=DelegateRelation.INDEPENDENT,
                    supersedes_work_id=None,
                    provider=request.backend_name,
                    model=request.model_name or "",
                    latency_ms=0,
                    requires_web_search=request.allow_web_search,
                    policy_override="caller fixed input_mode and operation",
                )
            else:
                decision = await self.routing.route(request, candidates)
            request = replace(
                request,
                input_mode=decision.input_mode,
                operation=decision.operation,
                allow_web_search=False,
            )
            applied = await self._apply_route(request, decision)
            if applied is None:
                return
            request, decision = applied

            capability_issue = self.work.capability_issue(request)
            if capability_issue is not None:
                result = self._results.unavailable(
                    request, self._clock(), capability_issue
                )
                self._emit(
                    "delegate_capability_unavailable",
                    result.completed_at_ms,
                    work_id=request.work_id,
                    details={
                        "operation": request.operation.value,
                        "reason": capability_issue,
                    },
                )
                await self._publish_terminal_result(result)
                return

            work_output = await self.work.execute(request)
            if work_output.direct_spoken_response is not None:
                spoken = OralizationOutput(
                    response=work_output.direct_spoken_response,
                    oralized=False,
                )
            elif request.response_mode is ResponseMode.SINGLE_STAGE:
                # The execution prompt already requires a concise, directly
                # speakable final answer, so a second model call is unnecessary.
                spoken = OralizationOutput(
                    response=work_output.source_response,
                    oralized=False,
                )
            else:
                try:
                    spoken = await self.oralization.render(
                        request, work_output.source_response
                    )
                except Exception as exc:
                    fallback = getattr(self._backend, "polish_fallback", None)
                    if not fallback:
                        raise
                    spoken = OralizationOutput(
                        response=fallback(request, work_output.source_response, exc),
                        oralized=False,
                    )
            result = self._results.success(
                request,
                decision,
                work_output.source_response,
                spoken.response,
                self._clock(),
                oralized=spoken.oralized,
                workspace_files=work_output.workspace_files,
            )
            if result.is_success:
                self._emit(
                    "delegate_completed",
                    result.completed_at_ms,
                    work_id=request.work_id,
                    details={"stale": result.completed_at_ms > result.expires_at_ms},
                )
            else:
                self._emit(
                    "delegate_failed",
                    result.completed_at_ms,
                    work_id=request.work_id,
                    details={"reason": result.error or "empty backend result"},
                )
        except asyncio.CancelledError:
            if getattr(self._backend, "feedback", None):
                result = self._results.failed(request, self._clock(), "cancelled")
                result = replace(
                    result,
                    status="cancelled",
                    spoken_text="任务已取消。",
                    metadata={"kind": "cancelled"},
                )
                await self._publish_terminal_result(result)
            raise
        except ApiNotConfiguredError as exc:
            self._record_fault(request, exc)
            result = self._results.api_not_configured(request, self._clock())
            self._emit(
                "delegate_api_not_configured",
                result.completed_at_ms,
                work_id=request.work_id,
            )
        except (WorkspaceNotConfiguredError, WorkspaceWriteError) as exc:
            self._record_fault(request, exc)
            result = self._results.unavailable(request, self._clock(), "workspace")
            self._emit(
                "delegate_capability_unavailable",
                result.completed_at_ms,
                work_id=request.work_id,
                details={"operation": request.operation.value, "reason": "workspace"},
            )
        except HarnessError as exc:
            self._record_fault(request, exc)
            result = self._results.failed(
                request,
                self._clock(),
                "delegate backend failed"
                if getattr(self._backend, "feedback", None)
                else str(exc),
            )
            self._emit(
                "delegate_failed",
                result.completed_at_ms,
                work_id=request.work_id,
                details={"reason": "delegate backend failed"},
            )
        except Exception as exc:
            self._record_fault(request, exc)
            # Provider payloads and unexpected tracebacks never cross the
            # private model-facing result boundary.
            result = self._results.failed(
                request,
                self._clock(),
                "delegate backend failed"
                if getattr(self._backend, "feedback", None)
                else str(exc),
            )
            self._emit(
                "delegate_failed",
                result.completed_at_ms,
                work_id=request.work_id,
                details={"reason": "unexpected backend failure"},
            )
        if result is not None:
            await self._publish_terminal_result(result)

    async def _publish_terminal_result(self, result: DelegateResult) -> None:
        """Let an execution backend commit its Work before result delivery."""

        prepare = getattr(self._backend, "prepare_terminal_result", None)
        if callable(prepare):
            result = prepare(result)
        observer = getattr(self._backend, "observe_terminal_result", None)
        if callable(observer):
            await observer(result)
        feedback = getattr(self._backend, "feedback", None)
        if feedback:
            result = feedback.terminal(result)
        await self._publish_result(result)

    def _record_fault(self, request, exc):
        handler = getattr(self._backend, "record_fault", None)
        if handler:
            handler(request.work_id, exc)

    async def _report_progress(self, request: DelegateRequest, stage: str) -> None:
        if getattr(self._backend, "feedback", None):
            return
        notice = self._results.waiting(request, self._clock(), stage)
        await self._publish_result(notice)
        self._emit(
            "delegate_waiting",
            notice.completed_at_ms,
            work_id=request.work_id,
            details={"stage": stage},
        )
