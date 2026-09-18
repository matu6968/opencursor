"""Compare vendored official `cursor_sdk` public names with `opencursor`.

Official Python talks to a Node bridge (`Client` / `Bridge` / async mirrors).
`opencursor` speaks Connect/protobuf in-process like `@cursor/sdk`, so those
bridge types stay unported on purpose. Everything else is either implemented,
re-exported, or listed in `KNOWN_MISSING_*` so a new official name fails CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import opencursor
from opencursor.agent import Agent, SDKAgent
from opencursor._cloud_agent import CloudAgent, CloudRun
from opencursor._local_runtime import LocalAgent, LocalRun
from opencursor.cursor import Cursor

_OFFICIAL_ROOT = Path(__file__).resolve().parents[1] / "official"
if str(_OFFICIAL_ROOT) not in sys.path:
    sys.path.insert(0, str(_OFFICIAL_ROOT))

import cursor_sdk  # noqa: E402
from cursor_sdk import Agent as OfficialAgent  # noqa: E402
from cursor_sdk import Cursor as OfficialCursor  # noqa: E402
from cursor_sdk import Run as OfficialRun  # noqa: E402

# Node bridge / default-client machinery. Not part of the native Python port.
INTENTIONALLY_UNPORTED = frozenset(
    {
        "AsyncAgent",
        "AsyncBridge",
        "AsyncClient",
        "AsyncConnectTransport",
        "AsyncCursor",
        "AsyncCursorClient",
        "AsyncListResult",
        "AsyncRun",
        "Bridge",
        "BridgeEndpoint",
        "Client",
        "CursorClient",
        "DefaultAsyncHttpxClient",
        "DefaultHttpxClient",
        "LocalAgentStoreHandler",
        "StoreAgentsHandler",
        "StoreCallbackServer",
        "StoreCheckpointsHandler",
        "StoreRunEventsHandler",
        "StoreRunsHandler",
        "ToolCallbackServer",
        "close_default_client",
    }
)

# Official Python uses one sync `Run`; opencursor splits cloud vs local handles.
INTENTIONALLY_SPLIT = frozenset({"Run"})

# Official names that are not re-exported from `opencursor` yet.
KNOWN_MISSING_EXPORTS = frozenset()

# Factory / catalog methods on `Agent` (class-level in both SDKs).
AGENT_FACTORY_METHODS = frozenset(
    {
        "archive",
        "cancel_run",
        "create",
        "delete",
        "get",
        "get_run",
        "get_usage",
        "list",
        "list_runs",
        "messages",
        "prompt",
        "resume",
        "unarchive",
        "archive_agent",
        "unarchive_agent",
        "delete_agent",
        "lifecycle",
    }
)

# Official puts these on the same `Agent` class; opencursor puts them on the
# instance returned by create/resume (`SDKAgent` / CloudAgent / LocalAgent).
AGENT_INSTANCE_METHODS = frozenset(
    {
        "close",
        "download_artifact",
        "list_artifacts",
        "list_messages",
        "reload",
        "send",
        "__enter__",
        "__exit__",
    }
)

# Official-only instance methods not on CloudAgent / LocalAgent yet.
KNOWN_MISSING_AGENT_INSTANCE = frozenset()

# Official `Agent` names still missing from the factory or handles.
KNOWN_MISSING_AGENT_CLASS = frozenset()

RUN_METHODS = frozenset(
    {
        "cancel",
        "conversation",
        "conversation_json",
        "events",
        "iter_text",
        "messages",
        "observe",
        "on_did_change_status",
        "stream",
        "supports",
        "text",
        "unsupported_reason",
        "usage",
        "wait",
    }
)

KNOWN_MISSING_RUN_METHODS = frozenset()

CURSOR_ATTRIBUTES = frozenset({"me", "models", "repositories"})


def _public_names(obj: object) -> set[str]:
    names: set[str] = set()
    for name in dir(obj):
        if name.startswith("_") and name not in {"__enter__", "__exit__", "__aenter__", "__aexit__"}:
            continue
        names.add(name)
    return names


def test_official_package_exports_are_classified() -> None:
    official = set(cursor_sdk.__all__)
    classified = INTENTIONALLY_UNPORTED | INTENTIONALLY_SPLIT | KNOWN_MISSING_EXPORTS
    overlap = (
        (INTENTIONALLY_UNPORTED & INTENTIONALLY_SPLIT)
        | (INTENTIONALLY_UNPORTED & KNOWN_MISSING_EXPORTS)
        | (INTENTIONALLY_SPLIT & KNOWN_MISSING_EXPORTS)
    )
    assert not overlap, f"visibility buckets overlap: {sorted(overlap)}"
    unknown = official - classified
    implemented = {name for name in unknown if hasattr(opencursor, name)}
    leftover = unknown - implemented
    assert not leftover, (
        "official cursor_sdk exported new names; add them to opencursor or "
        f"INTENTIONALLY_UNPORTED / KNOWN_MISSING_EXPORTS: {sorted(leftover)}"
    )


def test_known_missing_exports_are_still_missing() -> None:
    present = sorted(name for name in KNOWN_MISSING_EXPORTS if hasattr(opencursor, name))
    assert present == [], f"KNOWN_MISSING_EXPORTS is stale; {present} are now public"


def test_intentionally_unported_stay_unpublished() -> None:
    present = sorted(name for name in INTENTIONALLY_UNPORTED if hasattr(opencursor, name))
    assert present == [], f"bridge types leaked onto opencursor: {present}"


def test_opencursor_all_names_are_importable() -> None:
    missing = [name for name in opencursor.__all__ if not hasattr(opencursor, name)]
    assert missing == [], f"opencursor.__all__ names are not attributes: {missing}"
    duplicates = sorted({name for name in opencursor.__all__ if opencursor.__all__.count(name) > 1})
    assert duplicates == [], f"duplicate opencursor.__all__ entries: {duplicates}"


def test_agent_factory_methods_match_official() -> None:
    official = _public_names(OfficialAgent)
    open_names = _public_names(Agent)
    missing = sorted(AGENT_FACTORY_METHODS - open_names)
    assert missing == [], f"Agent factory methods missing: {missing}"
    unexpected = sorted(
        (official - open_names)
        - AGENT_INSTANCE_METHODS
        - KNOWN_MISSING_AGENT_CLASS
        - KNOWN_MISSING_AGENT_INSTANCE
    )
    assert unexpected == [], f"unclassified official Agent names: {unexpected}"
    stale = sorted(name for name in KNOWN_MISSING_AGENT_CLASS if name in open_names)
    assert stale == [], f"KNOWN_MISSING_AGENT_CLASS is stale; drop {stale}"


def test_agent_instance_methods_live_on_handles() -> None:
    for handle in (CloudAgent, LocalAgent, SDKAgent):
        names = _public_names(handle)
        missing = sorted(AGENT_INSTANCE_METHODS - names)
        assert missing == [], f"{handle.__name__} missing instance methods: {missing}"
        still_missing = sorted(name for name in KNOWN_MISSING_AGENT_INSTANCE if name in names)
        assert still_missing == [], f"{handle.__name__} implemented {still_missing}; drop from KNOWN_MISSING_AGENT_INSTANCE"


def test_run_methods_match_official() -> None:
    official = _public_names(OfficialRun)
    for handle in (CloudRun, LocalRun):
        names = _public_names(handle)
        missing = sorted(RUN_METHODS - names)
        assert missing == [], f"{handle.__name__} missing Run methods: {missing}"
        leftover = sorted((official - names) - KNOWN_MISSING_RUN_METHODS)
        assert leftover == [], f"{handle.__name__} unclassified official Run names: {leftover}"
        stale = sorted(name for name in KNOWN_MISSING_RUN_METHODS if name in names)
        assert stale == [], f"{handle.__name__} implemented {stale}; drop from KNOWN_MISSING_RUN_METHODS"


def test_cursor_namespace_matches_official() -> None:
    official = _public_names(OfficialCursor)
    open_names = _public_names(Cursor)
    missing = sorted(CURSOR_ATTRIBUTES - open_names)
    assert missing == [], f"Cursor attributes missing: {missing}"
    leftover = sorted(official - open_names)
    assert leftover == [], f"unclassified official Cursor names: {leftover}"
    for attr in CURSOR_ATTRIBUTES:
        assert hasattr(getattr(Cursor, attr), "list" if attr != "me" else "__call__")


_TYPED_DICT_BAGS = (
    "AgentOptionsDict",
    "CloudAgentOptionsDict",
    "CloudRepositoryDict",
    "CloudSendOptionsDict",
    "LocalAgentOptionsDict",
    "LocalSendOptionsDict",
    "ModelSelectionDict",
    "SendOptionsDict",
    "UserMessageDict",
)


def test_typed_dict_bags_cover_official_keys() -> None:
    import cursor_sdk.types as official_types
    import opencursor.types as open_types

    for name in _TYPED_DICT_BAGS:
        official = getattr(official_types, name)
        ours = getattr(open_types, name)
        missing = set(official.__annotations__) - set(ours.__annotations__)
        assert not missing, f"{name} is missing official keys: {sorted(missing)}"
        assert name in opencursor.__all__


def test_cloud_shapes_match_official_names() -> None:
    from opencursor.types import CloudEnv, CloudEnvironment, CloudRepo, CloudRepository

    assert CloudEnv is CloudEnvironment
    assert CloudRepo is CloudRepository
    env = CloudEnvironment.from_json({"type": "CLOUD_ENVIRONMENT_TYPE_POOL", "name": "prod"})
    assert env.type == "pool"
    assert env.to_json()["type"] == "CLOUD_ENVIRONMENT_TYPE_POOL"
    repo = CloudRepository.model_validate(
        {"url": "https://github.com/acme/app", "startingRef": "main", "pr_url": "https://example/pr"}
    )
    assert repo.starting_ref == "main"
    assert repo.pr_url == "https://example/pr"
    assert repo.to_json()["startingRef"] == "main"


def test_message_shapes_match_official_names() -> None:
    from opencursor.types import (
        AgentMessage,
        AssistantMessage,
        SDKAssistantMessageContent,
        SDKUserMessage,
        SDKUserMessageContent,
        ThinkingMessage,
        UserMessage,
    )

    assert UserMessage is SDKUserMessage
    assert UserMessage.from_value("hi").text == "hi"
    assert UserMessage.from_value({"text": "there"}).to_json()["text"] == "there"
    assert AssistantMessage(text="ok").text == "ok"
    assert ThinkingMessage(text="hmm", thinking_duration_ms=12).thinking_duration_ms == 12
    assert SDKUserMessageContent().role == "user"
    assert SDKAssistantMessageContent().role == "assistant"
    parsed = AgentMessage.from_json(
        {"type": "assistant", "uuid": "u1", "agentId": "a1", "message": {"text": "hi"}}
    )
    assert parsed.agent_id == "a1"
    assert parsed.message == {"text": "hi"}


def test_other_shapes_match_official_names() -> None:
    from opencursor.types import (
        LocalAgentStoreConfig,
        LocalSendOptions,
        SendOptionsLocal,
        ShellCommand,
        ShellOutput,
        ShellOutputDeltaUpdate,
    )

    assert LocalSendOptions is SendOptionsLocal
    assert LocalSendOptions(force=True).to_json() == {"force": True}
    store = LocalAgentStoreConfig.model_validate({"type": "jsonl", "root_dir": "/tmp/store"})
    assert store.root_dir == "/tmp/store"
    assert store.to_json() == {"type": "jsonl", "rootDir": "/tmp/store"}
    command = ShellCommand.model_validate({"command": "ls", "workingDirectory": "/repo"})
    assert command.working_directory == "/repo"
    output = ShellOutput.model_validate({"stdout": "ok", "stderr": "", "exitCode": 0})
    assert output.exit_code == 0
    delta = ShellOutputDeltaUpdate(event={"chunk": "hi"})
    assert delta.type == "shell-output-delta"


def test_conversation_stream_shapes_match_official_names() -> None:
    from opencursor.types import (
        AgentConversationTurn,
        AssistantConversationStep,
        ConversationTurn,
        ShellConversationTurn,
        ThinkingConversationStep,
        ToolCallConversationStep,
        parse_conversation_step,
    )

    assistant = parse_conversation_step(
        {"type": "assistantMessage", "message": {"text": "hi"}}
    )
    assert isinstance(assistant, AssistantConversationStep)
    assert assistant.message.text == "hi"
    nested = parse_conversation_step(
        {"type": "ignored", "step": {"type": "toolCall", "message": {"name": "read"}}}
    )
    assert isinstance(nested, ToolCallConversationStep)
    thinking = parse_conversation_step(
        {
            "type": "thinkingMessage",
            "message": {"text": "hmm", "thinkingDurationMs": 12},
        }
    )
    assert isinstance(thinking, ThinkingConversationStep)
    assert thinking.message.thinking_duration_ms == 12
    assert parse_conversation_step({"type": "unknown"}) == {"type": "unknown"}

    agent = ConversationTurn.from_json(
        {
            "type": "agentConversationTurn",
            "turn": {
                "userMessage": {"text": "go"},
                "steps": [{"type": "assistantMessage", "message": {"text": "ok"}}],
            },
        }
    )
    assert isinstance(agent.turn, AgentConversationTurn)
    assert agent.turn.user_message == {"text": "go"}
    assert isinstance(agent.turn.steps[0], AssistantConversationStep)

    shell = ConversationTurn.from_json(
        {
            "type": "shellConversationTurn",
            "turn": {
                "shellCommand": {"command": "ls", "workingDirectory": "/repo"},
                "shellOutput": {"stdout": "a", "stderr": "", "exitCode": 0},
            },
        }
    )
    assert isinstance(shell.turn, ShellConversationTurn)
    assert shell.turn.shell_command is not None
    assert shell.turn.shell_command.working_directory == "/repo"
    assert shell.turn.shell_output is not None
    assert shell.turn.shell_output.exit_code == 0

    empty = ConversationTurn.from_json({"type": "agentConversationTurn", "turn": None})
    assert isinstance(empty.turn, AgentConversationTurn)
    assert empty.turn.steps == []
    unknown = ConversationTurn.from_json({"type": "other", "turn": {"foo": 1}})
    assert unknown.turn == {"foo": 1}


def test_run_helpers_and_interaction_updates_match_official_names() -> None:
    from opencursor.types import (
        AssistantConversationStep,
        RunSnapshot,
        RunStreamEvent,
        TextDeltaUpdate,
        TokenUsage,
        UnknownInteractionUpdate,
        parse_interaction_update,
        sdk_message_from_json,
        sum_token_usage,
        to_token_usage,
    )

    usage = to_token_usage({"inputTokens": 2, "output_tokens": 3})
    assert usage is not None
    assert usage.input_tokens == 2
    assert usage.outputTokens == 3
    assert usage.total_tokens == 5
    assert to_token_usage({}) is None
    summed = sum_token_usage([usage, {"cacheReadTokens": 4}])
    assert summed is not None
    assert summed.cache_read_tokens == 4
    assert summed.input_tokens == 2

    parsed_usage = TokenUsage.from_json({"inputTokens": 1, "outputTokens": 1, "totalTokens": 9})
    assert parsed_usage.total_tokens == 9

    snap = RunSnapshot.from_json(
        {
            "runId": "r1",
            "agentId": "a1",
            "status": "RUN_LIFECYCLE_STATUS_FINISHED",
            "result": "ok",
            "model": {"id": "composer-2"},
            "usage": {"inputTokens": 1, "outputTokens": 2},
        }
    )
    assert snap.id == "r1"
    assert snap.agent_id == "a1"
    assert snap.status == "finished"
    assert snap.model is not None
    assert snap.model.id == "composer-2"
    assert snap.usage is not None
    assert snap.usage.output_tokens == 2

    text = parse_interaction_update({"type": "text-delta", "text": "hi"})
    assert isinstance(text, TextDeltaUpdate)
    nested = parse_interaction_update({"update": {"type": "token-delta", "tokens": 8}})
    assert nested.tokens == 8
    unknown_update = parse_interaction_update({"type": "nope", "x": 1})
    assert isinstance(unknown_update, UnknownInteractionUpdate)

    assistant = sdk_message_from_json(
        {
            "type": "assistant",
            "agentId": "a1",
            "runId": "r1",
            "message": {"content": [{"type": "text", "text": "hi"}]},
        }
    )
    assert assistant["type"] == "assistant"
    assert assistant["agent_id"] == "a1"
    assert assistant["message"]["content"][0]["text"] == "hi"
    assert assistant.type == "assistant"
    assert assistant.message.content[0]["text"] == "hi"

    event = RunStreamEvent.from_json(
        {
            "offset": "1",
            "sdkMessage": {"type": "status", "agent_id": "a1", "run_id": "r1", "status": "RUNNING"},
        }
    )
    assert event.kind == "sdk_message"
    assert event.sdk_message["status"] == "RUNNING"
    step_event = RunStreamEvent.from_json(
        {"step": {"type": "assistantMessage", "message": {"text": "ok"}}}
    )
    assert isinstance(step_event.step, AssistantConversationStep)
    delta_event = RunStreamEvent.from_json({"interactionUpdate": {"type": "text-delta", "text": "x"}})
    assert isinstance(delta_event.interaction_update, TextDeltaUpdate)
    result_event = RunStreamEvent.from_json({"result": {"result": {"ok": True}}})
    assert result_event.result_is_full is True
    assert result_event.result == {"ok": True}


def test_mcp_and_tool_shapes_match_official_names() -> None:
    from opencursor.types import (
        AgentDefinitionMcpServer,
        CustomTool,
        CustomToolContext,
        HttpMcpServerConfig,
        McpAuth,
        McpServerRemote,
        McpServerStdio,
        SDKCustomTool,
        SseMcpServerConfig,
        StdioMcpServerConfig,
    )

    assert McpServerStdio is StdioMcpServerConfig
    assert McpServerRemote is HttpMcpServerConfig
    assert SDKCustomTool is CustomTool
    assert isinstance(SseMcpServerConfig(url="https://example/sse"), HttpMcpServerConfig)

    auth = McpAuth.model_validate({"CLIENT_ID": "abc", "CLIENT_SECRET": "s", "SCOPES": "a b"})
    assert auth.client_id == "abc"
    assert auth.to_json()["clientId"] == "abc"
    assert auth.to_json()["scopes"] == ["a", "b"]

    http = HttpMcpServerConfig(url="https://mcp.example/mcp", auth=auth)
    wire = http.to_json()
    assert "http" in wire
    assert wire["http"]["url"] == "https://mcp.example/mcp"
    assert wire["http"]["type"] == "HTTP_MCP_TRANSPORT_TYPE_HTTP"

    sse = SseMcpServerConfig(url="https://mcp.example/sse")
    assert sse.to_json()["http"]["type"] == "HTTP_MCP_TRANSPORT_TYPE_SSE"

    stdio = StdioMcpServerConfig(command="npx", args=["-y", "demo"], cwd="/tmp")
    assert stdio.to_json()["stdio"]["command"] == "npx"
    assert stdio.to_json()["stdio"]["cwd"] == "/tmp"

    tool = CustomTool(description="ping", input_schema={"type": "object"}, execute=lambda args, ctx: "pong")
    assert tool.input_schema == {"type": "object"}
    assert CustomToolContext(tool_call_id="c1").tool_call_id == "c1"

    named = AgentDefinitionMcpServer.named("figma")
    assert named.to_json() == {"name": "figma"}
    inline = AgentDefinitionMcpServer.inline(http)
    assert "inlineConfig" in inline.to_json()
    assert "http" in inline.to_json()["inlineConfig"]


def test_documented_snake_case_return_types() -> None:
    from opencursor.types import (
        ListResult,
        RunResult,
        RunUsage,
        SDKAgentInfo,
        SDKArtifact,
        TokenUsage,
        UsageCost,
    )

    usage = TokenUsage(inputTokens=2, outputTokens=3)
    assert usage.input_tokens == 2
    assert usage.inputTokens == 2
    assert "input_tokens=" in repr(usage)

    cost = UsageCost(rawCostCents=1.5, chargedCents=0.5)
    assert cost.raw_cost_cents == 1.5
    assert cost.rawCostCents == 1.5
    assert "raw_cost_cents=" in repr(cost)

    run_usage = RunUsage(runId="r1", usage=usage, cost=cost)
    assert run_usage.run_id == "r1"
    assert run_usage.runId == "r1"

    result = RunResult(id="r1", agentId="a1", status="finished", durationMs=12, requestId="req")
    assert result.agent_id == "a1"
    assert result.duration_ms == 12
    assert result.request_id == "req"
    assert result.durationMs == 12
    assert "agent_id=" in repr(result)
    assert "duration_ms=" in repr(result)

    info = SDKAgentInfo(agentId="a1", name="n", summary="s", lastModified=123, cwd="/tmp", metadata={})
    assert info.agent_id == "a1"
    assert info.last_modified == "123"
    assert info.lastModified == 123
    assert info.cwd == "/tmp"

    page = ListResult(items=[info], nextCursor="abc")
    assert page.next_cursor == "abc"
    assert page.nextCursor == "abc"
    assert page.has_next_page() is True
    assert list(page) == [info]
    assert list(page.auto_paging_iter()) == [info]

    artifact = SDKArtifact(path="out.txt", sizeBytes=4, updatedAt="now")
    assert artifact.size_bytes == 4
    assert artifact.sizeBytes == 4
    assert artifact.updated_at == "now"
    assert artifact.updatedAt == "now"
    assert "size_bytes=" in repr(artifact)
