from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import quote

import httpx

from opencursor import errors
from opencursor._connect import CONNECT_PROTO_VERSION, PROTOCOL_SDK_VERSION

DEFAULT_LOGIN_API_KEY_TTL_MS = 90 * 24 * 60 * 60 * 1000
DEFAULT_WEBSITE_URL = "https://cursor.com"
DEFAULT_AUTH_BACKEND = "https://api2.cursor.sh"
_STORE_DEFAULT = object()

_stored_login_cache: dict[str, Any] | None = None
_stored_login_cache_loaded = False


def strip_trailing_slashes(url: str) -> str:
    return url.rstrip("/")


def resolve_website_url(url: str | None = None) -> str:
    return strip_trailing_slashes(url or os.environ.get("CURSOR_WEBSITE_URL") or DEFAULT_WEBSITE_URL)


def resolve_api_base_url(url: str | None = None) -> str:
    if url:
        return strip_trailing_slashes(url)
    backend = os.environ.get("CURSOR_BACKEND_URL")
    if backend and "api.cursor.com" not in backend:
        return strip_trailing_slashes(backend)
    return strip_trailing_slashes(os.environ.get("CURSOR_AGENT_BACKEND_URL") or DEFAULT_AUTH_BACKEND)


def get_default_sdk_auth_path() -> Path:
    return Path.home() / ".cursor" / "sdk" / "auth.json"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def create_login_handshake(website_url: str) -> dict[str, str]:
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    login_uuid = str(uuid.uuid4())
    login_url = (
        f"{strip_trailing_slashes(website_url)}/loginDeepControl"
        f"?challenge={quote(challenge)}&uuid={quote(login_uuid)}&mode=login&redirectTarget=sdk"
    )
    return {"uuid": login_uuid, "verifier": verifier, "loginUrl": login_url}


