from __future__ import annotations

from typing import Any


_default_use_http1_for_agent: bool | None = None
_default_workspace_scan_cache_ttl_ms: int | None = None
_default_local_store: Any | None = None


def configure_cursor_sdk(options: Any) -> None:
    """Set module-level defaults for local SDK operations."""
    global _default_use_http1_for_agent, _default_workspace_scan_cache_ttl_ms, _default_local_store
    local = getattr(options, "local", None)
    if local is None and isinstance(options, dict):
        local = options.get("local")
    if local is None:
        return
    if isinstance(local, dict):
        if "useHttp1ForAgent" in local:
            value = local["useHttp1ForAgent"]
            _default_use_http1_for_agent = None if value is None else bool(value)
        if "workspaceScanCacheTtlMs" in local:
            ttl = local["workspaceScanCacheTtlMs"]
            _default_workspace_scan_cache_ttl_ms = None if ttl is None else int(ttl)
        if "store" in local:
            _default_local_store = local["store"]
        return
    fields = getattr(local, "model_fields_set", None)
    if hasattr(local, "useHttp1ForAgent") and (fields is None or "useHttp1ForAgent" in fields):
        value = local.useHttp1ForAgent
        _default_use_http1_for_agent = None if value is None else bool(value)
    if hasattr(local, "workspaceScanCacheTtlMs") and (fields is None or "workspaceScanCacheTtlMs" in fields):
        ttl = local.workspaceScanCacheTtlMs
        _default_workspace_scan_cache_ttl_ms = None if ttl is None else int(ttl)
    if fields is None:
        if hasattr(local, "store"):
            _default_local_store = local.store
    elif "store" in fields:
        _default_local_store = local.store


def resolve_local_agent_store(options: Any | None = None) -> Any | None:
    """Per-call `local.store` / `store`, else `Cursor.configure({ local: { store } })`."""
    if options is not None:
        local = getattr(options, "local", None)
        if local is None and isinstance(options, dict):
            local = options.get("local")
        nested = None
        if isinstance(local, dict):
            nested = local.get("store")
        elif local is not None:
            nested = getattr(local, "store", None)
        if nested is not None:
            return nested
        top = getattr(options, "store", None)
        if top is None and isinstance(options, dict):
            top = options.get("store")
        if top is not None:
            return top
        platform = getattr(options, "platform", None)
        if platform is None and isinstance(options, dict):
            platform = options.get("platform")
        if isinstance(platform, dict) and platform.get("localStore") is not None:
            return platform["localStore"]
        if platform is not None and not isinstance(platform, dict):
            local_store = getattr(platform, "localStore", None)
            if local_store is not None:
                return local_store
    return get_default_local_store()


def get_default_use_http1_for_agent() -> bool | None:
    return _default_use_http1_for_agent


def get_default_workspace_scan_cache_ttl_ms() -> int | None:
    return _default_workspace_scan_cache_ttl_ms


def get_default_local_store() -> Any | None:
    return _default_local_store


def clear_default_network_config_for_tests() -> None:
    global _default_use_http1_for_agent, _default_workspace_scan_cache_ttl_ms, _default_local_store
    _default_use_http1_for_agent = None
    _default_workspace_scan_cache_ttl_ms = None
    _default_local_store = None
