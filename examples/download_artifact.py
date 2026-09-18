# SPDX-License-Identifier: MIT-0
"""After a cloud run, list artifacts and download one by path (CLI args)."""

import argparse
import asyncio
import os

from opencursor import Agent
from opencursor.types import AgentOptions


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("agent_id", help="bc-… agent id")
    p.add_argument("artifact_path", nargs="?", help="path from list_artifacts; if omitted, only list")
    args = p.parse_args()
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("Set CURSOR_API_KEY")
    agent = await Agent.resume(
        args.agent_id,
        AgentOptions.model_validate({"apiKey": api_key, "model": {"id": "composer-2"}, "cloud": {}}),
    )
    try:
        arts = await agent.list_artifacts()
        for a in arts:
            print(a.path, a.sizeBytes, a.updatedAt)
        if args.artifact_path:
            data = await agent.download_artifact(args.artifact_path)
            print("downloaded", len(data), "bytes")
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
