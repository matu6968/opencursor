# SPDX-License-Identifier: MIT-0
"""Local agent: call an inline stdio MCP server (no JS bridge)."""

import asyncio
import os
import sys
from pathlib import Path

from opencursor import Agent
from opencursor.types import AgentOptions

ECHO_SERVER = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "echo_mcp_server.py"


async def main() -> None:
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("Set CURSOR_API_KEY")
    cwd = os.environ.get("OPENCURSOR_LOCAL_CWD") or os.getcwd()
    command = os.environ.get("OPENCURSOR_MCP_COMMAND") or sys.executable
    args = os.environ.get("OPENCURSOR_MCP_ARGS")
    mcp_args = args.split() if args else [str(ECHO_SERVER)]
    agent = await Agent.create(
        AgentOptions.model_validate(
            {
                "apiKey": api_key,
                "model": {"id": "composer-2.5"},
                "local": {"cwd": cwd, "sandboxOptions": {"enabled": False}},
                "tools": ["mcp"],
                "mcpServers": {
                    "echo": {
                        "type": "stdio",
                        "command": command,
                        "args": mcp_args,
                    }
                },
            }
        )
    )
    try:
        run = await agent.send(
            "Use the echo MCP tool to echo the text hello-mcp. Reply with the tool output. Do not use the shell."
        )
        async for event in run.stream():
            print(event)
        print("result", await run.wait())
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
