"""Public Realtime-Venus-Omni contracts: raw token/media output and private backend results."""

from __future__ import annotations

from dataclasses import dataclass

from harness.core.models import DelegateResult


@dataclass(frozen=True, slots=True)
class ModelStep:
    """Playback-safe transcript projection of one VenusOmni generation update."""

    visible_text: str
    closed_delegate_queries: tuple[str, ...]
    suppress_audio: bool
    new_token_count: int


@dataclass(frozen=True, slots=True)
class BackendInjection:
    """One independently identified execution feedback ready for VenusOmni ``streaming_prefill``."""

    work_id: str
    text: str
    result: DelegateResult

    @property
    def feedback_id(self):
        return self.result.delivery_id


__all__ = ["BackendInjection", "ModelStep"]
