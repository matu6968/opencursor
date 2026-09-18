from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from opencursor.types import AgentOptions, McpServerRemote, McpServerStdio
from opencursor._mcp_oauth import (
    MCP_AUTH_TOOL_DESCRIPTION,
    MCP_AUTH_TOOL_NAME,
    McpAuthStore,
    McpOAuthError,
    McpUnauthorizedError,
    OpenBrowserFn,
    authorize_mcp_http,
    default_mcp_auth_path,
    discover_authorization_server,
    mcp_auth_fields,
    refresh_access_token,
)

MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_CLIENT_INFO = {"name": "opencursor", "version": "0.1.0"}
MCP_RPC_TIMEOUT_S = 30.0
MCP_CALL_TIMEOUT_S = 60.0


@dataclass
class McpSession:
    name: str
    identifier: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    instructions: str | None = None
    status: str = "loading"
    error_message: str | None = None
    cfg: Any | None = None
    www_authenticate: str | None = None
    _transport: Any | None = None

    def tool_definitions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for tool in self.tools:
            tool_name = str(tool.get("name") or "")
            schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
            item: dict[str, Any] = {
                "name": f"{self.name}-{tool_name}",
                "provider_identifier": self.name,
                "tool_name": tool_name,
                "description": str(tool.get("description") or ""),
                "input_schema": schema,
                "input_schema_json": json.dumps(schema),
            }
            if tool.get("outputSchema") is not None:
                item["output_schema_json"] = json.dumps(tool["outputSchema"])
            if tool.get("annotations") is not None:
                item["annotations_json"] = json.dumps(tool["annotations"])
            out.append(item)
        if self.status == "needsAuth" and not any(str(tool.get("name") or "") == MCP_AUTH_TOOL_NAME for tool in self.tools):
            schema = {"type": "object", "properties": {}, "additionalProperties": False}
            out.append(
                {
                    "name": f"{self.name}-{MCP_AUTH_TOOL_NAME}",
                    "provider_identifier": self.name,
                    "tool_name": MCP_AUTH_TOOL_NAME,
                    "description": MCP_AUTH_TOOL_DESCRIPTION,
                    "input_schema": schema,
                    "input_schema_json": json.dumps(schema),
                }
            )
        return out


