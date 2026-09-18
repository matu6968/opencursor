# SPDX-License-Identifier: MIT-0
"""Local agent: fetch a public URL via native webFetch (no JS bridge)."""

import asyncio
import os

from opencursor import Agent
from opencursor.types import AgentOptions


async def main() -> None:
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("Set CURSOR_API_KEY")
    cwd = os.environ.get("OPENCURSOR_LOCAL_CWD") or os.getcwd()
    url = os.environ.get("OPENCURSOR_FETCH_URL") or "https://example.com"
    agent = await Agent.create(
        AgentOptions.model_validate(
            {
                "apiKey": api_key,
                "model": {"id": "composer-2.5"},
                "local": {"cwd": cwd, "sandboxOptions": {"enabled": False}},
                "tools": ["webFetch"],
            }
        )
    )
    try:
        run = await agent.send(
            f"Use webFetch to retrieve {url} and tell me the page title. Do not use the shell."
        )
        async for event in run.stream():
            print(event)
        print("result", await run.wait())
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
