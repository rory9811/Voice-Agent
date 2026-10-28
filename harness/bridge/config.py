"""Opinionated defaults for the fine-tuned Realtime-Venus-Omni frontend."""

from __future__ import annotations

import os
from dataclasses import replace

from harness.core.config import (
    DelegateConfig,
    HarnessConfig,
    ResultManagementConfig,
)
from harness.core.models import (
    DelegateOperation,
    InputMode,
    ResponseMode,
)


def venus_harness_config(
    base: HarnessConfig | None = None,
    *,
    default_backend: str = "agent",
    routing_locked: bool = True,
    allow_web_search: bool = False,
    input_mode: InputMode = InputMode.TEXT_MULTIMODAL,
    operation: DelegateOperation = DelegateOperation.ANSWER,
    response_mode: ResponseMode = ResponseMode.TWO_STAGE,
    result_gate_enabled: bool = True,
) -> HarnessConfig:
    """Return safe defaults for a model that already emits delegate intent.

    Realtime-Venus-Omni is the foreground dialogue model. ``AgentDelegateBackend`` performs one model
    call to select direct multimodal, General Agent, or a registered Skill.
    The wire-level mode stays locked to multimodal/answer/two-stage so every
    execution result is polished before it is returned to the frontend.
    """

    source = base or HarnessConfig(language=os.getenv("HARNESS_LANGUAGE", "zh"))
    delegate: DelegateConfig = replace(
        source.delegate,
        default_routing_locked=routing_locked,
        default_allow_web_search=allow_web_search,
        default_input_mode=input_mode,
        default_operation=operation,
        default_response_mode=response_mode,
    )
    result_management: ResultManagementConfig = replace(
        source.result_management,
        enabled=result_gate_enabled,
    )
    # Preserve explicit search intent when routing through the task executor.
    backend_search_enabled = dict(source.backend_search_enabled)
    backend_search_enabled[default_backend] = True
    return replace(
        source,
        default_backend=default_backend,
        delegate=delegate,
        result_management=result_management,
        backend_search_enabled=backend_search_enabled,
    )


__all__ = ["venus_harness_config"]
