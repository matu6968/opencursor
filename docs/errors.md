# Errors — Cloud REST, Connect RPC, and run status

The bundled SDK maps failures in `dist/esm/errors.d.ts` (`convertConnectError`, `throwApiError`). Python `opencursor.errors` matches that surface.

A thrown `CursorAgentError` means the call did not complete (auth, config, network, Connect trailer). A returned `RunResult.status == "error"` means the run started and then failed; inspect `result.error` and the `status` SDK message.

## Exception classes

| Class | Typical cause |
|-------|----------------|
| `AuthenticationError` | 401, Connect `unauthenticated`, `ErrorDetails` login/token errors |
| `RateLimitError` | 429, Connect `resource_exhausted`, usage/rate `ErrorDetails` |
| `ConfigurationError` | 400/404/409, Connect `invalid_argument` / `not_found`, bad model/key `ErrorDetails` |
| `AgentBusyError` | Cloud/local code `agent_busy` (active run already in progress) |
| `AgentNotFoundError` | Code `agent_not_found` (missing or not visible under `cwd`) |
| `IntegrationNotConnectedError` | Cloud code `integration_not_connected` (`helpUrl`, `provider`) |
| `NetworkError` | 5xx, Connect `unavailable` / `deadline_exceeded` / `internal`, `ERROR_TIMEOUT` |
| `UnknownAgentError` | Unclassified fallback (not a missing-agent signal) |
| `UnsupportedRunOperationError` | `run.supports(op)` is false |

All of these are `CursorAgentError` (`CursorSdkError`). Fields: `code`, `status`, `is_retryable`, `endpoint`, `request_id`, `operation`.

## Cloud REST JSON body

Failed responses parse JSON for:

- `code` — stable string (`unauthorized`, `integration_not_connected`, `agent_busy`, `agent_not_found`, …)
- `message` — human-readable text
- `helpUrl` / `provider` — `integration_not_connected` when present

### HTTP status → class

| Status / code | Exception | Notes |
|---------------|-----------|--------|
| `401` | `AuthenticationError` | Non-retryable |
| `429` | `RateLimitError` | Retryable |
| `agent_busy` | `AgentBusyError` | Non-retryable |
| `agent_not_found` | `AgentNotFoundError` | Non-retryable; `code` is always `agent_not_found` |
| `integration_not_connected` | `IntegrationNotConnectedError` | Requires `helpUrl` + `provider` |
| `400`, `404`, `409` | `ConfigurationError` | Message is `[code] message` |
| `>= 500` | `NetworkError` | Retryable |
| Other `4xx` | `UnknownAgentError` | Non-retryable |

## Connect RPC (`api2.cursor.sh`)

Unary failures and stream end-trailers are Connect JSON (`{ code, message, details }`, optionally wrapped as `{ error: … }`).

When `details` includes `aiserver.v1.ErrorDetails` (base64 proto, or JSON `debug`), `convertConnectError` uses that first:

1. User-facing text is `CustomErrorDetails.title` + `detail` when present.
2. `isRetryable` comes from `CustomErrorDetails.is_retryable`.
3. `ErrorDetails.error` (enum, protobuf-es name without `ERROR_`) selects the class:

| `ErrorDetails.error` | Class |
|----------------------|--------|
| `NOT_LOGGED_IN`, `INVALID_AUTH_ID`, `NOT_HIGH_ENOUGH_PERMISSIONS`, `AGENT_REQUIRES_LOGIN`, `AUTH_TOKEN_NOT_FOUND`, `AUTH_TOKEN_EXPIRED`, `UNAUTHORIZED` | `AuthenticationError` |
| `FREE_USER_RATE_LIMIT_EXCEEDED`, `PRO_USER_RATE_LIMIT_EXCEEDED`, `FREE_USER_USAGE_LIMIT`, `PRO_USER_USAGE_LIMIT`, `RESOURCE_EXHAUSTED`, `OPENAI_RATE_LIMIT_EXCEEDED`, `GENERIC_RATE_LIMIT_EXCEEDED`, `GPT_4_VISION_PREVIEW_RATE_LIMIT`, `API_KEY_RATE_LIMIT`, `RATE_LIMITED`, `RATE_LIMITED_CHANGEABLE` | `RateLimitError` |
| `BAD_API_KEY`, `BAD_USER_API_KEY`, `BAD_MODEL_NAME`, `MODEL_BLOCKED`, `NOT_FOUND`, `DEPRECATED`, `USER_NOT_FOUND`, `BAD_REQUEST`, `FILE_NOT_FOUND` | `ConfigurationError` |
| `TIMEOUT` | `NetworkError` |
| Anything else with details | `UnknownAgentError` |

If there is no `ErrorDetails`, Connect `code` is used:

| Connect `code` | Class |
|----------------|--------|
| `unauthenticated` | `AuthenticationError` |
| `resource_exhausted` | `RateLimitError` |
| `invalid_argument`, `not_found` | `ConfigurationError` |
| `unavailable`, `deadline_exceeded`, `internal` | `NetworkError` |
| Other | `UnknownAgentError` |

HTTP status fills in a Connect code when the body omits one (`401` → `unauthenticated`, `429` → `resource_exhausted`, `503` → `unavailable`, …).

`RunResult.error` is `{ message, code? }` from `to_run_error` (the `[code] ` prefix is stripped from `message` when present).

## SSE `error` event

Inside the cloud run SSE consumer, `event: error` with `{ code, message }` maps to:

- `unauthorized` → `AuthenticationError`
- `forbidden`, `not_found` → `ConfigurationError`
- Others → `NetworkError` (non-retryable)

See [sse-event-stream.md](sse-event-stream.md).

## Status messages

Public `SDKMessage` `type: "status"` uses the V1 run lifecycle:

`CREATING` | `RUNNING` | `FINISHED` | `ERROR` | `CANCELLED` | `EXPIRED`

Mapping to `Run.status` / `RunResult.status`:

| Server / status message | SDK run status |
|-------------------------|----------------|
| `CREATING`, `RUNNING` (local also `QUEUED`) | `running` |
| `FINISHED` | `finished` |
| `CANCELLED` | `cancelled` |
| `ERROR`, `EXPIRED` | `error` |

Local failures also persist `result.errorCode` on the run record and a status message `message` (the user-facing text). `wait()` copies those into `RunResult.error`.

## Request correlation

HTTP responses may include `x-request-id`; the client attaches it as `requestId` on thrown errors when present.
