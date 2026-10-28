"""Central construction of frontend-safe delegate results."""

from __future__ import annotations

from typing import Optional, Tuple

from .config import DelegateConfig
from .formatter import to_spoken_text
from .models import (
    BackendResponse,
    DelegateOperation,
    DelegateRequest,
    DelegateResult,
    RoutingDecision,
    Usage,
    WorkspaceFile,
)

API_NOT_CONFIGURED_TEXT = "当前没有配置后端 API，暂时无法进行后台调用。"
MAX_WAIT_NOTICE_QUERY_CHARS = 36


def _operation_label(operation: DelegateOperation, language: str) -> str:
    if language == "en":
        return {
            DelegateOperation.LONG_WRITING: "long document",
            DelegateOperation.IMAGE_GENERATION: "image",
            DelegateOperation.VIDEO_GENERATION: "video",
        }.get(operation, "task")
    return {
        DelegateOperation.LONG_WRITING: "长文",
        DelegateOperation.IMAGE_GENERATION: "图片",
        DelegateOperation.VIDEO_GENERATION: "视频",
    }.get(operation, "任务")


def _capability_unavailable_text(
    operation: DelegateOperation, language: str, reason: str
) -> str:
    label = _operation_label(operation, language)
    if language == "en":
        if reason == "workspace":
            return "No workspace is configured, so I cannot save the {} yet.".format(
                label
            )
        if operation is DelegateOperation.IMAGE_GENERATION:
            return "No image generation capability is configured yet, so I cannot complete it."
        if operation is DelegateOperation.VIDEO_GENERATION:
            return "No video generation capability is configured yet, so I cannot complete it."
        return "No usable {} capability is configured yet, so I cannot complete it.".format(
            label
        )
    if reason == "workspace":
        return "当前没有配置工作区，暂时无法保存{}。".format(label)
    if operation is DelegateOperation.IMAGE_GENERATION:
        return "当前没有配置图片生成能力，暂时无法完成。"
    if operation is DelegateOperation.VIDEO_GENERATION:
        return "当前没有配置视频生成能力，暂时无法完成。"
    return "当前没有配置可用的{}能力，暂时无法完成。".format(label)


def _api_not_configured_text(operation: DelegateOperation, language: str) -> str:
    if language == "en":
        return "The backend API for this {} is not configured yet, so I cannot complete it.".format(
            _operation_label(operation, language),
        )
    if operation is DelegateOperation.ANSWER:
        return API_NOT_CONFIGURED_TEXT
    return "当前没有配置{}后端 API，暂时无法完成。".format(
        _operation_label(operation, language)
    )


def _failed_text(language: str) -> str:
    if language == "en":
        return "Sorry, I couldn't complete that request just now."
    return "抱歉，刚才的问题暂时没能处理完成。"


def _wait_notice_text(query: str, language: str) -> str:
    topic = " ".join(query.split())
    if len(topic) > MAX_WAIT_NOTICE_QUERY_CHARS:
        topic = topic[: MAX_WAIT_NOTICE_QUERY_CHARS - 1].rstrip() + "…"
    if language == "en":
        if not topic:
            return "Your request is still awaiting a result. Please wait a moment."
        return "Your request about '{}' is still awaiting a result. Please wait a moment.".format(
            topic
        )
    if not topic:
        return "您的问题还在等待结果，请稍等。"
    return "您问的“{}”还在等待结果，请稍等。".format(topic)


