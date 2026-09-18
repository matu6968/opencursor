from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path

import httpx
import pytest

from opencursor._local_executor import LocalAgentExecutor, _interaction_response_body
from opencursor._mcp import McpManager
from opencursor._mcp_oauth import (
    MCP_AUTH_TOOL_NAME,
    CURSOR_MCP_HTTPS_REDIRECT,
    CURSOR_MCP_LOGO_URI,
    McpAuthStore,
    oauth_client_metadata,
    oauth_registration_bodies,
    parse_resource_metadata_url,
)
from opencursor._protobuf import decode_message, encode_message, oneof_case
from opencursor._subagents import DiscardStore
from opencursor.types import AgentOptions, ModelSelection

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "oauth_mcp_http.py"
_spec = importlib.util.spec_from_file_location("oauth_mcp_http", _FIXTURE)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
OAuthMcpHttpServer = _mod.OAuthMcpHttpServer


def test_parse_www_authenticate_resource_metadata() -> None:
    param = "resource" + "_metadata"
    header = f'Bearer {param}="https://example.com/.well-known/oauth-protected-resource"'
    assert parse_resource_metadata_url(header) == "https://example.com/.well-known/oauth-protected-resource"


def test_oauth_client_metadata_matches_cursor() -> None:
    meta = oauth_client_metadata(
        redirect_uri="http://127.0.0.1:8787/callback",
        metadata={"scopes_supported": ["mcp:connect"]},
    )
    assert meta["client_name"] == "Cursor"
    assert meta["logo_uri"] == CURSOR_MCP_LOGO_URI
    assert meta["scope"] == "mcp:connect"
    assert meta["token_endpoint_auth_method"] == "none"
    uris = meta["redirect_uris"]
    assert uris[0] == "cursor://anysphere.cursor-mcp/oauth/callback"
    assert CURSOR_MCP_HTTPS_REDIRECT in uris
    assert "http://localhost:8787/callback" in uris
    assert uris[-1] == "http://127.0.0.1:8787/callback"


def test_oauth_client_metadata_client_name_override() -> None:
    meta = oauth_client_metadata(
        redirect_uri="http://127.0.0.1:8787/callback",
        auth={"clientName": "Codex"},
        metadata={},
    )
    assert meta["client_name"] == "Codex"
    assert "scope" not in meta


def test_oauth_registration_bodies_loopback_fallback() -> None:
    bound = "http://127.0.0.1:8787/callback"
    bodies = oauth_registration_bodies(redirect_uri=bound)
    assert len(bodies) == 2
    assert CURSOR_MCP_HTTPS_REDIRECT in bodies[0]["redirect_uris"]
    assert bodies[1]["redirect_uris"] == [bound]
    assert bodies[1]["client_name"] == bodies[0]["client_name"]


def test_mcp_auth_request_response_roundtrip() -> None:
    query = {
        "id": 11,
        "mcp_auth_request_query": {"args": {"server_identifier": "gmail", "tool_call_id": "t1"}},
        "_oneof_query": "mcp_auth_request_query",
    }
    body = _interaction_response_body(query)
    decoded = decode_message(
        "agent.v1.AgentClientMessage",
        encode_message("agent.v1.AgentClientMessage", {"interaction_response": body}),
    )
    assert oneof_case(decoded, "message") == "interaction_response"
    assert oneof_case(decoded["interaction_response"]["mcp_auth_request_response"], "result") == "approved"


def test_token_store_roundtrip(tmp_path: Path) -> None:
    store = McpAuthStore(tmp_path / "mcp-auth.json")
    store.save_tokens("gmail", {"access_token": "a", "refresh_token": "r"})
    store.save_client_info("gmail", {"client_id": "cid"})
    again = McpAuthStore(tmp_path / "mcp-auth.json")
    assert again.tokens("gmail")["access_token"] == "a"
    assert again.client_info("gmail")["client_id"] == "cid"


async def _follow_authorize(url: str) -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        resp = await client.get(url)
        assert resp.status_code == 200


def test_http_mcp_oauth_browser_loopback(tmp_path: Path) -> None:
    asyncio.run(_test_http_mcp_oauth_browser_loopback(tmp_path))


