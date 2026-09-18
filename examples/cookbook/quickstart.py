#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Local Agent.create + stream + wait.

Mirrors cookbook/sdk/quickstart/src/index.ts.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

_EXAMPLES = Path(__file__).resolve().parents[1]
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from cookbook._sdk import Agent, LocalAgentOptions  # noqa: E402


def _block_type(block: object) -> str:
    return str(getattr(block, "type", "") or (block.get("type") if isinstance(block, dict) else ""))


def _block_text(block: object) -> str:
    text = getattr(block, "text", None)
    if text is None and isinstance(block, dict):
        text = block.get("text")
    return str(text or "")


def run_quickstart(
    *,
    prompt: str = "Explain this project in one paragraph.",
    cwd: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    on_event: Callable[[object], None] | None = None,
) -> object:
    key = api_key or os.environ.get("CURSOR_API_KEY")
    if not key:
        raise SystemExit("Set CURSOR_API_KEY")
    workdir = cwd or os.getcwd()
    model_id = model or os.environ.get("CURSOR_MODEL") or "composer-2"
    with Agent.create(
        api_key=key,
        name="SDK quickstart",
        model=model_id,
        local=LocalAgentOptions(cwd=workdir),
    ) as agent:
        run = agent.send(prompt)
        for event in run.stream():
            if on_event is not None:
                on_event(event)
            if getattr(event, "type", None) != "assistant":
                continue
            message = getattr(event, "message", None)
            content = getattr(message, "content", None) if message is not None else None
            if not content:
                continue
            for block in content:
                if _block_type(block) == "text":
                    sys.stdout.write(_block_text(block))
                    sys.stdout.flush()
        return run.wait()


def main() -> int:
    run_quickstart()
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