class DelegateResultFactory:
    """Keep all user-facing fallback text and result metadata in one place."""

    def __init__(self, config: DelegateConfig, *, backend_name: str) -> None:
        self._config = config
        self._backend_name = backend_name

    def success(
        self,
        request: DelegateRequest,
        decision: RoutingDecision,
        source_response: BackendResponse,
        spoken_response: BackendResponse,
        completed_at_ms: int,
        *,
        oralized: bool,
        workspace_files: Tuple[WorkspaceFile, ...] = (),
    ) -> DelegateResult:
        # Keep the executor's complete result for diagnostics/artifacts while
        # exposing only the bounded, speakable rendering to VenusOmni.  In the
        # one-stage path both values are naturally identical.
        raw_text = source_response.text.strip()
        spoken_text = to_spoken_text(
            spoken_response.text.strip(),
            self._config.max_result_chars,
        )
        if not spoken_text:
            return self.failed(
                request, completed_at_ms, "delegate backend returned an empty result"
            )
        ttl = source_response.raw_metadata.get("result_ttl_ms")
        expires_at = (
            completed_at_ms + ttl
            if isinstance(ttl, int) and ttl > 0
            else request.expires_at_ms
        )
        return DelegateResult(
            work_id=request.work_id,
            session_id=request.session_id,
            status="completed",
            spoken_text=spoken_text,
            raw_text=raw_text,
            provider=spoken_response.provider,
            model=spoken_response.model,
            created_at_ms=request.created_at_ms,
            completed_at_ms=completed_at_ms,
            expires_at_ms=expires_at,
            usage=self._combined_usage(
                decision,
                source_response.usage,
                spoken_response.usage,
                oralized,
            ),
            artifacts=source_response.artifacts,
            workspace_files=workspace_files,
            metadata={
                "stale": completed_at_ms > expires_at,
                "input_mode": request.input_mode.value,
                "operation": request.operation.value,
                "response_mode": request.response_mode.value,
                "web_search": {
                    "requested": decision.requires_web_search,
                    "enabled": request.allow_web_search,
                },
                "relation": decision.relation.value,
                "supersedes_work_id": decision.supersedes_work_id,
                "execution": dict(source_response.raw_metadata),
                "stages": {
                    "routing": {
                        "provider": decision.provider,
                        "model": decision.model,
                        "latency_ms": decision.latency_ms,
                        "fallback": decision.fallback,
                        "policy_override": decision.policy_override,
                    },
                    "multimodal": {
                        "provider": source_response.provider,
                        "model": source_response.model,
                        "latency_ms": source_response.latency_ms,
                    },
                    "oralization": {
                        "enabled": oralized,
                        "provider": spoken_response.provider if oralized else "",
                        "model": spoken_response.model if oralized else "",
                        "latency_ms": spoken_response.latency_ms if oralized else 0,
                    },
                },
            },
        )

    def unavailable(
        self,
        request: DelegateRequest,
        completed_at_ms: int,
        reason: str,
    ) -> DelegateResult:
        text = _capability_unavailable_text(request.operation, request.language, reason)
        return DelegateResult(
            work_id=request.work_id,
            session_id=request.session_id,
            status="completed",
            spoken_text=text,
            raw_text=text,
            provider="harness.core",
            model="capability-gate",
            created_at_ms=request.created_at_ms,
            completed_at_ms=completed_at_ms,
            expires_at_ms=request.expires_at_ms,
            metadata={
                "stale": completed_at_ms > request.expires_at_ms,
                "fallback": "capability_unavailable",
                "reason": reason,
                "operation": request.operation.value,
                "response_mode": request.response_mode.value,
            },
        )

    def failed(
        self, request: DelegateRequest, completed_at_ms: int, reason: str
    ) -> DelegateResult:
        text = _failed_text(request.language)
        return DelegateResult(
            work_id=request.work_id,
            session_id=request.session_id,
            status="failed",
            spoken_text=text,
            raw_text=text,
            created_at_ms=request.created_at_ms,
            completed_at_ms=completed_at_ms,
            expires_at_ms=request.expires_at_ms,
            error=reason,
            metadata={
                "stale": completed_at_ms > request.expires_at_ms,
                "response_mode": request.response_mode.value,
            },
        )

    def waiting(
        self, request: DelegateRequest, completed_at_ms: int, stage: str
    ) -> DelegateResult:
        text = to_spoken_text(
            _wait_notice_text(request.query, request.language),
            self._config.max_result_chars,
        )
        return DelegateResult(
            work_id=request.work_id,
            session_id=request.session_id,
            status="pending",
            spoken_text=text,
            raw_text=text,
            provider=self._backend_name,
            created_at_ms=request.created_at_ms,
            completed_at_ms=completed_at_ms,
            expires_at_ms=request.expires_at_ms,
            metadata={
                "stale": completed_at_ms > request.expires_at_ms,
                "pending": True,
                "stage": stage,
            },
        )

    def api_not_configured(
        self, request: DelegateRequest, completed_at_ms: int
    ) -> DelegateResult:
        text = _api_not_configured_text(request.operation, request.language)
        return DelegateResult(
            work_id=request.work_id,
            session_id=request.session_id,
            status="completed",
            spoken_text=text,
            raw_text=text,
            provider=self._backend_name,
            created_at_ms=request.created_at_ms,
            completed_at_ms=completed_at_ms,
            expires_at_ms=request.expires_at_ms,
            metadata={
                "stale": completed_at_ms > request.expires_at_ms,
                "fallback": "api_not_configured",
                "operation": request.operation.value,
                "response_mode": request.response_mode.value,
            },
        )

    @staticmethod
    def _combined_usage(
        decision: RoutingDecision,
        source_usage: Usage,
        spoken_usage: Usage,
        oralized: bool,
    ) -> Usage:
        def summed(field: str) -> Optional[int]:
            usages = [decision.usage, source_usage]
            if oralized:
                usages.append(spoken_usage)
            values = [getattr(usage, field) for usage in usages]
            known = [value for value in values if value is not None]
            return sum(known) if known else None

        return Usage(
            input_tokens=summed("input_tokens"),
            output_tokens=summed("output_tokens"),
            total_tokens=summed("total_tokens"),
            raw={
                "routing": decision.usage.raw,
                "multimodal": source_usage.raw,
                "oralization": spoken_usage.raw if oralized else {},
            },
        )
