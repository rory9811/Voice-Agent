"""Routing, execution, oralization and output gating for a delegated request."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional, Protocol, TypeVar

from .backend import DelegateBackend
from .config import DelegateConfig, HarnessConfig
from .errors import BackendError
from .media import prepare_context
from .models import (
    BackendResponse,
    DelegateCandidate,
    DelegateOperation,
    DelegateRelation,
    DelegateRequest,
    InputMode,
    RoutingDecision,
    WorkspaceFile,
)
from .result_manager import DuplexResultManager
from .scheduler import DelegateScheduler
from .workspace import WorkspaceWriter


class EventSink(Protocol):
    def __call__(
        self,
        event_type: str,
        at_ms: int,
        *,
        work_id: Optional[str] = None,
        details: Optional[Dict[str, object]] = None,
    ) -> None: ...


class ProgressReporter(Protocol):
    def __call__(
        self,
        request: DelegateRequest,
        stage: str,
    ) -> Awaitable[None]: ...


T = TypeVar("T")


async def await_stage(
    *,
    request: DelegateRequest,
    stage: str,
    operation: Callable[[], Awaitable[T]],
    slow_notice_after_s: float,
    report_progress: ProgressReporter,
) -> T:
    """Run one provider stage and report once if it crosses the slow threshold."""

    task = asyncio.create_task(
        operation(),
        name="duplex-harness-{}-{}".format(stage, request.work_id),
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=slow_notice_after_s)
        if task in done:
            return task.result()
        await report_progress(request, stage)
        return await task
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


Clock = Callable[[], int]


class RoutingComponent:
    """Select evidence, operation, search, and task relation from text only."""

    def __init__(
        self,
        *,
        backend: DelegateBackend,
        backend_name: str,
        scheduler: DelegateScheduler,
        clock: Clock,
        emit: EventSink,
    ) -> None:
        self._backend = backend
        self._backend_name = backend_name
        self._scheduler = scheduler
        self._clock = clock
        self._emit = emit

    async def route(
        self,
        request: DelegateRequest,
        candidates: tuple[DelegateCandidate, ...],
    ) -> RoutingDecision:
        try:
            async with self._scheduler.acquire() as lease:
                self._emit(
                    "delegate_routing_started",
                    self._clock(),
                    work_id=request.work_id,
                    details={
                        "stage": "routing",
                        "queue_wait_ms": lease.queue_wait_ms,
                        "in_flight": lease.in_flight,
                        "waiting": lease.waiting,
                        "candidate_count": len(candidates),
                    },
                )
                return await self._backend.plan(request, candidates)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Routing is an optimisation; a classifier outage must not discard
            # a complete user task.
            decision = RoutingDecision(
                input_mode=InputMode.TEXT_MULTIMODAL,
                relation=DelegateRelation.INDEPENDENT,
                supersedes_work_id=None,
                provider=self._backend_name,
                model="",
                latency_ms=0,
                operation=DelegateOperation.ANSWER,
                requires_web_search=False,
                fallback=True,
            )
            self._emit(
                "delegate_routing_fallback",
                self._clock(),
                work_id=request.work_id,
                details={"candidate_count": len(candidates)},
            )
            return decision


@dataclass(frozen=True)
class WorkOutput:
    source_response: BackendResponse
    direct_spoken_response: BackendResponse | None = None
    workspace_files: tuple[WorkspaceFile, ...] = ()


class WorkComponent:
    """Execute routed work without deciding when its result should be spoken."""

    def __init__(
        self,
        *,
        config: HarnessConfig,
        backend: DelegateBackend,
        backend_name: str,
        scheduler: DelegateScheduler,
        clock: Clock,
        emit: EventSink,
        report_progress: ProgressReporter,
    ) -> None:
        self._config = config
        self._backend = backend
        self._backend_name = backend_name
        self._scheduler = scheduler
        self._clock = clock
        self._emit = emit
        self._report_progress = report_progress
        self._workspace = WorkspaceWriter(config.workspace)

    def capability_issue(self, request: DelegateRequest) -> str | None:
        capability = self._config.capabilities.get(request.operation)
        if capability is None or not capability.enabled:
            return "capability"
        if capability.workspace_required and not self._workspace.configured:
            return "workspace"
        backend = self._backend_for(request.backend_name)
        if backend is None:
            return "backend"
        supported = getattr(backend, "supported_operations", None)
        if supported is not None and request.operation not in supported:
            return "capability"
        return None

    async def execute(self, request: DelegateRequest) -> WorkOutput:
        context = prepare_context(
            request.snapshot,
            self._config.buffer.selected_video_frames,
            request.input_mode,
        )
        managed = getattr(self._backend, "execute_scheduled", None)
        if callable(managed):
            source = await await_stage(
                request=request,
                stage="execution",
                operation=lambda: managed(request, context, self._scheduler),
                slow_notice_after_s=self._config.delegate.slow_api_notice_after_s,
                report_progress=self._report_progress,
            )
        else:
            source = await self._execute_direct(request, context)
        if not source.text.strip():
            raise BackendError(
                self._backend_name, "delegate backend returned an empty result"
            )
        if source.raw_metadata.get("progress_query"):
            return WorkOutput(source_response=source, direct_spoken_response=source)
        if request.operation is not DelegateOperation.LONG_WRITING:
            return WorkOutput(source_response=source)
        workspace_file = self._workspace.write_document(request, source.text)
        spoken = BackendResponse(
            text=self._workspace_completion_text(request, workspace_file),
            provider="harness.core",
            model="workspace-writer",
            latency_ms=0,
        )
        return WorkOutput(
            source_response=source,
            direct_spoken_response=spoken,
            workspace_files=(workspace_file,),
        )

    async def _execute_direct(self, request, context):
        async with self._scheduler.acquire() as lease:
            self._emit(
                "delegate_started",
                self._clock(),
                work_id=request.work_id,
                details={
                    "stage": "multimodal",
                    "input_mode": request.input_mode.value,
                    "queue_wait_ms": lease.queue_wait_ms,
                    "in_flight": lease.in_flight,
                    "waiting": lease.waiting,
                },
            )
            source = await await_stage(
                request=request,
                stage="multimodal",
                operation=lambda: self._backend.execute(request, context),
                slow_notice_after_s=self._config.delegate.slow_api_notice_after_s,
                report_progress=self._report_progress,
            )
        return source

    def _backend_for(self, backend_name: str):
        getter = getattr(self._backend, "get", None)
        if callable(getter):
            return getter(backend_name)
        if backend_name == self._backend_name:
            return self._backend
        return None

    @staticmethod
    def _workspace_completion_text(
        request: DelegateRequest, workspace_file: WorkspaceFile
    ) -> str:
        if request.language == "en":
            return f"The long document is complete and saved in your workspace at {workspace_file.relative_path}."
        return f"长文已经写完，已保存到工作区：{workspace_file.relative_path}。"


@dataclass(frozen=True)
class OralizationOutput:
    response: BackendResponse
    oralized: bool


class OralizationComponent:
    """Render work output for speech without seeing user media."""

    def __init__(
        self,
        *,
        config: DelegateConfig,
        backend: DelegateBackend,
        backend_name: str,
        scheduler: DelegateScheduler,
        clock: Clock,
        emit: EventSink,
        report_progress: ProgressReporter,
    ) -> None:
        self._config = config
        self._backend = backend
        self._backend_name = backend_name
        self._scheduler = scheduler
        self._clock = clock
        self._emit = emit
        self._report_progress = report_progress

    async def render(
        self,
        request: DelegateRequest,
        source_response: BackendResponse,
    ) -> OralizationOutput:
        if not self._config.oralization_enabled:
            return OralizationOutput(response=source_response, oralized=False)
        async with self._scheduler.acquire() as lease:
            self._emit(
                "delegate_oralization_started",
                self._clock(),
                work_id=request.work_id,
                details={
                    "stage": "oralization",
                    "queue_wait_ms": lease.queue_wait_ms,
                    "in_flight": lease.in_flight,
                    "waiting": lease.waiting,
                },
            )

            async def oralize_with_timeout() -> BackendResponse:
                try:
                    return await asyncio.wait_for(
                        self._backend.oralize(request, source_response),
                        timeout=self._config.oralization_timeout_s,
                    )
                except asyncio.TimeoutError as exc:
                    raise BackendError(
                        self._backend_name, "delegate oralization timed out"
                    ) from exc

            response = await await_stage(
                request=request,
                stage="oralization",
                operation=oralize_with_timeout,
                slow_notice_after_s=self._config.slow_api_notice_after_s,
                report_progress=self._report_progress,
            )
        self._emit(
            "delegate_oralization_completed",
            self._clock(),
            work_id=request.work_id,
            details={"latency_ms": response.latency_ms},
        )
        return OralizationOutput(response=response, oralized=True)


OutputGateComponent = DuplexResultManager
