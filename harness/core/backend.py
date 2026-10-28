"""Small provider boundary used by the session orchestrator."""

from __future__ import annotations

from typing import Protocol

from .models import (
    BackendResponse,
    DelegateCandidate,
    DelegateRequest,
    PreparedContext,
    RoutingDecision,
)


class DelegateBackend(Protocol):
    """Provider boundary for evidence reasoning and final spoken rendering."""

    async def plan(
        self,
        request: DelegateRequest,
        candidates: tuple[DelegateCandidate, ...],
    ) -> RoutingDecision:
        """Select input evidence and optionally supersede an older request."""

    async def execute(
        self,
        request: DelegateRequest,
        context: PreparedContext,
    ) -> BackendResponse: ...

    async def oralize(
        self,
        request: DelegateRequest,
        source: BackendResponse,
    ) -> BackendResponse:
        """Turn a completed evidence answer into text that can be read aloud."""

        ...

    async def aclose(self) -> None: ...
