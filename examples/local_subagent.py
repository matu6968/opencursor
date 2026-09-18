# SPDX-License-Identifier: MIT-0
"""Local agent: launch a custom subagent via the task tool (nested RunSSE)."""

import asyncio
import os

from opencursor import Agent
from opencursor.types import AgentOptions


async def main() -> None:
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("Set CURSOR_API_KEY")
    cwd = os.environ.get("OPENCURSOR_LOCAL_CWD") or os.getcwd()
    agent = await Agent.create(
        AgentOptions.model_validate(
            {
                "apiKey": api_key,
                "model": {"id": "composer-2.5"},
                "local": {"cwd": cwd, "sandboxOptions": {"enabled": False}},
                "tools": ["task"],
                "agents": {
                    "pong": {
                        "description": "Replies with the single word pong. Use for a quick handshake.",
                        "prompt": (
                            "You are the pong subagent. Reply with the single word pong and nothing else. "
                            "Do not use tools."
                        ),
                    }
                },
            }
        )
    )
    try:
        run = await agent.send(
            "Use the task tool to launch the pong subagent. Wait until it finishes. "
            "Then reply with exactly the subagent's final message. Do not answer yourself."
        )
        async for event in run.stream():
            kind = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
            status = event.get("status") if isinstance(event, dict) else getattr(event, "status", None)
            name = event.get("name") if isinstance(event, dict) else getattr(event, "name", None)
            extra = ""
            if kind == "assistant":
                msg = event.get("message") if isinstance(event, dict) else None
                text = ""
                if isinstance(msg, dict):
                    parts = msg.get("content") or []
                    if parts and isinstance(parts[0], dict):
                        text = str(parts[0].get("text") or "")
                extra = " " + text[:160]
            print(kind, status or name or "", extra, flush=True)
        print("result", await run.wait())
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