def parse_stored_sdk_credentials(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    api_key = value.get("apiKey")
    backend_url = value.get("backendUrl")
    if not isinstance(api_key, str) or not api_key or not isinstance(backend_url, str) or not backend_url:
        return None
    out: dict[str, Any] = {
        "version": 1,
        "backendUrl": backend_url,
        "apiKey": api_key,
        "createdAtMs": int(value.get("createdAtMs") or 0),
    }
    if isinstance(value.get("apiKeyExpiresAtMs"), (int, float)):
        out["apiKeyExpiresAtMs"] = int(value["apiKeyExpiresAtMs"])
    if isinstance(value.get("email"), str) and value["email"]:
        out["email"] = value["email"]
    return out


class SdkCredentialStore(Protocol):
    async def load(self) -> dict[str, Any] | None: ...
    async def save(self, credentials: dict[str, Any]) -> None: ...
    async def clear(self) -> None: ...


class FileCredentialStore:
    def __init__(self, file_path: str | Path | None = None) -> None:
        self._file_path = Path(file_path) if file_path else get_default_sdk_auth_path()

    @property
    def path(self) -> Path:
        return self._file_path

    async def load(self) -> dict[str, Any] | None:
        try:
            raw = self._file_path.read_text(encoding="utf-8")
            return parse_stored_sdk_credentials(json.loads(raw))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None

    async def save(self, credentials: dict[str, Any]) -> None:
        path = self._file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(credentials, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    async def clear(self) -> None:
        try:
            self._file_path.unlink()
        except FileNotFoundError:
            return


class InMemoryCredentialStore:
    def __init__(self) -> None:
        self._credentials: dict[str, Any] | None = None

    async def load(self) -> dict[str, Any] | None:
        return self._credentials

    async def save(self, credentials: dict[str, Any]) -> None:
        self._credentials = dict(credentials)

    async def clear(self) -> None:
        self._credentials = None


def clear_stored_login_cache() -> None:
    global _stored_login_cache, _stored_login_cache_loaded
    _stored_login_cache = None
    _stored_login_cache_loaded = False


def _credentials_usable(creds: dict[str, Any] | None, backend_url: str | None = None) -> dict[str, Any] | None:
    if not creds:
        return None
    expires = creds.get("apiKeyExpiresAtMs")
    if isinstance(expires, (int, float)) and expires <= time.time() * 1000:
        return None
    expected = resolve_api_base_url(backend_url)
    stored = strip_trailing_slashes(str(creds.get("backendUrl") or ""))
    if stored and stored != expected:
        return None
    return creds


def get_stored_login_api_key() -> str | None:
    global _stored_login_cache, _stored_login_cache_loaded
    if not _stored_login_cache_loaded:
        store = FileCredentialStore()
        try:
            raw = store.path.read_text(encoding="utf-8")
            _stored_login_cache = parse_stored_sdk_credentials(json.loads(raw))
        except (OSError, json.JSONDecodeError, UnicodeError):
            _stored_login_cache = None
        _stored_login_cache_loaded = True
    creds = _credentials_usable(_stored_login_cache)
    if not creds:
        return None
    return str(creds["apiKey"])


def resolve_default_api_key(explicit: str | None = None) -> str | None:
    if explicit is not None:
        return explicit
    env = os.environ.get("CURSOR_API_KEY")
    if env:
        return env
    return get_stored_login_api_key()


def resolve_api_key(explicit: str | None = None) -> str:
    key = resolve_default_api_key(explicit)
    if not key:
        raise errors.ConfigurationError(
            "API key is required for cloud operations. Set CURSOR_API_KEY, pass apiKey, or call Cursor.auth.login().",
            is_retryable=False,
        )
    return key


def is_likely_to_open_browser(url: str) -> bool:
    if os.environ.get("NO_OPEN_BROWSER"):
        return False
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"):
        return False
    try:
        parsed = httpx.URL(url)
        return parsed.scheme in ("http", "https")
    except Exception:
        return url.startswith("http://") or url.startswith("https://")


async def open_browser(url: str) -> None:
    if not is_likely_to_open_browser(url) and not (url.startswith("http://") or url.startswith("https://")):
        raise errors.ConfigurationError(f"Invalid URL: only http:// and https:// links can be opened. Received: {url}", is_retryable=False)
    if sys.platform == "darwin":
        cmd = ["open", url]
    elif sys.platform == "win32":
        cmd = ["cmd", "/c", "start", "", url]
    else:
        cmd = ["xdg-open", url]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


def _default_api_key_name() -> str:
    try:
        host = socket.gethostname() or "unknown-host"
    except OSError:
        host = "unknown-host"
    return f"Cursor SDK login ({host})"


async def poll_for_login_tokens(
    *,
    api_url: str,
    login_uuid: str,
    verifier: str,
    max_attempts: int = 150,
    base_delay_ms: int = 1000,
    max_delay_ms: int = 10_000,
    on_warning: Callable[[str], None] | None = None,
) -> dict[str, str] | None:
    use_get = False
    consecutive_errors = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(max_attempts):
            delay = min(base_delay_ms * (1.2**attempt), max_delay_ms)
            try:
                if use_get:
                    resp = await client.get(
                        f"{api_url}/auth/poll",
                        params={"uuid": login_uuid, "verifier": verifier},
                        headers={"Accept": "application/json"},
                    )
                else:
                    resp = await client.post(
                        f"{api_url}/auth/poll",
                        headers={"Accept": "application/json", "Content-Type": "application/json"},
                        json={"uuid": login_uuid, "verifier": verifier},
                    )
            except httpx.RequestError:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    return None
                await asyncio.sleep(delay / 1000)
                continue
            if resp.status_code == 404:
                body = (resp.text or "").strip()
                if not use_get and body != "Not found":
                    use_get = True
                    if on_warning:
                        on_warning(
                            f"{api_url} has no POST /auth/poll; falling back to GET, which puts the "
                            "single-use login verifier in the request URL."
                        )
                    continue
                consecutive_errors = 0
                await asyncio.sleep(delay / 1000)
                continue
            if not resp.is_success:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    return None
                await asyncio.sleep(delay / 1000)
                continue
            consecutive_errors = 0
            try:
                payload = resp.json()
            except json.JSONDecodeError:
                return None
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("accessToken"), str)
                and isinstance(payload.get("refreshToken"), str)
            ):
                return {"accessToken": payload["accessToken"], "refreshToken": payload["refreshToken"]}
            return None
    return None


async def _dashboard_json(backend_url: str, access_token: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{strip_trailing_slashes(backend_url)}/aiserver.v1.DashboardService/{method}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Connect-Protocol-Version": CONNECT_PROTO_VERSION,
        "Content-Type": "application/json",
        "x-cursor-client-type": "sdk",
        "x-cursor-client-version": f"sdk-{PROTOCOL_SDK_VERSION}",
        "x-ghost-mode": "false",
        "x-request-id": str(uuid.uuid4()),
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=body)
    if resp.status_code >= 400:
        raise errors.AuthenticationError(
            f"Dashboard {method} failed: {resp.status_code} {resp.text[:300]}",
            status=resp.status_code,
            is_retryable=resp.status_code >= 500,
        )
    try:
        return resp.json() if resp.content else {}
    except json.JSONDecodeError as exc:
        raise errors.NetworkError("Failed to parse DashboardService response", cause=exc, is_retryable=False) from exc


async def mint_user_api_key(*, backend_url: str, access_token: str, name: str, expires_at_ms: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name}
    if expires_at_ms is not None:
        body["expiresAt"] = str(int(expires_at_ms))
    try:
        created = await _dashboard_json(backend_url, access_token, "CreateUserApiKey", body)
    except errors.CursorSdkError as exc:
        raise errors.AuthenticationError(
            f"Login succeeded, but creating an SDK API key failed: {exc} "
            "(your team's settings may restrict user API keys — ask a team admin, "
            "or pass an existing API key directly)",
            code=exc.code,
            is_retryable=exc.is_retryable,
            cause=exc,
        ) from exc
    api_key = str(created.get("apiKey") or created.get("api_key") or "")
    if not api_key:
        raise errors.AuthenticationError(
            "Login succeeded, but the server returned an empty API key.",
            code="internal",
            is_retryable=True,
        )
    email: str | None = None
    try:
        me = await _dashboard_json(backend_url, access_token, "GetMe", {})
        raw_email = me.get("email")
        if isinstance(raw_email, str) and raw_email:
            email = raw_email
    except Exception:
        email = None
    return {"apiKey": api_key, "email": email}


