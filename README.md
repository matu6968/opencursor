# Open Cursor SDK (experimental)

> [!NOTE]
> This SDK is a work in progress, don't use it in production replacing the official [`cursor-sdk`](https://pypi.org/project/cursor-sdk) package.

Open documentation and a **Python reference client** for the Cursor Agent API compatible with [`cursor-sdk`](https://pypi.org/project/cursor-sdk). The TypeScript package [`@cursor/sdk`](https://www.npmjs.com/package/@cursor/sdk) is the protocol source of truth: native Connect/protobuf to `api2.cursor.sh` for local agents, and cloud REST `/v1/*` plus run SSE. This package speaks that same wire path **in-process** (no `cursor-sdk-bridge`, no `sdk.v1`).

Public names match TypeScript (`agentId`, `listRuns`) and also expose official-Python snake_case aliases (`agent_id`, `list_runs`) so `cursor_sdk` callers can switch without a Node bridge. `Client` / `AsyncClient` / `Bridge` are intentionally not ported.

Library code is **LGPL-3.0-or-later** with a [linking exception](LINKING-EXCEPTION.md) to allow linking statically/dynamically. Documentation under [`docs/`](docs/) is **CC-BY-SA-4.0**. Examples are **MIT-0**. See [`REUSE.toml`](REUSE.toml) and [`LICENSES/`](LICENSES/).

## Python package (`opencursor`)

Requires **Python 3.10+**, `httpx[http2]`, `pydantic` v2, `tree-sitter`, and `tree-sitter-bash`.

### Install

From this directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Or add the repo root that contains the `opencursor` package to `PYTHONPATH` if dependencies are already installed.

### Quickstart

```python
import asyncio
import os
from opencursor import Agent
from opencursor.types import AgentOptions

async def main():
    async with await Agent.create(
        AgentOptions.model_validate({
            "apiKey": os.environ["CURSOR_API_KEY"],
            "model": {"id": "composer-2"},
            "cloud": {},
        }),
    ) as agent:
        run = await agent.send("Hello from Python")
        async for msg in run.stream():
            print(msg)

asyncio.run(main())
```

See [`examples/`](examples/) for `quickstart`, `list_agents`, `wait_for_result`, `download_artifact`, `local_agent`, `local_fetch`, `local_search`, `local_mcp`, `local_mcp_oauth`, `local_subagent`, `parse_shell`, `local_sandbox`, and `read_lints`. Python ports of the TypeScript cookbook live in [`examples/cookbook/`](examples/cookbook/) (`quickstart`, coding-agent CLI, DAG runner, app-builder and agent-kanban SDK layers).

### API notes

- **Default runtime:** `Agent.create` / `Agent.prompt` use **local** unless `cloud` is set or the agent id starts with `bc-` (TypeScript parity). Pass `cloud: {}` for cloud agents.
- **Handles:** `CloudAgent` / `LocalAgent` support `async with` (`aclose`) and `with` (`close`). `Agent.archive_agent` / `unarchive_agent` / `delete_agent` and `Agent.lifecycle` are deprecated wrappers around `Agent.archive` / `unarchive` / `delete`.
- **Cookbook live tests:** `examples/cookbook/` ports the TypeScript cookbook SDK examples. `pytest tests/test_cookbook_ports.py` covers parse/rank/canvas/shim without an API key. Opt-in live runs: `pytest tests/live/test_cookbook_live.py` (requires `CURSOR_API_KEY`; uses a scratch cwd). Switch backends with `OPENCURSOR_LIVE_SDK=opencursor|official` and print create/send/stream/wait traces with `OPENCURSOR_LIVE_DEBUG=1`. Cloud `Agent.list` stays behind `OPENCURSOR_LIVE_CLOUD=1`.
- **Dependent stress tests:** `tests/stress/catalog.json` lists public GitHub repos that import `cursor_sdk`. `pytest tests/test_sdk_stress.py` rewrites those import lines to `opencursor` and checks the names exist. Optional live clone+compile: `PYTHONPATH=. python scripts/run_sdk_stress.py --clone --limit 5` clones the next 5 not already under `tests/stress/.work` (repeat to walk the list). `OPENCURSOR_STRESS_CLONE=1 pytest tests/test_sdk_stress.py` does the same. Refresh the catalog with `python scripts/refresh_sdk_dependents.py` (`gh` required).
- **Base URL:** cloud REST uses `CURSOR_BACKEND_URL` or `https://api.cursor.com`. Local Connect uses `CURSOR_AGENT_BACKEND_URL` / `https://api2.cursor.sh`.
- **Auth:** explicit `apiKey`, then `CURSOR_API_KEY`, then a key stored by `Cursor.auth.login()` in `~/.cursor/sdk/auth.json`.
- **Agent IDs:** cloud agents use the `bc-` prefix (the Python client generates `bc-<uuid4>` when `agentId` is omitted).
- **Runs from `Agent.listRuns` / `Agent.getRun`:** each `CloudRun` owns its own HTTP client; call `await run.aclose()` after you finish streaming if you need to free connections early.
- **Local runtime:** native Connect/protobuf to `api2.cursor.sh` (HTTP/2 bidi `Run`, falling back to HTTP/1.1 `RunSSE` + `BidiAppend`) with in-process read/ls/grep/glob/write/shell/webFetch/MCP/customTools, server-side webSearch (client approves), custom subagents via `task` (nested `Run` / `RunSSE`), and no JS bridge. `Cursor.configure({ local: { useHttp1ForAgent: True } })` or `local.useHttp1ForAgent` / `CURSOR_USE_HTTP1` force the HTTP/1.1 fallback. Shell commands are analyzed with PyPI `tree-sitter` + `tree-sitter-bash` (admin denylist / redirects). Set `local.sandboxOptions.enabled` to wrap shell in the vendor `cursorsandbox` helper (`CURSOR_SANDBOX_BIN` or `@cursor/sdk-<platform>/bin`). Grep spawns the same package’s `rg` (`CURSOR_RIPGREP_PATH` or `@cursor/sdk-<platform>/bin/rg`). `readLints` runs local syntax checks plus optional `ruff` / `eslint` / `tsc` / `node --check`. `local.autoReview` selects the backend Auto-review classifier (`smart_mode_classifier_auto_mode_enabled`); leftover `smart_mode_approval` tool calls fail closed (no interactive prompt), except `customTools` / `skip_approval`. HTTP MCP `auth` (`CLIENT_ID` / optional secret / `scopes` / `clientName`) uses PKCE browser login on `localhost:8787/callback`; a 401 advertises the virtual `mcp_auth` tool. Live local `Run.steer(text)` injects a user message into the in-flight turn (`complete_delivered` or `revert_to_followup`); cloud and detached local handles revert. Persist local agents with the default SQLite store, `JsonlLocalAgentStore`, or a custom `LocalAgentStore` (`local.store` / `Cursor.configure({ local: { store } })`). See [docs/local-runtime.md](docs/local-runtime.md).

## Wire documentation

- [docs/README.md](docs/README.md) — index of REST, SSE, and Connect-RPC service maps.

## Scope

- **In this repo:** cloud REST + SSE; native local `agent.v1` Connect executor (HTTP/2 bidi `Run` with HTTP/1.1 `RunSSE` + `BidiAppend` fallback); reconstructed protos in [`proto/`](proto/).
- **Not in this repo (yet):** `cursorsandbox` internals. The `rg` binary is located from the SDK platform package / `CURSOR_RIPGREP_PATH` rather than vendored in the Python wheel.
