from __future__ import annotations

import asyncio

import pytest

from opencursor import Agent, Cursor, JsonlLocalAgentStore, composeLocalAgentStore
from opencursor._local_agent_store import (
    JSONL_LOCAL_AGENT_STORE_FILES,
    paginateAgentDocuments,
    paginateCheckpointBlobIds,
    paginateRunDocuments,
)
from opencursor._sdk_config import clear_default_network_config_for_tests
from opencursor.types import AgentOptions, ListAgentsLocalOptions, ListRunsLocalOptions


def test_jsonl_file_names() -> None:
    assert JSONL_LOCAL_AGENT_STORE_FILES == {
        "agents": "agents.ndjson",
        "runs": "runs.ndjson",
        "runEvents": "run_events.ndjson",
        "checkpoints": "checkpoints.ndjson",
    }


def test_paginate_agents_and_runs() -> None:
    agents = [
        {"agentId": "a", "cwd": "/one", "updatedAt": 2},
        {"agentId": "b", "cwd": "/one", "updatedAt": 2},
        {"agentId": "c", "cwd": "/two", "updatedAt": 1},
    ]
    first = paginateAgentDocuments(agents, {"cwd": "/one", "limit": 1})
    assert [item["agentId"] for item in first["items"]] == ["b"]
    assert first["nextCursor"]
    second = paginateAgentDocuments(agents, {"cwd": "/one", "limit": 1, "cursor": first["nextCursor"]})
    assert [item["agentId"] for item in second["items"]] == ["a"]
    assert "nextCursor" not in second

    runs = [
        {"runId": "r2", "turnNumber": 2},
        {"runId": "r1", "turnNumber": 1},
        {"runId": "r1b", "turnNumber": 1},
    ]
    page = paginateRunDocuments(runs, {"limit": 2})
    assert [item["runId"] for item in page["items"]] == ["r1", "r1b"]
    rest = paginateRunDocuments(runs, {"limit": 2, "cursor": page["nextCursor"]})
    assert [item["runId"] for item in rest["items"]] == ["r2"]

    blobs = paginateCheckpointBlobIds(["c", "a", "b"], {"limit": 2})
    assert blobs["items"] == ["a", "b"]
    assert blobs["nextCursor"] == "b"
    assert paginateCheckpointBlobIds(["c", "a", "b"], {"cursor": "b"})["items"] == ["c"]


def test_invalid_cursors() -> None:
    with pytest.raises(ValueError, match="Invalid list cursor"):
        paginateAgentDocuments([], {"cursor": "not-base64"})
    run_cursor = paginateRunDocuments(
        [{"runId": "r1", "turnNumber": 1}, {"runId": "r2", "turnNumber": 2}],
        {"limit": 1},
    )["nextCursor"]
    with pytest.raises(ValueError, match="Invalid agent list cursor"):
        paginateAgentDocuments([], {"cursor": run_cursor})


def test_jsonl_roundtrip_and_idempotency(tmp_path) -> None:
    asyncio.run(_test_jsonl_roundtrip_and_idempotency(tmp_path))


async def _test_jsonl_roundtrip_and_idempotency(tmp_path) -> None:
    store = JsonlLocalAgentStore(tmp_path)
    agent = await store.agents.create(
        {"agent": {"agentId": "agent-1", "cwd": str(tmp_path), "status": "idle", "createdAt": 10, "updatedAt": 20, "name": "A"}}
    )
    assert agent["agentId"] == "agent-1"
    run = await store.runs.create(
        {"run": {"runId": "run-1", "agentId": "agent-1", "turnNumber": 1, "status": "queued", "createdAt": 10, "updatedAt": 10}}
    )
    first = await store.runEvents.append(
        {"runId": "run-1", "eventType": "run_stream_event", "payload": {"n": 1}, "idempotencyKey": "k1"}
    )
    again = await store.runEvents.append(
        {"runId": "run-1", "eventType": "run_stream_event", "payload": {"n": 2}, "idempotencyKey": "k1"}
    )
    assert first["seq"] == 1
    assert again["seq"] == 1
    assert isinstance(first["createdAt"], int)
    listed = await store.runEvents.list({"runId": "run-1", "limit": 10})
    assert len(listed["items"]) == 1
    await store.checkpoints.create({"agentId": "agent-1", "blobId": "blob-1", "data": b"hello"})
    assert await store.checkpoints.get({"agentId": "agent-1", "blobId": "blob-1"}) == b"hello"
    blobs = await store.checkpoints.list({"filter": {"agentIds": ["agent-1"]}})
    assert blobs["items"] == ["blob-1"]
    with pytest.raises(ValueError, match="already exists"):
        await store.agents.create({"agent": agent})
    with pytest.raises(ValueError, match="already exists"):
        await store.runs.create({"run": run})
    composed = composeLocalAgentStore(
        {"agents": store.agents, "checkpoints": store.checkpoints, "runs": store.runs, "runEvents": store.runEvents}
    )
    got = await composed.agents.get({"agentId": "agent-1"})
    assert got is not None
    assert got["name"] == "A"
    assert (tmp_path / "agents.ndjson").is_file()
    assert (tmp_path / "runs.ndjson").is_file()
    assert (tmp_path / "run_events.ndjson").is_file()
    assert (tmp_path / "checkpoints.ndjson").is_file()


def test_jsonl_tolerates_partial_last_line(tmp_path) -> None:
    asyncio.run(_test_jsonl_tolerates_partial_last_line(tmp_path))


async def _test_jsonl_tolerates_partial_last_line(tmp_path) -> None:
    path = tmp_path / "agents.ndjson"
    path.write_text('{"agentId":"ok","cwd":"/x","status":"idle","createdAt":1,"updatedAt":1}\n{partial', encoding="utf-8")
    store = JsonlLocalAgentStore(tmp_path)
    listed = await store.agents.list()
    assert [item["agentId"] for item in listed["items"]] == ["ok"]
    path.write_text('{"agentId":"ok","cwd":"/x","status":"idle","createdAt":1,"updatedAt":1}\n{bad}\n{"agentId":"two"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt local agent store: failed to parse record 2"):
        await store.agents.list()


def test_agent_create_with_jsonl_store(tmp_path, monkeypatch) -> None:
    asyncio.run(_test_agent_create_with_jsonl_store(tmp_path, monkeypatch))


async def _test_agent_create_with_jsonl_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    clear_default_network_config_for_tests()
    store = JsonlLocalAgentStore(tmp_path / "jsonl")
    try:
        Cursor.configure({"local": {"store": store}})
        options = AgentOptions.model_validate(
            {"local": {"cwd": str(tmp_path), "store": store}, "model": {"id": "model-1"}, "name": "Jsonl"}
        )
        agent = await Agent.create(options)
        try:
            run = await agent.send("hello")
            await run.wait()
            listed = await Agent.list(ListAgentsLocalOptions(runtime="local", cwd=str(tmp_path), store=store))
            runs = await Agent.listRuns(agent.agent_id, ListRunsLocalOptions(runtime="local", cwd=str(tmp_path), store=store))
            resumed = await Agent.resume(agent.agent_id, options)
            try:
                assert [item.agentId for item in listed.items] == [agent.agent_id]
                assert [item.id for item in runs.items] == [run.id]
                assert resumed.agent_id == agent.agent_id
            finally:
                await resumed.aclose()
        finally:
            await agent.aclose()
        assert (tmp_path / "jsonl" / "agents.ndjson").read_text(encoding="utf-8").strip()
        assert not (tmp_path / "home" / ".cursor").exists() or not list((tmp_path / "home").rglob("index.db"))
    finally:
        clear_default_network_config_for_tests()
