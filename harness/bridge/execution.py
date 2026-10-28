"""VenusOmni delegate execution backed by direct media or agent planning.

This module deliberately sits below the frontend protocol boundary.  It knows
about closed ``DelegateRequest`` objects, but has no dependency on any frontend
transport implementation.
"""

from __future__ import annotations

import asyncio
import logging
import hashlib
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.agents import AgentRequest, CodexAgentProvider, GeneralAgentConfig
from harness.agents.vision import constrain_visual_result, prepare_visual
from harness.core.backend import DelegateBackend
from harness.core.errors import ApiNotConfiguredError, BackendError
from harness.core.models import (
    BackendResponse,
    DelegateCandidate,
    DelegateRelation,
    DelegateRequest,
    DelegateResult,
    PreparedContext,
    RoutingDecision,
)
from harness.jobs.faults import classify_fault
from harness.jobs.feedback import (
    FeedbackConfig,
    WorkFeedbackController,
    WorkJournal,
)
from harness.jobs.manager import WorkManager
from harness.jobs.messages import local_text, polish_error
from harness.jobs.models import Work, WorkState
from harness.jobs.progress import ProgressReporter
from harness.skills import validate_routing_arguments

if TYPE_CHECKING:
    from harness.llm.planner import PlannerLike
    from harness.skills import SkillRegistry


@dataclass(frozen=True, slots=True)
class _ExecutionRoute:
    objective: str
    capability: str
    arguments: dict[str, Any]
    parent_work_id: str | None = None


