from __future__ import annotations

from typing import Any

from opencursor._auth import sdk_auth_status, sdk_login, sdk_logout
from opencursor._cloud_agent import _resolve_api_key
from opencursor._cloud_api import CloudApiClient
from opencursor._sdk_config import configure_cursor_sdk
from opencursor.agent import _run_sync_or_awaitable
from opencursor.types import CursorConfigureOptions, CursorRequestOptions, SDKModel, SDKRepository, SDKUser


def _cursor_api_key(
    options: CursorRequestOptions | dict[str, Any] | None,
    api_key: str | None,
) -> str:
    if isinstance(options, dict):
        options = CursorRequestOptions.model_validate(options)
    from_opts = options.apiKey if options is not None else None
    return _resolve_api_key(api_key if api_key is not None else from_opts)


def _catalog_items(data: object) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "models", "repositories"):
        raw = data.get(key)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    return []


class Cursor:
    """Account-level helpers (`Cursor.me`, `Cursor.models.list`, `Cursor.repositories.list`)."""

    def __init__(self) -> None:
        raise RuntimeError("Cursor is not instantiable")

    @staticmethod
    def configure(options: CursorConfigureOptions | dict[str, Any]) -> None:
        if isinstance(options, dict):
            options = CursorConfigureOptions.model_validate(options)
        configure_cursor_sdk(options)

    @staticmethod
    def me(
        options: CursorRequestOptions | dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> SDKUser:
        return _run_sync_or_awaitable(Cursor._me(options, api_key=api_key))

    @staticmethod
    async def _me(
        options: CursorRequestOptions | dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> SDKUser:
        client = CloudApiClient(_cursor_api_key(options, api_key))
        try:
            raw = await client.get_me()
            if isinstance(raw, dict) and isinstance(raw.get("user"), dict):
                raw = raw["user"]
            return SDKUser.model_validate(raw)
        finally:
            await client.aclose()


class _Models:
    @staticmethod
    def list(
        options: CursorRequestOptions | dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> list[SDKModel]:
        return _run_sync_or_awaitable(_Models._list(options, api_key=api_key))

    @staticmethod
    async def _list(
        options: CursorRequestOptions | dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> list[SDKModel]:
        client = CloudApiClient(_cursor_api_key(options, api_key))
        try:
            data = await client.list_models()
            return [SDKModel.model_validate(item) for item in _catalog_items(data)]
        finally:
            await client.aclose()


class _Repositories:
    @staticmethod
    def list(
        options: CursorRequestOptions | dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> list[SDKRepository]:
        return _run_sync_or_awaitable(_Repositories._list(options, api_key=api_key))

    @staticmethod
    async def _list(
        options: CursorRequestOptions | dict[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> list[SDKRepository]:
        client = CloudApiClient(_cursor_api_key(options, api_key))
        try:
            data = await client.list_repositories()
            return [SDKRepository.model_validate(item) for item in _catalog_items(data)]
        finally:
            await client.aclose()


class _Auth:
    @staticmethod
    def login(options: Any | None = None) -> dict[str, Any]:
        return _run_sync_or_awaitable(sdk_login(options))

    @staticmethod
    def logout(options: Any | None = None) -> None:
        return _run_sync_or_awaitable(sdk_logout(options))

    @staticmethod
    def status(options: Any | None = None) -> dict[str, Any]:
        return _run_sync_or_awaitable(sdk_auth_status(options))


Cursor.models = _Models()  # type: ignore[attr-defined]
Cursor.repositories = _Repositories()  # type: ignore[attr-defined]
Cursor.auth = _Auth()  # type: ignore[attr-defined]
