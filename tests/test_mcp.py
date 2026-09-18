from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from opencursor._local_executor import MCP_PROTO_TOOLS, resolve_allowed_tools
from opencursor._mcp import McpManager
from opencursor._protobuf import decode_message, encode_message, oneof_case
from opencursor.types import AgentOptions

ECHO_SERVER = Path(__file__).resolve().parent / "fixtures" / "echo_mcp_server.py"


def _echo_options() -> AgentOptions:
    return AgentOptions.model_validate(
        {
            "local": {"cwd": str(ECHO_SERVER.parent)},
            "tools": ["mcp"],
            "mcpServers": {
                "echo": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [str(ECHO_SERVER)],
                }
            },
        }
    )


def test_mcp_maps_to_proto_tool_names() -> None:
    opts = AgentOptions.model_validate({"local": {"cwd": "."}, "tools": ["mcp"]})
    assert resolve_allowed_tools(opts) == list(MCP_PROTO_TOOLS)


def test_mcp_tools_included_when_servers_configured() -> None:
    opts = AgentOptions.model_validate(
        {
            "local": {"cwd": "."},
            "mcpServers": {"echo": {"type": "stdio", "command": "python3", "args": ["-m", "echo"]}},
        }
    )
    allowed = resolve_allowed_tools(opts)
    for name in MCP_PROTO_TOOLS:
        assert name in allowed


def test_protobuf_value_json_roundtrip() -> None:
    payload = {"hello": "world", "n": 3, "ok": True, "nested": [1, None, {"x": "y"}]}
    raw = encode_message("google.protobuf.Value", payload)
    assert decode_message("google.protobuf.Value", raw) == payload


def test_mcp_args_roundtrip() -> None:
    msg = {
        "id": 8,
        "exec_id": "e-mcp",
        "mcp_args": {
            "name": "echo-echo",
            "provider_identifier": "echo",
            "tool_name": "echo",
            "tool_call_id": "t1",
            "args": {"text": "hi"},
        },
    }
    raw = encode_message("agent.v1.ExecServerMessage", msg)
    decoded = decode_message("agent.v1.ExecServerMessage", raw)
    assert oneof_case(decoded, "message") == "mcp_args"
    assert decoded["mcp_args"]["args"]["text"] == "hi"
    assert decoded["mcp_args"]["provider_identifier"] == "echo"


def test_mcp_stdio_echo_tool_and_resource() -> None:
    asyncio.run(_test_mcp_stdio_echo_tool_and_resource())


async def _test_mcp_stdio_echo_tool_and_resource() -> None:
    manager = McpManager()
    try:
        await manager.start(_echo_options(), ECHO_SERVER.parent)
        defs = manager.tool_definitions()
        assert defs[0]["tool_name"] == "echo"
        assert defs[0]["provider_identifier"] == "echo"
        called = await manager.call_tool(
            {"provider_identifier": "echo", "tool_name": "echo", "args": {"text": "hello-mcp"}}
        )
        assert called["success"]["content"][0]["text"]["text"] == "echo:hello-mcp"
        listed = await manager.list_resources({})
        assert listed["success"]["resources"][0]["uri"] == "echo://hello"
        read = await manager.read_resource({"server": "echo", "uri": "echo://hello"})
        assert read["success"]["text"] == "hello from echo"
        state = manager.state_servers([])
        assert state[0]["status"] == "connected"
    finally:
        await manager.aclose()
