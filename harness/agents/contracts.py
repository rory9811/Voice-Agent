"""Protocol-neutral, completed-work contracts for the General agent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AgentInput:
    name: str
    data: bytes
    mime_type: str = "application/octet-stream"

    def __post_init__(self):
        if (
            not self.name
            or Path(self.name).name != self.name
            or self.name in {".", ".."}
        ):
            raise ValueError("input name must be a plain filename")
        if not isinstance(self.data, bytes):
            raise TypeError("input data must be immutable bytes")


@dataclass(frozen=True, slots=True)
class AgentImage:
    name: str
    captured_at_ms: float | None = None
    source: str = "uploaded_image"


@dataclass(frozen=True, slots=True)
class AgentRequest:
    session_id: str
    work_id: str
    objective: str
    context: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[AgentInput, ...] = ()
    # Trusted host-selected Work ID, never an arbitrary native thread ID.
    parent_work_id: str | None = None
    images: tuple[AgentImage, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkArtifact:
    path: str
    mime_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AgentResult:
    outcome: str
    full_result: str
    artifacts: tuple[WorkArtifact, ...] = ()
    assumptions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    thread_id: str = ""
    turn_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    session_id: str
    work_id: str
    kind: str
    details: dict[str, Any] = field(default_factory=dict)


EventSink = Callable[[AgentEvent], Awaitable[None]]


class GeneralAgentPort(Protocol):
    async def run(self, request: AgentRequest, emit: EventSink) -> AgentResult: ...
    async def aclose(self) -> None: ...
