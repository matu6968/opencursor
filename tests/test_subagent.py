from __future__ import annotations

from opencursor import errors
from opencursor._local_executor import resolve_allowed_tools
from opencursor._protobuf import decode_message, encode_message, oneof_case
from opencursor._subagents import (
    TASK_PROTO_TOOL,
    convert_agent_definitions,
    custom_subagents_for_context,
    execute_hook_result,
    nested_tool_names,
    resolve_nested_model,
)
from opencursor.types import AgentDefinition, AgentOptions, ModelSelection


def test_task_maps_to_proto_tool_name() -> None:
    opts = AgentOptions.model_validate({"local": {"cwd": "."}, "tools": ["task"]})
    assert resolve_allowed_tools(opts) == ["task_tool_call"]


def test_task_included_when_agents_configured() -> None:
    opts = AgentOptions.model_validate(
        {
            "local": {"cwd": "."},
            "agents": {"pong": {"description": "replies pong", "prompt": "Say pong."}},
        }
    )
    allowed = resolve_allowed_tools(opts)
    assert TASK_PROTO_TOOL in allowed
    assert "read_tool_call" in allowed


def test_task_omitted_from_default_without_agents() -> None:
    opts = AgentOptions.model_validate({"local": {"cwd": "."}})
    assert TASK_PROTO_TOOL not in resolve_allowed_tools(opts)


def test_nested_tools_strip_task() -> None:
    assert nested_tool_names(["read_tool_call", "task_tool_call", "ls_tool_call"]) == [
        "read_tool_call",
        "ls_tool_call",
    ]


def test_convert_agents_inherit_and_model_id() -> None:
    agents = {
        "inherit_me": AgentDefinition(description="d", prompt="p"),
        "composer": AgentDefinition(
            description="d2", prompt="p2", model=ModelSelection(id="composer-2.5")
        ),
    }
    converted = convert_agent_definitions(agents)
    by_name = {item["name"]: item for item in converted}
    assert by_name["inherit_me"]["model"] == "inherit"
    assert by_name["composer"]["model"] == "composer-2.5"
    assert by_name["inherit_me"]["prompt"] == "p"


def test_convert_agents_rejects_inline_mcp() -> None:
    try:
        convert_agent_definitions(
            {
                "bad": {
                    "description": "d",
                    "prompt": "p",
                    "mcpServers": [{"type": "stdio", "command": "echo"}],
                }
            }
        )
    except errors.ConfigurationError as exc:
        assert "inline McpServerConfig" in str(exc)
        return
    raise AssertionError("expected ConfigurationError")


def test_string_mcp_refs_are_allowed() -> None:
    converted = convert_agent_definitions(
        {"ok": {"description": "d", "prompt": "p", "mcpServers": ["echo"]}}
    )
    assert converted[0]["name"] == "ok"


def test_custom_subagents_on_request_context() -> None:
    opts = AgentOptions.model_validate(
        {
            "local": {"cwd": "."},
            "agents": {"researcher": {"description": "looks things up", "prompt": "Be brief."}},
        }
    )
    custom = custom_subagents_for_context(opts)
    raw = encode_message("agent.v1.RequestContext", {"custom_subagents": custom})
    decoded = decode_message("agent.v1.RequestContext", raw)
    assert decoded["custom_subagents"][0]["name"] == "researcher"
    assert decoded["custom_subagents"][0]["prompt"] == "Be brief."
    assert decoded["custom_subagents"][0]["model"] == "inherit"


def test_subagent_args_roundtrip() -> None:
    msg = {
        "id": 4,
        "exec_id": "e-sub",
        "subagent_args": {
            "tool_call_id": "t1",
            "subagent_type": "pong",
            "model_id": "composer-2.5",
            "prompt": "say pong",
        },
    }
    raw = encode_message("agent.v1.ExecServerMessage", msg)
    decoded = decode_message("agent.v1.ExecServerMessage", raw)
    assert oneof_case(decoded, "message") == "subagent_args"
    assert decoded["subagent_args"]["subagent_type"] == "pong"
    assert decoded["subagent_args"]["prompt"] == "say pong"


def test_subagent_result_roundtrip() -> None:
    msg = {
        "id": 4,
        "exec_id": "e-sub",
        "subagent_result": {
            "success": {
                "agent_id": "child-1",
                "final_message": "pong",
                "tool_call_count": 0,
            }
        },
    }
    raw = encode_message("agent.v1.ExecClientMessage", msg)
    decoded = decode_message("agent.v1.ExecClientMessage", raw)
    assert oneof_case(decoded, "message") == "subagent_result"
    assert decoded["subagent_result"]["success"]["final_message"] == "pong"


def test_subagent_await_and_force_background_roundtrip() -> None:
    await_msg = {
        "id": 5,
        "exec_id": "e-await",
        "subagent_await_result": {"still_running": {"agent_id": "child-1"}},
    }
    decoded = decode_message(
        "agent.v1.ExecClientMessage", encode_message("agent.v1.ExecClientMessage", await_msg)
    )
    assert oneof_case(decoded["subagent_await_result"], "result") == "still_running"
    force = {
        "id": 6,
        "exec_id": "e-bg",
        "force_background_subagent_result": {
            "status": "FORCE_BACKGROUND_SUBAGENT_STATUS_ACCEPTED"
        },
    }
    decoded_force = decode_message(
        "agent.v1.ExecClientMessage", encode_message("agent.v1.ExecClientMessage", force)
    )
    assert decoded_force["force_background_subagent_result"]["status"] == 1


def test_execute_hook_subagent_start() -> None:
    result = execute_hook_result({"request": {"subagent_start": {"subagent_type": "pong"}}})
    raw = encode_message("agent.v1.ExecuteHookResult", result)
    decoded = decode_message("agent.v1.ExecuteHookResult", raw)
    assert oneof_case(decoded["response"], "response") == "subagent_start"


def test_nested_model_prefers_args_then_definition() -> None:
    parent = ModelSelection(id="composer-2")
    assert resolve_nested_model(parent, {"model_id": "composer-2.5"}, None).id == "composer-2.5"
    assert (
        resolve_nested_model(parent, {}, {"name": "x", "model": "composer-2.5"}).id == "composer-2.5"
    )
    assert resolve_nested_model(parent, {}, {"name": "x", "model": "inherit"}).id == "composer-2"
