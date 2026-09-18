# SPDX-License-Identifier: MIT-0
"""Async quickstart: create agent, send prompt, print streamed messages."""

import asyncio
import os

from opencursor import Agent
from opencursor.types import AgentOptions


async def main() -> None:
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("Set CURSOR_API_KEY")
    agent = await Agent.create(
        AgentOptions.model_validate(
            {
                "apiKey": api_key,
                "model": {"id": "composer-2"},
                "cloud": {},
            }
        )
    )
    try:
        run = await agent.send("Summarize what this repository does")
        async for event in run.stream():
            print(event)
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
