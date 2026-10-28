"""Typed data contracts shared by the event loop, policy, and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class AudioChunk:
    """A timestamped user-audio chunk in PCM16LE or WAV form."""

    data: bytes
    start_ms: int
    end_ms: int
    mime_type: str = "audio/pcm"
    sample_rate_hz: int = 16000
    channels: int = 1
    sample_width_bytes: int = 2

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("audio chunk data must not be empty")
        if self.end_ms <= self.start_ms:
            raise ValueError("audio chunk end_ms must be greater than start_ms")
        if self.sample_rate_hz <= 0 or self.channels <= 0:
            raise ValueError("audio format values must be positive")
        if self.sample_width_bytes <= 0:
            raise ValueError("sample_width_bytes must be positive")

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class VideoSegment:
    """An encoded, already-remuxed video window suitable for direct upload."""

    data: bytes
    start_ms: int
    end_ms: int
    mime_type: str = "video/mp4"
    fps: float = 1.0

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("video segment data must not be empty")
        if self.end_ms <= self.start_ms:
            raise ValueError("video segment end_ms must be greater than start_ms")
        if self.fps <= 0:
            raise ValueError("video fps must be positive")

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class VideoFrame:
    """A pre-encoded frame for integrations that do not expose MP4 segments."""

    data: bytes
    captured_at_ms: int
    mime_type: str = "image/jpeg"

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("video frame data must not be empty")

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class BinaryAsset:
    """An assembled file that can be sent to an external multimodal API."""

    data: bytes
    mime_type: str

    @property
    def size_bytes(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class VideoInput:
    """Either one direct video segment or an ordered frame sequence."""

    segment: Optional[VideoSegment] = None
    frames: Tuple[VideoFrame, ...] = ()

    def __post_init__(self) -> None:
        if self.segment is not None and self.frames:
            raise ValueError("video input cannot contain both a segment and frames")

    @property
    def is_empty(self) -> bool:
        return self.segment is None and not self.frames


class InputMode(str, Enum):
    """Evidence shape selected by the text-only routing call."""

    TEXT = "text"
    TEXT_AUDIO = "text_audio"
    TEXT_MULTIMODAL = "text_multimodal"


class ResponseMode(str, Enum):
    """Whether execution returns spoken text in one call or a second pass."""

    SINGLE_STAGE = "single_stage"
    TWO_STAGE = "two_stage"


class DelegateOperation(str, Enum):
    """The result contract selected by the text-only routing call."""

    ANSWER = "answer"
    LONG_WRITING = "long_writing"
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"


class Language(str, Enum):
    """Language used for routing, execution, and spoken status prompts."""

    ZH = "zh"
    EN = "en"


class DelegateRelation(str, Enum):
    """How a new delegate relates to an older active request in one session."""

    INDEPENDENT = "independent"
    UPDATE = "update"


@dataclass(frozen=True)
class InputFile:
    """Immutable attachment bytes admitted by the host before the T0 fence."""

    name: str
    data: bytes
    mime_type: str = "application/octet-stream"

    def __post_init__(self):
        from pathlib import Path

        if (
            not self.name
            or Path(self.name).name != self.name
            or self.name in {".", ".."}
        ):
            raise ValueError("input name must be a filename")
        if not isinstance(self.data, bytes):
            raise TypeError("attachment data must be bytes")


@dataclass(frozen=True)
class ContextSnapshot:
    """Immutable media evidence frozen at the opening delegate tag."""

    session_id: str
    snapshot_id: str
    cutoff_ms: int
    cutoff_sequence: int
    audio_chunks: Tuple[AudioChunk, ...]
    video_segments: Tuple[VideoSegment, ...]
    video_frames: Tuple[VideoFrame, ...]
    played_tts_text: str
    window_start_ms: int
    input_files: Tuple[InputFile, ...] = ()


@dataclass(frozen=True)
class PreparedContext:
    """Provider-ready media, built from a snapshot without changing its scope."""

    snapshot: ContextSnapshot
    audio: Optional[BinaryAsset]
    video: Optional[VideoInput]
    played_tts_text: str


@dataclass(frozen=True)
class DelegateRequest:
    """A fully closed delegate task and its frozen multimodal evidence."""

    work_id: str
    session_id: str
    query: str
    snapshot: ContextSnapshot
    created_at_ms: int
    expires_at_ms: int
    backend_name: str
    model_name: Optional[str] = None
    routing_locked: bool = False
    allow_web_search: bool = False
    identity: str = "Venus"
    input_mode: InputMode = InputMode.TEXT_MULTIMODAL
    operation: DelegateOperation = DelegateOperation.ANSWER
    # Direct in-process callers retain the original two-stage behavior.
    # The Venus wire decoder defaults omitted fields to SINGLE_STAGE to meet
    # the serving gateway's strict latency budget.
    response_mode: ResponseMode = ResponseMode.TWO_STAGE
    language: str = Language.ZH.value
    available_operations: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DelegateCandidate:
    """Media-free summary of an earlier active request for routing only."""

    work_id: str
    query: str
    created_at_ms: int


@dataclass(frozen=True)
class Usage:
    """Best-effort usage metadata normalised across providers."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendResponse:
    """Raw textual output returned from a provider adapter."""

    text: str
    provider: str
    model: str
    latency_ms: int
    usage: Usage = field(default_factory=Usage)
    work_id: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
    artifacts: Tuple[DelegateArtifact, ...] = ()


