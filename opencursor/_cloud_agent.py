from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator, Callable, Literal, Mapping, cast

import httpx

from opencursor import errors, messages
from opencursor._auth import resolve_api_key
from opencursor._cloud_api import CloudApiClient
from opencursor._interaction_accumulator import RunInteractionAccumulator
from opencursor._sync_runtime import close_sync, iterate_async, run_sync_or_awaitable
from opencursor._run_api import (
    agent_messages_from_rows,
    agent_usage_from_v1,
    conversation_json_from_turns,
    empty_agent_usage,
    ensure_run_operation,
    iter_run_events,
    iter_run_messages,
    iter_run_text,
    run_stream_event_from_sdk_message,
    run_text,
    token_usage_from_mapping,
)
from opencursor._sse import iter_sse_events
from opencursor.types import (
    AgentMessage,
    AgentOptions,
    AgentUsage,
    GetAgentMessagesOptions,
    GetUsageOptions,
    ModelSelection,
    RunGitBranchInfo,
    RunGitInfo,
    RunResult,
    RunResultStatus,
    RunStatus,
    RunError,
    RunStreamEvent,
    SDKArtifact,
    SDKUserMessage,
    SendOptions,
    SteerAckOutcome,
    TokenUsage,
    parse_interaction_update,
)


def new_cloud_agent_id() -> str:
    return f"bc-{uuid.uuid4()}"


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _v1_run_status_to_sdk(status: str) -> RunStatus:
    if status in ("CREATING", "RUNNING"):
        return "running"
    if status == "FINISHED":
        return "finished"
    if status == "CANCELLED":
        return "cancelled"
    return "error"


def _map_v1_run_to_result(run: dict[str, Any], run_id: str) -> RunResult:
    st = _v1_run_status_to_sdk(str(run.get("status") or "FINISHED"))
    if st == "running":
        st = "error"
    git: RunGitInfo | None = None
    raw_git = run.get("git")
    if _is_record(raw_git) and isinstance(raw_git.get("branches"), list):
        branches: list[RunGitBranchInfo] = []
        for b in raw_git["branches"]:
            if not _is_record(b):
                continue
            branches.append(
                RunGitBranchInfo(
                    repoUrl=str(b.get("repoUrl") or ""),
                    branch=str(b["branch"]) if b.get("branch") is not None else None,
                    prUrl=str(b["prUrl"]) if b.get("prUrl") is not None else None,
                )
            )
        git = RunGitInfo(branches=branches)
    return RunResult(
        id=run_id,
        requestId=str(run["requestId"]) if run.get("requestId") is not None else None,
        status=cast(RunResultStatus, st),
        result=str(run["result"]) if run.get("result") is not None else None,
        durationMs=int(run["durationMs"]) if run.get("durationMs") is not None else None,
        git=git,
        error=_run_error_from_v1(run, st),
        usage=token_usage_from_mapping(run.get("usage")),
    )


def _run_error_from_v1(run: dict[str, Any], status: RunResultStatus) -> RunError | None:
    if status != "error":
        return None
    raw = run.get("error")
    if _is_record(raw) and (raw.get("message") or raw.get("code")):
        return RunError(
            message=str(raw.get("message") or raw.get("code") or "error"),
            code=str(raw["code"]) if raw.get("code") is not None else None,
        )
    code = run.get("errorCode") or run.get("error_code")
    message = run.get("errorMessage") or run.get("message")
    if code or message:
        return RunError(message=str(message or code or "error"), code=str(code) if code is not None else None)
    return None


