from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, TypedDict, runtime_checkable

JSONL_LOCAL_AGENT_STORE_FILES = {
    "agents": "agents.ndjson",
    "runs": "runs.ndjson",
    "runEvents": "run_events.ndjson",
    "checkpoints": "checkpoints.ndjson",
}

LocalAgentStatus = str
LocalAgentRunStatus = str
LocalAgentRunEventOffset = str

_JSONL_WRITE_LOCK: asyncio.Lock | None = None


class LocalAgentCheckpointRef(TypedDict):
    schemaVersion: int
    rootBlobId: str


class LocalAgentDocument(TypedDict, total=False):
    agentId: str
    cwd: str
    status: str
    activeRunId: str | None
    name: str | None
    createdAt: int
    updatedAt: int
    latestCheckpoint: LocalAgentCheckpointRef | None
    sdkMetadata: dict[str, Any]


class LocalAgentRunDocument(TypedDict, total=False):
    runId: str
    requestId: str | None
    agentId: str
    turnNumber: int
    status: str
    model: Any
    result: str | None
    error: str | None
    usageRef: str | None
    usage: Any
    createdAt: int
    updatedAt: int
    startedAt: int | None
    endedAt: int | None
    startCheckpointRef: LocalAgentCheckpointRef | None
    latestCheckpointRef: LocalAgentCheckpointRef | None


class LocalAgentRunEventDocument(TypedDict, total=False):
    runId: str
    seq: int
    offset: str
    eventType: str
    payload: Any
    payloadRef: str | None
    idempotencyKey: str | None
    createdAt: int


class LocalAgentStoreListResult(TypedDict, total=False):
    items: list[Any]
    nextCursor: str


class LocalAgentRunEventListResult(TypedDict, total=False):
    items: list[LocalAgentRunEventDocument]
    nextOffset: str


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj[key] if key in obj else default
    return getattr(obj, key, default)


def _has(obj: Any, key: str) -> bool:
    if obj is None:
        return False
    if isinstance(obj, Mapping):
        return key in obj
    return hasattr(obj, key)


def _jsonl_write_lock() -> asyncio.Lock:
    global _JSONL_WRITE_LOCK
    if _JSONL_WRITE_LOCK is None:
        _JSONL_WRITE_LOCK = asyncio.Lock()
    return _JSONL_WRITE_LOCK


def _encode_list_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_list_cursor(cursor: str) -> dict[str, Any]:
    pad = "=" * ((4 - len(cursor) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + pad))
    except Exception:
        raise ValueError("Invalid list cursor") from None
    if not isinstance(data, dict) or isinstance(data, list):
        raise ValueError("Invalid list cursor")
    return data


def _decode_agent_cursor(cursor: str) -> dict[str, Any]:
    data = _decode_list_cursor(cursor)
    if not isinstance(data.get("updatedAt"), (int, float)) or not isinstance(data.get("agentId"), str):
        raise ValueError("Invalid agent list cursor")
    return {"updatedAt": data["updatedAt"], "agentId": data["agentId"]}


def _decode_run_cursor(cursor: str) -> dict[str, Any]:
    data = _decode_list_cursor(cursor)
    if not isinstance(data.get("turnNumber"), (int, float)) or not isinstance(data.get("runId"), str):
        raise ValueError("Invalid run list cursor")
    return {"turnNumber": data["turnNumber"], "runId": data["runId"]}


def matchesAgentFilter(agent: Mapping[str, Any], filter: Mapping[str, Any] | None = None) -> bool:
    if filter is None:
        return True
    agent_ids = _field(filter, "agentIds")
    if not agent_ids and not _has(filter, "cwd"):
        return True
    if agent_ids and agent.get("agentId") not in agent_ids:
        return False
    if _has(filter, "cwd") and agent.get("cwd") != _field(filter, "cwd"):
        return False
    return True


def matchesRunFilter(run: Mapping[str, Any], filter: Mapping[str, Any] | None = None) -> bool:
    if filter is None:
        return True
    agent_ids = _field(filter, "agentIds")
    run_ids = _field(filter, "runIds")
    if not agent_ids and not run_ids:
        return True
    if agent_ids and run.get("agentId") not in agent_ids:
        return False
    if run_ids and run.get("runId") not in run_ids:
        return False
    return True