class McpManager:
    def __init__(self, *, open_browser: OpenBrowserFn | None = None, auth_store: McpAuthStore | None = None) -> None:
        self.sessions: dict[str, McpSession] = {}
        self._open_browser = open_browser
        self._auth_store = auth_store
        self._cwd: Path | None = None

    async def start(self, options: AgentOptions, cwd: Path) -> None:
        servers = getattr(options, "mcpServers", None) or {}
        if not servers:
            return
        self._cwd = cwd
        if self._auth_store is None:
            self._auth_store = McpAuthStore(default_mcp_auth_path(cwd))
        await asyncio.gather(*(self._start_one(name, cfg, cwd) for name, cfg in servers.items()))

    async def aclose(self) -> None:
        sessions = list(self.sessions.values())
        self.sessions.clear()
        await asyncio.gather(*(self._close_session(session) for session in sessions), return_exceptions=True)

    def tool_definitions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for session in self.sessions.values():
            if session.status in ("connected", "needsAuth"):
                out.extend(session.tool_definitions())
        return out

    def instructions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for session in self.sessions.values():
            if session.instructions:
                out.append(
                    {
                        "server_name": session.name,
                        "server_identifier": session.identifier,
                        "instructions": session.instructions,
                    }
                )
        return out

    def state_servers(self, identifiers: list[str] | None = None) -> list[dict[str, Any]]:
        wanted = {item for item in (identifiers or []) if item}
        out: list[dict[str, Any]] = []
        for session in self.sessions.values():
            if wanted and session.identifier not in wanted and session.name not in wanted:
                continue
            item: dict[str, Any] = {
                "server_name": session.name,
                "server_identifier": session.identifier,
                "tools": session.tool_definitions() if session.status in ("connected", "needsAuth") else [],
                "status": session.status,
            }
            if session.instructions:
                item["instructions"] = [
                    {
                        "server_name": session.name,
                        "server_identifier": session.identifier,
                        "instructions": session.instructions,
                    }
                ]
            if session.error_message:
                item["error_message"] = session.error_message
            out.append(item)
        return out

    def available_servers(self) -> list[str]:
        return [session.name for session in self.sessions.values()]

    def available_tools(self) -> list[str]:
        names: list[str] = []
        for session in self.sessions.values():
            names.extend(tool.get("name") or "" for tool in session.tools)
        return [name for name in names if name]

    async def call_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        session, tool_name = self._resolve_call(args)
        if session is None:
            return {
                "server_not_found": {
                    "name": str(args.get("provider_identifier") or args.get("server_identifier") or args.get("name") or ""),
                    "available_servers": self.available_servers(),
                }
            }
        if tool_name == MCP_AUTH_TOOL_NAME or str(args.get("tool_name") or "") == MCP_AUTH_TOOL_NAME:
            return await self._authenticate_result(session)
        if session.status != "connected" or session._transport is None:
            return {"error": {"error": session.error_message or f"MCP server {session.name!r} is not connected"}}
        if tool_name is None:
            return {"tool_not_found": {"name": str(args.get("tool_name") or args.get("name") or ""), "available_tools": self.available_tools()}}
        call_args = _plain_args(args.get("args"))
        try:
            result = await session._transport.request(
                "tools/call",
                {"name": tool_name, "arguments": call_args},
                timeout=MCP_CALL_TIMEOUT_S,
            )
        except Exception as exc:
            return {"error": {"error": str(exc)}}
        return {"success": _mcp_success(result)}

    async def list_resources(self, args: dict[str, Any]) -> dict[str, Any]:
        server = (args.get("server") or "").strip()
        sessions = list(self.sessions.values())
        if server:
            session = self._session_by_name(server)
            if session is None:
                return {"error": {"error": f"unknown MCP server {server!r}"}}
            sessions = [session]
        resources: list[dict[str, Any]] = []
        for session in sessions:
            if session.status != "connected":
                continue
            resources.extend(_resource_items(session))
        return {"success": {"resources": resources}}

    async def read_resource(self, args: dict[str, Any]) -> dict[str, Any]:
        server = (args.get("server") or "").strip()
        uri = (args.get("uri") or "").strip()
        session = self._session_by_name(server) if server else self._session_for_uri(uri)
        if session is None or session._transport is None:
            return {"not_found": {"uri": uri}}
        try:
            result = await session._transport.request("resources/read", {"uri": uri})
        except Exception as exc:
            return {"error": {"uri": uri, "error": str(exc)}}
        contents = result.get("contents") or []
        first = contents[0] if contents else {}
        payload: dict[str, Any] = {
            "uri": first.get("uri") or uri,
            "mime_type": first.get("mimeType") or "",
            "name": first.get("name") or "",
        }
        if first.get("text") is not None:
            payload["text"] = first.get("text") or ""
        elif first.get("blob"):
            try:
                payload["blob"] = base64.b64decode(first["blob"])
            except Exception:
                payload["text"] = str(first.get("blob") or "")
        return {"success": payload}

    async def _start_one(self, name: str, cfg: Any, cwd: Path) -> None:
        session = McpSession(name=name, identifier=name, cfg=cfg)
        self.sessions[name] = session
        try:
            await self._connect_session(session, cwd)
        except McpUnauthorizedError as exc:
            session.status = "needsAuth"
            session.www_authenticate = exc.www_authenticate
            session.error_message = str(exc)
            await self._close_session(session)
        except Exception as exc:
            session.status = "error"
            session.error_message = str(exc)
            await self._close_session(session)

    async def authenticate(self, server_identifier: str, *, cwd: Path | None = None) -> None:
        session = self._session_by_name(server_identifier)
        if session is None:
            raise McpOAuthError(f"unknown MCP server {server_identifier!r}")
        workdir = cwd or self._cwd or Path(".")
        if self._auth_store is None:
            self._auth_store = McpAuthStore(default_mcp_auth_path(workdir))
        cfg = session.cfg
        if not (isinstance(cfg, McpServerRemote) or (isinstance(cfg, dict) and cfg.get("url"))):
            raise McpOAuthError(f"MCP server {session.name!r} is not an HTTP server")
        remote = cfg if isinstance(cfg, McpServerRemote) else McpServerRemote.model_validate(cfg)
        await authorize_mcp_http(
            remote.url,
            identifier=session.identifier,
            auth=remote.auth,
            store=self._auth_store,
            www_authenticate=session.www_authenticate,
            open_browser_fn=self._open_browser,
        )
        await self._connect_session(session, workdir)

    async def _authenticate_result(self, session: McpSession) -> dict[str, Any]:
        try:
            await self.authenticate(session.identifier)
        except Exception as exc:
            return {"error": {"error": str(exc)}}
        return {
            "success": {
                "content": [{"text": {"text": f"Authenticated MCP server {session.name}"}}],
                "is_error": False,
            }
        }

    async def _connect_session(self, session: McpSession, cwd: Path) -> None:
        await self._close_session(session)
        tokens = None
        if self._auth_store is not None:
            tokens = self._auth_store.tokens(session.identifier)
        transport = await open_mcp_transport(session.cfg, cwd, access_token=(tokens or {}).get("access_token"))
        session._transport = transport
        try:
            init = await transport.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": MCP_CLIENT_INFO,
                },
            )
        except McpUnauthorizedError:
            if tokens and tokens.get("refresh_token") and isinstance(session.cfg, (McpServerRemote, dict)):
                remote = session.cfg if isinstance(session.cfg, McpServerRemote) else McpServerRemote.model_validate(session.cfg)
                refreshed = await self._try_refresh(session.identifier, remote, tokens)
                if refreshed:
                    await self._close_session(session)
                    transport = await open_mcp_transport(session.cfg, cwd, access_token=refreshed.get("access_token"))
                    session._transport = transport
                    init = await transport.request(
                        "initialize",
                        {
                            "protocolVersion": MCP_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": MCP_CLIENT_INFO,
                        },
                    )
                else:
                    raise
            else:
                raise
        session.instructions = init.get("instructions") or None
        await transport.notify("notifications/initialized")
        listed = await transport.request("tools/list", {})
        session.tools = list(listed.get("tools") or [])
        try:
            resources = await transport.request("resources/list", {})
            session.resources = list(resources.get("resources") or [])
        except Exception:
            session.resources = []
        session.status = "connected"
        session.error_message = None
        session.www_authenticate = None

    async def _try_refresh(self, identifier: str, remote: McpServerRemote, tokens: dict[str, Any]) -> dict[str, Any] | None:
        if self._auth_store is None:
            return None
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            try:
                metadata = await discover_authorization_server(client, remote.url)
                client_info = self._auth_store.client_info(identifier) or mcp_auth_fields(remote.auth)
                refreshed = await refresh_access_token(
                    client, metadata, client_info=client_info, refresh_token=str(tokens.get("refresh_token") or "")
                )
            except Exception:
                return None
        if refreshed:
            self._auth_store.save_tokens(identifier, refreshed)
        return refreshed

    async def _close_session(self, session: McpSession) -> None:
        transport = session._transport
        session._transport = None
        if transport is not None:
            try:
                await transport.aclose()
            except Exception:
                pass

    def _session_by_name(self, name: str) -> McpSession | None:
        if name in self.sessions:
            return self.sessions[name]
        for session in self.sessions.values():
            if session.identifier == name:
                return session
        return None

    def _session_for_uri(self, uri: str) -> McpSession | None:
        for session in self.sessions.values():
            for resource in session.resources:
                if resource.get("uri") == uri:
                    return session
        return None

    def _resolve_call(self, args: dict[str, Any]) -> tuple[McpSession | None, str | None]:
        provider = str(args.get("provider_identifier") or args.get("server_identifier") or "")
        tool_name = str(args.get("tool_name") or "")
        wire = str(args.get("name") or "")
        session = self._session_by_name(provider) if provider else None
        if session is None and wire:
            for name, candidate in self.sessions.items():
                prefix = f"{name}-"
                if wire.startswith(prefix):
                    session = candidate
                    if not tool_name:
                        tool_name = wire[len(prefix) :]
                    break
        if session is None and len(self.sessions) == 1:
            session = next(iter(self.sessions.values()))
        if session is None:
            return None, None
        if not tool_name:
            tool_name = wire
        names = {str(tool.get("name") or "") for tool in session.tools}
        if tool_name not in names:
            if tool_name == MCP_AUTH_TOOL_NAME or wire == MCP_AUTH_TOOL_NAME or wire.endswith(f"-{MCP_AUTH_TOOL_NAME}"):
                return session, MCP_AUTH_TOOL_NAME
            if wire in names:
                tool_name = wire
            else:
                return session, None
        return session, tool_name