def _build_v1_mcp_servers(mcp_servers: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if not mcp_servers:
        return None
    out: list[dict[str, Any]] = []
    for name, cfg in mcp_servers.items():
        if not _is_record(cfg):
            continue
        if "command" in cfg:
            if cfg.get("cwd") is not None:
                raise errors.ConfigurationError(
                    f'Cloud MCP server "{name}" cannot include cwd.',
                    is_retryable=False,
                )
            row = {"name": name, **{k: v for k, v in cfg.items() if k != "cwd"}}
            out.append(row)
        else:
            out.append({"name": name, **cfg})
    return out or None


def _custom_subagents_from_options(agents: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    if not agents:
        return None
    r: list[dict[str, Any]] = []
    for name, s in agents.items():
        if not _is_record(s):
            continue
        mcp = s.get("mcpServers")
        if mcp is not None:
            for t in mcp if isinstance(mcp, list) else []:
                if not isinstance(t, str):
                    raise errors.ConfigurationError(
                        f'Custom subagent "{name}" has an inline McpServerConfig in mcpServers; '
                        "SDK custom subagents only support string references in v1.",
                        is_retryable=False,
                    )
        model = s.get("model")
        if model is None or model == "inherit":
            mid: str = "inherit"
        elif _is_record(model) and "id" in model:
            mid = str(model["id"])
        else:
            mid = "inherit"
        r.append(
            {
                "name": name,
                "description": str(s.get("description") or ""),
                "prompt": str(s.get("prompt") or ""),
                "model": mid,
            }
        )
    return r or None


def _prompt_from_user_message(msg: str | SDKUserMessage) -> dict[str, Any]:
    if isinstance(msg, str):
        return {"text": msg}
    d = msg.model_dump(by_alias=True, exclude_none=True)
    text = str(d.pop("text", ""))
    images = d.get("images")
    out: dict[str, Any] = {"text": text}
    if images:
        out["images"] = images
    return out


LEGACY_SSE_EVENTS = frozenset({"assistant", "thinking", "tool_call"})


def _cloud_sse_to_run_stream_event(
    *,
    event_type: str,
    payload: Any,
    offset: str | None,
    agent_id: str,
    run_id: str,
) -> RunStreamEvent | None:
    data = payload if _is_record(payload) else {}
    if event_type == "interaction_update":
        return RunStreamEvent(
            kind="interaction_update",
            offset=offset,
            interaction_update=parse_interaction_update(data),
        )
    if event_type == "result":
        result = data.get("result")
        result_is_full = isinstance(result, Mapping)
        return RunStreamEvent(
            kind="result",
            offset=offset,
            result=dict(result) if result_is_full else dict(data),
            result_is_full=result_is_full,
        )
    if event_type == "done":
        return RunStreamEvent(kind="done", offset=offset, done=dict(data))
    if event_type == "assistant":
        message: messages.SDKMessage = {
            "type": "assistant",
            "agent_id": agent_id,
            "run_id": run_id,
            "message": {"role": "assistant", "content": [{"type": "text", "text": str(data.get("text") or "")}]},
        }
        return run_stream_event_from_sdk_message(message, offset)
    if event_type == "thinking":
        message = {
            "type": "thinking",
            "agent_id": agent_id,
            "run_id": run_id,
            "text": str(data.get("text") or ""),
        }
        return run_stream_event_from_sdk_message(cast(messages.SDKMessage, message), offset)
    if event_type == "tool_call":
        inner = data.get("data") if _is_record(data.get("data")) else data
        if not _is_record(inner):
            inner = {}
        status = str(inner.get("status") or "running")
        if status not in ("running", "completed", "error"):
            status = "running"
        tool: dict[str, Any] = {
            "type": "tool_call",
            "agent_id": agent_id,
            "run_id": run_id,
            "call_id": str(inner.get("callId") or ""),
            "name": str(inner.get("name") or "unknown"),
            "status": status,
        }
        if "args" in inner:
            tool["args"] = inner["args"]
        if "result" in inner:
            tool["result"] = inner["result"]
        return run_stream_event_from_sdk_message(cast(messages.SDKMessage, tool), offset)
    if event_type == "status":
        message = {
            "type": "status",
            "agent_id": agent_id,
            "run_id": run_id,
            "status": str(data.get("status") or ""),
        }
        return run_stream_event_from_sdk_message(cast(messages.SDKMessage, message), offset)
    return None


class CloudRun:
    @classmethod
    def from_summary(
        cls,
        api_key: str,
        run: dict[str, Any],
        *,
        base_url: str | None = None,
        model: ModelSelection | None = None,
    ) -> CloudRun:
        """Hydrate a run from list/get endpoints; owns its own ``CloudApiClient``."""

        return cls(CloudApiClient(api_key, base_url), run, None, model, own_client=True)

    def __init__(
        self,
        client: CloudApiClient,
        run: dict[str, Any],
        send_options: SendOptions | None,
        model: ModelSelection | None,
        *,
        own_client: bool = False,
    ) -> None:
        self._client = client
        self._own_client = own_client
        self.id = str(run["id"])
        self.agent_id = str(run["agentId"])
        self._request_id = str(run["requestId"]) if run.get("requestId") is not None else None
        self._usage: TokenUsage | None = token_usage_from_mapping(run.get("usage"))
        self._status: RunStatus = _v1_run_status_to_sdk(str(run.get("status") or "RUNNING"))
        self._result: str | None = str(run["result"]) if run.get("result") is not None else None
        self._duration_ms: int | None = int(run["durationMs"]) if run.get("durationMs") is not None else None
        self._git = self._parse_git(run.get("git"))
        self._model = model
        self._error: RunError | None = _run_error_from_v1(run, "error" if self._status == "error" else "finished")
        self._send_options = send_options
        self._queue: asyncio.Queue[messages.SDKMessage | None] = asyncio.Queue()
        self._listeners: set[Callable[[RunStatus], None]] = set()
        self._stream_started = False
        self._abort: asyncio.Event = asyncio.Event()
        self._stream_task: asyncio.Task[None] | None = None
        self._stream_done: asyncio.Future[None] | None = None
        self._last_event_id: str | None = None
        self._emitted_legacy: set[str] = set()
        self._accumulator: RunInteractionAccumulator | None = None
        self._created_at = int(time.time() * 1000)

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
        return self._result

    @property
    def error(self) -> RunError | None:
        return self._error

    @property
    def durationMs(self) -> int | None:  # noqa: N802
        return self._duration_ms

    @property
    def git(self) -> RunGitInfo | None:
        return self._git

    @property
    def model(self) -> ModelSelection | None:
        return self._model

    @property
    def createdAt(self) -> int | None:  # noqa: N802
        return self._created_at

    @property
    def duration_ms(self) -> int | None:
        return self._duration_ms

    @property
    def created_at(self) -> str | None:
        return None if self._created_at is None else str(self._created_at)

    def _parse_git(self, raw: Any) -> RunGitInfo | None:
        if not _is_record(raw) or not isinstance(raw.get("branches"), list):
            return None
        branches: list[RunGitBranchInfo] = []
        for b in raw["branches"]:
            if not _is_record(b):
                continue
            branches.append(
                RunGitBranchInfo(
                    repoUrl=str(b.get("repoUrl") or ""),
                    branch=str(b["branch"]) if b.get("branch") is not None else None,
                    prUrl=str(b["prUrl"]) if b.get("prUrl") is not None else None,
                )
            )
        return RunGitInfo(branches=branches)

    def supports(self, operation: str) -> bool:
        return operation in ("stream", "wait", "cancel", "conversation", "observe")

    def unsupported_reason(self, operation: str) -> str | None:
        return None if self.supports(operation) else f'Unknown run operation "{operation}"'

    def unsupportedReason(self, operation: str) -> str | None:  # noqa: N802
        return self.unsupported_reason(operation)

    def on_did_change_status(self, listener: Callable[[RunStatus], None]) -> Callable[[], None]:
        self._listeners.add(listener)

        def _off() -> None:
            self._listeners.discard(listener)

        return _off

    def onDidChangeStatus(self, listener: Callable[[RunStatus], None]) -> Callable[[], None]:
        return self.on_did_change_status(listener)

    def _set_status(self, s: RunStatus) -> None:
        if self._status == s:
            return
        self._status = s
        for li in list(self._listeners):
            li(s)

    def ensure_stream_started(self) -> None:
        if self._stream_started:
            return
        self._stream_started = True
        self._stream_done = asyncio.Future()
        so = self._send_options
        self._accumulator = RunInteractionAccumulator(
            on_step=so.onStep if so and getattr(so, "onStep", None) else None,
            on_delta=so.onDelta if so and getattr(so, "onDelta", None) else None,
        )
        self._stream_task = asyncio.create_task(self._run_stream_loop())

    async def _run_stream_loop(self) -> None:
        assert self._stream_done is not None
        deadline = time.monotonic() + 72 * 60 * 60
        attempt = 0
        try:
            while self._status == "running" and not self._abort.is_set():
                got = await self._run_one_stream_attempt()
                if self._abort.is_set():
                    break
                if got == "received-result":
                    break
                await self._refetch_and_sync()
                if self._abort.is_set() or self._status != "running":
                    break
                if time.monotonic() >= deadline:
                    break
                if attempt >= 6:
                    await self._poll_run_until_terminal(deadline)
                    break
                backoff = min(30_000, 1000 * (2**attempt))
                backoff = min(backoff, max(0, int((deadline - time.monotonic()) * 1000)))
                attempt += 1
                try:
                    await asyncio.wait_for(self._abort.wait(), timeout=backoff / 1000)
                    break
                except TimeoutError:
                    pass
            if self._status == "running" and time.monotonic() >= deadline:
                self._set_status("error")
            if self._status == "finished" and self._accumulator:
                await self._accumulator.flush_pending_step()
        finally:
            await self._queue.put(None)
            if not self._stream_done.done():
                self._stream_done.set_result(None)

    async def _poll_run_until_terminal(self, deadline_monotonic: float) -> None:
        while self._status == "running" and not self._abort.is_set():
            wait = min(15_000, max(0, int((deadline_monotonic - time.monotonic()) * 1000)))
            if wait <= 0:
                self._set_status("error")
                return
            try:
                await asyncio.wait_for(self._abort.wait(), timeout=wait / 1000)
                return
            except TimeoutError:
                pass
            await self._refetch_and_sync()

    async def _refetch_and_sync(self) -> None:
        try:
            t = await self._client.get_run(self.agent_id, self.id)
        except (errors.AuthenticationError, errors.ConfigurationError):
            if self._status == "running":
                self._set_status("error")
            raise
        except Exception:
            return
        meta = _map_v1_run_to_result(t, self.id)
        if meta.result is not None:
            self._result = meta.result
        if meta.durationMs is not None:
            self._duration_ms = meta.durationMs
        if meta.git is not None:
            self._git = meta.git
        if meta.usage is not None:
            self._usage = meta.usage
        if meta.requestId is not None:
            self._request_id = meta.requestId
        if self._status == "running":
            self._set_status(_v1_run_status_to_sdk(str(t.get("status") or "FINISHED")))

    async def _emit(self, msg: messages.SDKMessage) -> None:
        if self._accumulator:
            await self._accumulator.push_sdk_message(msg)
        await self._queue.put(msg)

    async def _run_one_stream_attempt(self) -> Literal["received-result", "stream-dropped"]:
        prev_id = self._last_event_id
        received_result = False
        try:
            async with self._client.stream_run(
                self.agent_id,
                self.id,
                last_event_id=self._last_event_id,
            ) as resp:
                async for ev in iter_sse_events(resp.aiter_bytes()):
                    if self._abort.is_set():
                        break
                    if ev.id and (not ev.event or ev.event not in LEGACY_SSE_EVENTS):
                        self._last_event_id = ev.id
                        self._emitted_legacy.clear()
                    et = ev.event or ""
                    if et == "done":
                        break
                    if not ev.data.strip() and et not in ("heartbeat", "done"):
                        continue
                    try:
                        payload = json.loads(ev.data) if ev.data.strip() else {}
                    except json.JSONDecodeError as e:
                        raise errors.NetworkError(
                            f"Malformed SSE event: {ev.data!r}",
                            is_retryable=True,
                            cause=e,
                        ) from e
                    if et == "assistant":
                        text = str(payload.get("text") or "") if _is_record(payload) else ""
                        msg = cast(
                            messages.SDKMessage,
                            {
                                "type": "assistant",
                                "agent_id": self.agent_id,
                                "run_id": self.id,
                                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                            },
                        )
                        await self._emit(msg)
                    elif et == "thinking":
                        text = str(payload.get("text") or "") if _is_record(payload) else ""
                        await self._emit(
                            cast(
                                messages.SDKMessage,
                                {"type": "thinking", "agent_id": self.agent_id, "run_id": self.id, "text": text},
                            )
                        )
                    elif et == "tool_call":
                        if not _is_record(payload):
                            payload = {}
                        data = payload.get("data") if _is_record(payload.get("data")) else payload
                        if not _is_record(data):
                            data = {}
                        st = str(data.get("status") or "")
                        if st not in ("running", "completed", "error"):
                            st = "running"
                        tool_msg = {
                            "type": "tool_call",
                            "agent_id": self.agent_id,
                            "run_id": self.id,
                            "call_id": str(data.get("callId") or ""),
                            "name": str(data.get("name") or "unknown"),
                            "status": st,
                        }
                        if "args" in data:
                            tool_msg["args"] = data["args"]
                        if "result" in data:
                            tool_msg["result"] = data["result"]
                        if _is_record(data.get("truncated")):
                            tool_msg["truncated"] = data["truncated"]
                        await self._emit(cast(messages.SDKMessage, tool_msg))
                    elif et == "interaction_update" and self._accumulator:
                        if _is_record(payload):
                            await self._accumulator.apply_interaction_update(payload)
                    elif et == "status":
                        st = str(payload.get("status") or "") if _is_record(payload) else ""
                        if st:
                            if st in ("CREATING", "RUNNING"):
                                self._set_status("running")
                            await self._emit(
                                cast(
                                    messages.SDKMessage,
                                    {
                                        "type": "status",
                                        "agent_id": self.agent_id,
                                        "run_id": self.id,
                                        "status": st,  # type: ignore[typeddict-item]
                                    },
                                )
                            )
                    elif et == "result" and _is_record(payload):
                        if isinstance(payload.get("text"), str) and payload.get("result") is None:
                            payload = {**payload, "result": payload["text"]}
                        self._result = str(payload["result"]) if payload.get("result") is not None else self._result
                        if payload.get("durationMs") is not None:
                            self._duration_ms = int(payload["durationMs"])
                        if _is_record(payload.get("git")):
                            self._git = self._parse_git(payload.get("git"))
                        usage = token_usage_from_mapping(payload.get("usage"))
                        if usage is not None:
                            self._usage = usage
                        if payload.get("requestId") is not None:
                            self._request_id = str(payload["requestId"])
                        self._set_status(_v1_run_status_to_sdk(str(payload.get("status") or "FINISHED")))
                        received_result = True
                        break
                    elif et == "error" and _is_record(payload):
                        code = str(payload.get("code") or "UNKNOWN")
                        msg = str(payload.get("message") or "Unknown error")
                        mapped = errors.error_from_sse_error_event(code, msg)
                        self._error = RunError(message=str(mapped), code=mapped.code)
                        raise mapped
        except Exception as e:
            if self._abort.is_set():
                return "stream-dropped"
            err = str(e)
            if err.startswith("[invalid_last_event_id]") and self._last_event_id == prev_id:
                self._last_event_id = None
            elif isinstance(e, errors.CursorAgentError) and not e.is_retryable:
                if self._status == "running":
                    self._set_status("error")
                raise
            return "stream-dropped"
        return "received-result" if received_result else "stream-dropped"

    def stream(self) -> Any:
        return iterate_async(self._stream_async())

    async def _stream_async(self) -> AsyncIterator[messages.SDKMessage]:
        ensure_run_operation(self, "stream")
        self.ensure_stream_started()
        while True:
            m = await self._queue.get()
            if m is None:
                break
            yield m

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
        self.ensure_stream_started()
        if self._stream_done:
            try:
                await asyncio.shield(self._stream_done)
            except Exception:
                pass
        if self._accumulator:
            return self._accumulator.conversation()
        return []

    def conversation_json(self) -> Any:
        return run_sync_or_awaitable(self._conversation_json_async())

    async def _conversation_json_async(self) -> str:
        return conversation_json_from_turns(await self._conversation_async())

    def observe(self, *, after_offset: str | None = None) -> Any:
        return iterate_async(self._observe_async(after_offset=after_offset))

    async def _observe_async(self, *, after_offset: str | None = None) -> AsyncIterator[RunStreamEvent]:
        try:
            async with self._client.stream_run(self.agent_id, self.id, last_event_id=after_offset) as resp:
                async for ev in iter_sse_events(resp.aiter_bytes()):
                    et = ev.event or ""
                    try:
                        payload = json.loads(ev.data) if ev.data.strip() else {}
                    except json.JSONDecodeError:
                        payload = {}
                    event = _cloud_sse_to_run_stream_event(
                        event_type=et,
                        payload=payload,
                        offset=ev.id or None,
                        agent_id=self.agent_id,
                        run_id=self.id,
                    )
                    if event is not None:
                        yield event
                    if et in ("done", "result"):
                        return
        except (errors.CursorSdkError, errors.NetworkError):
            return

    def wait(self) -> Any:
        return run_sync_or_awaitable(self._wait_async())

    async def _wait_async(self) -> RunResult:
        ensure_run_operation(self, "wait")
        if self._status != "running":
            st = self._status
            return RunResult(
                id=self.id,
                agent_id=self.agent_id,
                request_id=self._request_id,
                status=cast(RunResultStatus, st),
                result=self._result,
                model=self._model,
                duration_ms=self._duration_ms,
                git=self._git,
                error=self._error if st == "error" else None,
                usage=self._usage,
            )
        self.ensure_stream_started()
        if self._stream_task:
            try:
                await self._stream_task
            except Exception as exc:
                if self._status == "running":
                    self._set_status("error")
                if self._error is None:
                    mapped = errors.convert_error(exc)
                    self._error = RunError(**errors.to_run_error(mapped))
        st = self._status if self._status != "running" else "error"
        return RunResult(
            id=self.id,
            agent_id=self.agent_id,
            request_id=self._request_id,
            status=cast(RunResultStatus, st),
            result=self._result,
            model=self._model,
            duration_ms=self._duration_ms,
            git=self._git,
            error=self._error if st == "error" else None,
            usage=self._usage,
        )

    def cancel(self) -> Any:
        return run_sync_or_awaitable(self._cancel_async())

    async def _cancel_async(self) -> None:
        ensure_run_operation(self, "cancel")
        if self._status == "cancelled":
            return
        await self._client.cancel_run(self.agent_id, self.id)
        self._abort.set()
        self._set_status("cancelled")

    async def steer(self, text: str) -> SteerAckOutcome:
        # Cloud REST has no inject-context path; match TS "no generation UUID".
        return "revert_to_followup"

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()


class CloudAgent:
    def __init__(
        self,
        agent_id: str,
        client: CloudApiClient,
        options: AgentOptions,
        *,
        server_created: bool,
    ) -> None:
        self.agent_id = agent_id
        self._client = client
        self._options = options
        self._server_created = server_created
        self._model: ModelSelection | None = options.model
        self._closed = False

    @property
    def agentId(self) -> str:  # noqa: N802
        return self.agent_id

    @property
    def model(self) -> ModelSelection | None:
        return self._model

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def __aenter__(self) -> CloudAgent:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    def send(self, message: str | SDKUserMessage, options: SendOptions | None = None) -> Any:
        return run_sync_or_awaitable(self._send_async(message, options))

    async def _send_async(self, message: str | SDKUserMessage, options: SendOptions | None = None) -> CloudRun:
        try:
            prompt = _prompt_from_user_message(message)
            send_opts = options
            mcp: dict[str, Any] | None = None
            if options and options.mcpServers:
                mcp = {k: v.model_dump(by_alias=True) for k, v in options.mcpServers.items()}
            elif self._options.mcpServers:
                mcp = {k: v.model_dump(by_alias=True) for k, v in self._options.mcpServers.items()}
            model_sel = (options.model if options and options.model else None) or self._model
            mcp_v1 = _build_v1_mcp_servers(mcp)
            subagents = _custom_subagents_from_options(
                {k: v.model_dump() for k, v in self._options.agents.items()} if self._options.agents else None
            )
            run: dict[str, Any]
            if not self._server_created:
                body: dict[str, Any] = {"agentId": self.agent_id, "prompt": prompt}
                if model_sel is not None:
                    body["model"] = model_sel.model_dump(by_alias=True, exclude_none=True)
                if self._options.name:
                    body["name"] = self._options.name
                if mcp_v1 is not None:
                    body["mcpServers"] = mcp_v1
                if subagents is not None:
                    body["customSubagents"] = subagents
                cloud = self._options.cloud
                if cloud:
                    if cloud.env is not None:
                        body["env"] = cloud.env.model_dump(by_alias=True, exclude_none=True)
                    if cloud.repos is not None:
                        body["repos"] = [r.model_dump(by_alias=True, exclude_none=True) for r in cloud.repos]
                    if cloud.workOnCurrentBranch is not None:
                        body["workOnCurrentBranch"] = cloud.workOnCurrentBranch
                    if cloud.autoCreatePR is not None:
                        body["autoCreatePR"] = cloud.autoCreatePR
                    if cloud.skipReviewerRequest is not None:
                        body["skipReviewerRequest"] = cloud.skipReviewerRequest
                    if cloud.openAsCursorGithubApp is not None:
                        body["openAsCursorGithubApp"] = cloud.openAsCursorGithubApp
                    if cloud.envVars is not None:
                        body["envVars"] = cloud.envVars
                    if cloud.metadata is not None:
                        body["metadata"] = cloud.metadata
                    if cloud.agentServeAgent is not None:
                        body["agentServeAgent"] = cloud.agentServeAgent
                mode = (options.mode if options and options.mode else None) or self._options.mode
                if mode:
                    body["mode"] = mode
                idempotency = (options.idempotencyKey if options and options.idempotencyKey else None) or self._options.idempotencyKey
                resp = await self._client.create_agent(body, idempotency_key=idempotency)
                agent = resp["agent"]
                if str(agent["id"]) != self.agent_id:
                    raise errors.UnknownAgentError(
                        f'Server returned mismatched agent id "{agent["id"]}"; expected "{self.agent_id}".',
                        is_retryable=False,
                    )
                self._server_created = True
                run = resp["run"]
            else:
                cr_body: dict[str, Any] = {"prompt": prompt}
                if mcp_v1 is not None:
                    cr_body["mcpServers"] = mcp_v1
                if options and options.model is not None:
                    cr_body["model"] = options.model.model_dump(by_alias=True, exclude_none=True)
                mode = (options.mode if options and options.mode else None) or self._options.mode
                if mode:
                    cr_body["mode"] = mode
                send_env = None
                if options and options.cloud and options.cloud.envVars is not None:
                    send_env = options.cloud.envVars
                elif self._options.cloud and self._options.cloud.envVars is not None:
                    send_env = self._options.cloud.envVars
                if send_env is not None:
                    cr_body["envVars"] = send_env
                idempotency = options.idempotencyKey if options and options.idempotencyKey else None
                resp2 = await self._client.create_run(self.agent_id, cr_body, idempotency_key=idempotency)
                run = resp2["run"]
            if options and options.model is not None:
                self._model = options.model
            crun = CloudRun(self._client, run, send_opts, model_sel, own_client=False)
            crun.ensure_stream_started()
            return crun
        except errors.CursorSdkError:
            raise
        except Exception as e:
            raise errors.CursorSdkError(str(e), operation="agent.send", cause=e) from e

    def close(self) -> None:
        close_sync(self.aclose())

    def __enter__(self) -> CloudAgent:
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
        if not self._server_created:
            return []
        data = await self._client.list_artifacts(self.agent_id)
        items = data.get("items") or []
        return [SDKArtifact.model_validate(x) for x in items if isinstance(x, dict)]

    def download_artifact(self, path: str) -> Any:
        return run_sync_or_awaitable(self._download_artifact_async(path))

    async def _download_artifact_async(self, path: str) -> bytes:
        if not self._server_created:
            raise errors.ConfigurationError("Agent has not been created yet; call send() first.", is_retryable=False)
        pr = await self._client.get_artifact_download_url(self.agent_id, path)
        url = str(pr.get("url") or "")
        async with httpx.AsyncClient(timeout=120.0) as ac:
            r = await ac.get(url)
            if r.status_code >= 400:
                raise errors.NetworkError(
                    f"Failed to download artifact: {r.status_code}",
                    status=r.status_code,
                    is_retryable=r.status_code >= 500,
                )
            return r.content

    def listArtifacts(self) -> Any:  # noqa: N802
        return self.list_artifacts()

    def downloadArtifact(self, path: str) -> Any:  # noqa: N802
        return self.download_artifact(path)

    def getUsage(self, options: GetUsageOptions | None = None) -> Any:  # noqa: N802
        return run_sync_or_awaitable(self._get_usage_async(options))

    async def _get_usage_async(self, options: GetUsageOptions | None = None) -> AgentUsage:
        run_id = options.runId if options else None
        try:
            data = await self._client.get_agent_usage(self.agent_id, run_id)
        except errors.CursorSdkError as exc:
            if exc.status == 404:
                return empty_agent_usage()
            raise
        if not data:
            return empty_agent_usage()
        return agent_usage_from_v1(data)

    def get_usage(self, options: GetUsageOptions | None = None) -> Any:
        return self.getUsage(options)

    def list_messages(self, options: GetAgentMessagesOptions | Mapping[str, Any] | None = None) -> Any:
        return run_sync_or_awaitable(self._list_messages_async(options))

    async def _list_messages_async(self, options: GetAgentMessagesOptions | Mapping[str, Any] | None = None) -> list[AgentMessage]:
        if not self._server_created:
            return []
        if isinstance(options, GetAgentMessagesOptions) or options is None:
            opts = options or GetAgentMessagesOptions()
        else:
            opts = GetAgentMessagesOptions.model_validate(dict(options))
        try:
            rows = await self._client.list_agent_messages(self.agent_id, limit=opts.limit, offset=opts.offset)
        except errors.CursorSdkError as exc:
            if exc.status == 404:
                return []
            raise
        return agent_messages_from_rows(rows)


def _resolve_api_key(api_key: str | None) -> str:
    return resolve_api_key(api_key)


async def create_cloud_agent(options: AgentOptions) -> CloudAgent:
    api_key = _resolve_api_key(options.apiKey)
    client = CloudApiClient(api_key)
    agent_id = options.agentId or new_cloud_agent_id()
    return CloudAgent(agent_id, client, options, server_created=False)


async def resume_cloud_agent(agent_id: str, options: AgentOptions) -> CloudAgent:
    api_key = _resolve_api_key(options.apiKey)
    client = CloudApiClient(api_key)
    merged = options.model_copy(update={"agentId": agent_id}, deep=True)
    return CloudAgent(agent_id, client, merged, server_created=True)
