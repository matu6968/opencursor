# Open Cursor — Cursor SDK wire documentation

This directory documents the **Cursor Agent SDK** (`@cursor/sdk`) as observed from the published npm bundle (ESM `dist/esm/`), for interoperability and alternative client implementations (e.g. Python).

**License:** CC-BY-SA-4.0 (see [../LICENSES/CC-BY-SA-4.0.txt](../LICENSES/CC-BY-SA-4.0.txt) and repository `REUSE.toml`).

## Scope

- **In scope:** Cursor Cloud REST API (`/v1/*`), SSE run stream format, HTTP headers and errors, Connect-RPC **service method tables** (names + streaming kind), local run-event stream envelopes, local persistence model, and index of bundled protobuf modules.
- **Out of scope:** Full per-field protobuf reference and reverse-engineering `cursorsandbox` internals. Grep uses the public Anysphere `rg` binary rather than reimplementing search.

## Contents

| Document | Description |
|----------|-------------|
| [architecture.md](architecture.md) | Cloud vs local, transports, optional native helpers |
| [cloud-rest-api.md](cloud-rest-api.md) | All `https://api.cursor.com` `/v1` endpoints |
| [sse-event-stream.md](sse-event-stream.md) | Run SSE wire format and event types |
| [errors.md](errors.md) | Cloud REST, Connect `ErrorDetails`, SSE errors, and status messages |
| [messages.md](messages.md) | Public `SDKMessage` and related JSON shapes |
| [local-runtime.md](local-runtime.md) | Local agent runtime, run events, stores, and notifier protocol |
| [protobuf-modules.md](protobuf-modules.md) | Bundled `.pb.js` modules (from `discoveries.md`) |
| [connect-rpc/transport.md](connect-rpc/transport.md) | API key exchange, headers, base URLs |
| [connect-rpc/agent-service.md](connect-rpc/agent-service.md) | `agent.v1.AgentService` |
| [connect-rpc/analytics-service.md](connect-rpc/analytics-service.md) | `aiserver.v1.AnalyticsService` |
| [connect-rpc/dashboard-service.md](connect-rpc/dashboard-service.md) | `aiserver.v1.DashboardService` (full RPC name list) |

## Reference implementation

See the Python package under [`../opencursor/`](../opencursor/) (cloud REST + SSE, native local Connect HTTP/2 `Run` with HTTP/1.1 `RunSSE` + `BidiAppend` fallback, reconstructed `proto/`).