async def sdk_login(options: Any | None = None) -> dict[str, Any]:
    opts = options or {}
    backend_url = resolve_api_base_url(getattr(opts, "backendUrl", None) if not isinstance(opts, dict) else opts.get("backendUrl"))
    website_url = resolve_website_url(getattr(opts, "websiteUrl", None) if not isinstance(opts, dict) else opts.get("websiteUrl"))
    handshake = create_login_handshake(website_url)
    login_url = handshake["loginUrl"]
    on_login_url = getattr(opts, "onLoginUrl", None) if not isinstance(opts, dict) else opts.get("onLoginUrl")
    open_browser_opt = getattr(opts, "openBrowser", True) if not isinstance(opts, dict) else opts.get("openBrowser", True)
    surfaced = False
    if callable(on_login_url):
        on_login_url(login_url)
        surfaced = True
    if callable(open_browser_opt):
        maybe = open_browser_opt(login_url)
        if asyncio.iscoroutine(maybe):
            await maybe
        surfaced = True
    elif open_browser_opt and is_likely_to_open_browser(login_url):
        try:
            await open_browser(login_url)
            surfaced = True
        except Exception:
            pass
    if not surfaced:
        sys.stderr.write(f"Could not open a browser. Open this URL to sign in to Cursor:\n\n  {login_url}\n\n")
    tokens = await poll_for_login_tokens(
        api_url=backend_url,
        login_uuid=handshake["uuid"],
        verifier=handshake["verifier"],
        on_warning=lambda msg: sys.stderr.write(f"warning: {msg}\n"),
    )
    if tokens is None:
        raise errors.AuthenticationError("Login failed or timed out. Please try again.", code="unauthenticated", is_retryable=True)
    ttl = getattr(opts, "apiKeyTtlMs", None) if not isinstance(opts, dict) else opts.get("apiKeyTtlMs")
    if ttl is None:
        ttl = DEFAULT_LOGIN_API_KEY_TTL_MS
    if not isinstance(ttl, (int, float)) or ttl <= 0:
        raise errors.AuthenticationError(
            f"apiKeyTtlMs must be a positive number of milliseconds, got {ttl}",
            code="invalid_argument",
            is_retryable=False,
        )
    expires_at_ms = int(time.time() * 1000 + ttl)
    name = getattr(opts, "apiKeyName", None) if not isinstance(opts, dict) else opts.get("apiKeyName")
    minted = await mint_user_api_key(
        backend_url=backend_url,
        access_token=tokens["accessToken"],
        name=str(name or _default_api_key_name()),
        expires_at_ms=expires_at_ms,
    )
    if isinstance(opts, dict):
        persist = "store" not in opts or opts.get("store") is not None
        store = opts.get("store") if persist and opts.get("store") is not None else FileCredentialStore()
    else:
        store_attr = getattr(opts, "store", _STORE_DEFAULT)
        persist = store_attr is not None
        store = FileCredentialStore() if store_attr is _STORE_DEFAULT else store_attr
    if persist:
        creds = {
            "version": 1,
            "backendUrl": backend_url,
            "apiKey": minted["apiKey"],
            "apiKeyExpiresAtMs": expires_at_ms,
            "email": minted.get("email"),
            "createdAtMs": int(time.time() * 1000),
        }
        await store.save(creds)
        clear_stored_login_cache()
    return {"apiKey": minted["apiKey"], "email": minted.get("email"), "apiKeyExpiresAtMs": expires_at_ms}


async def sdk_logout(options: Any | None = None) -> None:
    opts = options or {}
    store = getattr(opts, "store", None) if not isinstance(opts, dict) else opts.get("store")
    store = store or FileCredentialStore()
    await store.clear()
    clear_stored_login_cache()


async def sdk_auth_status(options: Any | None = None) -> dict[str, Any]:
    opts = options or {}
    store = getattr(opts, "store", None) if not isinstance(opts, dict) else opts.get("store")
    store = store or FileCredentialStore()
    creds = await store.load()
    if not creds:
        return {"status": "logged-out"}
    expires = creds.get("apiKeyExpiresAtMs")
    if isinstance(expires, (int, float)) and expires <= time.time() * 1000:
        return {"status": "logged-out"}
    out: dict[str, Any] = {"status": "logged-in", "backendUrl": creds.get("backendUrl")}
    if creds.get("email"):
        out["email"] = creds["email"]
    if expires is not None:
        out["apiKeyExpiresAtMs"] = expires
    return out