async def _test_http_mcp_oauth_browser_loopback(tmp_path: Path) -> None:
    server = OAuthMcpHttpServer()
    server.start()
    try:
        store = McpAuthStore(tmp_path / "mcp-auth.json")
        manager = McpManager(open_browser=_follow_authorize, auth_store=store)
        opts = AgentOptions.model_validate(
            {
                "local": {"cwd": str(tmp_path)},
                "tools": ["mcp"],
                "mcpServers": {"oauth": {"type": "http", "url": server.mcp_url}},
            }
        )
        await manager.start(opts, tmp_path)
        session = manager.sessions["oauth"]
        assert session.status == "needsAuth"
        tools = session.tool_definitions()
        assert any(item["tool_name"] == MCP_AUTH_TOOL_NAME for item in tools)
        result = await manager.call_tool(
            {
                "name": f"oauth-{MCP_AUTH_TOOL_NAME}",
                "provider_identifier": "oauth",
                "tool_name": MCP_AUTH_TOOL_NAME,
            }
        )
        assert "success" in result
        assert session.status == "connected"
        echoed = await manager.call_tool(
            {
                "name": "oauth-echo",
                "provider_identifier": "oauth",
                "tool_name": "echo",
                "args": {"text": "hello-oauth"},
            }
        )
        assert echoed["success"]["content"][0]["text"]["text"] == "hello-oauth"
        assert store.tokens("oauth")["access_token"]
    finally:
        await manager.aclose()
        server.stop()


def test_mcp_auth_interaction_query_opens_browser(tmp_path: Path) -> None:
    asyncio.run(_test_mcp_auth_interaction_query_opens_browser(tmp_path))


async def _test_mcp_auth_interaction_query_opens_browser(tmp_path: Path) -> None:
    server = OAuthMcpHttpServer()
    server.start()
    store = McpAuthStore(tmp_path / "mcp-auth.json")
    manager = McpManager(open_browser=_follow_authorize, auth_store=store)
    try:
        opts = AgentOptions.model_validate(
            {
                "local": {"cwd": str(tmp_path)},
                "mcpServers": {"oauth": {"type": "http", "url": server.mcp_url}},
            }
        )
        executor = LocalAgentExecutor(
            store=DiscardStore(),
            agent_id="agent-1",
            run_id="run-1",
            options=opts,
            model=ModelSelection(id="composer-2"),
            prompt="hello",
            conversation_id="conv-1",
            nested=True,
            shared_mcp=manager,
        )
        await manager.start(opts, tmp_path)
        body = await executor._interaction_response(
            {
                "id": 12,
                "mcp_auth_request_query": {"args": {"server_identifier": "oauth"}},
                "_oneof_query": "mcp_auth_request_query",
            }
        )
        assert "approved" in body["mcp_auth_request_response"]
        assert manager.sessions["oauth"].status == "connected"
        ctx = executor._request_context()
        assert ctx["supports_mcp_auth"] is True
    finally:
        await manager.aclose()
        server.stop()


@pytest.mark.skipif(
    not os.environ.get("OPENCURSOR_LIVE_MCP_OAUTH"),
    reason="set OPENCURSOR_LIVE_MCP_OAUTH=1 and OPENCURSOR_MCP_OAUTH_URL to run a real browser login",
)
def test_live_mcp_oauth_browser_login(tmp_path: Path) -> None:
    asyncio.run(_test_live_mcp_oauth_browser_login(tmp_path))


async def _test_live_mcp_oauth_browser_login(tmp_path: Path) -> None:
    url = os.environ.get("OPENCURSOR_MCP_OAUTH_URL") or ""
    if not url:
        pytest.skip("OPENCURSOR_MCP_OAUTH_URL is required for the live MCP OAuth test")
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
    store = McpAuthStore(tmp_path / "mcp-auth.json")
    manager = McpManager(auth_store=store)
    server_cfg: dict[str, object] = {"type": "http", "url": url}
    if auth:
        server_cfg["auth"] = auth
    opts = AgentOptions.model_validate(
        {"local": {"cwd": str(tmp_path)}, "tools": ["mcp"], "mcpServers": {"live": server_cfg}}
    )
    try:
        await manager.start(opts, tmp_path)
        session = manager.sessions["live"]
        if session.status == "needsAuth":
            print("\nComplete MCP OAuth in the browser window that just opened.\n")
            await manager.authenticate("live", cwd=tmp_path)
        assert session.status == "connected", session.error_message
    finally:
        await manager.aclose()
