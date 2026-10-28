from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from ..config import Settings, validate_routing_mode
from ..prompts import CAPABILITY_ROUTER_INSTRUCTIONS
from ..skills import SkillManifest
from .backends import PlannerBackend, create_planner_backend


class PlannerClient:
    """Backend-neutral planner for core capabilities and extension skills."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend: PlannerBackend | None = None,
        router_backend: PlannerBackend | None = None,
        routing_mode: str = "auto",
    ) -> None:
        validate_routing_mode(routing_mode)
        self._routing_mode = routing_mode
        self._backend = backend or create_planner_backend(settings)
        self._router_backend = router_backend or self._backend

    async def route_capability(
        self,
        objective: str,
        available_capabilities: list[dict[str, Any]],
        *,
        evidence: dict[str, Any] | None = None,
        session_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch locally to General, or make one model-based Auto decision."""

        if self._routing_mode == "general":
            if not any(item.get("name") == "general" for item in available_capabilities):
                raise ValueError("General capability is not registered")
            # Preserve available visual evidence without a model call to decide
            # whether it is relevant. Text-only tasks must not be marked as
            # missing visual evidence by prepare_visual/constrain_visual_result.
            media = evidence or {}
            has_visual = bool(
                media.get("video_frames")
                or media.get("video_segments")
                or any(
                    item.get("mime_type", "").startswith("image/")
                    for item in media.get("input_files", [])
                )
            )
            return {
                "capability": "general",
                "arguments": {
                    "input_mode": "text_multimodal" if has_visual else "text"
                },
            }

        started = time.monotonic()
        try:
            content = await self._router_backend.complete(
                CAPABILITY_ROUTER_INSTRUCTIONS,
                {
                    "objective": objective,
                    "available_capabilities": available_capabilities,
                    "evidence": evidence or {},
                    "session_context": session_context or {},
                },
            )
        finally:
            logging.getLogger(__name__).info(
                "Capability routing call took %.2f seconds", time.monotonic() - started
            )
        if not content:
            raise ValueError("能力路由模型返回了空结果")
        result = _decode_json_object(content)
        capability = result.get("capability")
        arguments = result.get("arguments", {})
        names = [str(item.get("name", "")) for item in available_capabilities]
        if capability not in names:
            raise ValueError("能力路由模型返回了未注册能力")
        if not isinstance(arguments, dict):
            raise TypeError("能力路由模型的 arguments 必须是 object")
        decision = {"capability": capability, "arguments": dict(arguments)}
        parent = result.get("continuation_of")
        if parent is not None:
            if (
                capability != "general"
                or not isinstance(parent, str)
                or not parent.strip()
            ):
                raise ValueError("continuation_of must identify a General work")
            decision["continuation_of"] = parent
        return decision

    async def plan_skill(
        self,
        manifest: SkillManifest,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a registered Skill's planner contract without core changes."""
        return await self.plan_contract(manifest.planner_instructions, context)

    async def plan_contract(
        self,
        instructions: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a domain-neutral planner contract supplied by the caller."""
        return await self._complete(instructions, context)

    async def _complete(
        self, instructions: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        content = await self._backend.complete(instructions, context)
        if not content:
            raise ValueError("规划模型返回了空结果")
        result = _decode_json_object(content)
        if not isinstance(result, dict) or "speech" not in result:
            raise ValueError("规划模型结果缺少 speech")
        return result


def _decode_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        first_line_end = text.find("\n")
        closing_fence = text.rfind("```")
        if first_line_end >= 0 and closing_fence > first_line_end:
            text = text[first_line_end + 1 : closing_fence].strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("规划模型未返回有效 JSON object") from exc
    if not isinstance(result, dict):
        raise TypeError("规划模型未返回 JSON object")
    return result


class PlannerLike(Protocol):
    async def route_capability(
        self,
        objective: str,
        available_capabilities: list[dict[str, Any]],
        *,
        evidence: dict[str, Any] | None = None,
        session_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def plan_contract(
        self,
        instructions: str,
        context: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def plan_skill(
        self,
        manifest: SkillManifest,
        context: dict[str, Any],
    ) -> dict[str, Any]: ...