def mcp_servers_configured(options: AgentOptions) -> bool:
    servers = getattr(options, "mcpServers", None) or {}
    return bool(servers)


async def open_mcp_transport(cfg: Any, cwd: Path, *, access_token: str | None = None) -> Any:
    if isinstance(cfg, McpServerRemote) or (isinstance(cfg, dict) and cfg.get("url")):
        remote = cfg if isinstance(cfg, McpServerRemote) else McpServerRemote.model_validate(cfg)
        return await HttpMcpTransport.connect(remote, access_token=access_token)
    stdio = cfg if isinstance(cfg, McpServerStdio) else McpServerStdio.model_validate(cfg)
    return await StdioMcpTransport.connect(stdio, cwd)


class StdioMcpTransport:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, cfg: McpServerStdio, cwd: Path) -> "StdioMcpTransport":
        env = os.environ.copy()
        if cfg.env:
            env.update(cfg.env)
        workdir = cfg.cwd or str(cwd)
        proc = await asyncio.create_subprocess_exec(
            cfg.command,
            *(cfg.args or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=env,
        )
        transport = cls(proc)
        transport._stderr_task = asyncio.create_task(transport._drain_stderr())
        return transport

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = MCP_RPC_TIMEOUT_S) -> Any:
        self._id += 1
        req_id = self._id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def aclose(self) -> None:
        self._reader.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        if self._proc.stdin:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2)
        except Exception:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("MCP transport closed"))
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        if self._proc.stderr is None:
            return
        try:
            while await self._proc.stderr.read(4096):
                pass
        except Exception:
            return

    async def _write(self, message: dict[str, Any]) -> None:
        if self._proc.stdin is None:
            raise RuntimeError("MCP stdin closed")
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        self._proc.stdin.write(header + payload)
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        buf = b""
        try:
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    message, buf = _pop_mcp_frame(buf)
                    if message is None:
                        break
                    self._dispatch(message)
        except asyncio.CancelledError:
            return
        except Exception:
            return

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" not in message:
            return
        req_id = message.get("id")
        fut = self._pending.get(req_id) if isinstance(req_id, int) else None
        if fut is None or fut.done():
            return
        if "error" in message:
            err = message["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            fut.set_exception(RuntimeError(text or "MCP error"))
            return
        fut.set_result(message.get("result"))


class HttpMcpTransport:
    def __init__(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str], session_id: str | None
    ) -> None:
        self._client = client
        self._url = url
        self._headers = headers
        self._session_id = session_id
        self._id = 0

    @classmethod
    async def connect(cls, cfg: McpServerRemote, *, access_token: str | None = None) -> "HttpMcpTransport":
        parsed = urlparse(cfg.url)
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError(f"unsupported MCP url scheme: {parsed.scheme or '(none)'}")
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if cfg.headers:
            headers.update(cfg.headers)
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        client = httpx.AsyncClient(follow_redirects=True, timeout=MCP_RPC_TIMEOUT_S)
        return cls(client, cfg.url, headers, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            await self._client.post(self._url, json=message, headers=headers)
        except Exception:
            return

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = MCP_RPC_TIMEOUT_S) -> Any:
        self._id += 1
        req_id = self._id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = await self._client.post(self._url, json=message, headers=headers, timeout=timeout)
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if resp.status_code == 401:
            raise McpUnauthorizedError(www_authenticate=resp.headers.get("www-authenticate"))
        resp.raise_for_status()
        payload = _http_mcp_payload(resp)
        if "error" in payload:
            err = payload["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(text or "MCP error")
        return payload.get("result")

    async def aclose(self) -> None:
        await self._client.aclose()


def _pop_mcp_frame(buf: bytes) -> tuple[dict[str, Any] | None, bytes]:
    if not buf:
        return None, buf
    lower = buf.lower()
    if lower.startswith(b"content-length:") or b"\ncontent-length:" in lower[:200] or buf.startswith(b"Content-Length:"):
        sep = buf.find(b"\r\n\r\n")
        header_end = 4
        if sep < 0:
            sep = buf.find(b"\n\n")
            header_end = 2
        if sep < 0:
            return None, buf
        headers = buf[:sep].decode("ascii", errors="replace")
        length = None
        for line in headers.splitlines():
            if line.lower().startswith("content-length:"):
                try:
                    length = int(line.split(":", 1)[1].strip())
                except ValueError:
                    length = None
        if length is None:
            return None, buf[sep + header_end :]
        start = sep + header_end
        if len(buf) < start + length:
            return None, buf
        payload = buf[start : start + length]
        rest = buf[start + length :]
        try:
            return json.loads(payload.decode("utf-8")), rest
        except json.JSONDecodeError:
            return None, rest
    stripped = buf.lstrip()
    if not stripped.startswith(b"{"):
        nl = buf.find(b"\n")
        if nl < 0:
            return None, buf
        return None, buf[nl + 1 :]
    try:
        message = json.loads(stripped.decode("utf-8"))
        consumed = len(buf) - len(stripped) + len(json.dumps(message).encode("utf-8"))
        # Prefer newline-delimited JSON when present.
        nl = stripped.find(b"\n")
        if nl >= 0:
            line = stripped[:nl].strip()
            try:
                return json.loads(line.decode("utf-8")), buf[len(buf) - len(stripped) + nl + 1 :]
            except json.JSONDecodeError:
                pass
        return message, b""
    except json.JSONDecodeError:
        return None, buf


def _http_mcp_payload(resp: httpx.Response) -> dict[str, Any]:
    ctype = (resp.headers.get("content-type") or "").lower()
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    parsed = json.loads(data)
                    if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed or "id" in parsed):
                        return parsed
        return {}
    parsed = resp.json()
    return parsed if isinstance(parsed, dict) else {}


