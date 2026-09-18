# Run SSE event stream (`GET .../runs/{runId}/stream`)

Source: bundled `./src/agent/cloud-agent.ts` in `dist/esm/642.index-full.js` (SSE consumer + retry loop).

## Framing

- Stream body is UTF-8 text interpreted as **SSE**: events separated by **blank line** `\n\n`.
- Within each event, lines may include:
  - `id: <eventId>` — monotonic server event id; client stores this as `Last-Event-ID` for resume.
  - `event: <type>` — logical event name (optional for some frames).
  - `data: <payload>` — JSON payload; multiple `data:` lines are joined with `\n` before `JSON.parse`.

If `id` is present and `event` is missing or not in the “legacy” set `assistant`, `thinking`, `tool_call`, the client still records the id for resume.

## Server `event` types

| `event` | JSON body | Client handling |
|---------|-----------|------------------|
| `assistant` | Object, optional `text` | Pushes `SDKMessage` `type: "assistant"` with a single text block (`text` default `""`). |
| `thinking` | Object, optional `text` | Pushes `SDKMessage` `type: "thinking"`. |
| `tool_call` | Object with `callId`, `name`, `status`, optional `args`, `result`, `truncated` | Pushes `SDKMessage` `type: "tool_call"`; `status` normalized to `running` \| `completed` \| `error`. |
| `interaction_update` | JSON matching Zod `InteractionUpdateSchema` | Invokes `onDelta` with parsed update (invalid payloads ignored). |
| `status` | Object with `status` matching `V1Run` status enum | Updates internal run status mapping; pushes `SDKMessage` `type: "status"`. |
| `result` | Object (may include `text`, `result`, `status`, `durationMs`, `git`, …) | Maps via `mapV1RunResultMetadataToSdk`; completes run; **cancels** SSE reader. |
| `heartbeat` | any | Ignored (keep-alive). |
| `error` | `{ code?: string, message?: string }` | Throws mapped SDK error; non-retryable for `unauthorized`, `forbidden`, `not_found`. |
| `done` | — | Closes SSE reader (end of stream). |

Legacy frames (`assistant`, `thinking`, `tool_call`) may be deduplicated per `id` when the server replays history.

## Status mapping (`V1Run` → SDK run status)

| Server `V1Run.status` | SDK `RunStatus` |
|----------------------|-----------------|
| `CREATING`, `RUNNING` | `running` |
| `FINISHED` | `finished` |
| `CANCELLED` | `cancelled` |
| `ERROR`, `EXPIRED`, default | `error` |

## Resume and retry (`runStreamLoop`)

When the SSE connection drops **before** a terminal `result`:

1. Optionally treat `[invalid_last_event_id]` style errors by clearing `Last-Event-ID` and retrying.
2. **Refetch** `GET .../runs/{runId}` to sync `status`, `result`, `durationMs`, `git`.
3. If still `RUNNING` / `CREATING` and not aborted:
   - **Backoff** `min(30_000, 1_000 * 2**attempt)` ms (attempt counter capped in logic; after **6** failed stream attempts the client switches to **polling** `getRun` every ≤15s until terminal or **2h** wall clock from loop start).
4. Re-open SSE with `Last-Event-ID` when available.

On success path, after terminal `finished`, the client may `flushPendingStep` on the interaction accumulator.

## Client cancel

`POST .../cancel` then `AbortController.abort()` on the stream; run status set to `cancelled`.
