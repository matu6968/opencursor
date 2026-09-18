from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from opencursor import errors, messages
from opencursor._auth import resolve_default_api_key
from opencursor._connect import AgentRunSession, ConnectClient
from opencursor._custom_tools import (
    custom_tool_definitions,
    custom_tools_configured,
    custom_tools_from_options,
    execute_custom_tool,
    is_custom_user_tools_call,
)
from opencursor._diagnostics import exec_diagnostics
from opencursor._mcp import McpManager, mcp_servers_configured
from opencursor._protobuf import encode_message, oneof_case
from opencursor._ripgrep import exec_grep
from opencursor._run_api import token_usage_from_turn_ended
from opencursor._sandbox import (
    SandboxRuntime,
    effective_js_policy,
    js_policy_to_proto,
    run_sandboxed,
    sandbox_is_on,
)
from opencursor._sdk_config import get_default_use_http1_for_agent
from opencursor._settings import load_project_rules, project_settings_enabled
from opencursor._shell_parse import denylist_reason, parse_shell_command
from opencursor._subagents import (
    DiscardStore,
    SubagentJob,
    SubagentRegistry,
    TASK_PROTO_TOOL,
    agents_configured,
    custom_subagents_for_context,
    definition_for_type,
    execute_hook_result,
    nested_tool_names,
    resolve_nested_model,
)
from opencursor.types import AgentOptions, ModelSelection, SteerAckOutcome

MINIMAL_PROTO_TOOLS = [
    "read_tool_call",
    "ls_tool_call",
    "grep_tool_call",
    "edit_tool_call",
    "glob_tool_call",
    "shell_tool_call",
    "web_fetch_tool_call",
    "fetch_tool_call",
    "web_search_tool_call",
    "read_lints_tool_call",
]

MCP_PROTO_TOOLS = [
    "mcp_tool_call",
    "get_mcp_tools_tool_call",
    "list_mcp_resources_tool_call",
    "read_mcp_resource_tool_call",
]

PUBLIC_TO_PROTO = {
    "read": "read_tool_call",
    "ls": "ls_tool_call",
    "grep": "grep_tool_call",
    "edit": "edit_tool_call",
    "write": "edit_tool_call",
    "glob": "glob_tool_call",
    "shell": "shell_tool_call",
    "webFetch": "web_fetch_tool_call",
    "web_fetch": "web_fetch_tool_call",
    "fetch": "fetch_tool_call",
    "webSearch": "web_search_tool_call",
    "web_search": "web_search_tool_call",
    "mcp": "mcp_tool_call",
    "task": "task_tool_call",
    "readLints": "read_lints_tool_call",
    "read_lints": "read_lints_tool_call",
}

EXEC_RESULT_FIELD = {
    "shell_args": "shell_result",
    "mini_swe_agent_bash_args": "shell_result",
    "write_args": "write_result",
    "grep_args": "grep_result",
    "read_args": "read_result",
    "redacted_read_args": "redacted_read_result",
    "ls_args": "ls_result",
    "request_context_args": "request_context_result",
    "shell_allowlist_precheck_args": "shell_allowlist_precheck_result",
    "web_fetch_allowlist_precheck_args": "web_fetch_allowlist_precheck_result",
    "diagnostics_args": "diagnostics_result",
    "canvas_diagnostics_args": "canvas_diagnostics_result",
    "delete_args": "delete_result",
    "fetch_args": "fetch_result",
    "mcp_args": "mcp_result",
    "mcp_state_exec_args": "mcp_state_exec_result",
    "mcp_allowlist_precheck_args": "mcp_allowlist_precheck_result",
    "list_mcp_resources_exec_args": "list_mcp_resources_exec_result",
    "read_mcp_resource_exec_args": "read_mcp_resource_exec_result",
    "subagent_args": "subagent_result",
    "subagent_await_args": "subagent_await_result",
    "force_background_subagent_args": "force_background_subagent_result",
    "execute_hook_args": "execute_hook_result",
    "pi_find_args": "pi_find_result",
    "smart_mode_classifier_args": "smart_mode_classifier_result",
}

FETCH_MAX_BYTES = 1_000_000
STEER_ACK_TIMEOUT_S = 15.0
_STEER_STATE_CASES = ("queued", "delivered", "queued_for_next_turn", "cancelled", "rejected")


