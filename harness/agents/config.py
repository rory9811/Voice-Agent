"""General has its own runtime budget and Codex configuration."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import validate_model_options
from .vision import VisionConfig


@dataclass(frozen=True, slots=True)
class GeneralAgentConfig:
    command: tuple[str, ...] = ("codex", "app-server")
    workspace: str = "./general-workspace"
    model: str | None = None
    effort: str | None = None
    approval_policy: str = "never"
    sandbox: str = "workspace-write"
    network_access: bool = True
    vision: VisionConfig = field(default_factory=VisionConfig)
    control_timeout_s: float = 30
    execution_timeout_s: float = 1800
    interrupt_timeout_s: float = 10
    result_ttl_s: float = 300
    queue_timeout_s: float = 300

    def __post_init__(self):
        validate_model_options(self.model, self.effort, allow_inherit=True)
        if (
            isinstance(self.command, str)
            or not self.command
            or not all(isinstance(part, str) and part.strip() for part in self.command)
            or self.approval_policy != "never"
        ):
            raise ValueError("invalid General command or approval policy")
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise ValueError("General sandbox must be read-only or workspace-write")
        if type(self.network_access) is not bool:
            raise TypeError("network_access must be boolean")
        for name in (
            "control_timeout_s",
            "execution_timeout_s",
            "interrupt_timeout_s",
            "result_ttl_s",
            "queue_timeout_s",
        ):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            command=(os.getenv("GENERAL_CODEX_BINARY", "codex"), "app-server"),
            workspace=str(
                Path(os.getenv("GENERAL_WORKSPACE", "./general-workspace"))
                .expanduser()
                .resolve()
            ),
            model=os.getenv("GENERAL_CODEX_MODEL") or None,
            effort=os.getenv("GENERAL_CODEX_EFFORT") or None,
            approval_policy=os.getenv("GENERAL_APPROVAL_POLICY", "never"),
            sandbox=os.getenv("GENERAL_SANDBOX", "workspace-write"),
            execution_timeout_s=float(os.getenv("GENERAL_EXECUTION_TIMEOUT_S", "1800")),
            result_ttl_s=float(os.getenv("GENERAL_RESULT_TTL_S", "300")),
        )
