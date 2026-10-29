"""Provider selection for three independent model roles; General follows the task agent."""
from harness.config import ModelCallConfig, Settings
from harness.llm.backends import CodexPlannerBackend
from harness.llm.claude import ClaudeDirect, ClaudeModelBackend
from harness.llm.delegate import CodexDirectAndPolish, ConfiguredDirectAndPolish
from harness.llm.planner import PlannerClient


def codex_settings(setup, call):
    return Settings(
        codex_path=setup.general.command[0],
        codex_model=call.model or setup.general.model or "",
        codex_workspace=setup.general.workspace,
        codex_timeout_seconds=call.timeout_s,
        codex_effort=call.effort,
    )


def model_backend(setup, call):
    if call.provider == "gemini":
        from harness.llm.gemini import GeminiBackend
        return GeminiBackend(api_key=setup.gemini.resolved_key(),
                             model=call.model or setup.gemini.model, timeout_s=call.timeout_s)
    if call.provider == "claude":
        return ClaudeModelBackend(executable=setup.general.command[0],
                                  model=call.model or setup.general.model,
                                  effort=call.effort, timeout_s=call.timeout_s)
    return CodexPlannerBackend(codex_settings(setup, call))


def task_backends(setup):
    polish = model_backend(setup, setup.responses)
    multimodal = model_backend(setup, setup.multimodal)
    if setup.multimodal.provider == "gemini":
        from harness.llm.gemini import GeminiDirect
        direct = GeminiDirect(multimodal)
    elif setup.multimodal.provider == "claude":
        direct = ClaudeDirect(multimodal)
    else:
        direct = CodexDirectAndPolish(multimodal)
    # A dormant Gemini router must not require a key or create requests in General mode.
    router = model_backend(setup, setup.routing) if setup.routing.mode == "auto" else polish
    # Skill planning stays on the task agent; changing Polish to Gemini must not switch it.
    agent = setup.general.agent
    skill_call = setup.responses if setup.responses.provider == agent else ModelCallConfig(provider=agent)
    skill = model_backend(setup, skill_call)
    planner = PlannerClient(codex_settings(setup, ModelCallConfig()),
                            backend=skill, router_backend=router, routing_mode=setup.routing.mode)
    return ConfiguredDirectAndPolish(direct, CodexDirectAndPolish(polish)), planner
