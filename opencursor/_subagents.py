from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from opencursor import errors
from opencursor._protobuf import oneof_case
from opencursor.types import AgentDefinition, AgentOptions, ModelSelection

TASK_PROTO_TOOL = "task_tool_call"

HOOK_REQUEST_CASES = (
    "pre_compact",
    "subagent_start",
    "subagent_stop",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "before_submit_prompt",
    "after_agent_response",
    "after_agent_thought",
    "stop",
)


class DiscardStore:
    async def append_sdk_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mark_run_terminal(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def append_terminal_event(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass
class SubagentJob:
    agent_id: str
    tool_call_id: str
    future: asyncio.Future
    force_background: asyncio.Event = field(default_factory=asyncio.Event)


class SubagentRegistry:
    def __init__(self) -> None:
        self._by_agent: dict[str, SubagentJob] = {}
        self._by_tool: dict[str, SubagentJob] = {}

    def register(self, job: SubagentJob) -> None:
        self._by_agent[job.agent_id] = job
        if job.tool_call_id:
            self._by_tool[job.tool_call_id] = job

    def get_agent(self, agent_id: str) -> SubagentJob | None:
        return self._by_agent.get(agent_id)

    def get_tool(self, tool_call_id: str) -> SubagentJob | None:
        return self._by_tool.get(tool_call_id)


def agents_configured(options: AgentOptions) -> bool:
    agents = getattr(options, "agents", None) or {}
    return bool(agents)


def nested_tool_names(parent_allowed: list[str] | None) -> list[str]:
    return [name for name in (parent_allowed or []) if name != TASK_PROTO_TOOL]


def convert_agent_definitions(
    agents: dict[str, AgentDefinition] | dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not agents:
        return []
    out: list[dict[str, Any]] = []
    for name, spec in agents.items():
        raw = spec.model_dump(by_alias=True) if isinstance(spec, AgentDefinition) else dict(spec)
        mcp = raw.get("mcpServers")
        if mcp is not None:
            items = mcp if isinstance(mcp, list) else [mcp]
            for item in items:
                if not isinstance(item, str):
                    raise errors.ConfigurationError(
                        f'Custom subagent "{name}" has an inline McpServerConfig in mcpServers; '
                        "SDK custom subagents only support string references in v1.",
                        is_retryable=False,
                    )
        model = raw.get("model")
        if model is None or model == "inherit":
            mid = "inherit"
        elif isinstance(model, dict) and model.get("id"):
            mid = str(model["id"])
        elif hasattr(model, "id"):
            mid = str(model.id)
        else:
            mid = "inherit"
        out.append(
            {
                "name": name,
                "description": str(raw.get("description") or ""),
                "prompt": str(raw.get("prompt") or ""),
                "model": mid,
            }
        )
    return out


def custom_subagents_for_context(options: AgentOptions) -> list[dict[str, Any]]:
    return convert_agent_definitions(getattr(options, "agents", None))


def definition_for_type(
    definitions: list[dict[str, Any]], subagent_type: str
) -> dict[str, Any] | None:
    for item in definitions:
        if item.get("name") == subagent_type:
            return item
    return None


def resolve_nested_model(
    parent: ModelSelection,
    args: dict[str, Any],
    definition: dict[str, Any] | None,
) -> ModelSelection:
    mid = str(args.get("model_id") or "").strip()
    if mid:
        return ModelSelection(id=mid)
    if definition:
        model = definition.get("model") or "inherit"
        if model and model != "inherit":
            return ModelSelection(id=str(model))
    return parent


def execute_hook_result(args: dict[str, Any]) -> dict[str, Any]:
    request = args.get("request") or {}
    case = oneof_case(request, "request")
    if not case:
        for name in HOOK_REQUEST_CASES:
            if name in request:
                case = name
                break
    if not case:
        return {"response": {}}
    return {"response": {case: {}}}