def _plain_args(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and set(value) <= {"_raw"} and isinstance(value.get("_raw"), (bytes, bytearray)):
            continue
        out[str(key)] = value
    return out


def _mcp_success(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"content": [{"text": {"text": str(result)}}], "is_error": False}
    items: list[dict[str, Any]] = []
    for part in result.get("content") or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "image":
            data = part.get("data") or b""
            if isinstance(data, str):
                try:
                    data = base64.b64decode(data)
                except Exception:
                    data = data.encode("utf-8")
            items.append({"image": {"data": data, "mime_type": part.get("mimeType") or "image/png"}})
        else:
            text = part.get("text")
            if text is None:
                text = json.dumps(part)
            items.append({"text": {"text": str(text)}})
    payload: dict[str, Any] = {"content": items, "is_error": bool(result.get("isError"))}
    if isinstance(result.get("structuredContent"), dict):
        payload["structured_content"] = result["structuredContent"]
    return payload


def _resource_items(session: McpSession) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for resource in session.resources:
        item: dict[str, Any] = {
            "uri": resource.get("uri") or "",
            "server": session.name,
        }
        if resource.get("name"):
            item["name"] = resource["name"]
        if resource.get("description"):
            item["description"] = resource["description"]
        if resource.get("mimeType"):
            item["mime_type"] = resource["mimeType"]
        out.append(item)
    return out
