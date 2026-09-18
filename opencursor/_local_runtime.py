from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Mapping, cast

from opencursor import errors, messages
from opencursor._interaction_accumulator import accumulate_sdk_message_stream
from opencursor._local_executor import LocalAgentExecutor
from opencursor._sync_runtime import close_sync, iterate_async, run_sync_or_awaitable
from opencursor._run_api import (
    agent_messages_from_rows,
    assistant_text_from_message,
    conversation_json_from_turns,
    empty_agent_usage,
    ensure_run_operation,
    iter_run_events,
    iter_run_messages,
    iter_run_text,
    run_stream_event_from_local_payload,
    run_text,
    sum_token_usage,
    token_usage_from_mapping,
)
from opencursor._sandbox import SandboxRuntime
from opencursor._sdk_config import resolve_local_agent_store
from opencursor._local_agent_store import is_protocol_store
from opencursor.types import (
    AgentMessage,
    AgentOperationOptions,
    AgentOptions,
    AgentUsage,
    GetAgentMessagesOptions,
    GetAgentOptions,
    GetRunOptionsLocal,
    GetUsageOptions,
    ListAgentsLocalOptions,
    ListResult,
    ListRunsLocalOptions,
    LocalAgentOptions,
    ModelSelection,
    RunResult,
    RunResultStatus,
    RunStatus,
    RunError,
    RunStreamEvent,
    RunUsage,
    SDKAgentInfoLocal,
    SDKArtifact,
    SDKUserMessage,
    SendOptions,
    SteerAckOutcome,
    TokenUsage,
)

TERMINAL_LOCAL_STATUSES = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})


@dataclass(frozen=True)
class LocalRunEventRecord:
    runId: str
    seq: int
    offset: str
    eventType: str
    payload: Any
    payloadRef: str | None
    idempotencyKey: str | None
    createdAt: datetime


@dataclass(frozen=True)
class LocalAgentRecord:
    agentId: str
    workspaceRef: str
    status: str
    activeRunId: str | None
    name: str
    createdAt: datetime
    updatedAt: datetime
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LocalRunRecord:
    runId: str
    agentId: str
    turnNumber: int
    status: str
    model: ModelSelection | None
    errorCode: str | None
    createdAt: datetime
    updatedAt: datetime
    startedAt: datetime | None
    finishedAt: datetime | None
    cancelledAt: datetime | None
    expiredAt: datetime | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_ms() -> int:
    return int(_now().timestamp() * 1000)


def _datetime_from_ms(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _doc_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, Mapping):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    return dict(obj)


_AGENT_STATUS_TO_PROTOCOL = {"IDLE": "idle", "RUNNING": "running", "ERROR": "error", "ARCHIVED": "archived"}
_AGENT_STATUS_FROM_PROTOCOL = {value: key for key, value in _AGENT_STATUS_TO_PROTOCOL.items()}
_RUN_STATUS_TO_PROTOCOL = {
    "QUEUED": "queued",
    "RUNNING": "running",
    "FINISHED": "finished",
    "ERROR": "error",
    "CANCELLED": "cancelled",
    "EXPIRED": "expired",
}
_RUN_STATUS_FROM_PROTOCOL = {value: key for key, value in _RUN_STATUS_TO_PROTOCOL.items()}


