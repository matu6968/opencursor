# Local runtime

The ESM bundle implements local agents with three layers:

1. A Connect-RPC executor for `agent.v1.AgentService.Run`.
2. Local controllers for interaction updates, exec, KV, checkpoints, MCP, subagents, and hooks.
3. SQLite-backed run stores that persist public `SDKMessage` frames as `run_stream_event` records.

This document captures the store and stream contract needed by alternative SDKs. Shell parsing uses PyPI `tree-sitter` + `tree-sitter-bash` instead of the vendored Node bindings. Filesystem sandboxing wraps the vendor `cursorsandbox` CLI (`@cursor/sdk-<platform>/bin/cursorsandbox`); the helper’s internals are out of scope. Grep spawns the same vendored `rg` binary (`@cursor/sdk-<platform>/bin/rg`, Anysphere’s [ripgrep](https://github.com/anysphere/ripgrep) fork) via `CURSOR_RIPGREP_PATH` or PATH.

## State root

The TypeScript SDK derives a state root from the workspace:

```text
~/.cursor/projects/<sanitized-workspace>/sdk-agent-store/<md5-workspace>
```

The default store keeps:

| Path | Purpose |
|------|---------|
| `index.db` | Agent/run metadata and `run_events`. |
| `agents/agent-<sha256-agent-id>/store.db` | Checkpoint metadata and blobs. |

## Custom `LocalAgentStore`

`Agent.create({ local: { store } })` and `Cursor.configure({ local: { store } })` accept a TypeScript-shaped `LocalAgentStore`: substores `agents`, `runs`, `runEvents`, and `checkpoints`. Per-call `store` on list/get/archive options overrides the module default. Pass the same instance on create, resume, and local list/get.

Built-in portable backend: `JsonlLocalAgentStore(rootDir)` writes four NDJSON files (`agents.ndjson`, `runs.ndjson`, `run_events.ndjson`, `checkpoints.ndjson`). Combine separately implemented substores with `composeLocalAgentStore`. Catalog lists paginate with opaque `nextCursor` values (`paginateAgentDocuments` / `paginateRunDocuments` / `paginateCheckpointBlobIds`); run events use exclusive `afterOffset` / `nextOffset`.

When `local.store` is omitted, the SDK still opens the SQLite `index.db` layout under the workspace state root.

## Agent and run metadata

Local agents are not prefixed with `bc-`; cloud routing uses that prefix. Runs use lifecycle statuses `QUEUED`, `RUNNING`, `FINISHED`, `ERROR`, `CANCELLED`, and `EXPIRED`.

Follow-up run creation checks the agent's active run. If the active run is non-terminal, `send({ local: { force: true } })` expires it with `status: "EXPIRED"` and `errorCode: "force_send"` before creating a new run.

## Run events

`run_events` rows are append-only per run:

| Column | Meaning |
|--------|---------|
| `run_id` | Run identifier. |
| `seq` / `offset` | Monotonic sequence; offsets are decimal strings. |
| `event_type` | `run_stream_event` for public SDK frames. |
| `payload_json` | JSON envelope. |
| `payload_ref` | Optional external payload reference. |
| `idempotency_key` | Optional dedupe key. |
| `created_at` | ISO timestamp. |

Payloads use `schemaVersion: 1` and one of:

```json
{ "schemaVersion": 1, "type": "sdk_message", "agentId": "agent-id", "runId": "run-id", "message": { "type": "status", "agent_id": "agent-id", "run_id": "run-id", "status": "RUNNING" } }
```

```json
{ "schemaVersion": 1, "type": "result", "agentId": "agent-id", "runId": "run-id", "status": "finished" }
```

```json
{ "schemaVersion": 1, "type": "done", "agentId": "agent-id", "runId": "run-id" }
```

Only `sdk_message` envelopes are yielded to users. `result`, `done`, and terminal `status` messages stop tailing.

## Notifier socket

The local notifier is a newline-delimited JSON protocol over a Unix socket or Windows named pipe.

Client messages:

| Message | Meaning |
|---------|---------|
| `{ "type": "subscribe", "runId": "<run-id>" }` | Subscribe to append hints for a run. |
| `{ "type": "publish", "hint": { "runId": "<run-id>", "offset": "<offset>" } }` | Publish that an event was appended. |

Server messages:

| Message | Meaning |
|---------|---------|
| `{ "type": "ack" }` | Subscription or publish accepted. |
| `{ "type": "hint", "hint": { "runId": "<run-id>", "offset": "<offset>" } }` | A run event may now be available. |
| `{ "type": "error", "message": "..." }` | Invalid message or server error. |

## Native Connect path (no JS bridge)

Schemas reconstructed from `@cursor/sdk@1.0.31` live in [`proto/`](../proto/) (`descriptors.json` plus `.proto` files). Other languages should codegen from those files and speak this wire protocol directly.

### Endpoints

Base URL: `CURSOR_AGENT_BACKEND_URL` / `CURSOR_BACKEND_URL` / `https://api2.cursor.sh`.

| Step | RPC | HTTP |
|------|-----|------|
| 1 | `POST /auth/exchange_user_api_key` | JSON `{}` with `Authorization: Bearer <CURSOR_API_KEY>` → `{ accessToken }` |
| 2a | `POST /aiserver.v1.ServerConfigService/GetServerConfig` | Unary `application/proto` over HTTP/1.1. `http2_config` (field 7) can force bidi on or off. |
| 2b | `POST /agent.v1.AgentService/Run` | Connect **HTTP/2 bidi** (`application/connect+proto`). Client and server exchange Connect frames on one stream. |
| 2c | `POST /agent.v1.AgentService/RunSSE` | Connect server-stream (`application/connect+proto`). Body: `aiserver.v1.BidiRequestId`. Used when HTTP/2 bidi is unavailable. |
| 3 | `POST /aiserver.v1.BidiService/BidiAppend` | Connect unary (`application/proto`). Client frames as hex string `data` plus `append_seqno` starting at `0`. Only on the HTTP/1.1 fallback. |

Transport pick matches `@cursor/sdk@1.0.31`: `GetServerConfig.http2_config` wins when set (`FORCE_BIDI_DISABLED` / `FORCE_ALL_DISABLED` → HTTP/1.1; `FORCE_BIDI_ENABLED` / `FORCE_ALL_ENABLED` → HTTP/2). Otherwise HTTP/2 unless the URL is `http:`, `local.useHttp1ForAgent` is true, `CURSOR_USE_HTTP1` is set, or the `h2` package is missing. If HTTP/2 `Run` fails to negotiate or connect, the client falls back to `RunSSE` + `BidiAppend` for that session. Classic gRPC HTTP/2 (`application/grpc`) is not used. HTTP/1.1 appends keep `binaryEncoding=false` (hex `data`, not `data_binary`).

### Headers

- `Authorization: Bearer <exchanged JWT>`
- `Connect-Protocol-Version: 1`
- `x-cursor-client-type: sdk`
- `x-cursor-client-version: sdk-<version>`
- `x-ghost-mode: true\|false`
- `x-cursor-streaming: true` on `RunSSE` (HTTP/1.1 fallback only; not on HTTP/2 `Run`)
- `x-cursor-agent-allowed-tools`: comma-separated proto tool names (`read_tool_call`, `ls_tool_call`, `grep_tool_call`, `edit_tool_call`, `glob_tool_call`, `shell_tool_call`, `web_fetch_tool_call`, `web_search_tool_call`, `task_tool_call` when `agents` is set or `tools` includes `task`, plus `mcp_tool_call` / `get_mcp_tools_tool_call` / `list_mcp_resources_tool_call` / `read_mcp_resource_tool_call` when MCP or `local.customTools` is enabled). Empty / omitted with `tools: []` means no built-in tools. Public SDK names `webFetch` / `webSearch` / `mcp` / `task` map to those proto names.

### Connect envelope (streams)

Each frame is `flags:u8` + `length:u32be` + payload. `flags & 0x02` is end-stream; the trailer payload is JSON `{}` or a Connect error.

### First client message

`AgentClientMessage.run_request` (`agent.v1.AgentRunRequest`):

- `conversation_state` — `ConversationStateStructure`. A missing or zero-length submessage is rejected (`Conversation state is required`). First turn should set at least `mode = AGENT_MODE_AGENT`; later turns send the last `conversation_checkpoint_update`.
- `action.user_message_action.user_message.text` plus `mode = AGENT_MODE_AGENT`
- `action.user_message_action.request_context.env` — workspace paths, `sandbox_enabled` / `sandbox_supported` from `local.sandboxOptions.enabled` plus helper preflight, and `smart_mode_classifier_auto_mode_enabled` from `local.autoReview === true` (classifier-backed Auto mode). `CURSOR_SDK_DEV_FORCE_NEXT_SMART_MODE_CLASSIFIER_BLOCK_TOKEN` is copied to `dev_force_next_smart_mode_classifier_block_token` when set.
- `model_details` / `requested_model`
- `conversation_id`, `run_id`, `agent_session_id`
- Heartbeat: `client_heartbeat` about every 5s
- Mid-turn steer: `conversation_action.inject_context_action` (`Run.steer`); ack is `interaction_update.context_injection_state`

Sandbox off (`local.sandboxOptions.enabled` omitted/false) is `SandboxPolicy.type = TYPE_INSECURE_NONE` and runs the shell unsandboxed. When enabled, locate `cursorsandbox` (`CURSOR_SANDBOX_BIN`, `PATH`, or `node_modules/@cursor/sdk-<platform>-<arch>/bin/`), write a JSON policy file, Linux-preflight with `--preflight-only -- /bin/true`, then spawn `cursorsandbox --policy <file> -- /bin/sh -c <command>`. If sandboxing was requested but the helper is missing or preflight fails, raise `ConfigurationError` at agent create (same message as `@cursor/sdk`). Windows is proxy-only and treated as unsupported.

### Server messages → local handling

| `AgentServerMessage` | Client action |
|----------------------|---------------|
| `interaction_update` | Map to public `SDKMessage` (text_delta, thinking_*, tool_call_*, turn_ended) |
| `exec_server_message` | Run the matching exec locally; reply `exec_client_message` with the same `id` / `exec_id` |
| `kv_server_message` | In-memory blob get/set; reply `kv_client_message` |
| `conversation_checkpoint_update` | Keep `ConversationStateStructure` for the next `run_request` |
| `interaction_query` | Reply `interaction_response` with the query `id`. For `web_fetch_request_query` / `web_search_request_query`, send the matching `*_request_response.approved`. For `mcp_auth_request_query`, run PKCE loopback OAuth (open a browser, wait for `/callback`), then `approved` or `rejected` |
| `exec_server_control_message.abort` | Stop in-flight exec |

Minimal exec oneofs implemented in Python: `read_args`, `ls_args`, `grep_args` (spawn vendored `rg`: `CURSOR_RIPGREP_PATH`, `PATH`, or `@cursor/sdk-<platform>/bin/rg`; `--cursor-ignore` when the binary is the Anysphere fork; content / `files_with_matches` / `count`; 25s timeout), `write_args`, `shell_args` / `shell_stream_args` (PyPI `tree-sitter` + `tree-sitter-bash` parses the command into `simple_commands` / `ShellCommandParsingResult`; `admin_command_denylist` is fail-closed on parse failure and glob-matched against executable text; when sandboxing is on, spawn goes through `cursorsandbox --policy` and stream `start.sandbox_policy` is the effective policy), `delete_args`, `diagnostics_args` (`readLints`: Python `ast.parse` / JSON parse, plus `ruff` / `eslint` / `tsc` / `node --check` when those binaries are on `PATH`; 10s timeout; `file_not_found` / `permission_denied` match the TS executor), `request_context_args`, `shell_allowlist_precheck_args`, `fetch_args` (in-process HTTP GET; success is raw `content` plus `status_code` / `content_type`; the backend turns HTML into markdown for `WebFetchSuccess`), `web_fetch_allowlist_precheck_args` (`allowlisted: true`), `mcp_args` / `mcp_state_exec_args` / `list_mcp_resources_exec_args` / `read_mcp_resource_exec_args` / `mcp_allowlist_precheck_args`, `smart_mode_classifier_args` (headless SDK has no local classifier; reply `smart_mode_classifier_result.error`, same as no handler in `@cursor/sdk`), `subagent_args` (nested local `Run` / `RunSSE` + `BidiAppend`; success is `SubagentSuccess` with `final_message`), `subagent_await_args`, `force_background_subagent_args`, `execute_hook_args` (empty continue for `subagent_start` / `subagent_stop`). Set `request_context.web_fetch_enabled` / `web_search_enabled` / `read_lints_enabled` when those proto tools are allowed. Advertise `AgentOptions.agents` as `request_context.custom_subagents` (`name`, `description`, `prompt`, `model`). Nested runs reuse the parent Connect client and MCP sessions and omit `task_tool_call` so they cannot spawn further subagents. Inline MCP configs on a subagent definition are rejected; custom subagents inherit the parent MCP. `webSearch` has no local exec: after approval the backend runs the search and returns `WebSearchSuccess.references`. MCP is in-process JSON-RPC (stdio `command`/`args`/`env`, or HTTP `url`/`headers`; HTTP MCP OAuth is PKCE loopback browser login, tokens in `mcp-auth.json`). Advertise MCP tools on `AgentRunRequest.mcp_tools` and `request_context.tools` using wire `name` `{server}-{tool}`. HTTP servers that return 401 advertise a virtual `mcp_auth` tool and `request_context.supports_mcp_auth`.

`local.autoReview` selects classifier-backed Auto mode on the backend via `RequestContextEnv.smart_mode_classifier_auto_mode_enabled`. Classification itself is backend-side. The headless executor cannot prompt for interactive approval: MCP / shell / `read_mcp_resource` execs that carry `smart_mode_approval` (classifier BLOCK) fail closed with the TypeScript AG8 reason (`Local SDK runs cannot request interactive approval for …`). `skip_approval` and `local.customTools` (`custom-user-tools`) still run. When Auto-review/sandbox is off and `read_mcp_resource` still has a smart-mode reason, the result is `Auto-review approval provider is not configured`.

### Python reference status

`opencursor` now runs this native executor from `LocalAgent.send()` (no `cursor-sdk-bridge`, no bundled Node). The agent stream prefers HTTP/2 bidi `Run` and falls back to HTTP/1.1 `RunSSE` + `BidiAppend` (`local.useHttp1ForAgent`, `CURSOR_USE_HTTP1`, or a failed HTTP/2 negotiation). Live local `Run.steer(text)` writes `inject_context_action` on that stream; detached `getRun` handles and cloud REST runs return `revert_to_followup` (no generation UUID / no REST inject path). Grep exec locates `rg` the same way as `cursorsandbox` (`CURSOR_RIPGREP_PATH`, then PATH, then `@cursor/sdk-<platform>/bin/rg`) and uses the Anysphere `--cursor-ignore` flag when that fork is present. Sandboxing wraps the vendor `cursorsandbox` binary when `local.sandboxOptions.enabled` is true; helper internals are not reimplemented. HTTP MCP OAuth uses PKCE and a loopback callback (`http://localhost:8787/callback`, falling back to an ephemeral port). Dynamic client registration matches Cursor (`client_name`, `logo_uri`, Cursor redirect URIs) and retries with only the bound loopback URI if the server returns `invalid_redirect_uri`. `auth.clientName` / `auth.CLIENT_ID` override DCR. Tokens persist in `mcp-auth.json` next to the local agent store. `NO_OPEN_BROWSER` prints the authorize URL without spawning a browser.