class AgentDelegateBackend(DelegateBackend):
    """Add queued agent work to a direct multimodal delegate backend.

    General mode dispatches locally without a routing model call. Auto mode
    classifies a delegate once before choosing direct execution, General, or a Skill.

    Work is registered at acceptance, before dispatch admission. Direct callers
    of execute receive the same lifecycle through the lazy registration seam.
    """

    name = "agent_delegate"
    _CANCELLABLE_STATES = frozenset({WorkState.QUEUED, WorkState.RUNNING})

    @property
    def skill_manifests(self):
        return self._skills.manifests

    def __init__(
        self,
        direct_backend: DelegateBackend,
        planner: PlannerLike | None = None,
        skills: SkillRegistry | None = None,
        work_manager: WorkManager | None = None,
        general_agent=None,
        general_config: GeneralAgentConfig | None = None,
        feedback_config: FeedbackConfig | None = None,
    ) -> None:
        if skills is None:
            # Keep Skill loading lazy so importing this frontend-neutral module
            # does not initialize an unrelated frontend transport.
            from harness.skills import SkillRegistry

            skills = SkillRegistry()
        self._direct_backend = direct_backend
        self._planner = planner
        self._general_config = general_config or GeneralAgentConfig.from_env()
        self._general = general_agent or CodexAgentProvider(self._general_config)
        self._skills = skills
        self._works = work_manager or WorkManager()
        self._state_lock = asyncio.Lock()
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._retired_history: set[str] = set()
        self._feedback_routes = {}
        feedback_config = feedback_config or FeedbackConfig.from_env()
        self._general_config = replace(
            self._general_config,
            queue_timeout_s=feedback_config.queue_timeout_s,
        )
        if isinstance(self._general, CodexAgentProvider):
            self._general.config = self._general_config
        self.journal = WorkJournal(
            feedback_config.journal_path
            or str(Path(self._general_config.workspace) / "work-state.sqlite")
        )
        self.feedback = WorkFeedbackController(
            self._works, feedback_config, self.journal
        )
        self.progress_reporter = ProgressReporter(self)
        self.feedback.reporter = self.progress_reporter
        if planner is None:
            self.feedback.health["routing"] = {
                "status": "unavailable",
                "code": "NOT_CONFIGURED",
            }

    def refresh_backend_health(self):
        for name, backend in (
            ("routing", self._planner),
            ("multimodal", self._direct_backend),
        ):
            status, code = "unknown", ""
            if (
                backend is None
                or type(getattr(backend, "_backend", None)).__name__
                == "UnavailablePlannerBackend"
            ):
                status, code = "unavailable", "NOT_CONFIGURED"
            else:
                cfg = getattr(backend, "_config", None)
                key_reader = getattr(cfg, "api_key", None)
                if (
                    callable(key_reader)
                    and getattr(cfg, "require_api_key", True)
                    and not key_reader()
                ):
                    status, code = "unavailable", "NOT_CONFIGURED"
            if status != "unknown" or name not in self.feedback.health:
                self.feedback.health[name] = {"status": status, "code": code}
        return dict(self.feedback.health)

    def register_feedback_route(self, request, publish):
        self._feedback_routes[request.work_id] = (request, publish)

    def record_fault(self, work_id, exc):
        logging.getLogger(__name__).error(
            "Delegate failed work=%s",
            work_id,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        work = self._works.get(work_id)
        if work and not work.fault:
            fault = classify_fault(exc, work.phase, work.language)
            work.fault = fault.as_dict()
            work.error = fault.message
            work.execution_outcome = fault.certainty
            self.feedback.health[work.phase] = {
                "status": "unavailable",
                "code": fault.code,
            }
            self.feedback.changed(work)

    def polish_fallback(self, request, response, exc):
        work = self._works.require(request.work_id)
        work.delivery_error = classify_fault(exc, "polish", request.language).message
        self.feedback.health["polish"] = {
            "status": "unavailable",
            "code": classify_fault(exc, "polish", request.language).code,
        }
        self.feedback.changed(work)
        # Preserve the verified body/artifacts; spoken formatting remains local and bounded.
        return replace(
            response,
            text=polish_error(request.language),
            provider="harness-local",
            model="fallback",
        )

    async def _safe_call(self, work, operation):
        original_phase = work.phase
        for attempt in range(3):
            try:
                value = await asyncio.wait_for(
                    operation(), self.feedback.config.stage_timeout_s
                )
                work.fault = None
                work.phase = original_phase
                self.feedback.health[original_phase] = {"status": "available"}
                self.feedback.changed(work)
                return value
            except Exception as exc:
                fault = classify_fault(exc, original_phase, work.language)
                if not fault.retryable or attempt == 2:
                    work.fault = None
                    work.phase = original_phase
                    raise
                work.fault = fault.as_dict()
                work.phase = "retry_wait"
                self.feedback.changed(work)
                retry_after = getattr(
                    getattr(exc, "response", None), "headers", {}
                ).get("retry-after", "0")
                try:
                    delay = max(2**attempt, float(retry_after))
                except (TypeError, ValueError):
                    delay = 2**attempt
                if delay > self._general_config.control_timeout_s:
                    work.phase = original_phase
                    raise
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    @property
    def supported_operations(self) -> Any:
        """Preserve an operation declaration made by the direct backend."""

        return getattr(self._direct_backend, "supported_operations", None)

    @property
    def work_manager(self) -> WorkManager:
        return self._works

    async def plan(
        self,
        request: DelegateRequest,
        candidates: tuple[DelegateCandidate, ...],
    ) -> RoutingDecision:
        """Preserve wire policy without spending a second model-routing call."""

        del candidates
        return RoutingDecision(
            input_mode=request.input_mode,
            operation=request.operation,
            relation=DelegateRelation.INDEPENDENT,
            supersedes_work_id=None,
            provider=self.name,
            model="",
            latency_ms=0,
            requires_web_search=request.allow_web_search,
            policy_override="execution capability is routed by one model call",
        )

    async def execute(
        self,
        request: DelegateRequest,
        context: PreparedContext,
    ) -> BackendResponse:
        return await self.execute_scheduled(request, context, None)

    async def execute_scheduled(self, request, context, scheduler):
        work = self._works.get(request.work_id)
        if work is None:
            work = await self.accept_work(request)
        try:
            async with _admission(scheduler, self.feedback.config.queue_timeout_s):
                await self._start_work(work)
                route = await self._safe_call(
                    work, lambda: self._route_execution(request)
                )
            async with self._state_lock:
                work.objective = route.objective
                work.capability = route.capability
                work.parent_work_id = route.parent_work_id
                work.lineage_id = (
                    self._works.require(route.parent_work_id).lineage_id
                    or route.parent_work_id
                    if route.parent_work_id
                    else work.work_id
                )

            if route.capability == "progress_query":
                work.phase = "progress_query"
                target_id = route.arguments.get("target_work_id")
                target = self._works.get(target_id) if target_id else None
                if target is None:
                    text = await self.progress_reporter.clarify(
                        request,
                        route.arguments["resolution"],
                        self._progress_targets(request),
                    )
                else:
                    self.progress_reporter.claim_query(target)
                    text = await self.progress_reporter.query_text(request, target)
                    self.feedback._last[target.work_id] = time.monotonic()
                    self.feedback._poll_last[target.work_id] = time.monotonic()
                response = BackendResponse(
                    text=text,
                    provider="harness-progress",
                    model="read-only",
                    latency_ms=0,
                    work_id=request.work_id,
                    raw_metadata={"progress_query": True, "target_work_id": target_id},
                )
                payload = _response_payload(response)
            elif route.capability == "multimodal":
                work.phase = "multimodal"
                self.feedback.changed(work)
                async with _admission(scheduler, self.feedback.config.queue_timeout_s):
                    if self._direct_backend is None:
                        raise ApiNotConfiguredError("direct backend is not configured")
                    response = await self._safe_call(
                        work,
                        lambda: self._direct_backend.execute(
                            replace(request, query=route.objective),
                            context,
                        ),
                    )
                if not isinstance(response, BackendResponse):
                    raise BackendError(
                        self.name, "direct backend returned an invalid response"
                    )
                if not response.text.strip():
                    raise BackendError(
                        self.name, "direct backend returned an empty response"
                    )
                payload = _response_payload(response)
            elif route.capability == "general":
                started = time.perf_counter_ns()
                snapshot = request.snapshot
                inputs, pictures, visual = await asyncio.to_thread(
                    prepare_visual,
                    snapshot,
                    route.arguments["input_mode"],
                    self._general_config.vision,
                )
                result = await self._general.run(
                    AgentRequest(
                        request.session_id,
                        request.work_id,
                        route.objective,
                        {
                            **_evidence_summary(request),
                            "cutoff_sequence": snapshot.cutoff_sequence,
                            "language": request.language,
                            "arguments": route.arguments,
                            "visual_evidence": visual,
                        },
                        tuple(inputs),
                        parent_work_id=route.parent_work_id,
                        images=pictures,
                    ),
                    self._on_agent_event,
                )
                result = constrain_visual_result(result, visual, request.language)
                payload = result.as_dict()
                if result.outcome == "failed":
                    await self._record_work_output(work, payload)
                    raise BackendError(self.name, f"General outcome: {result.outcome}")
                response = BackendResponse(
                    text=(
                        result.full_result
                        + (
                            (
                                "\nUnresolved: "
                                if request.language == "en"
                                else "\n尚未完成："
                            )
                            + "; ".join(result.unresolved)
                            if result.outcome == "partial"
                            else ""
                        )
                    ),
                    provider="codex_agent",
                    model=self._general_config.model or "codex-configured",
                    latency_ms=int((time.perf_counter_ns() - started) // 1_000_000),
                    work_id=request.work_id,
                    raw_metadata={
                        "capability": "general",
                        "agent_result": payload,
                        "result_ttl_ms": int(self._general_config.result_ttl_s * 1000),
                    },
                )
            else:
                work.phase = "skill"
                self.feedback.changed(work)
                skill = self._skills.for_capability(route.capability)
                if skill is None:
                    raise BackendError(
                        self.name,
                        f"unregistered execution capability: {route.capability}",
                    )
                planner = self._require_planner()
                skill_arguments = dict(route.arguments)
                skill_arguments.setdefault("objective", route.objective)
                skill_arguments.setdefault("user_request", route.objective)
                started = time.perf_counter_ns()
                async with _admission(scheduler, self.feedback.config.queue_timeout_s):
                    import inspect

                    progress_kwargs = {}
                    if "report_progress" in inspect.signature(skill.plan).parameters:

                        async def report_progress(text, next_step="", kind="milestone"):
                            if not isinstance(text, str) or not 1 <= len(text) <= 800:
                                raise ValueError("invalid skill progress")
                            if (
                                kind not in {"milestone", "important", "correction"}
                                or not isinstance(next_step, str)
                                or len(next_step) > 500
                            ):
                                raise ValueError("invalid skill progress")
                            if work.state == WorkState.RUNNING:
                                work.progress = {
                                    "text": text,
                                    "next_step": next_step,
                                    "kind": kind,
                                    "source": "skill",
                                }
                                self.feedback.changed(work)

                        progress_kwargs["report_progress"] = report_progress
                    payload = await asyncio.wait_for(
                        skill.plan(
                            planner,
                            skill_arguments,
                            played_reply_context=(
                                [request.snapshot.played_tts_text]
                                if request.snapshot.played_tts_text
                                else []
                            ),
                            session_context=_session_summary(request),
                            **progress_kwargs,
                        ),
                        self.feedback.config.skill_timeout_s,
                    )
                response = _planned_response(
                    payload,
                    provider="agent_skill",
                    model=skill.name,
                    work_id=request.work_id,
                    started_ns=started,
                    capability=route.capability,
                )

            await self._record_work_output(work, payload)
            return response
        except asyncio.CancelledError:
            await self._cancel_running_work(work)
            raise
        except Exception as exc:
            await self._fail_work(work, exc)
            raise

    async def _route_execution(self, request: DelegateRequest) -> _ExecutionRoute:
        objective = request.query.strip()
        if not objective:
            raise BackendError(self.name, "delegate objective must not be empty")
        planner = self._require_planner()
        capabilities = [
            {
                "name": "progress_query",
                "description": "只查询已有任务的进度、状态或是否完成；不继续执行或修改任务，不启动 Agent。",
                "arguments_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["resolution"],
                    "properties": {
                        "resolution": {
                            "type": "string",
                            "enum": ["matched", "ambiguous", "not_found"],
                        },
                        "target_work_id": {"type": "string"},
                    },
                },
            },
            {
                "name": "multimodal",
                "description": "无需工具执行的普通问答、音视频或图片理解、总结和翻译，直接调用多模态模型。",
                "arguments_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
            {
                "name": "general",
                "description": "需要多步推理、检索、文件分析、代码或工具执行并验证结果的任务；支持文字及图片证据。",
                "arguments_schema": {
                    "type": "object",
                    "properties": {
                        "input_mode": {
                            "type": "string",
                            "enum": ["text", "text_multimodal"],
                        }
                    },
                    "required": ["input_mode"],
                    "additionalProperties": True,
                },
            },
            *self._skills.routing_contracts,
        ]
        history = self._general_history(request)
        decision = await planner.route_capability(
            objective,
            capabilities,
            evidence=_evidence_summary(request),
            session_context={
                **_session_summary(request),
                "general_history": history,
                "progress_targets": self._progress_targets(request),
            },
        )
        capability = decision.get("capability")
        arguments = decision.get("arguments", {})
        contracts = {str(item["name"]): item for item in capabilities}
        if capability not in contracts:
            raise BackendError(
                self.name, "capability router returned an unregistered capability"
            )
        if not isinstance(arguments, dict):
            raise BackendError(
                self.name, "capability router arguments must be an object"
            )
        schema = contracts[capability]["arguments_schema"]
        if not isinstance(schema, dict):
            raise BackendError(self.name, "capability routing schema must be an object")
        try:
            validate_routing_arguments(arguments, schema)
        except (TypeError, ValueError) as exc:
            raise BackendError(
                self.name, f"invalid arguments for {capability}: {exc}"
            ) from exc
        if capability == "progress_query":
            target_id = arguments.get("target_work_id")
            if arguments["resolution"] == "matched":
                if target_id not in {
                    item["work_id"] for item in self._progress_targets(request)
                }:
                    raise BackendError(self.name, "invalid progress query target")
            elif target_id is not None:
                raise BackendError(
                    self.name, "unresolved query must not specify a target"
                )
        parent_id = decision.get("continuation_of")
        if parent_id is not None and (
            capability != "general"
            or not isinstance(parent_id, str)
            or parent_id not in {item["work_id"] for item in history}
            or parent_id
            not in {item["work_id"] for item in self._general_history(request)}
        ):
            raise BackendError(self.name, "invalid or unavailable continuation target")
        return _ExecutionRoute(objective, capability, dict(arguments), parent_id)

    def _progress_targets(self, request):
        return [
            {
                "work_id": w.work_id,
                "objective": w.objective[:500],
                "state": str(w.state),
                "capability": w.capability,
            }
            for w in reversed(tuple(self._works.works.values()))
            if w.session_id == request.session_id
            and w.work_id != request.work_id
            and w.capability != "progress_query"
            and w.work_id not in self._retired_history
        ][:20]

    def _general_history(self, request):
        """Bounded same-session lineage heads; no unrelated history in the Router."""
        heads = {}
        for work in self._works.works.values():
            if (
                work.session_id == request.session_id
                and work.capability == "general"
                and work.work_id not in self._retired_history
            ):
                # A failed/cancelled head also hides older successful revisions: its
                # workspace may already contain partial changes.
                heads[work.lineage_id or work.work_id] = work
        items = []
        for work in reversed(tuple(heads.values())):
            if work.work_id == request.work_id or work.state not in {
                WorkState.RUNNING,
                WorkState.COMPLETED,
                WorkState.DELIVERING,
                WorkState.DELIVERED,
            }:
                continue
            result = work.result or {}
            items.append(
                {
                    "work_id": work.work_id,
                    "lineage_id": work.lineage_id or work.work_id,
                    "objective": work.objective[:500],
                    "state": str(work.state),
                    "result_summary": str(result.get("full_result", ""))[:1000],
                    "artifacts": [
                        Path(a["path"]).name for a in result.get("artifacts", [])[:8]
                    ],
                }
            )
            if len(items) >= 8:
                break
        return items

    def close_session_history(self, session_id):
        self.progress_reporter.forget_session(session_id)
        self.feedback.forget_session(session_id)
        for key, (request, _) in tuple(self._feedback_routes.items()):
            if request.session_id == session_id:
                self._feedback_routes.pop(key, None)
        self._retired_history.update(
            w.work_id for w in self._works.works.values() if w.session_id == session_id
        )
        forget = getattr(self._general, "forget_session", None)
        if forget is not None:
            forget(session_id)

    async def oralize(
        self,
        request: DelegateRequest,
        source: BackendResponse,
    ) -> BackendResponse:
        work = self._works.get(request.work_id)
        if work is not None:
            async with self._state_lock:
                if work.state in {WorkState.CANCELLING, WorkState.CANCELLED}:
                    work.set_state(WorkState.CANCELLED)
                    self._works.done_event(work.work_id).set()
                    raise asyncio.CancelledError
                current = asyncio.current_task()
                if current is not None:
                    self._active_tasks[work.work_id] = current
        try:
            # All execution branches share the same final spoken rendering.
            # The selected direct provider therefore polishes multimodal,
            # General Agent and Skill output consistently.
            if source.raw_metadata.get("progress_query"):
                return source
            response = await self._direct_backend.oralize(request, source)
            if not isinstance(response, BackendResponse) or not response.text.strip():
                raise ValueError("empty final Polish")
            return response
        except asyncio.CancelledError:
            # wait_for also cancels this child when Polish times out. Only an
            # explicit Work cancellation owns the business cancellation state.
            if work is not None and work.state is WorkState.CANCELLING:
                await self._cancel_running_work(work)
            raise

    def prepare_terminal_result(self, result: DelegateResult) -> DelegateResult:
        """Long General failures also deserve a fresh delivery window."""
        work = self._works.get(result.work_id)
        if (
            work is not None
            and work.state in {WorkState.CANCELLED, WorkState.CANCELLING}
            and result.metadata.get("kind") != "cancelled"
        ):
            raise asyncio.CancelledError
        if work is None or work.capability != "general":
            return result
        metadata = deepcopy(result.metadata)
        metadata["stale"] = False
        metadata.setdefault("execution", {}).update(
            capability="general", work_id=work.work_id
        )
        return replace(
            result,
            expires_at_ms=result.completed_at_ms
            + int(self._general_config.result_ttl_s * 1000),
            metadata=metadata,
        )

    async def observe_terminal_result(self, result: DelegateResult) -> None:
        """Commit Work state from the complete Harness pipeline outcome."""

        async with self._state_lock:
            work = self._works.get(result.work_id)
            if work is None:
                return
            if work.state in {WorkState.CANCELLING, WorkState.CANCELLED}:
                work.set_state(WorkState.CANCELLED)
            elif work.fault or result.metadata.get("fallback"):
                if not work.fault:
                    self.record_fault(
                        work.work_id, ApiNotConfiguredError("capability unavailable")
                    )
                work.set_state(WorkState.FAILED)
            elif result.status == "completed":
                if work.state is WorkState.RUNNING:
                    work.set_state(WorkState.COMPLETED)
                # A provider exception may intentionally leave a speakable
                # fallback result while the business Work remains failed.
            elif work.state in {
                WorkState.QUEUED,
                WorkState.RUNNING,
                WorkState.COMPLETED,
            }:
                work.error = result.error or "delegate pipeline failed"
                work.set_state(WorkState.FAILED)
            work.phase = str(work.state)
            self.feedback.changed(work)
            self._active_tasks.pop(work.work_id, None)
            self._works.done_event(work.work_id).set()
            self._works.refresh_queue_positions()

    async def aclose(self) -> None:
        await self.progress_reporter.close()
        tasks = tuple(
            {t for t in self._active_tasks.values() if t is not asyncio.current_task()}
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._general.aclose()
        self.journal.close()
        close = getattr(self._direct_backend, "aclose", None)
        if callable(close):
            await close()

    def get_work(self, work_id: str) -> dict[str, Any] | None:
        """Return a detached public snapshot, never the mutable Work object."""

        work = self._works.get(work_id)
        return work.snapshot() if work is not None else None

    def list(
        self,
        *,
        active_only: bool = False,
        newest_first: bool = True,
    ) -> list[dict[str, object]]:
        return self._works.snapshots(
            active_only=active_only,
            newest_first=newest_first,
        )

    def list_works(self, *, active_only: bool = False) -> list[dict[str, object]]:
        return self.list(active_only=active_only)

    async def mark_delivering(self, work_id: str) -> dict[str, Any]:
        async with self._state_lock:
            work = self._require_work(work_id)
            if work.state is WorkState.DELIVERING:
                return work.snapshot()
            self._require_state(work, WorkState.COMPLETED, "deliver")
            work.delivery_attempts += 1
            work.set_state(WorkState.DELIVERING)
            work.delivery_status = "delivering"
            self.feedback.changed(work)
            return work.snapshot()

    async def mark_delivered(self, work_id: str) -> dict[str, Any]:
        async with self._state_lock:
            work = self._require_work(work_id)
            if work.state is WorkState.DELIVERED:
                return work.snapshot()
            self._require_state(work, WorkState.DELIVERING, "mark delivered")
            work.set_state(WorkState.DELIVERED)
            work.delivery_status = "delivered"
            self.feedback.changed(work)
            return work.snapshot()

    async def mark_delivery_failed(self, work_id: str, error: str) -> dict[str, Any]:
        """Stop retrying a completed/delivering work after delivery failures."""

        async with self._state_lock:
            work = self._require_work(work_id)
            if work.state is WorkState.FAILED:
                return work.snapshot()
            if work.state not in {WorkState.COMPLETED, WorkState.DELIVERING}:
                raise ValueError(
                    f"cannot fail delivery for work {work.work_id} in state {work.state}"
                )
            work.delivery_error = local_text(
                "结果未能确认送达，请查看已保存的任务结果。", work.language
            )
            work.delivery_status = "failed"
            work.set_state(WorkState.COMPLETED)
            self.feedback.changed(work)
            return work.snapshot()

    async def cancel(self, work_id: str) -> dict[str, Any]:
        """Cancel cancellable work and wait until an executing call acknowledges it."""

        async with self._state_lock:
            work = self._require_work(work_id)
            if work.state not in self._CANCELLABLE_STATES:
                raise ValueError(f"work cannot be cancelled in state {work.state}")
            done = self._works.done_event(work_id)
            task = self._active_tasks.get(work_id)
            if work.state is WorkState.RUNNING:
                work.set_state(WorkState.CANCELLING)
                wait_for_task = (
                    task is not None
                    and not task.done()
                    and task is not asyncio.current_task()
                )
                if not wait_for_task:
                    work.set_state(WorkState.CANCELLED)
                    done.set()
            else:
                work.set_state(WorkState.CANCELLED)
                done.set()
                wait_for_task = False
            self._works.refresh_queue_positions()

        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if wait_for_task:
            await done.wait()
        work = self._require_work(work_id)
        if work.execution_outcome != "unknown":
            work.execution_outcome = "cancelled"
        self.feedback.changed(work)
        route = self._feedback_routes.get(work_id)
        if route and not any(r["terminal"] for r in work.feedback_records):
            _request, publish = route
            now = time.time_ns() // 1_000_000
            result = DelegateResult(
                work_id,
                work.session_id,
                "cancelled",
                spoken_text=local_text("任务已取消。", work.language),
                completed_at_ms=now,
                metadata={"kind": "cancelled"},
            )
            await publish(self.feedback.terminal(result))
        return work.snapshot()

    cancel_work = cancel

    async def accept_work(self, request: DelegateRequest) -> Work:
        work = Work(
            work_id=request.work_id,
            objective=request.query,
            session_id=request.session_id,
            language=str(request.language),
            snapshot_id=request.snapshot.snapshot_id,
            capability="unrouted",
        )
        async with self._state_lock:
            self._works.register(work)
            self.feedback.changed(work)
            self._works.refresh_queue_positions()
        return work

    def bind_work_task(self, work_id, task):
        self._active_tasks[work_id] = task

    async def _start_work(self, work):
        async with self._state_lock:
            if work.state in {WorkState.CANCELLED, WorkState.CANCELLING}:
                raise asyncio.CancelledError
            if work.state is not WorkState.QUEUED:
                raise BackendError(self.name, "duplicate delegate execution")
            work.set_state(WorkState.RUNNING)
            work.phase = "routing"
            work.execution_outcome = "running"
            self.feedback.changed(work)
            self._active_tasks[work.work_id] = asyncio.current_task()
            self._works.refresh_queue_positions()

    async def observe_cancelled(self, work_id):
        work = self._works.get(work_id)
        if work is not None and work.state in self._CANCELLABLE_STATES | {
            WorkState.CANCELLING
        }:
            await self._cancel_running_work(work)

    async def _on_agent_event(self, event):
        async with self._state_lock:
            work = self._works.get(event.work_id)
            if (
                work is None
                or work.session_id != event.session_id
                or (
                    work.state is not WorkState.RUNNING
                    and not (
                        event.kind == "cancelled" and work.state is WorkState.CANCELLING
                    )
                )
            ):
                return
            details = deepcopy(event.details)
            work.agent_events.append(
                {
                    "kind": event.kind,
                    "at_ms": time.time_ns() // 1_000_000,
                    "details": details,
                }
            )
            del work.agent_events[:-200]
            old_phase = work.phase
            if (
                event.kind == "cancelled"
                and details.get("background_terminals_cleaned") is False
            ):
                work.execution_outcome = "unknown"
                work.fault = {
                    "code": "STOP_UNCONFIRMED",
                    "message": local_text(
                        "已请求停止任务，但无法确认所有后台工具均已停止。",
                        work.language,
                    ),
                }
            work.phase = {
                "queued": "waiting_agent",
                "running": "agent",
                "started": "agent",
                "completed": "polish",
            }.get(event.kind, work.phase)
            if work.phase != old_phase:
                self.feedback.changed(work)

    def get_work_result(self, session_id, work_id):
        work = self._require_work(work_id)
        if work.session_id != session_id:
            raise ValueError("work belongs to another session")
        return deepcopy(work.result)

    def read_work_artifact(self, session_id, work_id, index):
        result = self.get_work_result(session_id, work_id)
        artifacts = result.get("artifacts", []) if result else []
        if type(index) is not int or not 0 <= index < len(artifacts):
            raise ValueError("unknown work artifact")
        artifact = artifacts[index]
        data = Path(artifact["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ValueError("artifact changed since the Agent verified it")
        return data

    def get_work_events(self, session_id, work_id):
        work = self._require_work(work_id)
        if work.session_id != session_id:
            raise ValueError("work belongs to another session")
        return deepcopy(work.agent_events)

    async def _record_work_output(self, work: Work, payload: dict[str, Any]) -> None:
        cancelled = False
        async with self._state_lock:
            if work.state in {WorkState.CANCELLING, WorkState.CANCELLED}:
                work.set_state(WorkState.CANCELLED)
                cancelled = True
            else:
                work.result = deepcopy(payload)
                work.execution_outcome = payload.get("outcome", "completed")
                work.phase = "polish"
                self.feedback.changed(work)
            self._works.refresh_queue_positions()
        if cancelled:
            raise asyncio.CancelledError

    async def _fail_work(self, work: Work, exc: Exception) -> None:
        async with self._state_lock:
            if work.state not in {WorkState.CANCELLED, WorkState.CANCELLING}:
                self.record_fault(work.work_id, exc)
                work.set_state(WorkState.FAILED)
                self.feedback.changed(work)
            else:
                work.set_state(WorkState.CANCELLED)
                if work.execution_outcome != "unknown":
                    work.execution_outcome = "cancelled"
            work.phase = str(work.state)
            self.feedback.changed(work)
            self._active_tasks.pop(work.work_id, None)
            self._works.done_event(work.work_id).set()
            self._works.refresh_queue_positions()

    async def _cancel_running_work(self, work: Work) -> None:
        async with self._state_lock:
            work.set_state(WorkState.CANCELLED)
            if work.execution_outcome != "unknown":
                work.execution_outcome = "cancelled"
            work.phase = "cancelled"
            work.phase = str(work.state)
            self.feedback.changed(work)
            self._active_tasks.pop(work.work_id, None)
            self._works.done_event(work.work_id).set()
            self._works.refresh_queue_positions()

    def _require_planner(self) -> PlannerLike:
        if self._planner is None:
            raise ApiNotConfiguredError("planner is not configured")
        return self._planner

    def _require_work(self, work_id: str) -> Work:
        work = self._works.get(work_id)
        if work is None:
            raise ValueError(f"unknown work_id: {work_id}")
        return work

    @staticmethod
    def _require_state(work: Work, expected: WorkState, action: str) -> None:
        if work.state is not expected:
            raise ValueError(
                f"cannot {action} work {work.work_id} in state {work.state}"
            )


def _planned_response(
    payload: Any,
    *,
    provider: str,
    model: str,
    work_id: str,
    started_ns: int,
    capability: str,
) -> BackendResponse:
    if not isinstance(payload, dict):
        raise BackendError(
            AgentDelegateBackend.name, "agent execution must return an object"
        )
    speech = payload.get("speech")
    if not isinstance(speech, str) or not speech.strip():
        raise BackendError(
            AgentDelegateBackend.name, "agent execution result is missing speech"
        )
    return BackendResponse(
        text=speech.strip(),
        provider=provider,
        model=model,
        latency_ms=int((time.perf_counter_ns() - started_ns) // 1_000_000),
        work_id=work_id,
        raw_metadata={
            "capability": capability,
            "agent_result": dict(payload),
        },
    )


def _response_payload(response: BackendResponse) -> dict[str, Any]:
    return {
        "ok": True,
        "speech": response.text,
        "provider": response.provider,
        "model": response.model,
        "latency_ms": response.latency_ms,
    }


def _evidence_summary(request: DelegateRequest) -> dict[str, Any]:
    snapshot = request.snapshot
    return {
        "snapshot_id": snapshot.snapshot_id,
        "cutoff_ms": snapshot.cutoff_ms,
        "window_start_ms": snapshot.window_start_ms,
        "audio_chunks": len(snapshot.audio_chunks),
        "video_segments": len(snapshot.video_segments),
        "video_frames": len(snapshot.video_frames),
        "input_files": [
            {"name": f.name, "mime_type": f.mime_type} for f in snapshot.input_files
        ],
        "visual_frame_times_ms": [f.captured_at_ms for f in snapshot.video_frames],
        "played_tts_text": snapshot.played_tts_text,
    }


def _session_summary(request: DelegateRequest) -> dict[str, Any]:
    return {
        "session_id": request.session_id,
        "work_id": request.work_id,
        "snapshot_id": request.snapshot.snapshot_id,
        "identity": request.identity,
        "language": request.language,
    }


__all__ = ["AgentDelegateBackend"]


@asynccontextmanager
async def _admission(scheduler, timeout_s=300):
    if scheduler is None:
        yield
    else:
        admission = scheduler.acquire()
        await asyncio.wait_for(admission.__aenter__(), timeout_s)
        try:
            yield
        finally:
            await admission.__aexit__(None, None, None)
