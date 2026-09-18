# Connect-RPC transport (as used by `@cursor/sdk`)

The SDK uses `@connectrpc/connect` + `@connectrpc/connect-node` with HTTP/2 (or HTTP/1.1 when `CURSOR_USE_HTTP1` / `http:` URL).

## Base URLs

| Purpose | Default | Env override |
|---------|---------|--------------|
| Cloud **REST** agents API | `https://api.cursor.com` | `CURSOR_BACKEND_URL` (used by `CloudApiClient`) |
| Connect transport for dashboard / privacy / token exchange | `https://api2.cursor.sh` | `CURSOR_BACKEND_URL` in `executor-common` (same name, different default in code paths) |

Always consult the published bundle when both are in play: `dist/esm/index-full.js` module `./src/agent/executor-common.ts`.

## API key → access token exchange

`POST ${baseUrl}/auth/exchange_user_api_key`

- Headers: `Authorization: Bearer <apiKey>`, `Content-Type: application/json`
- Body: `{}`
- Response JSON: `{ accessToken: string }` (field name as used in bundle)

Used before attaching the bearer token to Connect calls (interceptor replaces `authorization` header with the exchanged JWT).

## Client identity headers

On Connect transports the bundle sets:

- `x-cursor-client-type`: `sdk`
- `x-cursor-client-version`: `sdk-<version from @cursor/sdk package.json>` unless `AGENT_CLI_STATIC_VERSION` is set (`cli-<value>`)

## `x-ghost-mode`

Derived from user privacy mode (`GetUserPrivacyMode` RPC) and cached per API key. Values are stringified booleans (`"true"` / `"false"`) with mapping from `aiserver.v1.PrivacyMode` enum (see `privacy_mode_pb.js` in bundle).

The **cloud REST** client also sends `x-ghost-mode` using the same cached header helper.

## HTTP version selection

If env `CURSOR_USE_HTTP1` is `1`/`true`, `local.useHttp1ForAgent` is true, or the URL uses `http:`, the agent stream uses HTTP/1.1 `RunSSE` + `BidiAppend`. Otherwise the SDK prefers HTTP/2 bidi `Run`, unless `GetServerConfig.http2_config` forces the other direction. A failed HTTP/2 negotiation falls back to `RunSSE`.

## Connect path shape

Unary RPCs use the standard Connect path:

`POST ${baseUrl}/<package>.<Service>/<Method>`

Example from bundle: `POST ${api2}/aiserver.v1.AnalyticsService/BootstrapStatsig` (also used via `fetch` for Statsig bootstrap in some paths).
