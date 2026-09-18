# Cursor Cloud REST API (`/v1`)

**Base URL:** `process.env.CURSOR_BACKEND_URL` in the official SDK, default **`https://api.cursor.com`**.

**Auth:** `Authorization: Bearer <apiKey>` where `<apiKey>` is the Cursor user/team API key (often from `CURSOR_API_KEY`).

**Common headers** (from `CloudApiClient.headers` in `dist/esm/index-full.js`):

| Header | Value |
|--------|--------|
| `Authorization` | `Bearer <apiKey>` |
| `x-ghost-mode` | `"true"` / `"false"` after optional privacy resolution (see [connect-rpc/transport.md](connect-rpc/transport.md)) |
| `x-cursor-client-version` | `sdk-<@cursor/sdk version>` or `cli-<AGENT_CLI_STATIC_VERSION>` when set |
| `x-cursor-client-type` | `sdk` |
| `Content-Type` | `application/json` (JSON body requests only) |
| `x-background-composer-local-use-non-vm` | `true` when base URL host is `localhost` or `127.0.0.1` |
| `x-cursor-streaming` | `true` for the SSE stream request |
| `Accept` | `text/event-stream` for the SSE stream request |
| `Last-Event-ID` | optional; last SSE `id:` for resume |

**Errors:** see [errors.md](errors.md). Successful responses may include `x-request-id`.

---

## Summary table

| Method | Path | SDK method |
|--------|------|------------|
| `POST` | `/v1/agents` | `createAgent` |
| `GET` | `/v1/agents` | `listAgents` |
| `GET` | `/v1/agents/{agentId}` | `getAgent` |
| `POST` | `/v1/agents/{agentId}/archive` | `archiveAgent` |
| `POST` | `/v1/agents/{agentId}/unarchive` | `unarchiveAgent` |
| `DELETE` | `/v1/agents/{agentId}` | `deleteAgent` |
| `POST` | `/v1/agents/{agentId}/runs` | `createRun` |
| `GET` | `/v1/agents/{agentId}/runs` | `listRuns` |
| `GET` | `/v1/agents/{agentId}/runs/{runId}` | `getRun` |
| `POST` | `/v1/agents/{agentId}/runs/{runId}/cancel` | `cancelRun` |
| `GET` | `/v1/agents/{agentId}/runs/{runId}/stream` | `streamRun` (SSE) |
| `GET` | `/v1/agents/{agentId}/artifacts` | `listArtifacts` |
| `GET` | `/v1/agents/{agentId}/artifacts/download` | `getArtifactDownloadUrl` |
| `GET` | `/v1/agents/{agentId}/usage` | `getAgentUsage` |
| `GET` | `/v1/me` | `getMe` |
| `GET` | `/v1/models` | `listModels` |
| `GET` | `/v1/repositories` | `listRepositories` |

Path parameters are URL-encoded (`encodeURIComponent`).

---

## `POST /v1/agents` — create agent (and first run)

**Body:** `V1CreateAgentRequest` (`dist/esm/cloud-api-client.d.ts`)

| Field | Type | Notes |
|-------|------|--------|
| `agentId` | `string?` | Optional; SDK default cloud id is `bc-` + UUID |
| `prompt` | `{ text: string, images?: V1Image[] }` | Required |
| `model` | `{ id: string, params?: { id, value }[] }?` | Optional on cloud (server default) |
| `name` | `string?` | Display name |
| `mcpServers` | `V1McpServer[]?` | Each entry: `name` + stdio (`command`, `args?`, `env?`, **no `cwd`**) **or** remote (`url`, `headers?`, `auth?`, …) |
| `customSubagents` | `V1CustomSubagent[]?` | `name`, `description`, `prompt`, `model?` |
| `env` | `{ type: "cloud"\|"pool"\|"machine", name?: string }?` | |
| `repos` | `{ url, startingRef?, prUrl? }[]?` | |
| `workOnCurrentBranch` | `boolean?` | |
| `autoCreatePR` | `boolean?` | |
| `skipReviewerRequest` | `boolean?` | |

**Response:** JSON

```json
{
  "agent": { /* V1Agent + */ "latestRunId": "<string>" },
  "run": { /* V1Run */ }
}
```

`V1Agent`: `id`, `name?`, `status` (`ACTIVE` \| `ARCHIVED`), `env?`, `repos?`, flags, `url`, `createdAt`, `updatedAt`, `latestRunId?`.

`V1Run`: `id`, `agentId`, `status`, ISO `createdAt` / `updatedAt`, optional `durationMs`, `result`, `git.branches[]`.

---

## `GET /v1/agents` — list agents

**Query:** `limit?`, `cursor?`, `prUrl?`, `includeArchived?` (omitted keys skipped).

**Response:** `{ items: V1Agent[], nextCursor?: string }`

---

## `GET /v1/agents/{agentId}` — get agent

**Response:** `V1Agent`

---

## `POST /v1/agents/{agentId}/archive`

**Response:** `{ id: string }`

---

## `POST /v1/agents/{agentId}/unarchive`

**Response:** `{ id: string }`

---

## `DELETE /v1/agents/{agentId}`

**Response:** `{ id: string }`

---

## `POST /v1/agents/{agentId}/runs` — follow-up run

**Body:** `V1CreateRunRequest`

| Field | Type |
|-------|------|
| `prompt` | `V1Prompt` (required) |
| `mcpServers` | `V1McpServer[]?` |
| `model` | `ModelSelection?` |

**Response:** `{ run: V1Run }`

---

## `GET /v1/agents/{agentId}/runs`

**Query:** `limit?`, `cursor?`

**Response:** `{ items: V1Run[], nextCursor?: string }`

---

## `GET /v1/agents/{agentId}/runs/{runId}`

**Response:** `V1Run`

---

## `POST /v1/agents/{agentId}/runs/{runId}/cancel`

**Response:** `{ id: string }` (conventionally the run id)

---

## `GET /v1/agents/{agentId}/runs/{runId}/stream` — SSE

**Headers:** `Accept: text/event-stream`, `x-cursor-streaming: true`, optional `Last-Event-ID`.

**Response:** `200` with `text/event-stream` body (Fetch `Response` with `.body` stream).

**Wire format:** [sse-event-stream.md](sse-event-stream.md)

---

## `GET /v1/agents/{agentId}/artifacts`

**Response:** `{ items: V1Artifact[] }` — each `path`, `sizeBytes`, `updatedAt` (ISO).

---

## `GET /v1/agents/{agentId}/artifacts/download`

**Query:** `path=<artifact path>` (URL-encoded).

**Response:** `{ url: string, expiresAt: string }` — presigned URL; client then `GET`s that URL for bytes.

---

## `GET /v1/me`

**Response:** `V1MeResponse` — `apiKeyName`, optional user identity fields, `createdAt`.

---

## `GET /v1/models`

**Response:** `{ items: ModelListItem[] }` — see `dist/esm/options.d.ts` (`id`, `displayName`, `description?`, `parameters?`, `variants?`).

---

## `GET /v1/repositories`

**Response:** `{ items: V1GitHubRepository[] }` — each `{ url: string }`.

---

## `ModelSelection` (JSON)

```json
{ "id": "composer-2", "params": [{ "id": "max_tokens", "value": "8192" }] }
```

Exact `id` / `params` values come from `GET /v1/models`.
