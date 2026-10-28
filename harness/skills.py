"""Task capability interfaces, argument validation and registration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SkillManifest:
    name: str
    version: str
    description: str
    capabilities: frozenset[str]
    planner_instructions: str
    routing_schema: dict[str, Any] | None = None


class AgentSkill(Protocol):
    name: str
    capabilities: frozenset[str]
    manifest: SkillManifest | None

    async def plan(
        self,
        planner: Any,
        arguments: dict[str, Any],
        *,
        played_reply_context: list[str] | None = None,
        session_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def validate_routing_arguments(
    arguments: dict[str, Any], schema: dict[str, Any]
) -> None:
    """Validate the JSON-Schema subset used by routing contracts."""

    _validate_value(arguments, schema, "arguments")


def _validate_value(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise TypeError(f"{path} 必须是 object")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{path} 缺少必填字段: {', '.join(missing)}")
        unknown = set(value) - set(properties)
        if unknown and schema.get("additionalProperties", True) is False:
            raise ValueError(f"{path} 包含未知字段: {', '.join(sorted(unknown))}")
        for name, item in value.items():
            child = properties.get(name)
            if isinstance(child, dict):
                _validate_value(item, child, f"{path}.{name}")
    elif expected == "array":
        if not isinstance(value, list):
            raise TypeError(f"{path} 必须是 array")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{path}[{index}]")
    elif expected == "string" and not isinstance(value, str):
        raise TypeError(f"{path} 必须是 string")
    elif expected == "boolean" and not isinstance(value, bool):
        raise TypeError(f"{path} 必须是 boolean")
    elif expected == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise TypeError(f"{path} 必须是 integer")
    elif expected == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise TypeError(f"{path} 必须是 number")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} 不在允许的枚举值中")


class SkillRegistry:
    """Resolve skills by capability without coupling runtime to domains."""

    def __init__(self, skills: list[AgentSkill] | None = None) -> None:
        self._by_capability: dict[str, AgentSkill] = {}
        self._manifests: dict[str, SkillManifest] = {}
        for skill in skills or []:
            self.register(skill)

    def register(self, skill: AgentSkill) -> None:
        manifest = getattr(skill, "manifest", None)
        if manifest is not None:
            if manifest.name != skill.name:
                raise ValueError(
                    f"Skill Manifest 名称不匹配: {manifest.name} != {skill.name}"
                )
            if manifest.capabilities != skill.capabilities:
                raise ValueError(f"Skill Manifest 能力声明不匹配: {manifest.name}")
            if manifest.name in self._manifests:
                raise ValueError(f"Skill Manifest 已注册: {manifest.name}")
        for capability in skill.capabilities:
            if capability in self._by_capability:
                raise ValueError(f"能力已注册: {capability}")
        if manifest is not None:
            self._manifests[manifest.name] = manifest
        for capability in skill.capabilities:
            self._by_capability[capability] = skill

    def for_capability(self, capability: str) -> AgentSkill | None:
        return self._by_capability.get(capability)

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Return registered execution capabilities in stable prompt order."""

        return tuple(sorted(self._by_capability))

    @property
    def routing_contracts(self) -> tuple[dict[str, object], ...]:
        """Describe each registered capability for model routing."""

        contracts = []
        for capability in self.capabilities:
            skill = self._by_capability[capability]
            manifest = getattr(skill, "manifest", None)
            description = (
                manifest.description
                if manifest is not None
                else getattr(skill, "routing_description", "")
            )
            schema = (
                manifest.routing_schema
                if manifest is not None and manifest.routing_schema is not None
                else getattr(skill, "routing_schema", None)
            )
            contracts.append(
                {
                    "name": capability,
                    "description": str(description or f"注册能力 {capability}"),
                    "arguments_schema": schema
                    or {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            )
        return tuple(contracts)

    def require(self, capability: str) -> AgentSkill:
        skill = self.for_capability(capability)
        if skill is None:
            raise KeyError(f"未注册能力: {capability}")
        return skill

    @property
    def manifests(self) -> tuple[SkillManifest, ...]:
        return tuple(self._manifests.values())
