"""Configuration with conservative V1 defaults and environment key lookup."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

from .models import DelegateOperation, InputMode, Language, ResponseMode


@dataclass(frozen=True)
class BufferConfig:
    retention_ms: int = 60_000
    context_window_ms: int = 12_000
    max_audio_bytes: int = 8 * 1024 * 1024
    max_video_bytes: int = 32 * 1024 * 1024
    max_video_frames: int = 48
    selected_video_frames: int = 12
    played_tts_max_chars: int = 320

    def __post_init__(self) -> None:
        values = (
            self.retention_ms,
            self.context_window_ms,
            self.max_audio_bytes,
            self.max_video_bytes,
            self.max_video_frames,
            self.selected_video_frames,
            self.played_tts_max_chars,
        )
        if any(value < 1 for value in values):
            raise ValueError("buffer limits must be positive")


@dataclass(frozen=True)
class WorkspaceConfig:
    """Filesystem delivery settings for long documents and generated assets."""

    root: Optional[str] = None
    documents_subdir: str = "delegate_documents"
    max_document_chars: int = 500_000

    def __post_init__(self) -> None:
        if self.root is not None and not str(self.root).strip():
            raise ValueError("workspace root must not be blank")
        if (
            not self.documents_subdir.strip()
            or self.documents_subdir in {".", ".."}
            or "/" in self.documents_subdir
            or "\\" in self.documents_subdir
        ):
            raise ValueError(
                "documents_subdir must be a single non-empty path component"
            )
        if self.max_document_chars < 1:
            raise ValueError("max_document_chars must be positive")

    @property
    def root_path(self) -> Optional[Path]:
        return Path(self.root).expanduser().resolve() if self.root is not None else None


@dataclass(frozen=True)
class OperationCapability:
    """User-declared backend and delivery contract for one operation."""

    enabled: bool = True
    backend_name: Optional[str] = None
    workspace_required: bool = False

    def __post_init__(self) -> None:
        if self.backend_name is not None and not self.backend_name.strip():
            raise ValueError("backend_name must not be blank")


def _default_capabilities() -> Dict[DelegateOperation, OperationCapability]:
    return {
        DelegateOperation.ANSWER: OperationCapability(),
        DelegateOperation.LONG_WRITING: OperationCapability(workspace_required=True),
        DelegateOperation.IMAGE_GENERATION: OperationCapability(),
        DelegateOperation.VIDEO_GENERATION: OperationCapability(),
    }


@dataclass(frozen=True)
class DelegateConfig:
    max_query_chars: int = 4_000
    max_result_chars: int = 3_000
    result_ttl_ms: int = 60_000
    request_timeout_s: float = 30.0
    web_search_timeout_s: float = 45.0
    # The first call reasons over media; the second call renders only its text
    # answer into an utterance suitable for direct injection into the thinker.
    oralization_enabled: bool = True
    oralization_timeout_s: float = 30.0
    slow_api_notice_after_s: float = 10.0
    # The text-only routing call only sees compact task summaries. Bounding
    # candidates keeps a long-lived session from growing this prompt forever.
    max_relation_candidates: int = 8
    # Defaults used when the fine-tuned duplex frontend emits only
    # ``<delegate>natural-language objective</delegate>``.  Explicit wire
    # requests can still override every field.
    default_routing_locked: bool = False
    default_allow_web_search: bool = False
    default_input_mode: InputMode = InputMode.TEXT_MULTIMODAL
    default_operation: DelegateOperation = DelegateOperation.ANSWER
    default_response_mode: ResponseMode = ResponseMode.TWO_STAGE

    def __post_init__(self) -> None:
        if self.max_query_chars < 1 or self.max_result_chars < 1:
            raise ValueError("delegate character limits must be positive")
        if self.result_ttl_ms < 1:
            raise ValueError("result_ttl_ms must be positive")
        if self.max_relation_candidates < 0:
            raise ValueError("max_relation_candidates must not be negative")
        if (
            self.request_timeout_s <= 0
            or self.web_search_timeout_s <= 0
            or self.oralization_timeout_s <= 0
            or self.slow_api_notice_after_s <= 0
        ):
            raise ValueError("delegate timeouts must be positive")
        object.__setattr__(
            self, "default_input_mode", InputMode(self.default_input_mode)
        )
        object.__setattr__(
            self, "default_operation", DelegateOperation(self.default_operation)
        )
        object.__setattr__(
            self, "default_response_mode", ResponseMode(self.default_response_mode)
        )


@dataclass(frozen=True)
class RuntimeConfig:
    """Cross-session resource bounds for the local Harness process."""

    max_concurrent_delegate_requests: int = 8
    max_buffered_delegate_results: int = 16

    def __post_init__(self) -> None:
        if self.max_concurrent_delegate_requests < 1:
            raise ValueError("max_concurrent_delegate_requests must be at least 1")
        if self.max_buffered_delegate_results < 1:
            raise ValueError("max_buffered_delegate_results must be at least 1")


@dataclass(frozen=True)
class ResultManagementConfig:
    """Optional training-free gate for completed delegate results.

    Disabled is the compatibility mode: completed results are delivered to the
    frontend immediately. When enabled, terminal results are released one at a
    time during a stable duplex gap.
    """

    enabled: bool = False
    initial_silence_ms: int = 800
    min_silence_ms: int = 250
    silence_decay: float = 0.05
    max_queue_wait_ms: int = 8_000
    frontend_reaction_timeout_ms: int = 2_500
    max_playback_wait_ms: int = 60_000
    chars_per_second: float = 4.0
    speech_rms_threshold: float = 500.0
    speech_hold_ms: int = 250
    manager_tick_ms: int = 50

    def __post_init__(self) -> None:
        if self.initial_silence_ms < 0 or self.min_silence_ms < 0:
            raise ValueError("result-management silence windows must not be negative")
        if self.min_silence_ms > self.initial_silence_ms:
            raise ValueError("min_silence_ms must not exceed initial_silence_ms")
        if self.silence_decay < 0:
            raise ValueError("silence_decay must not be negative")
        if self.max_queue_wait_ms < 1:
            raise ValueError("max_queue_wait_ms must be positive")
        if self.frontend_reaction_timeout_ms < 1 or self.max_playback_wait_ms < 1:
            raise ValueError("result-management presentation timeouts must be positive")
        if self.chars_per_second <= 0:
            raise ValueError("chars_per_second must be positive")
        if self.speech_rms_threshold < 0:
            raise ValueError("speech_rms_threshold must not be negative")
        if self.speech_hold_ms < 0:
            raise ValueError("speech_hold_ms must not be negative")
        if self.manager_tick_ms < 1:
            raise ValueError("manager_tick_ms must be positive")


@dataclass(frozen=True)
class HarnessConfig:
    """Top-level V1 configuration.

    ``default_backend`` selects the routing backend for a session. Execution
    backends may be remapped per operation only through explicit capabilities;
    semantic routing remains a model decision plus a small deterministic policy.
    """

    default_backend: str = "agent"
    default_identity: str = "Venus"
    language: Language = Language.ZH
    buffer: BufferConfig = field(default_factory=BufferConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    delegate: DelegateConfig = field(default_factory=DelegateConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    result_management: ResultManagementConfig = field(
        default_factory=ResultManagementConfig
    )
    backend_search_enabled: Dict[str, bool] = field(
        default_factory=lambda: {"agent": True},
    )
    capabilities: Mapping[DelegateOperation, OperationCapability] = field(
        default_factory=_default_capabilities,
    )

    def __post_init__(self) -> None:
        if not " ".join(self.default_identity.split()):
            raise ValueError("default_identity must not be blank")
        try:
            object.__setattr__(self, "language", Language(self.language))
        except ValueError as exc:
            raise ValueError("language must be 'zh' or 'en'") from exc
        merged_capabilities = _default_capabilities()
        merged_capabilities.update(self.capabilities)
        object.__setattr__(self, "capabilities", merged_capabilities)
        for operation, capability in merged_capabilities.items():
            if not isinstance(operation, DelegateOperation):
                raise ValueError("capability keys must be DelegateOperation values")
            if not isinstance(capability, OperationCapability):
                raise ValueError("capability values must be OperationCapability values")
