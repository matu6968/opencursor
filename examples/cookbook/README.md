# SPDX-License-Identifier: MIT-0
"""Python ports of the Cursor TypeScript cookbook (`cookbook/sdk/`).

Each module talks to the SDK only through `_sdk.py`, so the same example can
run against opencursor or official `cursor_sdk`.

## Examples

| Python | TypeScript source |
| --- | --- |
| `quickstart.py` | `cookbook/sdk/quickstart/src/index.ts` |
| `coding_agent_cli/` | `cookbook/sdk/coding-agent-cli` (one-shot + REPL; no OpenTUI) |
| `dag_task_runner/` | `cookbook/sdk/dag-task-runner` |
| `app_builder/server.py` | `cookbook/sdk/app-builder/src/lib/app-builder/server.ts` (SDK layer) |
| `agent_kanban/server.py` | `cookbook/sdk/agent-kanban/src/lib/agents/server.ts` (SDK layer) |

Not ported: Grok CLIs, LiveKit intake, Perl bridge, self-hosted AWS lab, hooks.

## Environment

```bash
export CURSOR_API_KEY=...
# Optional. Default is opencursor.
export OPENCURSOR_LIVE_SDK=opencursor   # or official / cursor_sdk
# Optional. Logs create/send/stream/wait to stderr, tagged by backend.
export OPENCURSOR_LIVE_DEBUG=1
```

Compare backends by running the same command twice:

```bash
OPENCURSOR_LIVE_DEBUG=1 python examples/cookbook/quickstart.py
OPENCURSOR_LIVE_SDK=official OPENCURSOR_LIVE_DEBUG=1 python examples/cookbook/quickstart.py
```

Official mode inserts `opencursor/official/` on `sys.path` and still needs the
vendored Node bridge.

## Run

From the `opencursor` package root (`PYTHONPATH=.`):

```bash
python examples/cookbook/quickstart.py
python examples/cookbook/coding_agent_cli/cli.py "Explain this repo"
python examples/cookbook/dag_task_runner/run_dag.py --init-only \
  --dag examples/cookbook/dag_task_runner/examples/example_dag.json \
  --canvas-path /tmp/dag-example.canvas.tsx
```

Offline pytest (no API key):

```bash
pytest tests/test_cookbook_ports.py
```

Live pytest (skips without `CURSOR_API_KEY`):

```bash
pytest tests/live/test_cookbook_live.py
OPENCURSOR_LIVE_SDK=official OPENCURSOR_LIVE_DEBUG=1 pytest tests/live/test_cookbook_live.py
```
