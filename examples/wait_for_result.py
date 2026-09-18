# SPDX-License-Identifier: MIT-0
"""Create agent, run one prompt, wait for terminal result (no streaming)."""

import asyncio
import os

from opencursor import Agent
from opencursor.types import AgentOptions


async def main() -> None:
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("Set CURSOR_API_KEY")
    result = await Agent.prompt(
        "What is 2+2? One line.",
        AgentOptions.model_validate({"apiKey": api_key, "model": {"id": "composer-2"}, "cloud": {}}),
    )
    print(result.model_dump(by_alias=True))


if __name__ == "__main__":
    asyncio.run(main())
