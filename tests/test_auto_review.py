from __future__ import annotations

import asyncio
from typing import Any

from opencursor._custom_tools import CUSTOM_USER_TOOLS_PROVIDER
from opencursor._local_executor import (
    DEV_FORCE_SMART_MODE_CLASSIFIER_BLOCK_TOKEN_ENV,
    LocalAgentExecutor,
    _auto_review_enabled,
    interactive_approval_denied_reason,
)
from opencursor._protobuf import decode_message, encode_message, oneof_case
from opencursor._subagents import DiscardStore
from opencursor.types import AgentOptions, ModelSelection


class _FakeSession:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def aclose(self) -> None:
        return None


def _executor(*, auto_review: bool = False, custom_tools: dict[str, Any] | None = None) -> LocalAgentExecutor:
    local: dict[str, Any] = {"cwd": "."}
    if auto_review:
        local["autoReview"] = True
    if custom_tools is not None:
        local["customTools"] = custom_tools
    executor = LocalAgentExecutor(
        store=DiscardStore(),
        agent_id="agent-1",
        run_id="run-1",
        options=AgentOptions.model_validate({"local": local, "model": {"id": "composer-2"}}),
        model=ModelSelection(id="composer-2"),
        prompt="hello",
        conversation_id="conv-1",
        request_id="gen-1",
        nested=True,
    )
    executor._generation_active = True
    executor._run_session = _FakeSession()
    executor._session_ready.set()
    return executor


def _last_exec(executor: LocalAgentExecutor) -> dict[str, Any]:
    session = executor._run_session
    assert isinstance(session, _FakeSession)
    decoded = decode_message("agent.v1.AgentClientMessage", session.sent[-1])
    assert oneof_case(decoded, "message") == "exec_client_message"
    return decoded["exec_client_message"]


def _server_exec(msg: dict[str, Any]) -> dict[str, Any]:
    return decode_message(
        "agent.v1.ExecServerMessage", encode_message("agent.v1.ExecServerMessage", msg)
    )


def test_auto_review_option_is_strict_true() -> None:
    assert _auto_review_enabled(AgentOptions()) is False
    assert _auto_review_enabled(AgentOptions.model_validate({"local": {"cwd": "."}})) is False
    assert _auto_review_enabled(AgentOptions.model_validate({"local": {"autoReview": False}})) is False
    assert _auto_review_enabled(AgentOptions.model_validate({"local": {"autoReview": True}})) is True


def test_request_context_advertises_classifier_auto_mode() -> None:
    off = _executor(auto_review=False)._request_context()["env"]
    on = _executor(auto_review=True)._request_context()["env"]
    assert off["smart_mode_classifier_auto_mode_enabled"] is False
    assert on["smart_mode_classifier_auto_mode_enabled"] is True
    assert "dev_force_next_smart_mode_classifier_block_token" not in off
    assert "dev_force_next_smart_mode_classifier_block_token" not in on


def test_request_context_forwards_dev_force_block_token(monkeypatch) -> None:
    monkeypatch.setenv(DEV_FORCE_SMART_MODE_CLASSIFIER_BLOCK_TOKEN_ENV, "block-token-1")
    env = _executor(auto_review=True)._request_context()["env"]
    assert env["dev_force_next_smart_mode_classifier_block_token"] == "block-token-1"
    raw = encode_message("agent.v1.RequestContextEnv", env)
    decoded = decode_message("agent.v1.RequestContextEnv", raw)
    assert decoded["smart_mode_classifier_auto_mode_enabled"] is True
    assert decoded["dev_force_next_smart_mode_classifier_block_token"] == "block-token-1"


def test_smart_mode_classifier_args_roundtrip() -> None:
    msg = {
        "id": 9,
        "exec_id": "e-cls",
        "smart_mode_classifier_args": {
            "tool_call_id": "t1",
            "target": {"action": "Shell", "arguments": {"command": "ls"}},
            "conversation_context": [{"role": "user", "content": "list files"}],
        },
    }
    decoded = decode_message(
        "agent.v1.ExecServerMessage", encode_message("agent.v1.ExecServerMessage", msg)
    )
    assert oneof_case(decoded, "message") == "smart_mode_classifier_args"
    assert decoded["smart_mode_classifier_args"]["tool_call_id"] == "t1"
    assert decoded["smart_mode_classifier_args"]["target"]["action"] == "Shell"
    assert decoded["smart_mode_classifier_args"]["target"]["arguments"]["command"] == "ls"


def test_smart_mode_classifier_result_roundtrip() -> None:
    allow = {
        "id": 9,
        "exec_id": "e-cls",
        "smart_mode_classifier_result": {
            "success": {"decision": "SMART_MODE_CLASSIFIER_DECISION_ALLOW"}
        },
    }
    decoded = decode_message(
        "agent.v1.ExecClientMessage", encode_message("agent.v1.ExecClientMessage", allow)
    )
    assert oneof_case(decoded, "message") == "smart_mode_classifier_result"
    assert decoded["smart_mode_classifier_result"]["success"]["decision"] == 1
    block = {
        "id": 9,
        "exec_id": "e-cls",
        "smart_mode_classifier_result": {
            "error": {"error": "No handler found for server message of type smartModeClassifierArgs"}
        },
    }
    decoded = decode_message(
        "agent.v1.ExecClientMessage", encode_message("agent.v1.ExecClientMessage", block)
    )
    assert oneof_case(decoded["smart_mode_classifier_result"], "result") == "error"


def test_classifier_exec_has_no_local_model() -> None:
    asyncio.run(_test_classifier_exec_has_no_local_model())