def _to_iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat().replace("+00:00", "Z")


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ms(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"))


def _sanitize_workspace_ref(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]", "-", value)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "workspace"


def _default_state_root(cwd: str) -> Path:
    digest = hashlib.md5(cwd.encode("utf-8")).hexdigest()  # noqa: S324 - parity with the SDK store path.
    return Path.home() / ".cursor" / "projects" / _sanitize_workspace_ref(cwd) / "sdk-agent-store" / digest


def _workspace_ref_from_cwd(cwd: str | list[str] | None) -> str:
    if isinstance(cwd, list):
        return str(Path(cwd[0] if cwd else ".").expanduser().resolve())
    return str(Path(cwd or ".").expanduser().resolve())


def _workspace_ref_from_options(options: AgentOptions | GetAgentOptions | AgentOperationOptions | None) -> str:
    local = getattr(options, "local", None)
    cwd = getattr(local, "cwd", None) if local is not None else getattr(options, "cwd", None)
    return _workspace_ref_from_cwd(cwd)


def _pagination(limit: int | None, cursor: str | None) -> tuple[int, int]:
    page_limit = limit if isinstance(limit, int) and limit > 0 else 50
    try:
        offset = int(cursor or "0")
    except ValueError:
        offset = 0
    return page_limit, max(offset, 0)


def _run_status_to_sdk(status: str) -> RunStatus:
    if status in ("QUEUED", "RUNNING"):
        return "running"
    if status == "FINISHED":
        return "finished"
    if status == "CANCELLED":
        return "cancelled"
    return "error"


def _public_agent_status(status: str, active_run_status: str | None) -> Literal["running", "finished", "error"] | None:
    source = active_run_status or status
    if source in ("QUEUED", "RUNNING"):
        return "running"
    if source == "FINISHED":
        return "finished"
    if source in ("ERROR", "EXPIRED"):
        return "error"
    return None


def _model_from_row(model: str | None, params_json: str | None) -> ModelSelection | None:
    if model is None:
        return None
    raw = {"id": model}
    params = _json_loads(params_json, None)
    if params:
        raw["params"] = params
    return ModelSelection.model_validate(raw)


def _model_id(model: ModelSelection | None) -> str | None:
    return model.id if model is not None else None


def _model_params_json(model: ModelSelection | None) -> str | None:
    if model is None or not model.params:
        return None
    return _json_dumps([p.model_dump() for p in model.params])


def _agent_id() -> str:
    return f"agent-{uuid.uuid4()}"


def _run_id() -> str:
    return str(uuid.uuid4())


def _user_text(message: str | SDKUserMessage) -> str:
    return message if isinstance(message, str) else message.text


def _user_sdk_message(agent_id: str, run_id: str, text: str) -> messages.SDKMessage:
    return cast(
        messages.SDKMessage,
        {
            "type": "user",
            "agent_id": agent_id,
            "run_id": run_id,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        },
    )


def _status_sdk_message(
    agent_id: str,
    run_id: str,
    status: Literal["CREATING", "RUNNING", "FINISHED", "ERROR", "CANCELLED", "EXPIRED"],
    message: str | None = None,
) -> messages.SDKMessage:
    payload: dict[str, Any] = {"type": "status", "agent_id": agent_id, "run_id": run_id, "status": status}
    if message:
        payload["message"] = message
    return cast(messages.SDKMessage, payload)


class LocalAgentStore:
    def __init__(self, workspace_ref: str, state_root: Path | None = None) -> None:
        self.workspace_ref = workspace_ref
        self.state_root = state_root or _default_state_root(workspace_ref)
        self.db_path = self.state_root / "index.db"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path)
        self._db.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        self._db.close()

    def _init_db(self) -> None:
        self._db.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            PRAGMA busy_timeout = 5000;
            CREATE TABLE IF NOT EXISTS agents (
              agent_id TEXT PRIMARY KEY,
              workspace_ref TEXT NOT NULL,
              status TEXT NOT NULL,
              active_run_id TEXT,
              latest_checkpoint_ref_json TEXT,
              name TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              metadata_json TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
              run_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              turn_number INTEGER NOT NULL,
              status TEXT NOT NULL,
              model TEXT,
              model_params_json TEXT,
              start_checkpoint_ref_json TEXT,
              latest_checkpoint_ref_json TEXT,
              error_code TEXT,
              usage_ref TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              started_at TEXT,
              finished_at TEXT,
              cancelled_at TEXT,
              expired_at TEXT,
              PRIMARY KEY (agent_id, run_id)
            );
            CREATE TABLE IF NOT EXISTS run_events (
              run_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              offset TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload_json TEXT,
              payload_ref TEXT,
              idempotency_key TEXT,
              created_at TEXT NOT NULL,
              PRIMARY KEY (run_id, seq),
              UNIQUE(run_id, offset)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS run_events_idempotency_key_idx
              ON run_events (run_id, idempotency_key)
              WHERE idempotency_key IS NOT NULL;
            """
        )
        self._db.commit()

    def _agent_from_row(self, row: sqlite3.Row | None) -> LocalAgentRecord | None:
        if row is None:
            return None
        return LocalAgentRecord(
            agentId=str(row["agent_id"]),
            workspaceRef=str(row["workspace_ref"]),
            status=str(row["status"]),
            activeRunId=row["active_run_id"],
            name=str(row["name"]),
            createdAt=cast(datetime, _from_iso(row["created_at"])),
            updatedAt=cast(datetime, _from_iso(row["updated_at"])),
            metadata=_json_loads(row["metadata_json"], {}),
        )

    def _run_from_row(self, row: sqlite3.Row | None) -> LocalRunRecord | None:
        if row is None:
            return None
        return LocalRunRecord(
            runId=str(row["run_id"]),
            agentId=str(row["agent_id"]),
            turnNumber=int(row["turn_number"]),
            status=str(row["status"]),
            model=_model_from_row(row["model"], row["model_params_json"]),
            errorCode=row["error_code"],
            createdAt=cast(datetime, _from_iso(row["created_at"])),
            updatedAt=cast(datetime, _from_iso(row["updated_at"])),
            startedAt=_from_iso(row["started_at"]),
            finishedAt=_from_iso(row["finished_at"]),
            cancelledAt=_from_iso(row["cancelled_at"]),
            expiredAt=_from_iso(row["expired_at"]),
        )

    def _event_from_row(self, row: sqlite3.Row) -> LocalRunEventRecord:
        return LocalRunEventRecord(
            runId=str(row["run_id"]),
            seq=int(row["seq"]),
            offset=str(row["offset"]),
            eventType=str(row["event_type"]),
            payload=_json_loads(row["payload_json"], None),
            payloadRef=row["payload_ref"],
            idempotencyKey=row["idempotency_key"],
            createdAt=cast(datetime, _from_iso(row["created_at"])),
        )

    async def create_agent(self, options: AgentOptions) -> tuple[LocalAgentRecord, LocalRunRecord]:
        agent_id = options.agentId or _agent_id()
        name = (options.name or "").strip() or "New Agent"
        now = _to_iso()
        run_id = _run_id()
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO agents (agent_id, workspace_ref, status, active_run_id, name, created_at, updated_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (agent_id, self.workspace_ref, "IDLE", run_id, name, now, now, _json_dumps({})),
                )
                self._db.execute(
                    "INSERT INTO runs (run_id, agent_id, turn_number, status, model, model_params_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, agent_id, 1, "QUEUED", _model_id(options.model), _model_params_json(options.model), now, now),
                )
        except sqlite3.IntegrityError as e:
            raise errors.ConfigurationError(f"Local agent {agent_id} already exists", is_retryable=False) from e
        agent = await self.get_agent(agent_id)
        run = await self.get_run(agent_id, run_id)
        assert agent is not None and run is not None
        return agent, run

    async def get_agent(self, agent_id: str) -> LocalAgentRecord | None:
        row = self._db.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return self._agent_from_row(row)

    async def list_agents(self, options: ListAgentsLocalOptions) -> ListResult[LocalAgentRecord]:
        limit, offset = _pagination(options.limit, options.cursor)
        rows = self._db.execute(
            "SELECT * FROM agents WHERE workspace_ref = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (self.workspace_ref, limit + 1, offset),
        ).fetchall()
        items = [self._agent_from_row(row) for row in rows[:limit]]
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return ListResult(items=[x for x in items if x is not None], nextCursor=next_cursor)

    async def archive_agent(self, agent_id: str) -> None:
        await self._set_agent_status(agent_id, "ARCHIVED")

    async def unarchive_agent(self, agent_id: str) -> None:
        await self._set_agent_status(agent_id, "IDLE")

    async def _set_agent_status(self, agent_id: str, status: str) -> None:
        with self._db:
            cur = self._db.execute(
                "UPDATE agents SET status = ?, updated_at = ? WHERE agent_id = ?",
                (status, _to_iso(), agent_id),
            )
        if cur.rowcount == 0:
            raise errors.AgentNotFoundError(f"Agent {agent_id} not found", is_retryable=False)

    async def delete_agent(self, agent_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM run_events WHERE run_id IN (SELECT run_id FROM runs WHERE agent_id = ?)", (agent_id,))
            self._db.execute("DELETE FROM runs WHERE agent_id = ?", (agent_id,))
            self._db.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))

    async def create_follow_up_run(self, agent_id: str, model: ModelSelection | None) -> LocalRunRecord:
        agent = await self.get_agent(agent_id)
        if not agent:
            raise errors.AgentNotFoundError(f"Agent {agent_id} not found", is_retryable=False)
        if agent.status == "ARCHIVED":
            raise errors.ConfigurationError(f"Cannot create follow-up run on archived agent {agent_id}.", is_retryable=False)
        if agent.activeRunId:
            active = await self.get_run(agent_id, agent.activeRunId)
            if active and active.status not in TERMINAL_LOCAL_STATUSES:
                raise errors.AgentBusyError(
                    f"Agent {agent_id} already has active run",
                    code="agent_busy",
                    is_retryable=False,
                )
        row = self._db.execute("SELECT COUNT(*) AS count FROM runs WHERE agent_id = ?", (agent_id,)).fetchone()
        turn_number = int(row["count"] or 0) + 1
        run_id = _run_id()
        now = _to_iso()
        with self._db:
            self._db.execute(
                "INSERT INTO runs (run_id, agent_id, turn_number, status, model, model_params_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, agent_id, turn_number, "QUEUED", _model_id(model), _model_params_json(model), now, now),
            )
            self._db.execute(
                "UPDATE agents SET active_run_id = ?, status = ?, updated_at = ? WHERE agent_id = ?",
                (run_id, "IDLE", now, agent_id),
            )
        run = await self.get_run(agent_id, run_id)
        assert run is not None
        return run

    async def get_run(self, agent_id: str, run_id: str) -> LocalRunRecord | None:
        row = self._db.execute(
            "SELECT * FROM runs WHERE agent_id = ? AND run_id = ?",
            (agent_id, run_id),
        ).fetchone()
        return self._run_from_row(row)

    async def find_run(self, run_id: str) -> LocalRunRecord:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ? LIMIT 1", (run_id,)).fetchone()
        run = self._run_from_row(row)
        if not run:
            raise errors.ConfigurationError(f"Run {run_id} not found", is_retryable=False)
        return run

    async def list_runs(self, agent_id: str, options: ListRunsLocalOptions) -> ListResult[LocalRunRecord]:
        limit, offset = _pagination(options.limit, options.cursor)
        rows = self._db.execute(
            "SELECT * FROM runs WHERE agent_id = ? ORDER BY turn_number ASC LIMIT ? OFFSET ?",
            (agent_id, limit + 1, offset),
        ).fetchall()
        items = [self._run_from_row(row) for row in rows[:limit]]
        next_cursor = str(offset + limit) if len(rows) > limit else None
        return ListResult(items=[x for x in items if x is not None], nextCursor=next_cursor)

    async def mark_run_starting(self, agent_id: str, run_id: str) -> None:
        now = _to_iso()
        with self._db:
            self._db.execute(
                "UPDATE runs SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ? WHERE agent_id = ? AND run_id = ?",
                ("RUNNING", now, now, agent_id, run_id),
            )
            self._db.execute(
                "UPDATE agents SET active_run_id = ?, status = CASE WHEN status = 'ARCHIVED' THEN status ELSE 'RUNNING' END, updated_at = ? WHERE agent_id = ?",
                (run_id, now, agent_id),
            )

    async def mark_run_terminal(self, agent_id: str, run_id: str, status: str, error_code: str | None = None) -> None:
        now = _to_iso()
        column = {"FINISHED": "finished_at", "ERROR": "finished_at", "CANCELLED": "cancelled_at", "EXPIRED": "expired_at"}.get(
            status,
            "finished_at",
        )
        with self._db:
            self._db.execute(
                f"UPDATE runs SET status = ?, error_code = ?, {column} = COALESCE({column}, ?), updated_at = ? WHERE agent_id = ? AND run_id = ?",
                (status, error_code, now, now, agent_id, run_id),
            )
            self._db.execute(
                "UPDATE agents SET active_run_id = NULL, status = CASE WHEN status = 'ARCHIVED' THEN status ELSE 'IDLE' END, updated_at = ? WHERE agent_id = ?",
                (now, agent_id),
            )

    async def expire_active_run(self, agent_id: str) -> None:
        agent = await self.get_agent(agent_id)
        if not agent or not agent.activeRunId:
            return
        run = await self.get_run(agent_id, agent.activeRunId)
        if run and run.status not in TERMINAL_LOCAL_STATUSES:
            await self.mark_run_terminal(agent_id, run.runId, "EXPIRED", "force_send")
            await self.append_sdk_message(_status_sdk_message(agent_id, run.runId, "EXPIRED"))

    async def append_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: Any,
        *,
        payload_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> LocalRunEventRecord:
        if idempotency_key:
            row = self._db.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND idempotency_key = ? LIMIT 1",
                (run_id, idempotency_key),
            ).fetchone()
            if row:
                return self._event_from_row(row)
        row = self._db.execute("SELECT MAX(seq) AS max_seq FROM run_events WHERE run_id = ?", (run_id,)).fetchone()
        seq = int(row["max_seq"] or 0) + 1
        offset = str(seq)
        created_at = _to_iso()
        with self._db:
            self._db.execute(
                "INSERT INTO run_events (run_id, seq, offset, event_type, payload_json, payload_ref, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, seq, offset, event_type, _json_dumps(payload), payload_ref, idempotency_key, created_at),
            )
        stored = self._db.execute("SELECT * FROM run_events WHERE run_id = ? AND seq = ?", (run_id, seq)).fetchone()
        return self._event_from_row(stored)

    async def append_sdk_message(self, message: messages.SDKMessage) -> LocalRunEventRecord:
        return await self.append_run_event(
            str(message["run_id"]),
            messages.LOCAL_RUN_STREAM_EVENT_TYPE,
            messages.create_sdk_message_run_stream_event(message),
        )

    async def append_terminal_event(
        self,
        agent_id: str,
        run_id: str,
        status: Literal["finished", "error", "cancelled"],
        *,
        error_code: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schemaVersion": messages.LOCAL_RUN_STREAM_SCHEMA_VERSION,
            "type": "result",
            "agentId": agent_id,
            "runId": run_id,
            "status": status,
        }
        if error_code:
            payload["errorCode"] = error_code
        await self.append_run_event(run_id, messages.LOCAL_RUN_STREAM_EVENT_TYPE, payload)
        await self.append_run_event(
            run_id,
            messages.LOCAL_RUN_STREAM_EVENT_TYPE,
            {
                "schemaVersion": messages.LOCAL_RUN_STREAM_SCHEMA_VERSION,
                "type": "done",
                "agentId": agent_id,
                "runId": run_id,
            },
        )

    async def list_run_events(
        self,
        run_id: str,
        *,
        after_offset: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[LocalRunEventRecord], str | None]:
        try:
            after = int(after_offset or "0")
        except ValueError as e:
            raise errors.ConfigurationError(f"Invalid run event offset {after_offset}", is_retryable=False) from e
        page_limit = limit if isinstance(limit, int) and limit > 0 else 100
        rows = self._db.execute(
            "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
            (run_id, after, page_limit + 1),
        ).fetchall()
        events = [self._event_from_row(row) for row in rows[:page_limit]]
        next_offset = events[-1].offset if len(rows) > page_limit and events else None
        return events, next_offset

    async def list_agent_messages(self, agent_id: str, options: GetAgentMessagesOptions) -> list[dict[str, Any]]:
        run_rows = self._db.execute("SELECT run_id FROM runs WHERE agent_id = ? ORDER BY turn_number ASC", (agent_id,)).fetchall()
        out: list[dict[str, Any]] = []
        for run_row in run_rows:
            events, _ = await self.list_run_events(str(run_row["run_id"]), limit=10000)
            for event in events:
                if event.eventType != messages.LOCAL_RUN_STREAM_EVENT_TYPE:
                    continue
                try:
                    sdk_message = messages.decode_sdk_message_run_stream_event(event.payload)
                except errors.ConfigurationError:
                    continue
                if sdk_message.get("type") in ("user", "assistant"):
                    out.append(
                        {
                            "type": sdk_message["type"],
                            "uuid": f"{agent_id}:{len(out)}",
                            "agent_id": agent_id,
                            "message": sdk_message.get("message"),
                        }
                    )
        offset = options.offset or 0
        end = offset + options.limit if options.limit is not None else None
        return out[offset:end]

    async def collect_usage(self, agent_id: str, run_id: str | None = None) -> AgentUsage:
        if run_id and run_id.startswith("run-"):
            raise errors.ConfigurationError(
                "Local getUsage runId must be a usage UUID, not a client-side run-<uuid> label.",
                is_retryable=False,
            )
        run_rows = self._db.execute("SELECT run_id FROM runs WHERE agent_id = ? ORDER BY turn_number ASC", (agent_id,)).fetchall()
        entries: list[RunUsage] = []
        usages: list[TokenUsage] = []
        for run_row in run_rows:
            events, _ = await self.list_run_events(str(run_row["run_id"]), limit=10000)
            for event in events:
                if event.eventType != messages.LOCAL_RUN_STREAM_EVENT_TYPE:
                    continue
                try:
                    sdk_message = messages.decode_sdk_message_run_stream_event(event.payload)
                except errors.ConfigurationError:
                    continue
                if sdk_message.get("type") != "usage":
                    continue
                usage = token_usage_from_mapping(sdk_message.get("usage"))
                if usage is None:
                    continue
                usage_id = str((sdk_message.get("usage") or {}).get("runId") or event.offset)
                if run_id and usage_id != run_id:
                    continue
                usages.append(usage)
                entries.append(RunUsage(runId=usage_id, usage=usage))
        total = sum_token_usage(usages)
        if total is None:
            return empty_agent_usage()
        return AgentUsage(usage=total, runs=entries)


class ProtocolRuntimeStore:
    """High-level local runtime API over a TypeScript-shaped `LocalAgentStore`."""

    def __init__(self, store: Any, workspace_ref: str) -> None:
        self._store = store
        self.workspace_ref = workspace_ref

    def close(self) -> None:
        return

    def _agent_from_doc(self, doc: Any) -> LocalAgentRecord | None:
        if doc is None:
            return None
        data = _doc_dict(doc)
        status = str(data.get("status") or "idle")
        return LocalAgentRecord(
            agentId=str(data["agentId"]),
            workspaceRef=str(data.get("cwd") or self.workspace_ref),
            status=_AGENT_STATUS_FROM_PROTOCOL.get(status, status.upper()),
            activeRunId=data.get("activeRunId"),
            name=str(data.get("name") or "New Agent"),
            createdAt=cast(datetime, _datetime_from_ms(data.get("createdAt")) or _now()),
            updatedAt=cast(datetime, _datetime_from_ms(data.get("updatedAt")) or _now()),
            metadata=dict(data.get("sdkMetadata") or {}),
        )

    def _run_from_doc(self, doc: Any) -> LocalRunRecord | None:
        if doc is None:
            return None
        data = _doc_dict(doc)
        status = str(data.get("status") or "queued")
        mapped = _RUN_STATUS_FROM_PROTOCOL.get(status, status.upper())
        ended = _datetime_from_ms(data.get("endedAt"))
        model_raw = data.get("model")
        model = None
        if model_raw:
            try:
                model = ModelSelection.model_validate(model_raw)
            except Exception:
                model = _model_from_row(str(model_raw.get("id") if isinstance(model_raw, dict) else model_raw), None)
        return LocalRunRecord(
            runId=str(data["runId"]),
            agentId=str(data["agentId"]),
            turnNumber=int(data.get("turnNumber") or 1),
            status=mapped,
            model=model,
            errorCode=data.get("error"),
            createdAt=cast(datetime, _datetime_from_ms(data.get("createdAt")) or _now()),
            updatedAt=cast(datetime, _datetime_from_ms(data.get("updatedAt")) or _now()),
            startedAt=_datetime_from_ms(data.get("startedAt")),
            finishedAt=ended if mapped in ("FINISHED", "ERROR") else None,
            cancelledAt=ended if mapped == "CANCELLED" else None,
            expiredAt=ended if mapped == "EXPIRED" else None,
        )

    def _event_from_doc(self, doc: Any) -> LocalRunEventRecord:
        data = _doc_dict(doc)
        created = data.get("createdAt")
        if isinstance(created, str):
            created_at = cast(datetime, _from_iso(created) or _now())
        else:
            created_at = cast(datetime, _datetime_from_ms(created) or _now())
        return LocalRunEventRecord(
            runId=str(data["runId"]),
            seq=int(data.get("seq") or 0),
            offset=str(data.get("offset") or data.get("seq") or "0"),
            eventType=str(data.get("eventType") or ""),
            payload=data.get("payload"),
            payloadRef=data.get("payloadRef"),
            idempotencyKey=data.get("idempotencyKey"),
            createdAt=created_at,
        )

    def _model_doc(self, model: ModelSelection | None) -> dict[str, Any] | None:
        if model is None:
            return None
        return model.model_dump(exclude_none=True)

    async def _list_all_runs(self, agent_id: str) -> list[Any]:
        items: list[Any] = []
        cursor: str | None = None
        while True:
            filt: dict[str, Any] = {"agentIds": [agent_id], "limit": 1000}
            if cursor:
                filt["cursor"] = cursor
            result = await self._store.runs.list({"filter": filt})
            page = list(_field_items(result))
            items.extend(page)
            cursor = _doc_dict(result).get("nextCursor")
            if not cursor:
                return items

    async def create_agent(self, options: AgentOptions) -> tuple[LocalAgentRecord, LocalRunRecord]:
        agent_id = options.agentId or _agent_id()
        name = (options.name or "").strip() or "New Agent"
        now = _now_ms()
        run_id = _run_id()
        agent_doc = {
            "agentId": agent_id,
            "cwd": self.workspace_ref,
            "status": "idle",
            "activeRunId": run_id,
            "name": name,
            "createdAt": now,
            "updatedAt": now,
            "sdkMetadata": {},
        }
        run_doc = {
            "runId": run_id,
            "agentId": agent_id,
            "turnNumber": 1,
            "status": "queued",
            "model": self._model_doc(options.model),
            "createdAt": now,
            "updatedAt": now,
        }
        try:
            stored_agent = await self._store.agents.create({"agent": agent_doc})
            stored_run = await self._store.runs.create({"run": run_doc})
        except Exception as exc:
            message = str(exc)
            if "already exists" in message:
                raise errors.ConfigurationError(f"Local agent {agent_id} already exists", is_retryable=False) from exc
            raise
        agent = self._agent_from_doc(stored_agent)
        run = self._run_from_doc(stored_run)
        assert agent is not None and run is not None
        return agent, run

    async def get_agent(self, agent_id: str) -> LocalAgentRecord | None:
        return self._agent_from_doc(await self._store.agents.get({"agentId": agent_id}))

    async def list_agents(self, options: ListAgentsLocalOptions) -> ListResult[LocalAgentRecord]:
        filt: dict[str, Any] = {"cwd": self.workspace_ref}
        if options.limit is not None:
            filt["limit"] = options.limit
        if options.cursor is not None:
            filt["cursor"] = options.cursor
        result = await self._store.agents.list({"filter": filt})
        items = [self._agent_from_doc(item) for item in _field_items(result)]
        return ListResult(items=[item for item in items if item is not None], nextCursor=_doc_dict(result).get("nextCursor"))

    async def archive_agent(self, agent_id: str) -> None:
        await self._set_agent_status(agent_id, "archived")

    async def unarchive_agent(self, agent_id: str) -> None:
        await self._set_agent_status(agent_id, "idle")

    async def _set_agent_status(self, agent_id: str, status: str) -> None:
        doc = await self._store.agents.get({"agentId": agent_id})
        if doc is None:
            raise errors.AgentNotFoundError(f"Agent {agent_id} not found", is_retryable=False)
        updated = {**_doc_dict(doc), "status": status, "updatedAt": _now_ms()}
        await self._store.agents.update({"agent": updated})

    async def delete_agent(self, agent_id: str) -> None:
        await self._store.runs.delete({"filter": {"agentIds": [agent_id]}})
        await self._store.checkpoints.delete({"filter": {"agentIds": [agent_id]}})
        try:
            await self._store.agents.delete({"filter": {"agentIds": [agent_id]}})
        except Exception as exc:
            if "No agents matched delete filter" not in str(exc):
                raise

    async def create_follow_up_run(self, agent_id: str, model: ModelSelection | None) -> LocalRunRecord:
        agent = await self.get_agent(agent_id)
        if not agent:
            raise errors.AgentNotFoundError(f"Agent {agent_id} not found", is_retryable=False)
        if agent.status == "ARCHIVED":
            raise errors.ConfigurationError(f"Cannot create follow-up run on archived agent {agent_id}.", is_retryable=False)
        if agent.activeRunId:
            active = await self.get_run(agent_id, agent.activeRunId)
            if active and active.status not in TERMINAL_LOCAL_STATUSES:
                raise errors.AgentBusyError(
                    f"Agent {agent_id} already has active run",
                    code="agent_busy",
                    is_retryable=False,
                )
        turn_number = len(await self._list_all_runs(agent_id)) + 1
        run_id = _run_id()
        now = _now_ms()
        stored = await self._store.runs.create(
            {
                "run": {
                    "runId": run_id,
                    "agentId": agent_id,
                    "turnNumber": turn_number,
                    "status": "queued",
                    "model": self._model_doc(model),
                    "createdAt": now,
                    "updatedAt": now,
                }
            }
        )
        agent_doc = await self._store.agents.get({"agentId": agent_id})
        if agent_doc is not None:
            updated = {**_doc_dict(agent_doc), "activeRunId": run_id, "updatedAt": now}
            if updated.get("status") != "archived":
                updated["status"] = "idle"
            await self._store.agents.update({"agent": updated})
        run = self._run_from_doc(stored)
        assert run is not None
        return run

    async def get_run(self, agent_id: str, run_id: str) -> LocalRunRecord | None:
        return self._run_from_doc(await self._store.runs.get({"agentId": agent_id, "runId": run_id}))

    async def find_run(self, run_id: str) -> LocalRunRecord:
        result = await self._store.runs.list({"filter": {"runIds": [run_id], "limit": 1}})
        items = _field_items(result)
        run = self._run_from_doc(items[0]) if items else None
        if not run:
            raise errors.ConfigurationError(f"Run {run_id} not found", is_retryable=False)
        return run

    async def list_runs(self, agent_id: str, options: ListRunsLocalOptions) -> ListResult[LocalRunRecord]:
        filt: dict[str, Any] = {"agentIds": [agent_id]}
        if options.limit is not None:
            filt["limit"] = options.limit
        if options.cursor is not None:
            filt["cursor"] = options.cursor
        result = await self._store.runs.list({"filter": filt})
        items = [self._run_from_doc(item) for item in _field_items(result)]
        return ListResult(items=[item for item in items if item is not None], nextCursor=_doc_dict(result).get("nextCursor"))

    async def mark_run_starting(self, agent_id: str, run_id: str) -> None:
        now = _now_ms()
        run_doc = await self._store.runs.get({"agentId": agent_id, "runId": run_id})
        if run_doc is not None:
            updated = {**_doc_dict(run_doc), "status": "running", "updatedAt": now}
            if not updated.get("startedAt"):
                updated["startedAt"] = now
            await self._store.runs.update({"run": updated})
        agent_doc = await self._store.agents.get({"agentId": agent_id})
        if agent_doc is not None:
            updated_agent = {**_doc_dict(agent_doc), "activeRunId": run_id, "updatedAt": now}
            if updated_agent.get("status") != "archived":
                updated_agent["status"] = "running"
            await self._store.agents.update({"agent": updated_agent})

    async def mark_run_terminal(self, agent_id: str, run_id: str, status: str, error_code: str | None = None) -> None:
        now = _now_ms()
        run_doc = await self._store.runs.get({"agentId": agent_id, "runId": run_id})
        if run_doc is not None:
            updated = {
                **_doc_dict(run_doc),
                "status": _RUN_STATUS_TO_PROTOCOL.get(status, status.lower()),
                "error": error_code,
                "endedAt": now,
                "updatedAt": now,
            }
            await self._store.runs.update({"run": updated})
        agent_doc = await self._store.agents.get({"agentId": agent_id})
        if agent_doc is not None:
            updated_agent = {**_doc_dict(agent_doc), "activeRunId": None, "updatedAt": now}
            if updated_agent.get("status") != "archived":
                updated_agent["status"] = "idle"
            await self._store.agents.update({"agent": updated_agent})

    async def expire_active_run(self, agent_id: str) -> None:
        agent = await self.get_agent(agent_id)
        if not agent or not agent.activeRunId:
            return
        run = await self.get_run(agent_id, agent.activeRunId)
        if run and run.status not in TERMINAL_LOCAL_STATUSES:
            await self.mark_run_terminal(agent_id, run.runId, "EXPIRED", "force_send")
            await self.append_sdk_message(_status_sdk_message(agent_id, run.runId, "EXPIRED"))

    async def append_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: Any,
        *,
        payload_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> LocalRunEventRecord:
        stored = await self._store.runEvents.append(
            {
                "runId": run_id,
                "eventType": event_type,
                "payload": payload,
                "payloadRef": payload_ref,
                "idempotencyKey": idempotency_key,
            }
        )
        return self._event_from_doc(stored)

    async def append_sdk_message(self, message: messages.SDKMessage) -> LocalRunEventRecord:
        return await self.append_run_event(
            str(message["run_id"]),
            messages.LOCAL_RUN_STREAM_EVENT_TYPE,
            messages.create_sdk_message_run_stream_event(message),
        )

    async def append_terminal_event(
        self,
        agent_id: str,
        run_id: str,
        status: Literal["finished", "error", "cancelled"],
        *,
        error_code: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "schemaVersion": messages.LOCAL_RUN_STREAM_SCHEMA_VERSION,
            "type": "result",
            "agentId": agent_id,
            "runId": run_id,
            "status": status,
        }
        if error_code:
            payload["errorCode"] = error_code
        await self.append_run_event(run_id, messages.LOCAL_RUN_STREAM_EVENT_TYPE, payload)
        await self.append_run_event(
            run_id,
            messages.LOCAL_RUN_STREAM_EVENT_TYPE,
            {
                "schemaVersion": messages.LOCAL_RUN_STREAM_SCHEMA_VERSION,
                "type": "done",
                "agentId": agent_id,
                "runId": run_id,
            },
        )

    async def list_run_events(
        self,
        run_id: str,
        *,
        after_offset: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[LocalRunEventRecord], str | None]:
        result = await self._store.runEvents.list({"runId": run_id, "afterOffset": after_offset, "limit": limit})
        data = _doc_dict(result)
        events = [self._event_from_doc(item) for item in data.get("items") or []]
        return events, data.get("nextOffset")

    async def list_agent_messages(self, agent_id: str, options: GetAgentMessagesOptions) -> list[dict[str, Any]]:
        runs = await self._list_all_runs(agent_id)
        out: list[dict[str, Any]] = []
        for run_doc in runs:
            run_id = str(_doc_dict(run_doc).get("runId"))
            events, _ = await self.list_run_events(run_id, limit=10000)
            for event in events:
                if event.eventType != messages.LOCAL_RUN_STREAM_EVENT_TYPE:
                    continue
                try:
                    sdk_message = messages.decode_sdk_message_run_stream_event(event.payload)
                except errors.ConfigurationError:
                    continue
                if sdk_message.get("type") in ("user", "assistant"):
                    out.append(
                        {
                            "type": sdk_message["type"],
                            "uuid": f"{agent_id}:{len(out)}",
                            "agent_id": agent_id,
                            "message": sdk_message.get("message"),
                        }
                    )
        offset = options.offset or 0
        end = offset + options.limit if options.limit is not None else None
        return out[offset:end]

    async def collect_usage(self, agent_id: str, run_id: str | None = None) -> AgentUsage:
        if run_id and run_id.startswith("run-"):
            raise errors.ConfigurationError(
                "Local getUsage runId must be a usage UUID, not a client-side run-<uuid> label.",
                is_retryable=False,
            )
        runs = await self._list_all_runs(agent_id)
        entries: list[RunUsage] = []
        usages: list[TokenUsage] = []
        for run_doc in runs:
            events, _ = await self.list_run_events(str(_doc_dict(run_doc).get("runId")), limit=10000)
            for event in events:
                if event.eventType != messages.LOCAL_RUN_STREAM_EVENT_TYPE:
                    continue
                try:
                    sdk_message = messages.decode_sdk_message_run_stream_event(event.payload)
                except errors.ConfigurationError:
                    continue
                if sdk_message.get("type") != "usage":
                    continue
                usage = token_usage_from_mapping(sdk_message.get("usage"))
                if usage is None:
                    continue
                usage_id = str((sdk_message.get("usage") or {}).get("runId") or event.offset)
                if run_id and usage_id != run_id:
                    continue
                usages.append(usage)
                entries.append(RunUsage(runId=usage_id, usage=usage))
        total = sum_token_usage(usages)
        if total is None:
            return empty_agent_usage()
        return AgentUsage(usage=total, runs=entries)


def _field_items(result: Any) -> list[Any]:
    items = _doc_dict(result).get("items")
    return list(items) if items else []


class RunEventTailer:
    def __init__(self, store: LocalAgentStore) -> None:
        self.store = store

    async def stream_run_events(
        self,
        run_id: str,
        *,
        after_offset: str | None = None,
        limit: int | None = None,
        mode: Literal["replay", "tail", "replay-and-tail"] = "replay",
        poll_interval_ms: int = 250,
    ) -> AsyncIterator[messages.SDKMessage]:
        offset = after_offset
        if mode == "tail" and offset is None:
            offset = await self._drain_existing(run_id, limit)
        while True:
            events, next_offset = await self.store.list_run_events(run_id, after_offset=offset, limit=limit)
            saw_terminal = False
            for event in events:
                offset = event.offset
                if event.eventType != messages.LOCAL_RUN_STREAM_EVENT_TYPE:
                    continue
                try:
                    decoded = messages.decode_local_run_stream_event(event.payload)
                except errors.ConfigurationError:
                    continue
                message = messages.local_run_stream_event_to_sdk_message(decoded)
                if message:
                    yield message
                if messages.is_terminal_local_run_stream_event(decoded):
                    saw_terminal = True
            if saw_terminal:
                return
            if next_offset:
                offset = next_offset
                continue
            if mode == "replay":
                return
            run = await self.store.find_run(run_id)
            if run.status in TERMINAL_LOCAL_STATUSES:
                return
            await asyncio.sleep(max(poll_interval_ms, 1) / 1000)

    async def stream_observe_events(
        self,
        run_id: str,
        *,
        after_offset: str | None = None,
        limit: int | None = None,
        mode: Literal["replay", "tail", "replay-and-tail"] = "replay",
        poll_interval_ms: int = 250,
    ) -> AsyncIterator[RunStreamEvent]:
        offset = after_offset
        if mode == "tail" and offset is None:
            offset = await self._drain_existing(run_id, limit)
        while True:
            events, next_offset = await self.store.list_run_events(run_id, after_offset=offset, limit=limit)
            saw_terminal = False
            for event in events:
                offset = event.offset
                if event.eventType != messages.LOCAL_RUN_STREAM_EVENT_TYPE:
                    continue
                converted = run_stream_event_from_local_payload(event.payload, event.offset)
                if converted is None:
                    continue
                yield converted
                if converted.kind in ("done", "result"):
                    saw_terminal = True
                message = converted.sdk_message if converted.kind == "sdk_message" else None
                if (
                    isinstance(message, dict)
                    and message.get("type") == "status"
                    and message.get("status") in ("FINISHED", "ERROR", "CANCELLED", "EXPIRED")
                ):
                    saw_terminal = True
            if saw_terminal:
                return
            if next_offset:
                offset = next_offset
                continue
            if mode == "replay":
                return
            run = await self.store.find_run(run_id)
            if run.status in TERMINAL_LOCAL_STATUSES:
                return
            await asyncio.sleep(max(poll_interval_ms, 1) / 1000)

    async def _drain_existing(self, run_id: str, limit: int | None) -> str | None:
        offset: str | None = None
        while True:
            events, next_offset = await self.store.list_run_events(run_id, after_offset=offset, limit=limit)
            if events:
                offset = events[-1].offset
            if not next_offset:
                return offset
            offset = next_offset


class LocalRun:
    def __init__(
        self,
        store: LocalAgentStore,
        record: LocalRunRecord,
        *,
        own_store: bool = False,
        request_id: str | None = None,
        usage: TokenUsage | None = None,
        executor: LocalAgentExecutor | None = None,
    ) -> None:
        self._store = store
        self._own_store = own_store
        self.id = record.runId
        self.agent_id = record.agentId
        self._request_id = request_id
        self._usage = usage
        self._status = _run_status_to_sdk(record.status)
        self._model = record.model
        self._created_at = _ms(record.createdAt)
        self._listeners: set[Callable[[RunStatus], None]] = set()
        self._error: RunError | None = None
        self._executor = executor
        if record.status == "ERROR":
            self._error = RunError(message=record.errorCode or "error", code=record.errorCode)

    @property
    def agentId(self) -> str:  # noqa: N802
        return self.agent_id

    @property
    def run_id(self) -> str:
        return self.id

    @property
    def requestId(self) -> str | None:  # noqa: N802
        return self._request_id

    @property
    def usage(self) -> TokenUsage | None:
        return self._usage

    @property
    def status(self) -> RunStatus:
        return self._status

    @property
    def result(self) -> str | None:
        return None

    @property
    def error(self) -> RunError | None:
        return self._error

    @property
    def durationMs(self) -> int | None:  # noqa: N802
        return None

    @property
    def git(self) -> None:
        return None

    @property
    def model(self) -> ModelSelection | None:
        return self._model

    @property
    def createdAt(self) -> int | None:  # noqa: N802
        return self._created_at

    @property
    def duration_ms(self) -> int | None:
        return self.durationMs

    @property
    def created_at(self) -> str | None:
        return None if self.createdAt is None else str(self.createdAt)

    def supports(self, operation: str) -> bool:
        return operation in ("stream", "wait", "conversation", "cancel", "observe")

    def unsupported_reason(self, operation: str) -> str | None:
        if self.supports(operation):
            return None
        if operation == "wait":
            return "Cannot wait on a detached running local run"
        return f'Unknown run operation "{operation}"'

    def unsupportedReason(self, operation: str) -> str | None:  # noqa: N802
        return self.unsupported_reason(operation)

    def on_did_change_status(self, listener: Callable[[RunStatus], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        def _off() -> None:
            self._listeners.discard(listener)

        return _off

    def onDidChangeStatus(self, listener: Callable[[RunStatus], None]) -> Callable[[], None]:
        return self.on_did_change_status(listener)

    def _set_status(self, status: RunStatus) -> None:
        if self._status == status:
            return
        self._status = status
        for listener in list(self._listeners):
            listener(status)

    def stream(self) -> Any:
        return iterate_async(self._stream_async())

    async def _stream_async(self) -> AsyncIterator[messages.SDKMessage]:
        ensure_run_operation(self, "stream")
        mode: Literal["replay", "replay-and-tail"] = "replay-and-tail" if self._status == "running" else "replay"
        async for message in RunEventTailer(self._store).stream_run_events(self.id, mode=mode):
            if message.get("type") == "status":
                status = message.get("status")
                if status in ("FINISHED", "ERROR", "CANCELLED", "EXPIRED"):
                    self._set_status(_run_status_to_sdk(str(status)))
                if status == "ERROR":
                    text = message.get("message")
                    self._error = RunError(message=str(text or "error"), code=self._error.code if self._error else None)
            if message.get("type") == "usage":
                usage = token_usage_from_mapping(message.get("usage"))
                if usage is not None:
                    self._usage = usage
            if message.get("type") == "request" and message.get("request_id"):
                self._request_id = str(message["request_id"])
            yield message

    def messages(self) -> Any:
        return iterate_async(self._messages_async())

    async def _messages_async(self) -> AsyncIterator[messages.SDKMessage]:
        async for message in iter_run_messages(self):
            yield message

    def iter_text(self) -> Any:
        return iterate_async(self._iter_text_async())

    async def _iter_text_async(self) -> AsyncIterator[str]:
        async for chunk in iter_run_text(self):
            yield chunk

    def text(self) -> Any:
        return run_sync_or_awaitable(self._text_async())

    async def _text_async(self) -> str:
        return await run_text(self)

    def events(self) -> Any:
        return iterate_async(self._events_async())

    async def _events_async(self) -> AsyncIterator[dict[str, Any]]:
        async for event in iter_run_events(self):
            yield event

    def conversation(self) -> Any:
        return run_sync_or_awaitable(self._conversation_async())

    async def _conversation_async(self) -> list[dict[str, Any]]:
        ensure_run_operation(self, "conversation")
        return await accumulate_sdk_message_stream(self._stream_async())

    def conversation_json(self) -> Any:
        return run_sync_or_awaitable(self._conversation_json_async())

    async def _conversation_json_async(self) -> str:
        return conversation_json_from_turns(await self._conversation_async())

    def observe(self, *, after_offset: str | None = None) -> Any:
        return iterate_async(self._observe_async(after_offset=after_offset))

    async def _observe_async(self, *, after_offset: str | None = None) -> AsyncIterator[RunStreamEvent]:
        mode: Literal["replay", "replay-and-tail"] = "replay-and-tail" if self._status == "running" else "replay"
        async for event in RunEventTailer(self._store).stream_observe_events(self.id, after_offset=after_offset, mode=mode):
            yield event

    def wait(self) -> Any:
        return run_sync_or_awaitable(self._wait_async())

    async def _wait_async(self) -> RunResult:
        ensure_run_operation(self, "wait")
        parts: list[str] = []
        async for message in self._stream_async():
            chunk = assistant_text_from_message(message)
            if chunk:
                parts.append(chunk)
        run = await self._store.find_run(self.id)
        self._set_status(_run_status_to_sdk(run.status))
        error: RunError | None = None
        if self._status == "error":
            message = (self._error.message if self._error else None) or run.errorCode or "error"
            error = RunError(message=message, code=run.errorCode)
            self._error = error
        return RunResult(
            id=self.id,
            agent_id=self.agent_id,
            request_id=self._request_id,
            status=cast(RunResultStatus, self._status),
            result="".join(parts),
            model=self._model,
            error=error,
            usage=self._usage,
        )

    def cancel(self) -> Any:
        return run_sync_or_awaitable(self._cancel_async())

    async def _cancel_async(self) -> None:
        ensure_run_operation(self, "cancel")
        run = await self._store.find_run(self.id)
        if run.status in TERMINAL_LOCAL_STATUSES:
            return
        await self._store.mark_run_terminal(self.agent_id, self.id, "CANCELLED")
        await self._store.append_sdk_message(_status_sdk_message(self.agent_id, self.id, "CANCELLED"))
        await self._store.append_terminal_event(self.agent_id, self.id, "cancelled")
        self._set_status("cancelled")

    async def steer(self, text: str) -> SteerAckOutcome:
        if not str(text).strip() or self._executor is None:
            return "revert_to_followup"
        return await self._executor.steer(text)

    async def aclose(self) -> None:
        if self._own_store:
            self._store.close()


class LocalAgent:
    def __init__(
        self,
        store: LocalAgentStore,
        record: LocalAgentRecord,
        options: AgentOptions,
        pending_run_id: str | None,
        *,
        owns_store: bool = True,
    ) -> None:
        self._store = store
        self._record = record
        self._options = options
        self._pending_run_id = pending_run_id
        self._owns_store = owns_store
        self._model = options.model
        self.agent_id = record.agentId
        self._tasks: set[asyncio.Task[Any]] = set()
        self._checkpoint: dict[str, Any] | None = None
        self._conversation_id = record.agentId
        self._closed = False

    @property
    def agentId(self) -> str:  # noqa: N802
        return self.agent_id

    @property
    def model(self) -> ModelSelection | None:
        return self._model

    def send(self, message: str | SDKUserMessage, options: SendOptions | None = None) -> Any:
        return run_sync_or_awaitable(self._send_async(message, options))

    async def _send_async(self, message: str | SDKUserMessage, options: SendOptions | None = None) -> LocalRun:
        model = (options.model if options and options.model is not None else None) or self._model
        if model is None:
            raise errors.ConfigurationError(
                'Local SDK agents require an explicit `model`. Pass `model={"id": "<model-id>"}` to Agent.create() or send().',
                is_retryable=False,
            )
        if options and options.local and options.local.force:
            await self._store.expire_active_run(self.agent_id)
        if self._pending_run_id:
            run = await self._store.get_run(self.agent_id, self._pending_run_id)
            self._pending_run_id = None
            if run is None or run.status != "QUEUED":
                run = await self._store.create_follow_up_run(self.agent_id, model)
        else:
            run = await self._store.create_follow_up_run(self.agent_id, model)
        self._model = model
        text = _user_text(message)
        run_options = self._options
        updates: dict[str, Any] = {}
        if options is not None and options.mcpServers is not None:
            updates["mcpServers"] = options.mcpServers
        if options is not None and options.mode is not None:
            updates["mode"] = options.mode
        if options is not None and options.local is not None and options.local.customTools is not None:
            local = (run_options.local or LocalAgentOptions()).model_copy(update={"customTools": options.local.customTools})
            updates["local"] = local
        if updates:
            run_options = self._options.model_copy(update=updates)
        request_id = str(uuid.uuid4())
        await self._store.mark_run_starting(self.agent_id, run.runId)
        await self._store.append_sdk_message(
            cast(
                messages.SDKMessage,
                {"type": "request", "agent_id": self.agent_id, "run_id": run.runId, "request_id": request_id},
            )
        )
        await self._store.append_sdk_message(_user_sdk_message(self.agent_id, run.runId, text))
        await self._store.append_sdk_message(_status_sdk_message(self.agent_id, run.runId, "RUNNING"))
        executor = LocalAgentExecutor(
            store=self._store,
            agent_id=self.agent_id,
            run_id=run.runId,
            options=run_options,
            model=model,
            prompt=text,
            conversation_id=self._conversation_id,
            request_id=request_id,
            checkpoint=self._checkpoint,
        )

        async def _drive() -> None:
            try:
                await executor.run()
                self._checkpoint = executor.checkpoint
            except errors.CursorAgentError:
                raise
            except Exception:
                pass

        task = asyncio.create_task(_drive())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await executor.wait_until_started()
        if not executor.generation_active:
            await task
        elif task.done():
            exc = task.exception()
            if exc is not None:
                raise exc
        refreshed = await self._store.get_run(self.agent_id, run.runId)
        assert refreshed is not None
        return LocalRun(self._store, refreshed, request_id=request_id, executor=executor)

    def close(self) -> None:
        from opencursor._sync_runtime import loop_running

        if self._closed:
            return
        if not loop_running():
            close_sync(self.aclose())
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._owns_store:
            self._store.close()

    def __enter__(self) -> LocalAgent:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def reload(self) -> Any:
        return run_sync_or_awaitable(self._reload_async())

    async def _reload_async(self) -> None:
        return

    def list_artifacts(self) -> Any:
        return run_sync_or_awaitable(self._list_artifacts_async())

    async def _list_artifacts_async(self) -> list[SDKArtifact]:
        return []

    def listArtifacts(self) -> Any:  # noqa: N802
        return self.list_artifacts()

    def download_artifact(self, path: str) -> Any:
        return run_sync_or_awaitable(self._download_artifact_async(path))

    async def _download_artifact_async(self, path: str) -> bytes:
        raise errors.ConfigurationError("Artifacts are not implemented for local SDK agents yet", is_retryable=False)

    def downloadArtifact(self, path: str) -> Any:  # noqa: N802
        return self.download_artifact(path)

    def getUsage(self, options: GetUsageOptions | None = None) -> Any:  # noqa: N802
        return run_sync_or_awaitable(self._get_usage_async(options))

    async def _get_usage_async(self, options: GetUsageOptions | None = None) -> AgentUsage:
        run_id = options.runId if options else None
        return await self._store.collect_usage(self.agent_id, run_id)

    def get_usage(self, options: GetUsageOptions | None = None) -> Any:
        return self.getUsage(options)

    def list_messages(self, options: GetAgentMessagesOptions | Mapping[str, Any] | None = None) -> Any:
        return run_sync_or_awaitable(self._list_messages_async(options))

    async def _list_messages_async(self, options: GetAgentMessagesOptions | Mapping[str, Any] | None = None) -> list[AgentMessage]:
        if isinstance(options, GetAgentMessagesOptions) or options is None:
            opts = options or GetAgentMessagesOptions()
        else:
            opts = GetAgentMessagesOptions.model_validate(dict(options))
        rows = await self._store.list_agent_messages(self.agent_id, opts)
        return agent_messages_from_rows(rows)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._owns_store:
            self._store.close()

    async def __aenter__(self) -> LocalAgent:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()


def _to_sdk_agent_info(record: LocalAgentRecord, active_run: LocalRunRecord | None) -> SDKAgentInfoLocal:
    return SDKAgentInfoLocal(
        agentId=record.agentId,
        name=record.name,
        summary=record.name,
        lastModified=cast(int, _ms(record.updatedAt)),
        status=_public_agent_status(record.status, active_run.status if active_run else None),
        createdAt=_ms(record.createdAt),
        archived=record.status == "ARCHIVED",
        runtime="local",
        cwd=record.workspaceRef,
    )


def _store_for_workspace(workspace_ref: str, platform: dict[str, Any] | None = None) -> LocalAgentStore:
    state_root_raw = platform.get("stateRoot") if platform else None
    state_root = Path(str(state_root_raw)).expanduser() if state_root_raw else None
    return LocalAgentStore(workspace_ref, state_root)


def _open_runtime_store(workspace_ref: str, options: Any | None = None) -> tuple[Any, bool]:
    platform = getattr(options, "platform", None) if options is not None else None
    custom = resolve_local_agent_store(options)
    if custom is None and isinstance(platform, dict) and platform.get("localStore") is not None:
        custom = platform["localStore"]
    if custom is not None:
        if is_protocol_store(custom):
            return ProtocolRuntimeStore(custom, workspace_ref), False
        return custom, False
    plat = platform if isinstance(platform, dict) else None
    return _store_for_workspace(workspace_ref, plat), True


async def create_local_agent(options: AgentOptions) -> LocalAgent:
    SandboxRuntime.from_options(options).raise_if_requested_unsupported()
    workspace_ref = _workspace_ref_from_options(options)
    store, owns = _open_runtime_store(workspace_ref, options)
    try:
        agent, run = await store.create_agent(options)
    except Exception:
        if owns:
            store.close()
        raise
    return LocalAgent(store, agent, options, run.runId, owns_store=owns)


async def resume_local_agent(agent_id: str, options: AgentOptions) -> LocalAgent:
    SandboxRuntime.from_options(options).raise_if_requested_unsupported()
    workspace_ref = _workspace_ref_from_options(options)
    store, owns = _open_runtime_store(workspace_ref, options)
    agent = await store.get_agent(agent_id)
    if not agent:
        if owns:
            store.close()
        raise errors.AgentNotFoundError(f"Agent {agent_id} not found", is_retryable=False)
    merged = options.model_copy(update={"agentId": agent_id}, deep=True)
    return LocalAgent(store, agent, merged, None, owns_store=owns)


async def list_local_agents(options: ListAgentsLocalOptions) -> ListResult[SDKAgentInfoLocal]:
    store, owns = _open_runtime_store(_workspace_ref_from_cwd(options.cwd), options)
    try:
        result = await store.list_agents(options)
        items: list[SDKAgentInfoLocal] = []
        for agent in result.items:
            active = await store.get_run(agent.agentId, agent.activeRunId) if agent.activeRunId else None
            items.append(_to_sdk_agent_info(agent, active))
        return ListResult(items=items, nextCursor=result.nextCursor)
    finally:
        if owns:
            store.close()


async def get_local_agent(agent_id: str, options: GetAgentOptions | None = None) -> SDKAgentInfoLocal:
    store, owns = _open_runtime_store(_workspace_ref_from_options(options), options)
    try:
        agent = await store.get_agent(agent_id)
        if not agent:
            raise errors.AgentNotFoundError(f"Agent {agent_id} not found", is_retryable=False)
        active = await store.get_run(agent.agentId, agent.activeRunId) if agent.activeRunId else None
        return _to_sdk_agent_info(agent, active)
    finally:
        if owns:
            store.close()


async def list_local_runs(agent_id: str, options: ListRunsLocalOptions) -> ListResult[LocalRun]:
    store, _owns = _open_runtime_store(_workspace_ref_from_cwd(options.cwd), options)
    result = await store.list_runs(agent_id, options)
    return ListResult(items=[LocalRun(store, run) for run in result.items], nextCursor=result.nextCursor)


async def get_local_run(run_id: str, options: GetRunOptionsLocal) -> LocalRun:
    store, owns = _open_runtime_store(_workspace_ref_from_cwd(options.cwd), options)
    run = await store.find_run(run_id)
    return LocalRun(store, run, own_store=owns)


async def archive_local_agent(agent_id: str, options: AgentOperationOptions | None = None) -> None:
    store, owns = _open_runtime_store(_workspace_ref_from_options(options), options)
    try:
        await store.archive_agent(agent_id)
    finally:
        if owns:
            store.close()


async def unarchive_local_agent(agent_id: str, options: AgentOperationOptions | None = None) -> None:
    store, owns = _open_runtime_store(_workspace_ref_from_options(options), options)
    try:
        await store.unarchive_agent(agent_id)
    finally:
        if owns:
            store.close()


async def delete_local_agent(agent_id: str, options: AgentOperationOptions | None = None) -> None:
    store, owns = _open_runtime_store(_workspace_ref_from_options(options), options)
    try:
        await store.delete_agent(agent_id)
    finally:
        if owns:
            store.close()


async def list_local_agent_messages(agent_id: str, options: GetAgentMessagesOptions | None = None) -> list[dict[str, Any]]:
    opts = options or GetAgentMessagesOptions(runtime="local")
    store, owns = _open_runtime_store(_workspace_ref_from_cwd(opts.cwd), opts)
    try:
        return await store.list_agent_messages(agent_id, opts)
    finally:
        if owns:
            store.close()


async def get_local_usage(agent_id: str, options: GetUsageOptions | None = None) -> AgentUsage:
    opts = options or GetUsageOptions()
    store, owns = _open_runtime_store(_workspace_ref_from_cwd(opts.cwd), opts)
    try:
        return await store.collect_usage(agent_id, opts.runId)
    finally:
        if owns:
            store.close()