@dataclass(frozen=True)
class WorkspaceFile:
    """A document written by the harness into the configured workspace."""

    path: str
    relative_path: str
    mime_type: str = "text/markdown"
    char_count: int = 0


@dataclass(frozen=True)
class DelegateArtifact:
    """A provider-owned image or video reference returned by a generation backend."""

    uri: str
    mime_type: str
    kind: DelegateOperation
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {
            DelegateOperation.IMAGE_GENERATION,
            DelegateOperation.VIDEO_GENERATION,
        }:
            raise ValueError(
                "delegate artifact kind must be an image or video generation operation"
            )
        if not self.uri.strip() or not self.mime_type.strip():
            raise ValueError("delegate artifact uri and mime_type must not be blank")


@dataclass(frozen=True)
class RoutingDecision:
    """Constrained text-only plan emitted before a delegate is executed."""

    input_mode: InputMode
    relation: DelegateRelation
    supersedes_work_id: Optional[str]
    provider: str
    model: str
    latency_ms: int
    operation: DelegateOperation = DelegateOperation.ANSWER
    requires_web_search: bool = False
    usage: Usage = field(default_factory=Usage)
    fallback: bool = False
    policy_override: Optional[str] = None


@dataclass(frozen=True)
class DelegateResult:
    """A private result ready for delivery to the calling frontend."""

    work_id: str
    session_id: str
    status: str
    spoken_text: str = ""
    raw_text: str = ""
    provider: str = ""
    model: str = ""
    created_at_ms: int = 0
    completed_at_ms: int = 0
    expires_at_ms: int = 0
    usage: Usage = field(default_factory=Usage)
    artifacts: Tuple[DelegateArtifact, ...] = ()
    workspace_files: Tuple[WorkspaceFile, ...] = ()
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    feedback_id: str = ""

    @property
    def delivery_id(self) -> str:
        return self.feedback_id or self.work_id

    @property
    def is_success(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class HarnessEvent:
    """Observable lifecycle event; never contains raw media bytes."""

    type: str
    session_id: str
    at_ms: int
    work_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParserStep:
    """One incremental parser output for a streamed model text delta."""

    visible_text: str = ""
    delegate_candidate_started: bool = False
    delegate_candidate_abandoned: bool = False
    opened_delegate: bool = False
    delegate_query: Optional[str] = None
    protocol_error: Optional[str] = None
