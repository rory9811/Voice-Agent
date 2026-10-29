"""Full tool-using agents for the General execution branch."""

from .claude import ClaudeAgentProvider
from .codex import CodexAgentProvider
from .config import GeneralAgentConfig
from .contracts import (
    AgentEvent,
    AgentImage,
    AgentInput,
    AgentRequest,
    AgentResult,
    GeneralAgentPort,
    WorkArtifact,
)

__all__ = [
    "AgentEvent",
    "AgentImage",
    "AgentInput",
    "AgentRequest",
    "AgentResult",
    "ClaudeAgentProvider",
    "CodexAgentProvider",
    "GeneralAgentConfig",
    "GeneralAgentPort",
    "WorkArtifact",
]
