from __future__ import annotations

import os
from typing import Any

import httpx

from opencursor.version import __version__


def default_cloud_base_url() -> str:
    return os.environ.get("CURSOR_BACKEND_URL") or "https://api.cursor.com"


def client_version_header() -> str:
    v = os.environ.get("AGENT_CLI_STATIC_VERSION")
    if isinstance(v, str) and v.strip():
        return f"cli-{v.strip()}"
    return f"sdk-{__version__}"


def default_ghost_mode() -> str:
    return "true"


def build_rest_headers(
    *,
    has_body: bool = False,
    streaming: bool = False,
) -> dict[str, str]:
    h: dict[str, str] = {
        "x-ghost-mode": default_ghost_mode(),
        "x-cursor-client-version": client_version_header(),
        "x-cursor-client-type": "sdk",
    }
    if has_body:
        h["Content-Type"] = "application/json"
    if streaming:
        h["x-cursor-streaming"] = "true"
    return h


def localhost_extra_header(base_url: str) -> dict[str, str]:
    try:
        from urllib.parse import urlparse

        host = urlparse(base_url).hostname or ""
        if host in ("localhost", "127.0.0.1"):
            return {"x-background-composer-local-use-non-vm": "true"}
    except Exception:
        pass
    return {}


def request_id_from_response(resp: httpx.Response) -> str | None:
    rid = resp.headers.get("x-request-id")
    return str(rid) if rid else None
