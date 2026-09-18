from __future__ import annotations

import asyncio

from opencursor import Agent
from opencursor import messages
from opencursor._local_runtime import LocalAgentStore, RunEventTailer
from opencursor.types import AgentOptions, GetAgentMessagesOptions, ListAgentsLocalOptions, ListRunsLocalOptions, LocalAgentOptions


def test_local_run_stream_event_roundtrip() -> None:
    msg = messages.SDKStatusMessage(type="status", agent_id="agent-1", run_id="run-1", status="RUNNING")
    event = messages.create_sdk_message_run_stream_event(msg)

    decoded = messages.decode_local_run_stream_event(event)

    assert decoded["type"] == "sdk_message"
    assert messages.local_run_stream_event_to_sdk_message(decoded) == msg
    assert not messages.is_terminal_local_run_stream_event(decoded)


def test_terminal_local_run_stream_events() -> None:
    result = {
        "schemaVersion": messages.LOCAL_RUN_STREAM_SCHEMA_VERSION,
        "type": "result",
        "agentId": "agent-1",
        "runId": "run-1",
        "status": "error",
    }
    done = {
        "schemaVersion": messages.LOCAL_RUN_STREAM_SCHEMA_VERSION,
        "type": "done",
        "agentId": "agent-1",
        "runId": "run-1",
    }

    assert messages.is_terminal_local_run_stream_event(messages.decode_local_run_stream_event(result))
    assert messages.is_terminal_local_run_stream_event(messages.decode_local_run_stream_event(done))


def test_local_store_paginates_and_tails_events(tmp_path) -> None:
    asyncio.run(_test_local_store_paginates_and_tails_events(tmp_path))


async def _test_local_store_paginates_and_tails_events(tmp_path) -> None:
    store = LocalAgentStore(str(tmp_path), tmp_path / "state")
    try:
        opts = AgentOptions.model_validate({"local": {"cwd": str(tmp_path)}, "model": {"id": "model-1"}})
        agent, run = await store.create_agent(opts)
        await store.append_sdk_message(
            messages.SDKStatusMessage(type="status", agent_id=agent.agentId, run_id=run.runId, status="RUNNING")
        )
        await store.append_sdk_message(
            messages.SDKStatusMessage(type="status", agent_id=agent.agentId, run_id=run.runId, status="ERROR")
        )
        await store.append_terminal_event(agent.agentId, run.runId, "error")

        first_page, next_offset = await store.list_run_events(run.runId, limit=1)
        assert len(first_page) == 1
        assert next_offset == "1"

        streamed = [m async for m in RunEventTailer(store).stream_run_events(run.runId)]
        assert [m["status"] for m in streamed if m["type"] == "status"] == ["RUNNING", "ERROR"]
    finally:
        store.close()


def test_agent_local_routes_create_send_and_list(tmp_path, monkeypatch) -> None:
    asyncio.run(_test_agent_local_routes_create_send_and_list(tmp_path, monkeypatch))


async def _test_agent_local_routes_create_send_and_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    options = AgentOptions.model_validate(
        {
            "local": {"cwd": str(tmp_path)},
            "model": {"id": "model-1"},
            "name": "Local Test",
        }
    )
    agent = await Agent.create(options)
    try:
        run = await agent.send("hello")
        result = await run.wait()

        assert result.status == "error"

        agents = await Agent.list(ListAgentsLocalOptions(runtime="local", cwd=str(tmp_path)))
        runs = await Agent.listRuns(agent.agent_id, ListRunsLocalOptions(runtime="local", cwd=str(tmp_path)))
        listed_messages = await Agent.messages.list(
            agent.agent_id,
            GetAgentMessagesOptions(runtime="local", cwd=str(tmp_path)),
        )

        assert [a.agentId for a in agents.items] == [agent.agent_id]
        assert [r.id for r in runs.items] == [run.id]
        assert [m["type"] for m in listed_messages] == ["user"]
    finally:
        await agent.aclose()


def test_prompt_raises_invalid_user_api_key(tmp_path, monkeypatch) -> None:
    from opencursor import AuthenticationError

    async def boom(self) -> str:
        raise AuthenticationError(
            "Invalid User API Key",
            code="unauthenticated",
            status=401,
            is_retryable=False,
        )

    async def slow_aclose(self) -> None:
        await asyncio.sleep(0.05)

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("opencursor._connect.ConnectClient.exchange_access_token", boom)
    monkeypatch.setattr("opencursor._connect.ConnectClient.aclose", slow_aclose)
    try:
        Agent.prompt(
            "hello",
            AgentOptions(
                api_key="sk-fake",
                model="composer-2.5",
                local=LocalAgentOptions(cwd=str(tmp_path)),
            ),
        )
    except AuthenticationError as exc:
        assert exc.message == "Invalid User API Key"
        assert exc.code == "unauthenticated"
        assert str(exc) == "unauthenticated: Invalid User API Key"
        return
    raise AssertionError("expected AuthenticationError from Agent.prompt")