async def _test_classifier_exec_has_no_local_model() -> None:
    executor = _executor(auto_review=True)
    await executor._on_exec(
        _server_exec(
            {
                "id": 9,
                "exec_id": "e-cls",
                "smart_mode_classifier_args": {
                    "tool_call_id": "t1",
                    "target": {"action": "Mcp", "arguments": {}},
                },
            }
        )
    )
    result = _last_exec(executor)
    assert oneof_case(result, "message") == "smart_mode_classifier_result"
    assert "error" in result["smart_mode_classifier_result"]
    assert "No handler found" in result["smart_mode_classifier_result"]["error"]["error"]


def test_mcp_smart_mode_approval_fail_closed() -> None:
    asyncio.run(_test_mcp_smart_mode_approval_fail_closed())


async def _test_mcp_smart_mode_approval_fail_closed() -> None:
    executor = _executor(auto_review=True)
    await executor._on_exec(
        _server_exec(
            {
                "id": 2,
                "exec_id": "e-mcp",
                "mcp_args": {
                    "name": "echo-echo",
                    "provider_identifier": "echo",
                    "tool_name": "echo",
                    "tool_call_id": "t1",
                    "smart_mode_approval": {"request_id": "r1", "reason": "blocked"},
                },
            }
        )
    )
    result = _last_exec(executor)
    rejected = result["mcp_result"]["rejected"]
    assert "User rejected MCP: echo-echo" in rejected["reason"]
    assert interactive_approval_denied_reason("mcp") in rejected["reason"]


def test_mcp_skip_approval_still_runs() -> None:
    asyncio.run(_test_mcp_skip_approval_still_runs())


async def _test_mcp_skip_approval_still_runs() -> None:
    executor = _executor(auto_review=True)
    await executor._on_exec(
        _server_exec(
            {
                "id": 2,
                "exec_id": "e-mcp",
                "mcp_args": {
                    "name": "echo-echo",
                    "provider_identifier": "echo",
                    "tool_name": "echo",
                    "tool_call_id": "t1",
                    "skip_approval": True,
                    "smart_mode_approval": {"request_id": "r1", "reason": "blocked"},
                },
            }
        )
    )
    result = _last_exec(executor)
    assert "rejected" not in result["mcp_result"]
    assert "server_not_found" in result["mcp_result"] or "error" in result["mcp_result"]


def test_custom_tools_run_under_auto_review() -> None:
    asyncio.run(_test_custom_tools_run_under_auto_review())


async def _test_custom_tools_run_under_auto_review() -> None:
    executor = _executor(
        auto_review=True,
        custom_tools={"ping": {"description": "ping", "execute": lambda args, ctx: "pong"}},
    )
    await executor._on_exec(
        _server_exec(
            {
                "id": 3,
                "exec_id": "e-custom",
                "mcp_args": {
                    "name": f"{CUSTOM_USER_TOOLS_PROVIDER}-ping",
                    "provider_identifier": CUSTOM_USER_TOOLS_PROVIDER,
                    "tool_name": "ping",
                    "tool_call_id": "t1",
                    "smart_mode_approval": {"request_id": "r1", "reason": "blocked"},
                },
            }
        )
    )
    result = _last_exec(executor)
    assert result["mcp_result"]["success"]["content"][0]["text"]["text"] == "pong"


def test_shell_smart_mode_approval_fail_closed() -> None:
    asyncio.run(_test_shell_smart_mode_approval_fail_closed())


async def _test_shell_smart_mode_approval_fail_closed() -> None:
    executor = _executor(auto_review=True)
    await executor._on_exec(
        _server_exec(
            {
                "id": 4,
                "exec_id": "e-shell",
                "shell_args": {
                    "command": "echo hi",
                    "working_directory": ".",
                    "tool_call_id": "t1",
                    "smart_mode_approval": {"request_id": "r1", "reason": "blocked"},
                },
            }
        )
    )
    result = _last_exec(executor)
    rejected = result["shell_result"]["rejected"]
    assert rejected["command"] == "echo hi"
    assert rejected["reason"] == interactive_approval_denied_reason("shell")


def test_fetch_mcp_resource_requires_approval_provider() -> None:
    asyncio.run(_test_fetch_mcp_resource_requires_approval_provider())


async def _test_fetch_mcp_resource_requires_approval_provider() -> None:
    off = _executor(auto_review=False)
    await off._on_exec(
        _server_exec(
            {
                "id": 5,
                "exec_id": "e-res",
                "read_mcp_resource_exec_args": {
                    "server": "echo",
                    "uri": "echo://n",
                    "tool_call_id": "t1",
                    "smart_mode_approval": {"request_id": "r1", "reason": "blocked"},
                },
            }
        )
    )
    err = _last_exec(off)["read_mcp_resource_exec_result"]["error"]
    assert err["error"] == "Auto-review approval provider is not configured"

    on = _executor(auto_review=True)
    await on._on_exec(
        _server_exec(
            {
                "id": 6,
                "exec_id": "e-res2",
                "read_mcp_resource_exec_args": {
                    "server": "echo",
                    "uri": "echo://n",
                    "tool_call_id": "t1",
                    "smart_mode_approval": {"request_id": "r1", "reason": "blocked"},
                },
            }
        )
    )
    rejected = _last_exec(on)["read_mcp_resource_exec_result"]["rejected"]
    assert "User rejected MCP resource fetch" in rejected["reason"]
    assert interactive_approval_denied_reason("mcp") in rejected["reason"]
