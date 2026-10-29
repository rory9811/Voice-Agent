"""Construct a configured Harness without importing the browser demo or model code."""
from .agents import ClaudeAgentProvider, CodexAgentProvider
from .backends import task_backends
from .bridge.runtime import VenusOmniAgentHarness
from .settings import load_setup


def build_harness(setup):
    """Build from HarnessSetup; caller owns the returned agent and must aclose()."""
    direct, planner = task_backends(setup)
    provider = ClaudeAgentProvider if setup.general.agent == "claude" else CodexAgentProvider
    return VenusOmniAgentHarness(
        direct,
        planner=planner,
        general_agent=provider(setup.general),
        general_config=setup.general,
        feedback_config=setup.feedback,
        config=setup.harness,
    )


def load_harness(path):
    """Load a JSON configuration and construct the task runtime (no model weights)."""
    return build_harness(load_setup(path))
