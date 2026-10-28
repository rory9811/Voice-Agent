"""Model-call settings for Codex and optional official Gemini roles."""

import math
import os
import re
from dataclasses import dataclass, field


REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})
ROUTING_MODES = frozenset({"general", "auto"})


def validate_routing_mode(mode):
    if not isinstance(mode, str) or mode not in ROUTING_MODES:
        raise ValueError("Routing mode must be general or auto")


def validate_model_options(model, effort, *, allow_inherit=False):
    if model is not None and not isinstance(model, str):
        raise ValueError("Model must be a name or null")
    if allow_inherit and effort is None:
        return
    if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
        raise ValueError("Invalid reasoning effort")


@dataclass(frozen=True, slots=True)
class ModelCallConfig:
    """Independent budgets for routing and response preparation."""

    model: str | None = None
    effort: str = "low"
    timeout_s: float = 180.0
    provider: str = "codex"

    def __post_init__(self):
        validate_model_options(self.model, self.effort)
        if self.provider not in {"codex", "gemini"}:
            raise ValueError("Provider must be codex or gemini")
        if self.provider == "gemini" and self.model:
            validate_gemini_model(self.model)
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not math.isfinite(self.timeout_s)
            or self.timeout_s <= 0
        ):
            raise ValueError("Model call timeout must be finite and positive")


@dataclass(frozen=True, slots=True)
class RoutingConfig(ModelCallConfig):
    """Browser tasks go straight to General unless Auto is explicitly selected."""

    timeout_s: float = 30.0
    mode: str = "general"

    def __post_init__(self):
        ModelCallConfig.__post_init__(self)
        validate_routing_mode(self.mode)


def validate_gemini_model(model):
    if not isinstance(model, str) or not re.fullmatch(r"gemini-[A-Za-z0-9._-]+", model):
        raise ValueError("Use an official Gemini model ID, not a URL or proxy model")


@dataclass(frozen=True, slots=True)
class GeminiConfig:
    model: str = "gemini-flash-latest"
    api_key: str = field(default="", repr=False)

    def __post_init__(self):
        validate_gemini_model(self.model)
        if not isinstance(self.api_key, str) or any(c.isspace() for c in self.api_key):
            raise ValueError("Invalid Gemini API key")

    def resolved_key(self):
        return self.api_key or os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")


@dataclass(frozen=True, slots=True)
class Settings:
    planner_backend: str = "codex"
    codex_path: str = "codex"
    codex_model: str = ""
    codex_workspace: str = ""
    codex_timeout_seconds: float = 180.0
    codex_effort: str = "low"

    def __post_init__(self):
        ModelCallConfig(self.codex_model, self.codex_effort, self.codex_timeout_seconds)
        if self.planner_backend != "codex":
            raise ValueError("Invalid Codex backend configuration")

    @classmethod
    def from_env(cls):
        return cls(
            codex_path=os.getenv("GENERAL_CODEX_BINARY", "codex"),
            codex_model=os.getenv("GENERAL_CODEX_MODEL", ""),
            codex_workspace=os.getenv("GENERAL_WORKSPACE", ""),
            codex_timeout_seconds=float(os.getenv("CODEX_TIMEOUT_SECONDS", "180")),
            codex_effort=os.getenv("GENERAL_CODEX_EFFORT") or "low",
        )
