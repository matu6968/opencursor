from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from opencursor._auth import is_likely_to_open_browser, open_browser

DEFAULT_LOOPBACK_REDIRECT = "http://localhost:8787/callback"
CURSOR_MCP_CLIENT_NAME = "Cursor"
CURSOR_MCP_LOGO_URI = (
    "https://ptht05hbb1ssoooe.public.blob.vercel-storage.com/assets/uploads/cursorlogomcpv3.svg"
)
CURSOR_MCP_HTTPS_REDIRECT = "https://www.cursor.com/agents/mcp/oauth/callback"
CURSOR_MCP_CUSTOM_SCHEME_REDIRECT = "cursor://anysphere.cursor-mcp/oauth/callback"
GROKBOT_MCP_REDIRECT = "grokbot://mcp/oauth/callback"
GROKBOT_MCP_HTTPS_REDIRECT = "https://www.cursor.com/bot/mcp/oauth/callback"
MCP_OAUTH_TIMEOUT_S = 300.0
MCP_AUTH_TOOL_NAME = "mcp_auth"
MCP_AUTH_TOOL_DESCRIPTION = (
    "Authenticate this MCP server so its tools can be used. Call this tool through your "
    "MCP tool-calling interface when STATUS.md indicates this server needs authentication."
)
_CALLBACK_HTML = b"""<!DOCTYPE html><html><body><p>Authentication complete. You can close this window.</p></body></html>"""
OpenBrowserFn = Callable[[str], Awaitable[None]]


class McpUnauthorizedError(RuntimeError):
    def __init__(self, message: str = "MCP server requires authentication", *, www_authenticate: str | None = None) -> None:
        super().__init__(message)
        self.www_authenticate = www_authenticate


class McpOAuthError(RuntimeError):
    pass