def matchesCheckpointFilter(key: Mapping[str, Any], filter: Mapping[str, Any] | None = None) -> bool:
    if filter is None:
        return True
    agent_ids = _field(filter, "agentIds")
    blob_ids = _field(filter, "blobIds")
    if not agent_ids and not blob_ids:
        return True
    if agent_ids and key.get("agentId") not in agent_ids:
        return False
    if blob_ids and key.get("blobId") not in blob_ids:
        return False
    return True


def _agent_after_cursor(item: Mapping[str, Any], cursor: Mapping[str, Any]) -> bool:
    updated = item.get("updatedAt") or 0
    cursor_updated = cursor["updatedAt"]
    if updated < cursor_updated:
        return True
    if updated > cursor_updated:
        return False
    return str(item.get("agentId") or "") < str(cursor["agentId"])


def _run_after_cursor(item: Mapping[str, Any], cursor: Mapping[str, Any]) -> bool:
    turn = item.get("turnNumber") or 0
    cursor_turn = cursor["turnNumber"]
    if turn > cursor_turn:
        return True
    if turn < cursor_turn:
        return False
    return str(item.get("runId") or "") > str(cursor["runId"])


def paginateAgentDocuments(
    items: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    filter: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if filter is not None and _has(filter, "cwd"):
        cwd = _field(filter, "cwd")
        rows = [item for item in items if item.get("cwd") == cwd]
    else:
        rows = list(items)
    rows.sort(key=lambda item: (int(item.get("updatedAt") or 0), str(item.get("agentId") or "")), reverse=True)
    limit = _field(filter, "limit")
    if not isinstance(limit, int):
        limit = 50
    cursor = _field(filter, "cursor")
    remaining = rows
    if cursor:
        decoded = _decode_agent_cursor(str(cursor))
        remaining = [item for item in rows if _agent_after_cursor(item, decoded)]
    page = remaining[:limit]
    result: dict[str, Any] = {"items": [copy.deepcopy(item) for item in page]}
    if len(remaining) > limit and page:
        last = page[-1]
        result["nextCursor"] = _encode_list_cursor({"updatedAt": last.get("updatedAt"), "agentId": last.get("agentId")})
    return result


def paginateRunDocuments(
    items: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(items)
    rows.sort(key=lambda item: (int(item.get("turnNumber") or 0), str(item.get("runId") or "")))
    limit = _field(options, "limit")
    if not isinstance(limit, int):
        limit = 50
    cursor = _field(options, "cursor")
    remaining = rows
    if cursor:
        decoded = _decode_run_cursor(str(cursor))
        remaining = [item for item in rows if _run_after_cursor(item, decoded)]
    page = remaining[:limit]
    result: dict[str, Any] = {"items": [copy.deepcopy(item) for item in page]}
    if len(remaining) > limit and page:
        last = page[-1]
        result["nextCursor"] = _encode_list_cursor({"turnNumber": last.get("turnNumber"), "runId": last.get("runId")})
    return result


def paginateCheckpointBlobIds(
    blob_ids: list[str] | tuple[str, ...],
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    rows = sorted(blob_ids)
    limit = _field(options, "limit")
    if not isinstance(limit, int):
        limit = 50
    cursor = _field(options, "cursor")
    remaining = rows
    if cursor:
        remaining = [blob_id for blob_id in rows if blob_id > str(cursor)]
    page = remaining[:limit]
    result: dict[str, Any] = {"items": page}
    if len(remaining) > limit and page:
        result["nextCursor"] = page[-1]
    return result


@runtime_checkable
class LocalAgentStoreAgents(Protocol):
    async def get(self, input: Any) -> Any: ...
    async def create(self, input: Any) -> Any: ...
    async def update(self, input: Any) -> Any: ...
    async def delete(self, input: Any) -> None: ...
    async def list(self, input: Any | None = None) -> Any: ...


@runtime_checkable
class LocalAgentStoreRuns(Protocol):
    async def get(self, input: Any) -> Any: ...
    async def create(self, input: Any) -> Any: ...
    async def update(self, input: Any) -> Any: ...
    async def delete(self, input: Any) -> None: ...
    async def list(self, input: Any | None = None) -> Any: ...


@runtime_checkable
class LocalAgentStoreRunEvents(Protocol):
    async def append(self, input: Any) -> Any: ...
    async def list(self, input: Any) -> Any: ...
    async def delete(self, input: Any) -> None: ...


@runtime_checkable
class LocalAgentStoreCheckpoints(Protocol):
    async def get(self, input: Any) -> bytes | None: ...
    async def create(self, input: Any) -> None: ...
    async def update(self, input: Any) -> None: ...
    async def delete(self, input: Any) -> None: ...
    async def list(self, input: Any | None = None) -> Any: ...


@runtime_checkable
class LocalAgentStore(Protocol):
    """Custom persistence for local SDK agents (TypeScript `LocalAgentStore`)."""

    agents: LocalAgentStoreAgents
    checkpoints: LocalAgentStoreCheckpoints
    runs: LocalAgentStoreRuns
    runEvents: LocalAgentStoreRunEvents


class _ComposedLocalAgentStore:
    def __init__(self, agents: Any, checkpoints: Any, runs: Any, run_events: Any) -> None:
        self.agents = agents
        self.checkpoints = checkpoints
        self.runs = runs
        self.runEvents = run_events


def composeLocalAgentStore(parts: Any) -> LocalAgentStore:
    return _ComposedLocalAgentStore(  # type: ignore[return-value]
        _field(parts, "agents"),
        _field(parts, "checkpoints"),
        _field(parts, "runs"),
        _field(parts, "runEvents"),
    )


def is_protocol_store(obj: Any) -> bool:
    return (
        obj is not None
        and hasattr(obj, "agents")
        and hasattr(obj, "runs")
        and hasattr(obj, "runEvents")
        and hasattr(obj, "checkpoints")
        and hasattr(obj.agents, "get")
        and hasattr(obj.runs, "get")
        and hasattr(obj.runEvents, "append")
        and hasattr(obj.checkpoints, "get")
    )


def _read_jsonl_file(path: Path) -> list[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    lines = [line for line in text.split("\n") if line.strip()]
    records: list[Any] = []
    for index, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise ValueError(f"Corrupt local agent store: failed to parse record {index + 1} of {path}") from None
    return records


def _write_jsonl_file(path: Path, records: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        path.write_text("", encoding="utf-8")
        return
    body = "\n".join(json.dumps(record, separators=(",", ":"), ensure_ascii=False) for record in records)
    path.write_text(body + "\n", encoding="utf-8")


def _event_to_public(record: Mapping[str, Any]) -> dict[str, Any]:
    created = record.get("createdAt")
    if isinstance(created, (int, float)):
        created_ms = int(created)
    else:
        from datetime import datetime

        created_ms = int(datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp() * 1000)
    out = dict(record)
    out["createdAt"] = created_ms
    return out


def _as_bytes(data: Any) -> bytes:
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, memoryview):
        return data.tobytes()
    return bytes(data)


class _JsonlAgents:
    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir

    def _path(self) -> Path:
        return self._root / JSONL_LOCAL_AGENT_STORE_FILES["agents"]

    async def get(self, input: Any) -> dict[str, Any] | None:
        agent_id = _field(input, "agentId")
        for row in _read_jsonl_file(self._path()):
            if row.get("agentId") == agent_id:
                return row
        return None

    async def create(self, input: Any) -> dict[str, Any]:
        agent = copy.deepcopy(_field(input, "agent"))
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            if any(row.get("agentId") == agent.get("agentId") for row in rows):
                raise ValueError(f"Agent {agent.get('agentId')} already exists")
            rows.append(agent)
            _write_jsonl_file(self._path(), rows)
            return copy.deepcopy(agent)

    async def update(self, input: Any) -> dict[str, Any]:
        agent = copy.deepcopy(_field(input, "agent"))
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            index = next((i for i, row in enumerate(rows) if row.get("agentId") == agent.get("agentId")), -1)
            if index < 0:
                raise ValueError(f"Agent {agent.get('agentId')} not found")
            rows[index] = agent
            _write_jsonl_file(self._path(), rows)
            return copy.deepcopy(agent)

    async def delete(self, input: Any) -> None:
        filt = _field(input, "filter") or {}
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            kept = [row for row in rows if not matchesAgentFilter(row, filt)]
            if len(kept) == len(rows):
                raise ValueError("No agents matched delete filter")
            _write_jsonl_file(self._path(), kept)

    async def list(self, input: Any | None = None) -> dict[str, Any]:
        filt = _field(input, "filter") if input is not None else None
        return paginateAgentDocuments(_read_jsonl_file(self._path()), filt)


class _JsonlRuns:
    def __init__(self, root_dir: Path, run_events: _JsonlRunEvents) -> None:
        self._root = root_dir
        self._run_events = run_events

    def _path(self) -> Path:
        return self._root / JSONL_LOCAL_AGENT_STORE_FILES["runs"]

    async def get(self, input: Any) -> dict[str, Any] | None:
        agent_id = _field(input, "agentId")
        run_id = _field(input, "runId")
        for row in _read_jsonl_file(self._path()):
            if row.get("agentId") == agent_id and row.get("runId") == run_id:
                return row
        return None

    async def create(self, input: Any) -> dict[str, Any]:
        run = copy.deepcopy(_field(input, "run"))
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            if any(row.get("agentId") == run.get("agentId") and row.get("runId") == run.get("runId") for row in rows):
                raise ValueError(f"Run {run.get('runId')} already exists for agent {run.get('agentId')}")
            rows.append(run)
            _write_jsonl_file(self._path(), rows)
            return copy.deepcopy(run)

    async def update(self, input: Any) -> dict[str, Any]:
        run = copy.deepcopy(_field(input, "run"))
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            index = next(
                (
                    i
                    for i, row in enumerate(rows)
                    if row.get("agentId") == run.get("agentId") and row.get("runId") == run.get("runId")
                ),
                -1,
            )
            if index < 0:
                raise ValueError(f"Run {run.get('runId')} not found for agent {run.get('agentId')}")
            rows[index] = run
            _write_jsonl_file(self._path(), rows)
            return copy.deepcopy(run)

    async def delete(self, input: Any) -> None:
        filt = _field(input, "filter") or {}
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            removed = [row for row in rows if matchesRunFilter(row, filt)]
            if not removed:
                return
            kept = [row for row in rows if not matchesRunFilter(row, filt)]
            _write_jsonl_file(self._path(), kept)
        for row in removed:
            await self._run_events.delete({"filter": {"runIds": [row.get("runId")]}})

    async def list(self, input: Any | None = None) -> dict[str, Any]:
        filt = _field(input, "filter") if input is not None else None
        rows = [row for row in _read_jsonl_file(self._path()) if matchesRunFilter(row, filt)]
        return paginateRunDocuments(rows, filt)


class _JsonlRunEvents:
    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir

    def _path(self) -> Path:
        return self._root / JSONL_LOCAL_AGENT_STORE_FILES["runEvents"]

    async def append(self, input: Any) -> dict[str, Any]:
        from datetime import datetime, timezone

        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            run_id = _field(input, "runId")
            idempotency_key = _field(input, "idempotencyKey")
            if idempotency_key:
                existing = next(
                    (row for row in rows if row.get("runId") == run_id and row.get("idempotencyKey") == idempotency_key),
                    None,
                )
                if existing is not None:
                    return _event_to_public(existing)
            seq = max((int(row.get("seq") or 0) for row in rows if row.get("runId") == run_id), default=0) + 1
            record = {
                "runId": run_id,
                "seq": seq,
                "offset": str(seq),
                "eventType": _field(input, "eventType"),
                "payload": _field(input, "payload", None),
                "payloadRef": _field(input, "payloadRef", None),
                "idempotencyKey": idempotency_key if idempotency_key is not None else None,
                "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            rows.append(record)
            _write_jsonl_file(self._path(), rows)
            return _event_to_public(record)

    async def list(self, input: Any) -> dict[str, Any]:
        run_id = _field(input, "runId")
        after_raw = _field(input, "afterOffset") or "0"
        try:
            after = int(str(after_raw))
        except (TypeError, ValueError):
            after = float("nan")
        limit = _field(input, "limit")
        if not isinstance(limit, int):
            limit = 100
        rows = [row for row in _read_jsonl_file(self._path()) if row.get("runId") == run_id and (row.get("seq") or 0) > after]
        rows.sort(key=lambda row: int(row.get("seq") or 0))
        page = [_event_to_public(row) for row in rows[:limit]]
        result: dict[str, Any] = {"items": page}
        if len(rows) > limit and page:
            result["nextOffset"] = page[-1].get("offset")
        return result

    async def delete(self, input: Any) -> None:
        filt = _field(input, "filter") or {}
        run_ids = _field(filt, "runIds")
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            if run_ids:
                keep = {str(run_id) for run_id in run_ids}
                kept = [row for row in rows if row.get("runId") not in keep]
            else:
                kept = []
            _write_jsonl_file(self._path(), kept)


class _JsonlCheckpoints:
    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir

    def _path(self) -> Path:
        return self._root / JSONL_LOCAL_AGENT_STORE_FILES["checkpoints"]

    async def get(self, input: Any) -> bytes | None:
        agent_id = _field(input, "agentId")
        blob_id = _field(input, "blobId")
        for row in _read_jsonl_file(self._path()):
            if row.get("agentId") == agent_id and row.get("blobId") == blob_id:
                return base64.b64decode(row.get("dataBase64") or "")
        return None

    async def create(self, input: Any) -> None:
        agent_id = _field(input, "agentId")
        blob_id = _field(input, "blobId")
        data = _as_bytes(_field(input, "data") or b"")
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            if any(row.get("agentId") == agent_id and row.get("blobId") == blob_id for row in rows):
                raise ValueError(f"Checkpoint blob {blob_id} already exists for agent {agent_id}")
            rows.append({"agentId": agent_id, "blobId": blob_id, "dataBase64": base64.b64encode(data).decode("ascii")})
            _write_jsonl_file(self._path(), rows)

    async def update(self, input: Any) -> None:
        agent_id = _field(input, "agentId")
        blob_id = _field(input, "blobId")
        data = _as_bytes(_field(input, "data") or b"")
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            index = next(
                (i for i, row in enumerate(rows) if row.get("agentId") == agent_id and row.get("blobId") == blob_id),
                -1,
            )
            if index < 0:
                raise ValueError(f"Checkpoint blob {blob_id} not found for agent {agent_id}")
            rows[index] = {"agentId": agent_id, "blobId": blob_id, "dataBase64": base64.b64encode(data).decode("ascii")}
            _write_jsonl_file(self._path(), rows)

    async def list(self, input: Any | None = None) -> dict[str, Any]:
        filt = _field(input, "filter") if input is not None else None
        blob_ids = [
            str(row.get("blobId"))
            for row in _read_jsonl_file(self._path())
            if matchesCheckpointFilter(row, filt)
        ]
        return paginateCheckpointBlobIds(blob_ids, filt)

    async def delete(self, input: Any) -> None:
        filt = _field(input, "filter") or {}
        async with _jsonl_write_lock():
            rows = _read_jsonl_file(self._path())
            kept = [row for row in rows if not matchesCheckpointFilter(row, filt)]
            _write_jsonl_file(self._path(), kept)


class JsonlLocalAgentStore:
    """File-backed `LocalAgentStore` using newline-delimited JSON (JSONL / NDJSON)."""

    def __init__(self, root_dir: str | Path) -> None:
        self.rootDir = str(root_dir)
        root = Path(root_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.runEvents = _JsonlRunEvents(root)
        self.agents = _JsonlAgents(root)
        self.checkpoints = _JsonlCheckpoints(root)
        self.runs = _JsonlRuns(root, self.runEvents)
