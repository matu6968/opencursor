from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class OAuthMcpHttpServer:
    """In-process MCP HTTP server plus a minimal OAuth authorization server."""

    def __init__(self) -> None:
        self.access_tokens: set[str] = set()
        self.refresh_tokens: dict[str, str] = {}
        self._codes: dict[str, dict[str, str]] = {}
        self._clients: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/.well-known/oauth-authorization-server":
                    parent._json(
                        self,
                        {
                            "issuer": parent.base_url,
                            "authorization_endpoint": f"{parent.base_url}/authorize",
                            "token_endpoint": f"{parent.base_url}/token",
                            "registration_endpoint": f"{parent.base_url}/register",
                            "code_challenge_methods_supported": ["S256"],
                            "grant_types_supported": ["authorization_code", "refresh_token"],
                            "response_types_supported": ["code"],
                        },
                    )
                    return
                if parsed.path == "/.well-known/oauth-protected-resource":
                    parent._json(
                        self,
                        {
                            "resource": f"{parent.base_url}/mcp",
                            "authorization_servers": [parent.base_url],
                        },
                    )
                    return
                if parsed.path == "/authorize":
                    parent._authorize(self, parse_qs(parsed.query))
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length else b""
                if parsed.path == "/register":
                    parent._register(self, raw)
                    return
                if parsed.path == "/token":
                    parent._token(self, raw)
                    return
                if parsed.path == "/mcp":
                    parent._mcp(self, raw)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=2)

    def _json(self, handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _authorize(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> None:
        redirect = (query.get("redirect_uri") or [""])[0]
        state = (query.get("state") or [""])[0]
        challenge = (query.get("code_challenge") or [""])[0]
        client_id = (query.get("client_id") or [""])[0]
        if not redirect or not challenge:
            handler.send_response(400)
            handler.end_headers()
            return
        code = secrets.token_urlsafe(16)
        with self._lock:
            self._codes[code] = {
                "challenge": challenge,
                "redirect_uri": redirect,
                "client_id": client_id,
            }
        location = f"{redirect}{'&' if '?' in redirect else '?'}code={code}&state={state}"
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.end_headers()

    def _register(self, handler: BaseHTTPRequestHandler, raw: bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        client_id = "dyn-" + secrets.token_hex(8)
        with self._lock:
            self._clients[client_id] = {"redirect_uri": (payload.get("redirect_uris") or [""])[0]}
        self._json(handler, {"client_id": client_id, "token_endpoint_auth_method": "none"}, status=201)

    def _token(self, handler: BaseHTTPRequestHandler, raw: bytes) -> None:
        form = parse_qs(raw.decode("utf-8"))
        grant = (form.get("grant_type") or [""])[0]
        if grant == "refresh_token":
            refresh = (form.get("refresh_token") or [""])[0]
            with self._lock:
                old = self.refresh_tokens.get(refresh)
            if not old:
                self._json(handler, {"error": "invalid_grant"}, status=400)
                return
            access = secrets.token_urlsafe(16)
            with self._lock:
                self.access_tokens.discard(old)
                self.access_tokens.add(access)
                self.refresh_tokens[refresh] = access
            self._json(handler, {"access_token": access, "token_type": "Bearer", "refresh_token": refresh, "expires_in": 3600})
            return
        code = (form.get("code") or [""])[0]
        verifier = (form.get("code_verifier") or [""])[0]
        redirect = (form.get("redirect_uri") or [""])[0]
        with self._lock:
            record = self._codes.pop(code, None)
        if not record or record["redirect_uri"] != redirect:
            self._json(handler, {"error": "invalid_grant"}, status=400)
            return
        digest = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        if digest != record["challenge"]:
            self._json(handler, {"error": "invalid_grant"}, status=400)
            return
        access = secrets.token_urlsafe(16)
        refresh = secrets.token_urlsafe(16)
        with self._lock:
            self.access_tokens.add(access)
            self.refresh_tokens[refresh] = access
        self._json(
            handler,
            {"access_token": access, "token_type": "Bearer", "refresh_token": refresh, "expires_in": 3600},
        )

    def _mcp(self, handler: BaseHTTPRequestHandler, raw: bytes) -> None:
        auth = handler.headers.get("Authorization") or ""
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        with self._lock:
            allowed = token in self.access_tokens
        if not allowed:
            handler.send_response(401)
            handler.send_header(
                "WWW-Authenticate",
                f'Bearer {"resource" + "_metadata"}="{self.base_url}/.well-known/oauth-protected-resource"',
            )
            handler.end_headers()
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            message = {}
        method = message.get("method")
        req_id = message.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oauth-echo", "version": "0.1.0"},
                "instructions": "oauth ok",
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "echo",
                        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                    }
                ]
            }
        elif method == "tools/call":
            args = (message.get("params") or {}).get("arguments") or {}
            result = {"content": [{"type": "text", "text": str(args.get("text") or "")}]}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "notifications/initialized" or req_id is None:
            handler.send_response(202)
            handler.end_headers()
            return
        else:
            result = {}
        self._json(handler, {"jsonrpc": "2.0", "id": req_id, "result": result})