def mcp_auth_fields(auth: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if not auth:
        return {}
    if not isinstance(auth, Mapping):
        dumped = getattr(auth, "model_dump", None)
        if not callable(dumped):
            return {}
        auth = dumped(by_alias=True, exclude_none=True)
        if not isinstance(auth, Mapping):
            return {}
    client_id = auth.get("CLIENT_ID") or auth.get("clientId") or auth.get("client_id")
    client_secret = auth.get("CLIENT_SECRET") or auth.get("clientSecret") or auth.get("client_secret")
    client_name = auth.get("CLIENT_NAME") or auth.get("clientName") or auth.get("client_name")
    scopes_raw = auth.get("scopes") or auth.get("SCOPES")
    scopes: list[str] = []
    if isinstance(scopes_raw, str):
        scopes = [part for part in scopes_raw.split() if part]
    elif isinstance(scopes_raw, (list, tuple)):
        scopes = [str(item) for item in scopes_raw if item]
    out: dict[str, Any] = {}
    if client_id:
        out["client_id"] = str(client_id)
    if client_secret:
        out["client_secret"] = str(client_secret)
    if client_name:
        out["client_name"] = str(client_name)
    if scopes:
        out["scopes"] = scopes
    return out


def default_mcp_auth_path(cwd: Path) -> Path:
    from opencursor._local_runtime import _default_state_root

    return _default_state_root(str(cwd.resolve())) / "mcp-auth.json"


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _is_loopback_http(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "http":
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def mcp_oauth_redirect_uris(redirect_uri: str) -> list[str]:
    """Cursor registers a set of redirect URIs, not only the bound loopback."""
    seen: list[str] = []

    def add(url: str) -> None:
        if url and url not in seen:
            seen.append(url)

    loopback = _is_loopback_http(redirect_uri)
    if redirect_uri and not loopback:
        add(redirect_uri)
    add(CURSOR_MCP_CUSTOM_SCHEME_REDIRECT)
    add(CURSOR_MCP_HTTPS_REDIRECT)
    if redirect_uri == GROKBOT_MCP_REDIRECT:
        add(GROKBOT_MCP_HTTPS_REDIRECT)
    add(DEFAULT_LOOPBACK_REDIRECT)
    if redirect_uri and loopback:
        add(redirect_uri)
    return seen


def _scope_value(scopes: object) -> str | None:
    if isinstance(scopes, str):
        text = scopes.strip()
        return text or None
    if isinstance(scopes, (list, tuple)):
        parts = [str(item) for item in scopes if item]
        return " ".join(parts) if parts else None
    return None


def oauth_client_metadata(
    *,
    redirect_uri: str,
    auth: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    redirect_uris: list[str] | None = None,
) -> dict[str, Any]:
    fields = mcp_auth_fields(auth)
    body: dict[str, Any] = {
        "client_name": fields.get("client_name") or CURSOR_MCP_CLIENT_NAME,
        "logo_uri": CURSOR_MCP_LOGO_URI,
        "redirect_uris": list(redirect_uris) if redirect_uris is not None else mcp_oauth_redirect_uris(redirect_uri),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    scope = _scope_value(fields.get("scopes") or (metadata or {}).get("scopes_supported"))
    if scope:
        body["scope"] = scope
    return body


def oauth_registration_bodies(
    *,
    redirect_uri: str,
    auth: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Cursor's URI set first; loopback-only fallback for strict DCR (Figma)."""
    primary = oauth_client_metadata(redirect_uri=redirect_uri, auth=auth, metadata=metadata)
    loopback = oauth_client_metadata(
        redirect_uri=redirect_uri,
        auth=auth,
        metadata=metadata,
        redirect_uris=[redirect_uri],
    )
    if loopback["redirect_uris"] == primary["redirect_uris"]:
        return [primary]
    return [primary, loopback]


def _is_invalid_redirect_uri(resp: httpx.Response) -> bool:
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("error") == "invalid_redirect_uri":
        return True
    return "invalid_redirect_uri" in (resp.text or "")


def _http_error_detail(resp: httpx.Response) -> str:
    text = (resp.text or "").strip().replace("\n", " ")
    if len(text) > 240:
        text = text[:240] + "..."
    if text:
        return f"HTTP {resp.status_code}: {text}"
    return f"HTTP {resp.status_code}"


def parse_resource_metadata_url(www_authenticate: str | None) -> str | None:
    if not www_authenticate:
        return None
    match = re.search(r'resource_metadata\s*=\s*"([^"]+)"', www_authenticate, re.I)
    if match:
        return match.group(1)
    match = re.search(r"resource_metadata\s*=\s*([^\s,]+)", www_authenticate, re.I)
    return match.group(1).strip('"') if match else None


class McpAuthStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self._loaded = False

    def load(self) -> dict[str, Any]:
        if self._loaded:
            return self._data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        out: dict[str, Any] = {}
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            if "tokens" in value or "clientInfo" in value:
                out[str(key)] = value
            elif "access_token" in value or "refresh_token" in value:
                out[str(key)] = {"tokens": value}
        self._data = out
        self._loaded = True
        return self._data

    def tokens(self, identifier: str) -> dict[str, Any] | None:
        entry = self.load().get(identifier) or {}
        tokens = entry.get("tokens")
        return tokens if isinstance(tokens, dict) else None

    def client_info(self, identifier: str) -> dict[str, Any] | None:
        entry = self.load().get(identifier) or {}
        info = entry.get("clientInfo")
        return info if isinstance(info, dict) else None

    def save_tokens(self, identifier: str, tokens: dict[str, Any] | None) -> None:
        data = self.load()
        entry = dict(data.get(identifier) or {})
        if tokens is None:
            entry.pop("tokens", None)
        else:
            entry["tokens"] = tokens
        if entry:
            data[identifier] = entry
        elif identifier in data:
            del data[identifier]
        self._write(data)

    def save_client_info(self, identifier: str, info: dict[str, Any] | None) -> None:
        data = self.load()
        entry = dict(data.get(identifier) or {})
        if info is None:
            entry.pop("clientInfo", None)
        else:
            entry["clientInfo"] = info
        if entry:
            data[identifier] = entry
        elif identifier in data:
            del data[identifier]
        self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._data = data
        self._loaded = True


async def discover_authorization_server(
    client: httpx.AsyncClient, mcp_url: str, www_authenticate: str | None = None
) -> dict[str, Any]:
    parsed = urlparse(mcp_url)
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    metadata_url = parse_resource_metadata_url(www_authenticate)
    candidates = []
    if metadata_url:
        candidates.append(metadata_url)
    path = parsed.path.rstrip("/")
    if path:
        candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    candidates.append(f"{origin}/.well-known/oauth-protected-resource")
    resource_meta: dict[str, Any] = {}
    for url in candidates:
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict):
                    resource_meta = payload
                    break
        except Exception:
            continue
    servers = resource_meta.get("authorization_servers") or []
    issuer = servers[0] if servers else origin
    issuer = str(issuer).rstrip("/")
    as_candidates = [
        f"{issuer}/.well-known/oauth-authorization-server",
        f"{issuer}/.well-known/openid-configuration",
    ]
    issuer_parsed = urlparse(issuer)
    if issuer_parsed.path and issuer_parsed.path != "/":
        as_origin = urlunparse((issuer_parsed.scheme, issuer_parsed.netloc, "", "", "", ""))
        as_candidates.insert(
            0,
            f"{as_origin}/.well-known/oauth-authorization-server{issuer_parsed.path}",
        )
    for url in as_candidates:
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("authorization_endpoint") and payload.get("token_endpoint"):
                    payload.setdefault("issuer", issuer)
                    return payload
        except Exception:
            continue
    raise McpOAuthError(f"could not discover OAuth authorization server for {mcp_url}")


async def register_oauth_client(
    client: httpx.AsyncClient,
    metadata: dict[str, Any],
    *,
    redirect_uri: str,
    auth: Mapping[str, Any] | None = None,
    stored: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fields = mcp_auth_fields(auth)
    static_id = fields.get("client_id")
    if static_id:
        info = {"client_id": static_id, "redirect_uris": [redirect_uri]}
        if fields.get("client_secret"):
            info["client_secret"] = fields["client_secret"]
        return info
    if stored and stored.get("client_id"):
        return dict(stored)
    endpoint = metadata.get("registration_endpoint")
    if not endpoint:
        raise McpOAuthError(
            "OAuth client registration has not completed. The MCP server may not support "
            "dynamic client registration or the registration failed."
        )
    bodies = oauth_registration_bodies(redirect_uri=redirect_uri, auth=auth, metadata=metadata)
    resp: httpx.Response | None = None
    for index, body in enumerate(bodies):
        resp = await client.post(str(endpoint), json=body)
        if resp.status_code < 400:
            break
        if index + 1 < len(bodies) and _is_invalid_redirect_uri(resp):
            continue
        detail = _http_error_detail(resp)
        hint = ""
        if resp.status_code == 403:
            hint = (
                " This authorization server may allowlist DCR client_name; "
                "set mcpServers.*.auth.clientName or auth.CLIENT_ID."
            )
        raise McpOAuthError(f"OAuth client registration failed: {detail}.{hint}")
    assert resp is not None
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("client_id"):
        raise McpOAuthError("OAuth client registration returned no client_id")
    return payload


async def default_open_browser(url: str) -> None:
    sys.stderr.write(f"Authenticate the MCP server in your browser:\n\n  {url}\n\n")
    sys.stderr.flush()
    if os.environ.get("NO_OPEN_BROWSER"):
        return
    if is_likely_to_open_browser(url) or url.startswith("http://") or url.startswith("https://"):
        try:
            await open_browser(url)
        except Exception:
            sys.stderr.write("Could not open a browser automatically.\n")
            sys.stderr.flush()


class _LoopbackResult:
    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.query: dict[str, list[str]] = {}
        self.error: str | None = None


def _start_loopback(loop: asyncio.AbstractEventLoop, result: _LoopbackResult) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in ("/callback", "/"):
                self.send_response(404)
                self.end_headers()
                return
            query = parse_qs(parsed.query)
            result.query = query
            if query.get("error"):
                result.error = query["error"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_CALLBACK_HTML)))
            self.end_headers()
            self.wfile.write(_CALLBACK_HTML)
            loop.call_soon_threadsafe(result.event.set)

        def log_message(self, format: str, *args: object) -> None:
            return

    last_error: OSError | None = None
    for host, port in (("127.0.0.1", 8787), ("localhost", 8787), ("127.0.0.1", 0)):
        try:
            server = ThreadingHTTPServer((host, port), Handler)
            bound_host, bound_port = server.server_address[:2]
            redirect = f"http://127.0.0.1:{bound_port}/callback"
            return server, redirect
        except OSError as exc:
            last_error = exc
    raise McpOAuthError(f"could not bind OAuth loopback server: {last_error}")


async def exchange_authorization_code(
    client: httpx.AsyncClient,
    metadata: dict[str, Any],
    *,
    client_info: Mapping[str, Any],
    code: str,
    verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "client_id": client_info["client_id"],
    }
    if client_info.get("client_secret"):
        data["client_secret"] = client_info["client_secret"]
    resp = await client.post(str(metadata["token_endpoint"]), data=data)
    if resp.status_code >= 400:
        raise McpOAuthError(f"OAuth token exchange failed: HTTP {resp.status_code} {resp.text[:300]}")
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise McpOAuthError("OAuth token exchange returned no access_token")
    return payload


async def refresh_access_token(
    client: httpx.AsyncClient,
    metadata: dict[str, Any],
    *,
    client_info: Mapping[str, Any],
    refresh_token: str,
) -> dict[str, Any] | None:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_info.get("client_id") or "",
    }
    if client_info.get("client_secret"):
        data["client_secret"] = client_info["client_secret"]
    try:
        resp = await client.post(str(metadata["token_endpoint"]), data=data)
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        return None
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh_token
    return payload


async def authorize_mcp_http(
    mcp_url: str,
    *,
    identifier: str,
    auth: Mapping[str, Any] | Any | None,
    store: McpAuthStore,
    www_authenticate: str | None = None,
    open_browser_fn: OpenBrowserFn | None = None,
    timeout_s: float = MCP_OAUTH_TIMEOUT_S,
) -> dict[str, Any]:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        metadata = await discover_authorization_server(client, mcp_url, www_authenticate)
        loop = asyncio.get_running_loop()
        result = _LoopbackResult()
        server, redirect_uri = _start_loopback(loop, result)
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client_info = await register_oauth_client(
                client, metadata, redirect_uri=redirect_uri, auth=auth, stored=store.client_info(identifier)
            )
            store.save_client_info(identifier, client_info)
            verifier, challenge = pkce_pair()
            state = secrets.token_urlsafe(16)
            params = {
                "response_type": "code",
                "client_id": client_info["client_id"],
                "redirect_uri": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "resource": mcp_url.rstrip("/"),
            }
            fields = mcp_auth_fields(auth)
            scopes = fields.get("scopes") or metadata.get("scopes_supported")
            if scopes:
                params["scope"] = " ".join(scopes) if isinstance(scopes, list) else str(scopes)
            authorize_url = str(metadata["authorization_endpoint"])
            join = "&" if "?" in authorize_url else "?"
            authorize_url = f"{authorize_url}{join}{urlencode(params)}"
            opener = open_browser_fn or default_open_browser
            await opener(authorize_url)
            try:
                await asyncio.wait_for(result.event.wait(), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise McpOAuthError("timed out waiting for MCP OAuth browser login") from exc
            if result.error:
                desc = (result.query.get("error_description") or [result.error])[0]
                raise McpOAuthError(f"OAuth authorization failed: {desc}")
            code = (result.query.get("code") or [""])[0]
            returned_state = (result.query.get("state") or [""])[0]
            if not code:
                raise McpOAuthError("OAuth callback did not include an authorization code")
            if returned_state != state:
                raise McpOAuthError("OAuth callback state mismatch")
            tokens = await exchange_authorization_code(
                client,
                metadata,
                client_info=client_info,
                code=code,
                verifier=verifier,
                redirect_uri=redirect_uri,
            )
            store.save_tokens(identifier, tokens)
            return tokens
        finally:
            server.shutdown()
            thread.join(timeout=2)


async def ensure_access_token(
    mcp_url: str,
    *,
    identifier: str,
    auth: Mapping[str, Any] | Any | None,
    store: McpAuthStore,
    www_authenticate: str | None = None,
    open_browser_fn: OpenBrowserFn | None = None,
    interactive: bool = True,
) -> dict[str, Any] | None:
    tokens = store.tokens(identifier)
    if tokens and tokens.get("access_token"):
        return tokens
    if not interactive:
        return None
    return await authorize_mcp_http(
        mcp_url,
        identifier=identifier,
        auth=auth,
        store=store,
        www_authenticate=www_authenticate,
        open_browser_fn=open_browser_fn,
    )