class _SteerWaiter:
    __slots__ = ("future", "timer")

    def __init__(self, future: asyncio.Future[str]) -> None:
        self.future = future
        self.timer: asyncio.TimerHandle | None = None

    def resolve(self, outcome: str) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if not self.future.done():
            self.future.set_result(outcome)

    def confirm(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None


def context_injection_state_case(state: dict[str, Any] | None) -> str | None:
    if not isinstance(state, dict):
        return None
    case = oneof_case(state, "state")
    if case:
        return case
    for name in _STEER_STATE_CASES:
        if name in state:
            return name
    return None


def steer_outcome_from_state(state_case: str | None) -> str:
    if state_case == "queued":
        return "confirm_steering"
    if state_case == "delivered":
        return "complete_delivered"
    return "revert_to_followup"


FETCH_TIMEOUT_S = 30.0
FETCH_USER_AGENT = "Mozilla/5.0 (compatible; CursorSDK/1.0.31)"
DEV_FORCE_SMART_MODE_CLASSIFIER_BLOCK_TOKEN_ENV = (
    "CURSOR_SDK_DEV_FORCE_NEXT_SMART_MODE_CLASSIFIER_BLOCK_TOKEN"
)
_SMART_MODE_CLASSIFIER_NO_HANDLER = (
    "No handler found for server message of type smartModeClassifierArgs"
)
_APPROVAL_ACTION_LABELS = {
    "shell": "this shell command",
    "mcp": "this MCP tool call",
    "write": "this file edit",
    "delete": "this file deletion",
}


def resolve_allowed_tools(options: AgentOptions) -> list[str] | None:
    tools = getattr(options, "tools", None)
    if tools is None:
        out = list(MINIMAL_PROTO_TOOLS)
        if mcp_servers_configured(options) or custom_tools_configured(options):
            out.extend(name for name in MCP_PROTO_TOOLS if name not in out)
        if agents_configured(options) and TASK_PROTO_TOOL not in out:
            out.append(TASK_PROTO_TOOL)
        return out
    out: list[str] = []
    for name in tools:
        proto = PUBLIC_TO_PROTO.get(str(name), str(name))
        if not proto.endswith("_tool_call"):
            proto = f"{proto}_tool_call"
        if proto == "mcp_tool_call" or str(name) == "mcp":
            for item in MCP_PROTO_TOOLS:
                if item not in out:
                    out.append(item)
            continue
        if proto not in out:
            out.append(proto)
    if custom_tools_configured(options):
        for item in MCP_PROTO_TOOLS:
            if item not in out:
                out.append(item)
    return out


def _cwd(options: AgentOptions) -> Path:
    local = options.local
    raw = getattr(local, "cwd", None) if local is not None else None
    if isinstance(raw, list):
        raw = raw[0] if raw else "."
    return Path(raw or ".").expanduser().resolve()


def _workspace_paths(options: AgentOptions) -> list[str]:
    cwd = str(_cwd(options))
    paths = [cwd]
    local = options.local
    extra = getattr(local, "dirs", None) if local is not None else None
    for item in extra or []:
        resolved = str(Path(item).expanduser().resolve())
        if resolved not in paths:
            paths.append(resolved)
    return paths


def _conversation_mode(options: AgentOptions) -> str:
    mode = getattr(options, "mode", None) or "agent"
    return "AGENT_MODE_PLAN" if mode == "plan" else "AGENT_MODE_AGENT"


def _unwrap_startup_exc(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        auth = next((inner for inner in exc.exceptions if isinstance(inner, errors.AuthenticationError)), None)
        exc = auth if auth is not None else exc.exceptions[0]
    return exc


def _should_reraise_startup(exc: BaseException) -> bool:
    if isinstance(exc, errors.AuthenticationError):
        return exc.message != "Missing CURSOR_API_KEY"
    return isinstance(exc, errors.CursorAgentError) and exc.status is not None


class LocalAgentExecutor:
    def __init__(
        self,
        *,
        store: Any,
        agent_id: str,
        run_id: str,
        options: AgentOptions,
        model: ModelSelection,
        prompt: str,
        conversation_id: str,
        request_id: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        nested: bool = False,
        custom_system_prompt: str | None = None,
        subagent_type_name: str | None = None,
        shared_client: ConnectClient | None = None,
        shared_mcp: McpManager | None = None,
    ) -> None:
        self.store = store
        self.agent_id = agent_id
        self.run_id = run_id
        self.options = options
        self.model = model
        self.prompt = prompt
        self.conversation_id = conversation_id
        self.checkpoint = checkpoint
        self.cwd = _cwd(options)
        self.kv: dict[bytes, bytes] = {}
        self._client: ConnectClient | None = shared_client
        self._request_id = request_id or str(uuid.uuid4())
        self._assistant_text: list[str] = []
        self._thinking: list[str] = []
        self._abort = asyncio.Event()
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._allowed_tools: list[str] | None = None
        self._mcp = shared_mcp if shared_mcp is not None else McpManager()
        self._owns_client = shared_client is None
        self._owns_mcp = shared_mcp is None
        self._nested = nested
        self._custom_system_prompt = custom_system_prompt
        self._subagent_type_name = subagent_type_name
        self._tool_call_count = 0
        self._subagents = SubagentRegistry()
        self._sandbox = SandboxRuntime.from_options(options, self.cwd)
        self._sandbox.raise_if_requested_unsupported()
        self._run_session: AgentRunSession | None = None
        self._custom_tools = custom_tools_from_options(options)
        self._generation_active = False
        self._session_ready = asyncio.Event()
        self._started = asyncio.Event()
        self._steer_waiters: dict[str, _SteerWaiter] = {}

    async def wait_until_started(self) -> None:
        await self._started.wait()

    @property
    def generation_active(self) -> bool:
        return self._generation_active

    def _fail_closed_approval(self) -> bool:
        return self._sandbox.enabled or _auto_review_enabled(self.options)

    async def run(self) -> dict[str, Any] | None:
        api_key = (resolve_default_api_key(self.options.apiKey) or "").strip()
        client: ConnectClient | None = self._client
        try:
            if client is None:
                if not api_key:
                    raise errors.AuthenticationError("Missing CURSOR_API_KEY", is_retryable=False)
                local = self.options.local
                use_http1 = getattr(local, "useHttp1ForAgent", None) if local is not None else None
                if use_http1 is None:
                    use_http1 = get_default_use_http1_for_agent()
                client = ConnectClient(
                    api_key=api_key,
                    use_http1=use_http1,
                )
                self._client = client
            allowed = resolve_allowed_tools(self.options)
            self._allowed_tools = allowed
            starts: list[Any] = []
            if self._owns_client or not client._access_token:
                starts.append(client.exchange_access_token())
            if self._owns_mcp:
                starts.append(self._mcp.start(self.options, self.cwd))
            if starts:
                await asyncio.gather(*starts)
            self._generation_active = True
            ready = asyncio.Event()
            stream_task = asyncio.create_task(self._consume_server(client, allowed, ready))
            await asyncio.wait_for(ready.wait(), timeout=30)
            if stream_task.done():
                await stream_task
            self._started.set()
            await self._append(
                {
                    "run_request": self._run_request(),
                }
            )
            hb = asyncio.create_task(self._heartbeats())
            try:
                await stream_task
            finally:
                hb.cancel()
            if self._nested:
                return {
                    "success": {
                        "agent_id": self.agent_id,
                        "final_message": "".join(self._assistant_text),
                        "tool_call_count": self._tool_call_count,
                    }
                }
            return None
        except Exception as exc:
            exc = _unwrap_startup_exc(exc)
            startup = not self._started.is_set()
            if self._nested:
                self._started.set()
                raise
            await self._fail(exc)
            self._started.set()
            if startup and _should_reraise_startup(exc):
                raise
            return None
        finally:
            self._generation_active = False
            self._revert_pending_steers()
            if self._owns_mcp:
                await self._mcp.aclose()
            if self._owns_client and client is not None:
                await client.aclose()

    async def steer(self, text: str) -> SteerAckOutcome:
        if not str(text).strip():
            return "revert_to_followup"
        if not self._generation_active:
            return "revert_to_followup"
        if self._run_session is None:
            try:
                await asyncio.wait_for(self._session_ready.wait(), timeout=STEER_ACK_TIMEOUT_S)
            except TimeoutError:
                return "revert_to_followup"
        if self._run_session is None or self._abort.is_set() or not self._generation_active:
            return "revert_to_followup"
        injection_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        waiter = _SteerWaiter(future)
        waiter.timer = loop.call_later(
            STEER_ACK_TIMEOUT_S, lambda: waiter.resolve("revert_to_followup")
        )
        self._steer_waiters[injection_id] = waiter
        try:
            await self._append(
                {
                    "conversation_action": {
                        "inject_context_action": {
                            "injection_id": injection_id,
                            "expected_run_id": self._request_id,
                            "user_context": {
                                "user_message": {
                                    "text": text,
                                    "message_id": str(uuid.uuid4()),
                                }
                            },
                        }
                    }
                }
            )
            outcome = await future
        except Exception:
            waiter.resolve("revert_to_followup")
            outcome = "revert_to_followup"
        finally:
            self._steer_waiters.pop(injection_id, None)
        if outcome == "complete_delivered":
            return "complete_delivered"
        return "revert_to_followup"

    def _revert_pending_steers(self) -> None:
        for waiter in list(self._steer_waiters.values()):
            waiter.resolve("revert_to_followup")

    def _on_context_injection_state(self, body: dict[str, Any]) -> None:
        injection_id = str(body.get("injection_id") or "")
        waiter = self._steer_waiters.get(injection_id)
        if waiter is None:
            return
        state_case = context_injection_state_case(body.get("state") if isinstance(body.get("state"), dict) else None)
        mapped = steer_outcome_from_state(state_case)
        if mapped == "confirm_steering":
            waiter.confirm()
            return
        waiter.resolve(mapped)

    def _run_request(self) -> dict[str, Any]:
        model_id = self.model.id
        mode = _conversation_mode(self.options)
        state = dict(self.checkpoint) if self.checkpoint else {"mode": mode}
        req = {
            "conversation_state": state,
            "action": {
                "user_message_action": {
                    "user_message": {
                        "text": self.prompt,
                        "message_id": str(uuid.uuid4()),
                        "mode": mode,
                    },
                    "request_context": self._request_context(),
                }
            },
            "model_details": {
                "model_id": model_id,
                "display_model_id": model_id,
                "display_name": model_id,
            },
            "requested_model": {
                "model_id": model_id,
                "max_mode": False,
                "built_in_model": True,
            },
            "mcp_tools": {"mcp_tools": self._all_mcp_tool_definitions()},
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "agent_session_id": self.agent_id,
            "exclude_workspace_context": False,
        }
        if self._custom_system_prompt:
            req["custom_system_prompt"] = self._custom_system_prompt
        if self._subagent_type_name:
            req["subagent_type_name"] = self._subagent_type_name
        return req

    def _all_mcp_tool_definitions(self) -> list[dict[str, Any]]:
        tools = list(self._mcp.tool_definitions())
        if self._custom_tools:
            tools.extend(custom_tool_definitions(self._custom_tools))
        return tools

    def _request_context(self) -> dict[str, Any]:
        cwd = str(self.cwd)
        paths = _workspace_paths(self.options)
        ctx = {
            "env": {
                "os_version": platform.platform(),
                "workspace_paths": paths,
                "shell": os.environ.get("SHELL", "/bin/bash"),
                "sandbox_enabled": self._sandbox.enabled,
                "sandbox_supported": self._sandbox.supported,
                "project_folder": cwd,
                "process_working_directory": cwd,
                "time_zone": time.tzname[0] if time.tzname else "UTC",
                "is_working_dir_home_dir": Path.home().resolve() == self.cwd,
                "smart_mode_classifier_auto_mode_enabled": _auto_review_enabled(self.options),
            },
            "web_search_enabled": self._tool_enabled("web_search_tool_call"),
            "web_fetch_enabled": self._tool_enabled("web_fetch_tool_call", "fetch_tool_call"),
            "read_lints_enabled": self._tool_enabled("read_lints_tool_call"),
            "tools": self._all_mcp_tool_definitions(),
            "mcp_instructions": self._mcp.instructions(),
            "supports_mcp_auth": True,
            "env_info_complete": True,
            "rules_info_complete": True,
            "repository_info_complete": True,
            "custom_subagents_info_complete": True,
            "agent_skills_info_complete": True,
            "mcp_info_complete": True,
            "mcp_file_system_info_complete": True,
            "git_status_info_complete": True,
            "git_repo_info_complete": True,
        }
        token = _dev_force_classifier_block_token()
        if token:
            ctx["env"]["dev_force_next_smart_mode_classifier_block_token"] = token
        custom = custom_subagents_for_context(self.options)
        if custom:
            ctx["custom_subagents"] = custom
        if project_settings_enabled(self.options):
            ctx["non_file_rules"] = load_project_rules(Path(p) for p in paths)
        return ctx

    def _tool_enabled(self, *names: str) -> bool:
        allowed = self._allowed_tools or []
        return any(name in names for name in allowed)

    async def _interaction_response(self, query: dict[str, Any]) -> dict[str, Any]:
        case = oneof_case(query, "query")
        if case == "mcp_auth_request_query" or "mcp_auth_request_query" in query:
            args = (query.get("mcp_auth_request_query") or {}).get("args") or {}
            server = str(args.get("server_identifier") or "")
            body: dict[str, Any] = {"id": query.get("id", 0)}
            try:
                await self._mcp.authenticate(server, cwd=self.cwd)
                body["mcp_auth_request_response"] = {"approved": {}}
            except Exception as exc:
                body["mcp_auth_request_response"] = {"rejected": {"reason": str(exc)}}
            return body
        return _interaction_response_body(query)

    async def _append(self, client_message: dict[str, Any]) -> None:
        assert self._run_session is not None
        payload = encode_message("agent.v1.AgentClientMessage", client_message)
        await self._run_session.send(payload)

    async def _heartbeats(self) -> None:
        try:
            while not self._abort.is_set():
                await asyncio.sleep(5)
                try:
                    await self._append({"client_heartbeat": {}})
                except Exception:
                    return
        except asyncio.CancelledError:
            return

    async def _consume_server(
        self, client: ConnectClient, allowed: list[str] | None, ready: asyncio.Event
    ) -> None:
        finished = False
        try:
            session = await client.open_run(self._request_id, allowed_tools=allowed)
        except BaseException:
            ready.set()
            self._session_ready.set()
            raise
        self._run_session = session
        ready.set()
        self._session_ready.set()
        try:
            async for msg in session:
                if self._abort.is_set():
                    break
                case = oneof_case(msg, "message")
                if case == "interaction_update":
                    await self._on_interaction(msg["interaction_update"])
                elif case == "exec_server_message":
                    self._spawn(self._on_exec_safe(msg["exec_server_message"]))
                elif case == "exec_server_control_message":
                    abort = msg.get("exec_server_control_message") or {}
                    if abort.get("abort"):
                        self._abort.set()
                elif case == "kv_server_message":
                    self._spawn(self._on_kv_safe(msg["kv_server_message"]))
                elif case == "conversation_checkpoint_update":
                    self.checkpoint = msg.get("conversation_checkpoint_update") or {}
                elif case == "interaction_query":
                    query = msg.get("interaction_query") or {}
                    await self._append({"interaction_response": await self._interaction_response(query)})
                tu = msg.get("interaction_update") or {}
                if oneof_case(tu, "message") == "turn_ended":
                    finished = True
                    break
        finally:
            await session.aclose()
            self._run_session = None
            self._revert_pending_steers()
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
        if self._nested:
            if not finished or self._abort.is_set():
                raise RuntimeError("nested agent stream ended before turn_ended")
            return
        if finished and not self._abort.is_set():
            await self._finish()
        elif not self._abort.is_set():
            await self._fail("agent stream ended before turn_ended")

    async def _on_interaction(self, update: dict[str, Any]) -> None:
        case = oneof_case(update, "message")
        if case == "text_delta":
            text = (update.get("text_delta") or {}).get("text") or ""
            if text:
                self._assistant_text.append(text)
                await self._emit_assistant("".join(self._assistant_text))
        elif case == "thinking_delta":
            text = (update.get("thinking_delta") or {}).get("text") or ""
            if text:
                self._thinking.append(text)
                await self.store.append_sdk_message(
                    {
                        "type": "thinking",
                        "agent_id": self.agent_id,
                        "run_id": self.run_id,
                        "text": "".join(self._thinking),
                    }
                )
        elif case == "thinking_completed":
            dur = (update.get("thinking_completed") or {}).get("thinking_duration_ms")
            if dur is not None:
                await self.store.append_sdk_message(
                    {
                        "type": "thinking",
                        "agent_id": self.agent_id,
                        "run_id": self.run_id,
                        "text": "".join(self._thinking),
                        "thinking_duration_ms": dur,
                    }
                )
        elif case == "tool_call_started":
            started = update.get("tool_call_started") or {}
            self._tool_call_count += 1
            await self.store.append_sdk_message(
                {
                    "type": "tool_call",
                    "agent_id": self.agent_id,
                    "run_id": self.run_id,
                    "call_id": started.get("call_id") or "",
                    "name": _tool_call_name(started.get("tool_call") or {}),
                    "status": "running",
                    "args": started.get("tool_call"),
                }
            )
        elif case == "tool_call_completed":
            done = update.get("tool_call_completed") or {}
            await self.store.append_sdk_message(
                {
                    "type": "tool_call",
                    "agent_id": self.agent_id,
                    "run_id": self.run_id,
                    "call_id": done.get("call_id") or "",
                    "name": _tool_call_name(done.get("tool_call") or {}),
                    "status": "completed",
                    "args": done.get("tool_call"),
                }
            )
        elif case == "user_message_appended":
            um = ((update.get("user_message_appended") or {}).get("user_message") or {})
            text = um.get("text") or self.prompt
            await self.store.append_sdk_message(
                {
                    "type": "user",
                    "agent_id": self.agent_id,
                    "run_id": self.run_id,
                    "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                }
            )
        elif case == "context_injection_state" or "context_injection_state" in update:
            self._on_context_injection_state(update.get("context_injection_state") or {})
        elif case == "turn_ended":
            ended = update.get("turn_ended") or {}
            usage = token_usage_from_turn_ended(ended)
            if usage is not None:
                usage_uuid = str(uuid.uuid4())
                payload = usage.model_dump(by_alias=True)
                payload["runId"] = usage_uuid
                await self.store.append_sdk_message(
                    {
                        "type": "usage",
                        "agent_id": self.agent_id,
                        "run_id": self.run_id,
                        "usage": payload,
                    }
                )

    async def _emit_assistant(self, text: str) -> None:
        await self.store.append_sdk_message(
            {
                "type": "assistant",
                "agent_id": self.agent_id,
                "run_id": self.run_id,
                "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            }
        )

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_kv_safe(self, msg: dict[str, Any]) -> None:
        try:
            await self._on_kv(msg)
        except Exception:
            return

    async def _on_exec_safe(self, msg: dict[str, Any]) -> None:
        try:
            await self._on_exec(msg)
        except Exception:
            return

    async def _on_kv(self, msg: dict[str, Any]) -> None:
        kv_id = msg.get("id", 0)
        case = oneof_case(msg, "message")
        reply: dict[str, Any] = {"id": kv_id}
        if case == "get_blob_args":
            blob_id = (msg.get("get_blob_args") or {}).get("blob_id") or b""
            reply["get_blob_result"] = {"blob_data": self.kv.get(bytes(blob_id), b"")}
        elif case == "set_blob_args":
            args = msg.get("set_blob_args") or {}
            blob_id = bytes(args.get("blob_id") or b"")
            self.kv[blob_id] = bytes(args.get("blob_data") or b"")
            reply["set_blob_result"] = {}
        else:
            reply["get_blob_result"] = {"error": {"message": "unsupported kv op"}}
        await self._append({"kv_client_message": reply})

    async def _on_exec(self, msg: dict[str, Any]) -> None:
        exec_id = msg.get("id", 0)
        wire_exec_id = msg.get("exec_id") or ""
        case = oneof_case(msg, "message")
        started = time.monotonic()
        result_field = EXEC_RESULT_FIELD.get(case or "")
        payload: dict[str, Any]
        try:
            if case in ("read_args", "redacted_read_args"):
                payload = _exec_read(self.cwd, msg.get(case) or {})
            elif case == "ls_args":
                payload = _exec_ls(self.cwd, msg.get("ls_args") or {})
            elif case == "pi_find_args":
                payload = _exec_pi_find(self.cwd, msg.get("pi_find_args") or {})
            elif case == "grep_args":
                payload = exec_grep(self.cwd, msg.get("grep_args") or {}, self._sandbox)
            elif case == "write_args":
                payload = _exec_write(self.cwd, msg.get("write_args") or {})
            elif case in ("shell_args", "mini_swe_agent_bash_args"):
                shell_args = msg.get(case) or {}
                blocked = _shell_smart_mode_rejected(self.cwd, shell_args)
                if blocked is not None:
                    payload = blocked
                else:
                    payload = _exec_shell(self.cwd, shell_args, self._sandbox)
                    unsupported = payload.get("sandbox_unsupported")
                    if unsupported:
                        payload = {
                            "spawn_error": {
                                "command": unsupported.get("command") or "",
                                "working_directory": unsupported.get("working_directory") or str(self.cwd),
                                "error": unsupported.get("reason") or "sandbox unsupported",
                            }
                        }
            elif case == "shell_stream_args":
                await self._on_shell_stream(exec_id, wire_exec_id, msg.get("shell_stream_args") or {}, started)
                return
            elif case == "request_context_args":
                payload = {
                    "success": {
                        "request_context": self._request_context(),
                        "served_from_disk_cache": False,
                    }
                }
            elif case == "shell_allowlist_precheck_args":
                payload = {"allowlisted": True}
            elif case == "web_fetch_allowlist_precheck_args":
                payload = {"allowlisted": True}
            elif case == "fetch_args":
                payload = await _exec_fetch(msg.get("fetch_args") or {})
            elif case == "mcp_allowlist_precheck_args":
                payload = {"allowlisted": True}
            elif case == "mcp_args":
                mcp_args = msg.get("mcp_args") or {}
                if self._custom_tools and is_custom_user_tools_call(mcp_args):
                    payload = await execute_custom_tool(self._custom_tools, mcp_args)
                else:
                    blocked = _mcp_smart_mode_rejected(mcp_args)
                    if blocked is not None:
                        payload = blocked
                    else:
                        payload = await self._mcp.call_tool(mcp_args)
            elif case == "mcp_state_exec_args":
                args = msg.get("mcp_state_exec_args") or {}
                payload = {
                    "success": {
                        "servers": self._mcp.state_servers(list(args.get("server_identifiers") or [])),
                    }
                }
            elif case == "list_mcp_resources_exec_args":
                payload = await self._mcp.list_resources(msg.get("list_mcp_resources_exec_args") or {})
            elif case == "read_mcp_resource_exec_args":
                resource_args = msg.get("read_mcp_resource_exec_args") or {}
                blocked = _mcp_resource_smart_mode_result(
                    resource_args, provider=self._fail_closed_approval()
                )
                if blocked is not None:
                    payload = blocked
                else:
                    payload = await self._mcp.read_resource(resource_args)
            elif case == "smart_mode_classifier_args":
                payload = _exec_smart_mode_classifier(msg.get("smart_mode_classifier_args") or {})
            elif case == "delete_args":
                payload = _exec_delete(self.cwd, msg.get("delete_args") or {})
            elif case == "diagnostics_args":
                payload = await asyncio.to_thread(
                    exec_diagnostics, self.cwd, msg.get("diagnostics_args") or {}
                )
            elif case == "canvas_diagnostics_args":
                args = msg.get("canvas_diagnostics_args") or {}
                payload = {
                    "error": {
                        "path": args.get("path") or "",
                        "error": "canvas diagnostics are not supported in the native Python executor",
                    }
                }
            elif case == "subagent_args":
                payload = await self._exec_subagent(msg.get("subagent_args") or {})
            elif case == "subagent_await_args":
                payload = await self._exec_subagent_await(msg.get("subagent_await_args") or {})
            elif case == "force_background_subagent_args":
                payload = self._exec_force_background(msg.get("force_background_subagent_args") or {})
            elif case == "execute_hook_args":
                payload = execute_hook_result(msg.get("execute_hook_args") or {})
            else:
                raise NotImplementedError(f"unsupported exec {case}")
        except Exception as exc:
            payload = _exec_error_payload(case, exc)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body: dict[str, Any] = {
            "id": exec_id,
            "exec_id": wire_exec_id,
            "local_execution_time_ms": elapsed_ms,
        }
        if result_field:
            body[result_field] = payload
        else:
            await self._append(
                {
                    "exec_client_control_message": {
                        "throw": {
                            "id": exec_id,
                            "error": f"unsupported exec {case}",
                        }
                    }
                }
            )
            return
        await self._append({"exec_client_message": body})

    async def _on_shell_stream(
        self, exec_id: int, wire_exec_id: str, args: dict[str, Any], started: float
    ) -> None:
        async def _stream_event(event: dict[str, Any], elapsed_ms: int) -> None:
            await self._append(
                {
                    "exec_client_message": {
                        "id": exec_id,
                        "exec_id": wire_exec_id,
                        "local_execution_time_ms": elapsed_ms,
                        "shell_stream": event,
                    }
                }
            )

        command = args.get("command") or ""
        working = str(_safe_path(self.cwd, args.get("working_directory") or str(self.cwd)))
        parsed = parse_shell_command(command)
        reason = denylist_reason(
            command, parsed["structured"], list(args.get("admin_command_denylist") or [])
        )
        if reason:
            await _stream_event(
                {
                    "rejected": {
                        "command": command,
                        "working_directory": working,
                        "reason": reason,
                    }
                },
                0,
            )
            await self._append({"exec_client_control_message": {"stream_close": {"id": exec_id}}})
            return
        blocked = _shell_smart_mode_rejected(self.cwd, args)
        if blocked is not None:
            await _stream_event(blocked, 0)
            await self._append({"exec_client_control_message": {"stream_close": {"id": exec_id}}})
            return
        start_policy = js_policy_to_proto(
            effective_js_policy(self._sandbox, args.get("requested_sandbox_policy"))
        )
        await _stream_event({"start": {"sandbox_policy": start_policy}}, 0)
        result = await asyncio.to_thread(_exec_shell, self.cwd, args, self._sandbox)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        unsupported = result.get("sandbox_unsupported")
        if unsupported:
            await _stream_event({"sandbox_unsupported": unsupported}, elapsed_ms)
            await self._append({"exec_client_control_message": {"stream_close": {"id": exec_id}}})
            return
        success = result.get("success") or result.get("failure") or {}
        timeout = result.get("timeout") or {}
        spawn = result.get("spawn_error") or {}
        stdout = success.get("stdout") or timeout.get("stdout") or ""
        stderr = success.get("stderr") or timeout.get("stderr") or spawn.get("error") or ""
        cwd = success.get("working_directory") or timeout.get("working_directory") or str(self.cwd)
        code = success.get("exit_code")
        if code is None:
            code = 124 if timeout else 1
        if stdout:
            await _stream_event({"stdout": {"data": stdout}}, elapsed_ms)
        if stderr:
            await _stream_event({"stderr": {"data": stderr}}, elapsed_ms)
        await _stream_event(
            {
                "exit": {
                    "code": int(code),
                    "cwd": cwd,
                    "aborted": bool(timeout),
                    "local_execution_time_ms": elapsed_ms,
                }
            },
            elapsed_ms,
        )
        await self._append({"exec_client_control_message": {"stream_close": {"id": exec_id}}})

    async def _exec_subagent(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._nested:
            return {"error": {"error": "nested subagents are not supported"}}
        job = self._start_subagent_job(args)
        if args.get("run_in_background"):
            return {
                "success": {
                    "agent_id": job.agent_id,
                    "background_reason": "SUBAGENT_BACKGROUND_REASON_AGENT_REQUEST",
                }
            }
        wait_done = asyncio.create_task(asyncio.shield(job.future))
        wait_bg = asyncio.create_task(job.force_background.wait())
        done, pending = await asyncio.wait({wait_done, wait_bg}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if job.future.done():
            return job.future.result()
        return {
            "success": {
                "agent_id": job.agent_id,
                "background_reason": "SUBAGENT_BACKGROUND_REASON_USER_REQUEST",
            }
        }

    def _start_subagent_job(self, args: dict[str, Any]) -> SubagentJob:
        job = SubagentJob(
            agent_id=str(uuid.uuid4()),
            tool_call_id=str(args.get("tool_call_id") or ""),
            future=asyncio.get_running_loop().create_future(),
        )
        self._subagents.register(job)
        self._spawn(self._drive_subagent_job(job, args))
        return job

    async def _drive_subagent_job(self, job: SubagentJob, args: dict[str, Any]) -> None:
        try:
            result = await self._run_nested_executor(job.agent_id, args)
        except Exception as exc:
            result = {"error": {"agent_id": job.agent_id, "error": str(exc)}}
        if not job.future.done():
            job.future.set_result(result)

    async def _run_nested_executor(self, agent_id: str, args: dict[str, Any]) -> dict[str, Any]:
        definitions = custom_subagents_for_context(self.options)
        definition = definition_for_type(definitions, str(args.get("subagent_type") or ""))
        nested_options = self.options.model_copy(
            update={"tools": nested_tool_names(self._allowed_tools), "agents": None}
        )
        child = LocalAgentExecutor(
            store=DiscardStore(),
            agent_id=agent_id,
            run_id=str(uuid.uuid4()),
            options=nested_options,
            model=resolve_nested_model(self.model, args, definition),
            prompt=str(args.get("prompt") or ""),
            conversation_id=str(uuid.uuid4()),
            nested=True,
            custom_system_prompt=(definition or {}).get("prompt") or None,
            subagent_type_name=str(args.get("subagent_type") or "") or None,
            shared_client=self._client,
            shared_mcp=self._mcp,
        )
        result = await child.run()
        if not result:
            return {"error": {"agent_id": agent_id, "error": "nested subagent returned no result"}}
        return result

    async def _exec_subagent_await(self, args: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(args.get("agent_id") or "")
        job = self._subagents.get_agent(agent_id)
        if job is None:
            return {"not_found": {"agent_id": agent_id}}
        timeout_ms = int(args.get("timeout_ms") or 0)
        timeout_s = timeout_ms / 1000.0 if timeout_ms > 0 else None
        try:
            result = await asyncio.wait_for(asyncio.shield(job.future), timeout=timeout_s)
        except asyncio.TimeoutError:
            return {"still_running": {"agent_id": agent_id}}
        success = result.get("success") if isinstance(result, dict) else None
        error = result.get("error") if isinstance(result, dict) else None
        if success:
            return {
                "complete": {
                    "agent_id": success.get("agent_id") or agent_id,
                    "final_message": success.get("final_message"),
                    "tool_call_count": success.get("tool_call_count") or 0,
                }
            }
        return {"error": {"agent_id": agent_id, "error": (error or {}).get("error") or "subagent failed"}}

    def _exec_force_background(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._subagents.get_tool(str(args.get("tool_call_id") or ""))
        if job is None:
            return {"status": "FORCE_BACKGROUND_SUBAGENT_STATUS_NOT_FOUND"}
        job.force_background.set()
        return {"status": "FORCE_BACKGROUND_SUBAGENT_STATUS_ACCEPTED"}

    async def _finish(self) -> None:
        await self.store.append_sdk_message(
            {
                "type": "status",
                "agent_id": self.agent_id,
                "run_id": self.run_id,
                "status": "FINISHED",
            }
        )
        await self.store.mark_run_terminal(self.agent_id, self.run_id, "FINISHED")
        await self.store.append_terminal_event(self.agent_id, self.run_id, "finished")

    async def _fail(self, err: BaseException | str) -> None:
        sdk_err = errors.convert_error(err) if not isinstance(err, str) else errors.UnknownAgentError(err)
        run_err = errors.to_run_error(sdk_err)
        message = run_err["message"]
        code = run_err.get("code") or "local_executor_error"
        await self.store.append_sdk_message(
            {
                "type": "status",
                "agent_id": self.agent_id,
                "run_id": self.run_id,
                "status": "ERROR",
                "message": message,
            }
        )
        await self.store.mark_run_terminal(self.agent_id, self.run_id, "ERROR", code)
        await self.store.append_terminal_event(self.agent_id, self.run_id, "error", error_code=code)


def _interaction_response_body(query: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {"id": query.get("id", 0)}
    case = oneof_case(query, "query")
    if case == "web_fetch_request_query" or "web_fetch_request_query" in query:
        body["web_fetch_request_response"] = {"approved": {}}
    elif case == "web_search_request_query" or "web_search_request_query" in query:
        body["web_search_request_response"] = {"approved": {}}
    elif case == "mcp_auth_request_query" or "mcp_auth_request_query" in query:
        body["mcp_auth_request_response"] = {"approved": {}}
    return body


def _auto_review_enabled(options: AgentOptions) -> bool:
    local = getattr(options, "local", None)
    return local is not None and local.autoReview is True


def _dev_force_classifier_block_token() -> str | None:
    token = os.environ.get(DEV_FORCE_SMART_MODE_CLASSIFIER_BLOCK_TOKEN_ENV)
    if token:
        return token
    return None


def interactive_approval_denied_reason(action: str = "operation") -> str:
    label = _APPROVAL_ACTION_LABELS.get(action, "this operation")
    return (
        f"Local SDK runs cannot request interactive approval for {label}. "
        "Keep the action within the configured sandbox policy or Smart Auto Review's "
        "auto-approval boundary, or re-run without sandboxing/autoReview enabled."
    )


def _smart_mode_approval_requested(args: dict[str, Any]) -> bool:
    if args.get("smart_mode_approval_only"):
        return True
    approval = args.get("smart_mode_approval")
    if not isinstance(approval, dict):
        return False
    return bool(approval.get("reason") or approval.get("request_id"))


def _mcp_smart_mode_rejected(args: dict[str, Any]) -> dict[str, Any] | None:
    if args.get("skip_approval") or not _smart_mode_approval_requested(args):
        return None
    name = str(args.get("name") or args.get("tool_name") or "")
    reason = interactive_approval_denied_reason("mcp")
    return {"rejected": {"reason": f"User rejected MCP: {name} - {reason}"}}


def _mcp_resource_smart_mode_result(
    args: dict[str, Any], *, provider: bool
) -> dict[str, Any] | None:
    approval = args.get("smart_mode_approval")
    if not isinstance(approval, dict) or approval.get("reason") is None:
        return None
    uri = str(args.get("uri") or "")
    if not provider:
        return {
            "error": {
                "uri": uri,
                "error": "Auto-review approval provider is not configured",
            }
        }
    reason = interactive_approval_denied_reason("mcp")
    return {"rejected": {"reason": f"User rejected MCP resource fetch - {reason}"}}


def _shell_smart_mode_rejected(cwd: Path, args: dict[str, Any]) -> dict[str, Any] | None:
    if args.get("skip_approval") or not _smart_mode_approval_requested(args):
        return None
    command = args.get("command") or ""
    working = str(_safe_path(cwd, args.get("working_directory") or str(cwd)))
    return {
        "rejected": {
            "command": command,
            "working_directory": working,
            "reason": interactive_approval_denied_reason("shell"),
        }
    }


def _exec_smart_mode_classifier(_args: dict[str, Any]) -> dict[str, Any]:
    return {"error": {"error": _SMART_MODE_CLASSIFIER_NO_HANDLER}}


def _fetch_url_error(url: str) -> str | None:
    if not url:
        return "missing url"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported url scheme: {parsed.scheme or '(none)'}"
    if not parsed.netloc:
        return "invalid url"
    return None


def _fetch_is_binary(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    if not ct:
        return False
    if ct.startswith("text/") or ct.startswith("application/json") or ct.startswith("application/xml"):
        return False
    if ct.startswith("application/javascript") or ct.startswith("application/xhtml"):
        return False
    if "+json" in ct or "+xml" in ct:
        return False
    return ct.startswith(("image/", "audio/", "video/", "font/", "application/octet-stream", "application/pdf", "application/zip"))


def _decode_fetch_body(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    ct = content_type.lower()
    if "charset=" in ct:
        charset = ct.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"') or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


async def _exec_fetch(args: dict[str, Any]) -> dict[str, Any]:
    url = (args.get("url") or "").strip()
    err = _fetch_url_error(url)
    if err:
        return {"error": {"url": url, "error": err}}
    headers = {
        "User-Agent": FETCH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.1",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT_S, max_redirects=10) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                final_url = str(resp.url)
                scheme_err = _fetch_url_error(final_url)
                if scheme_err:
                    return {"error": {"url": url, "error": scheme_err}}
                content_type = resp.headers.get("content-type") or ""
                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    remain = FETCH_MAX_BYTES - total
                    if remain <= 0:
                        truncated = True
                        break
                    if len(chunk) > remain:
                        chunks.append(chunk[:remain])
                        total += remain
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                raw = b"".join(chunks)
                status = resp.status_code
        if _fetch_is_binary(content_type):
            return {
                "error": {
                    "url": final_url,
                    "error": f"binary content not returned ({content_type or 'unknown type'}, {total} bytes)",
                }
            }
        text = _decode_fetch_body(raw, content_type)
        if truncated:
            text += "\n\n[truncated]"
        return {
            "success": {
                "url": final_url,
                "content": text,
                "status_code": status,
                "content_type": content_type,
            }
        }
    except Exception as exc:
        return {"error": {"url": url, "error": str(exc)}}


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    case = oneof_case(tool_call, "tool")
    if case:
        return case.removesuffix("_tool_call")
    for key in tool_call:
        if key.endswith("_tool_call"):
            return key.removesuffix("_tool_call")
    return "unknown"


def _safe_path(cwd: Path, raw: str | None) -> Path:
    path = Path(raw or ".")
    if not path.is_absolute():
        path = cwd / path
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(cwd)
    except ValueError:
        # allow absolute paths the model asked for; still resolve
        pass
    return resolved


def _exec_read(cwd: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = _safe_path(cwd, args.get("path"))
    if not path.exists():
        return {"file_not_found": {"path": str(path)}}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return {"permission_denied": {"path": str(path)}}
    except OSError as exc:
        return {"error": {"path": str(path), "error": str(exc)}}
    lines = text.splitlines()
    offset = args.get("offset") or 0
    limit = args.get("limit")
    sliced = lines[offset:]
    truncated = False
    if isinstance(limit, int) and limit >= 0:
        truncated = len(sliced) > limit
        sliced = sliced[:limit]
    content = "\n".join(sliced)
    return {
        "success": {
            "path": str(path),
            "content": content,
            "total_lines": len(lines),
            "file_size": path.stat().st_size,
            "truncated": truncated,
            "range_applied": bool(offset or limit),
        }
    }


def _exec_ls(cwd: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = _safe_path(cwd, args.get("path"))
    if not path.exists():
        return {"error": {"error": f"not found: {path}"}}
    ignore = set(args.get("ignore") or [])
    try:
        node = _ls_node(path, ignore=ignore, depth=0, max_depth=2)
    except OSError as exc:
        return {"error": {"error": str(exc)}}
    return {"success": {"directory_tree_root": node}}


def _exec_glob(cwd: Path, args: dict[str, Any]) -> dict[str, Any]:
    pattern = str(args.get("glob_pattern") or args.get("globPattern") or args.get("pattern") or "*")
    root = _safe_path(cwd, args.get("target_directory") or args.get("targetDirectory") or args.get("path") or ".")
    try:
        files = _glob_files(root, pattern)
    except OSError as exc:
        return {"error": {"error": str(exc)}}
    limit = 2000
    truncated = len(files) > limit
    files = files[:limit]
    return {
        "success": {
            "pattern": pattern,
            "path": str(root),
            "files": files,
            "total_files": len(files),
            "client_truncated": truncated,
            "ripgrep_truncated": False,
        }
    }


def _exec_pi_find(cwd: Path, args: dict[str, Any]) -> dict[str, Any]:
    pattern = str(args.get("pattern") or "*")
    root = _safe_path(cwd, args.get("path") or ".")
    limit = args.get("limit")
    try:
        files = _glob_files(root, pattern)
    except OSError as exc:
        return {"error": {"error": str(exc)}}
    reached = False
    if isinstance(limit, int) and limit >= 0 and len(files) > limit:
        files = files[:limit]
        reached = True
    return {
        "success": {
            "output": "\n".join(files),
            "result_limit_reached": len(files) if reached else None,
        }
    }


def _glob_files(root: Path, pattern: str) -> list[str]:
    if root.is_file():
        return [str(root)] if root.match(pattern) or pattern in ("*", "**/*") else []
    matches: list[str] = []
    iterator = root.rglob(pattern) if "**" in pattern or "/" in pattern or "*" in pattern or "?" in pattern else root.glob(pattern)
    try:
        for path in iterator:
            if not path.is_file():
                continue
            if ".git" in path.parts or "node_modules" in path.parts:
                continue
            matches.append(str(path))
            if len(matches) >= 4000:
                break
    except OSError:
        pass
    matches.sort()
    return matches


def _ls_node(path: Path, *, ignore: Iterable[str], depth: int, max_depth: int) -> dict[str, Any]:
    ignore_set = set(ignore)
    files = []
    dirs = []
    ext_counts: dict[str, int] = {}
    num_files = 0
    if path.is_dir() and depth <= max_depth:
        try:
            entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            entries = []
        for child in entries:
            if child.name in ignore_set or child.name.startswith("."):
                continue
            if child.is_dir():
                dirs.append(_ls_node(child, ignore=ignore_set, depth=depth + 1, max_depth=max_depth))
            else:
                num_files += 1
                files.append({"name": child.name})
                ext = child.suffix.lower()
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
    return {
        "abs_path": str(path),
        "children_dirs": dirs,
        "children_files": files,
        "children_were_processed": path.is_dir() and depth <= max_depth,
        "full_subtree_extension_counts": ext_counts,
        "num_files": num_files,
    }


def _exec_write(cwd: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = _safe_path(cwd, args.get("path"))
    text = args.get("file_text")
    data = args.get("file_bytes")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if data:
            path.write_bytes(bytes(data))
            size = len(bytes(data))
            lines = 0
        else:
            payload = text if isinstance(text, str) else ""
            path.write_text(payload, encoding="utf-8")
            size = len(payload.encode("utf-8"))
            lines = payload.count("\n") + (1 if payload else 0)
    except PermissionError:
        return {"permission_denied": {"path": str(path)}}
    except OSError as exc:
        return {"error": {"error": str(exc)}}
    out: dict[str, Any] = {
        "success": {
            "path": str(path),
            "lines_created": lines,
            "file_size": size,
        }
    }
    if args.get("return_file_content_after_write"):
        out["success"]["file_content_after_write"] = path.read_text(encoding="utf-8", errors="replace")
    return out


def _exec_delete(cwd: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = _safe_path(cwd, args.get("path"))
    try:
        if path.is_dir():
            path.rmdir()
        elif path.exists():
            path.unlink()
        return {"success": {"path": str(path)}}
    except OSError as exc:
        return {"error": {"error": str(exc)}}


def _exec_shell(
    cwd: Path, args: dict[str, Any], sandbox: SandboxRuntime | None = None
) -> dict[str, Any]:
    command = args.get("command") or ""
    working = _safe_path(cwd, args.get("working_directory") or str(cwd))
    parsed = parse_shell_command(command)
    reason = denylist_reason(
        command, parsed["structured"], list(args.get("admin_command_denylist") or [])
    )
    if reason:
        return {
            "rejected": {
                "command": command,
                "working_directory": str(working),
                "reason": reason,
            }
        }
    raw_timeout = args.get("timeout") or 30_000
    try:
        timeout_val = float(raw_timeout)
    except (TypeError, ValueError):
        timeout_val = 30_000
    # ShellArgs.timeout is milliseconds in the TS SDK (e.g. 30000).
    timeout_s = timeout_val / 1000.0 if timeout_val >= 1000 else timeout_val
    timeout_s = min(max(timeout_s, 1.0), 120.0)
    policy = effective_js_policy(sandbox, args.get("requested_sandbox_policy"))
    proto_policy = js_policy_to_proto(policy)
    started = time.monotonic()
    try:
        if sandbox_is_on(policy):
            binary = sandbox.binary if sandbox is not None else None
            if binary is None:
                return {
                    "sandbox_unsupported": {
                        "command": command,
                        "working_directory": str(working),
                        "sandbox_policy_type": str(policy.get("type") or ""),
                        "reason": "Sandbox binary path was not configured",
                    }
                }
            proc = run_sandboxed(
                command=command,
                working_directory=working,
                workspace=sandbox.workspace if sandbox is not None else cwd,
                policy=policy,
                binary=binary,
                timeout_s=timeout_s,
            )
        else:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(working),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
            )
        elapsed = int((time.monotonic() - started) * 1000)
        body = {
            "command": command,
            "working_directory": str(working),
            "exit_code": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "execution_time": elapsed,
            "interleaved_output": (proc.stdout or "") + (proc.stderr or ""),
            "local_execution_time_ms": elapsed,
            "sandbox_policy": proto_policy,
        }
        if proc.returncode == 0:
            return {"success": body}
        return {"failure": body}
    except subprocess.TimeoutExpired as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "timeout": {
                "command": command,
                "working_directory": str(working),
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "execution_time": elapsed,
            }
        }
    except OSError as exc:
        if sandbox_is_on(policy):
            return {
                "sandbox_unsupported": {
                    "command": command,
                    "working_directory": str(working),
                    "sandbox_policy_type": str(policy.get("type") or ""),
                    "reason": str(exc),
                }
            }
        return {"spawn_error": {"command": command, "working_directory": str(working), "error": str(exc)}}


def _exec_error_payload(case: str | None, exc: Exception) -> dict[str, Any]:
    err = {"error": str(exc)}
    if case in ("read_args", "redacted_read_args"):
        return {"error": err}
    if case == "ls_args":
        return {"error": err}
    if case == "pi_find_args":
        return {"error": err}
    if case == "grep_args":
        return {"error": err}
    if case == "write_args":
        return {"error": err}
    if case == "diagnostics_args":
        return {"error": {"path": "", "error": str(exc)}}
    if case == "canvas_diagnostics_args":
        return {"error": {"path": "", "error": str(exc)}}
    if case == "shell_args":
        return {"failure": {"command": "", "working_directory": "", "exit_code": 1, "stderr": str(exc)}}
    if case == "request_context_args":
        return {"error": {"error": str(exc)}}
    if case == "fetch_args":
        return {"error": {"url": "", "error": str(exc)}}
    if case == "mcp_args":
        return {"error": {"error": str(exc)}}
    if case == "mcp_state_exec_args":
        return {"error": {"error": str(exc)}}
    if case == "list_mcp_resources_exec_args":
        return {"error": {"error": str(exc)}}
    if case == "read_mcp_resource_exec_args":
        return {"error": {"uri": "", "error": str(exc)}}
    if case == "smart_mode_classifier_args":
        return {"error": {"error": str(exc)}}
    if case == "subagent_args":
        return {"error": {"error": str(exc)}}
    if case == "subagent_await_args":
        return {"error": {"error": str(exc)}}
    if case == "force_background_subagent_args":
        return {"status": "FORCE_BACKGROUND_SUBAGENT_STATUS_NOT_FOUND"}
    if case == "execute_hook_args":
        return {"response": {}}
    return err
