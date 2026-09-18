from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from opencursor._cloud_agent import CloudRun
from opencursor._local_executor import (
    LocalAgentExecutor,
    context_injection_state_case,
    steer_outcome_from_state,
)
from opencursor._local_runtime import LocalRun, LocalRunRecord
from opencursor._protobuf import decode_message, oneof_case
from opencursor._subagents import DiscardStore
from opencursor.types import AgentOptions, ModelSelection


class _FakeSession:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def aclose(self) -> None:
        return None


def _executor(*, generation: bool = True) -> LocalAgentExecutor:
    executor = LocalAgentExecutor(
        store=DiscardStore(),
        agent_id="agent-1",
        run_id="run-1",
        options=AgentOptions(),
        model=ModelSelection(id="composer-2"),
        prompt="hello",
        conversation_id="conv-1",
        request_id="gen-1",
        nested=True,
    )
    if generation:
        executor._generation_active = True
        executor._run_session = _FakeSession()
        executor._session_ready.set()
    return executor


def _last_inject(executor: LocalAgentExecutor) -> dict[str, Any]:
    session = executor._run_session
    assert isinstance(session, _FakeSession)
    decoded = decode_message("agent.v1.AgentClientMessage", session.sent[-1])
    assert oneof_case(decoded, "message") == "conversation_action"
    return decoded["conversation_action"]["inject_context_action"]


def test_steer_state_mapping() -> None:
    assert context_injection_state_case({"queued": {}}) == "queued"
    assert context_injection_state_case({"delivered": {}}) == "delivered"
    assert steer_outcome_from_state("queued") == "confirm_steering"
    assert steer_outcome_from_state("delivered") == "complete_delivered"
    assert steer_outcome_from_state("queued_for_next_turn") == "revert_to_followup"
    assert steer_outcome_from_state("cancelled") == "revert_to_followup"
    assert steer_outcome_from_state("rejected") == "revert_to_followup"
    assert steer_outcome_from_state(None) == "revert_to_followup"


def test_steer_empty_or_inactive_reverts() -> None:
    asyncio.run(_test_steer_empty_or_inactive_reverts())


async def _test_steer_empty_or_inactive_reverts() -> None:
    live = _executor()
    assert await live.steer("   ") == "revert_to_followup"
    assert await live.steer("") == "revert_to_followup"
    idle = _executor(generation=False)
    assert await idle.steer("redirect") == "revert_to_followup"


def test_steer_delivered_and_rejected() -> None:
    asyncio.run(_test_steer_delivered_and_rejected())


async def _test_steer_delivered_and_rejected() -> None:
    executor = _executor()
    task = asyncio.create_task(executor.steer("go left"))
    for _ in range(20):
        if isinstance(executor._run_session, _FakeSession) and executor._run_session.sent:
            break
        await asyncio.sleep(0)
    inject = _last_inject(executor)
    assert inject["expected_run_id"] == "gen-1"
    assert inject["user_context"]["user_message"]["text"] == "go left"
    injection_id = inject["injection_id"]
    executor._on_context_injection_state({"injection_id": injection_id, "state": {"delivered": {}}})
    assert await task == "complete_delivered"

    task = asyncio.create_task(executor.steer("go right"))
    for _ in range(20):
        if isinstance(executor._run_session, _FakeSession) and len(executor._run_session.sent) >= 2:
            break
        await asyncio.sleep(0)
    injection_id = _last_inject(executor)["injection_id"]
    executor._on_context_injection_state({"injection_id": injection_id, "state": {"queued": {}}})
    await asyncio.sleep(0)
    assert not task.done()
    executor._on_context_injection_state({"injection_id": injection_id, "state": {"rejected": {}}})
    assert await task == "revert_to_followup"


def test_steer_queued_then_delivered() -> None:
    asyncio.run(_test_steer_queued_then_delivered())


async def _test_steer_queued_then_delivered() -> None:
    executor = _executor()
    task = asyncio.create_task(executor.steer("keep going"))
    for _ in range(20):
        if isinstance(executor._run_session, _FakeSession) and executor._run_session.sent:
            break
        await asyncio.sleep(0)
    injection_id = _last_inject(executor)["injection_id"]
    executor._on_context_injection_state({"injection_id": injection_id, "state": {"queued": {}}})
    await asyncio.sleep(0)
    assert not task.done()
    executor._on_context_injection_state({"injection_id": injection_id, "state": {"delivered": {}}})
    assert await task == "complete_delivered"


def test_steer_timeout_reverts(monkeypatch) -> None:
    asyncio.run(_test_steer_timeout_reverts(monkeypatch))


async def _test_steer_timeout_reverts(monkeypatch) -> None:
    import opencursor._local_executor as local_executor

    monkeypatch.setattr(local_executor, "STEER_ACK_TIMEOUT_S", 0.05)
    executor = _executor()
    assert await executor.steer("too slow") == "revert_to_followup"


def test_steer_turn_end_reverts_pending() -> None:
    asyncio.run(_test_steer_turn_end_reverts_pending())


async def _test_steer_turn_end_reverts_pending() -> None:
    executor = _executor()
    task = asyncio.create_task(executor.steer("late"))
    for _ in range(20):
        if isinstance(executor._run_session, _FakeSession) and executor._run_session.sent:
            break
        await asyncio.sleep(0)
    executor._revert_pending_steers()
    assert await task == "revert_to_followup"


def test_local_and_cloud_run_steer_without_live_turn() -> None:
    asyncio.run(_test_local_and_cloud_run_steer_without_live_turn())


async def _test_local_and_cloud_run_steer_without_live_turn() -> None:
    now = datetime.now(timezone.utc)
    record = LocalRunRecord(
        runId="run-1",
        agentId="agent-1",
        turnNumber=1,
        status="RUNNING",
        model=None,
        errorCode=None,
        createdAt=now,
        updatedAt=now,
        startedAt=None,
        finishedAt=None,
        cancelledAt=None,
        expiredAt=None,
    )
    detached = LocalRun(None, record)  # type: ignore[arg-type]
    assert await detached.steer("hello") == "revert_to_followup"
    assert await detached.steer("  ") == "revert_to_followup"

    class _StubClient:
        pass

    cloud = CloudRun(_StubClient(), {"id": "run-1", "agentId": "bc-1"}, None, None)  # type: ignore[arg-type]
    assert await cloud.steer("hello") == "revert_to_followup"
