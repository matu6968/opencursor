# SPDX-License-Identifier: MIT-0
"""Local agent: HTTP MCP with PKCE browser OAuth (loopback :8787)."""

import asyncio
import os

from opencursor import Agent
from opencursor.types import AgentOptions


async def main() -> None:
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise SystemExit("Set CURSOR_API_KEY")
    url = os.environ.get("OPENCURSOR_MCP_OAUTH_URL")
    if not url:
        raise SystemExit("Set OPENCURSOR_MCP_OAUTH_URL to an HTTP MCP server that requires OAuth")
    auth: dict[str, str] = {}
    client_id = os.environ.get("OPENCURSOR_MCP_OAUTH_CLIENT_ID")
    if client_id:
        auth["CLIENT_ID"] = client_id
    secret = os.environ.get("OPENCURSOR_MCP_OAUTH_CLIENT_SECRET")
    if secret:
        auth["CLIENT_SECRET"] = secret
    client_name = os.environ.get("OPENCURSOR_MCP_OAUTH_CLIENT_NAME")
    if client_name:
        auth["CLIENT_NAME"] = client_name
    scopes = os.environ.get("OPENCURSOR_MCP_OAUTH_SCOPES")
    if scopes:
        auth["scopes"] = scopes
    cwd = os.environ.get("OPENCURSOR_LOCAL_CWD") or os.getcwd()
    cfg: dict[str, object] = {"type": "http", "url": url}
    if auth:
        cfg["auth"] = auth
    agent = await Agent.create(
        AgentOptions.model_validate(
            {
                "apiKey": api_key,
                "model": {"id": "composer-2.5"},
                "local": {"cwd": cwd, "sandboxOptions": {"enabled": False}},
                "tools": ["mcp"],
                "mcpServers": {"oauth": cfg},
            }
        )
    )
    try:
        run = await agent.send(
            "If an MCP server needs authentication, call its mcp_auth tool with empty arguments, "
            "then use the server's tools. Reply with what you did."
        )
        async for event in run.stream():
            print(event)
        print("result", await run.wait())
    finally:
        await agent.aclose()


if __name__ == "__main__":
    asyncio.run(main())
