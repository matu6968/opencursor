# SPDX-License-Identifier: MIT-0
"""List cloud agents with pagination."""

import asyncio
import os

from opencursor import Agent
from opencursor.types import ListAgentsCloudOptions


async def main() -> None:
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("Set CURSOR_API_KEY")
    page = await Agent.list(ListAgentsCloudOptions(apiKey=api_key, limit=20))
    for a in page.items:
        print(a.agentId, a.name, a.archived)
    if page.nextCursor:
        print("nextCursor:", page.nextCursor)


if __name__ == "__main__":
    asyncio.run(main())
