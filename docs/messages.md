# Public message shapes (`SDKMessage`)

The SDK exposes a **JSON-oriented** stream of messages for both cloud (SSE) and local (decoded run events). Type definitions: `dist/esm/messages.d.ts`, `dist/esm/run.d.ts`, `dist/esm/types/conversation-types.d.ts`, `dist/esm/types/delta-types.d.ts` (re-export from `@anysphere/cursor-sdk-shared`).

## `SDKMessage` union (discriminant: `type`)

| `type` | Purpose |
|--------|---------|
| `system` | Run init: `agent_id`, `run_id`, optional `model`, `tools` |
| `user` | User role message appended to the run |
| `assistant` | Assistant role message (`content`: text / tool_use blocks) |
| `tool_call` | Tool invocation lifecycle: `call_id`, `name`, `status` (`running` \| `completed` \| `error`), optional `args`, `result`, `truncated` |
| `thinking` | Model thinking text / duration |
| `status` | Run lifecycle mirror: `CREATING`, `RUNNING`, `FINISHED`, `ERROR`, `CANCELLED`, `EXPIRED`. Terminal `ERROR` may include `message`. Maps to `Run.status` `running` / `finished` / `cancelled` / `error` (see [errors.md](errors.md)). |
| `request` | Opaque `request_id` marker |
| `task` | Task-style status / text |
| `usage` | Per-turn token usage (`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `totalTokens`) |

Cloud SSE maps server events to these types (see [sse-event-stream.md](sse-event-stream.md)).
Local runs persist the same public messages inside `run_stream_event` records (see [local-runtime.md](local-runtime.md)).

## Content blocks

- **Text:** `{ "type": "text", "text": "..." }`
- **Tool use:** `{ "type": "tool_use", "id": "...", "name": "...", "input": <any> }`

## `InteractionUpdate` (deltas / steps)

Used for `onDelta` / `onStep` callbacks when the server emits `event: interaction_update`. The wire payload is validated in the TS SDK with Zod schemas re-exported from `@anysphere/cursor-sdk-shared` (`InteractionUpdateSchema`, etc.). Shapes include (non-exhaustive names): `text-delta`, `thinking-delta`, `thinking-completed`, `user-message-appended`, `token-delta`, `summary*`, `shell-output-delta`, `tool-call-started`, `partial-tool-call`, `tool-call-completed`, `turn-ended`, `step-started`, `step-completed`, `context-injection-state` (`Run.steer` ack).

Protobuf backing types live under `agent.v1` (e.g. `InteractionUpdate` message) in the bundle — field-level docs are out of scope for this folder.

## `ConversationTurn`

High-level turns built by `RunInteractionAccumulator` / `run.conversation()` in TypeScript. See `dist/esm/types/conversation-types.d.ts` for the discriminated union (`user_message`, `thinking`, `assistant`, `tool_call`, `shell_command`, etc.).

The Python reference may accumulate a **subset** from the `SDKMessage` stream for parity without re-implementing every proto path.

## Local run stream envelope

Local event records use `eventType: "run_stream_event"` with `payload.schemaVersion === 1`.

| Envelope `type` | Fields | Meaning |
|-----------------|--------|---------|
| `sdk_message` | `agentId`, `runId`, `message` | A public `SDKMessage` frame. |
| `result` | `agentId`, `runId`, `status`, optional `errorCode` | Terminal result marker (`finished`, `error`, `cancelled`). |
| `done` | `agentId`, `runId` | Terminal stream marker. |

`sdk_message` payloads are yielded by local `run.stream()`. `result`, `done`, and terminal status messages stop tailing.
