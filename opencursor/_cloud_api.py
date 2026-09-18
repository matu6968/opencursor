from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from opencursor import errors
from opencursor._http import (
    build_rest_headers,
    default_cloud_base_url,
    localhost_extra_header,
    request_id_from_response,
)


def _encode(s: str) -> str:
    return quote(s, safe="")


class CloudApiClient:
    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = (base_url or default_cloud_base_url()).rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(300.0, connect=30.0))

    async def aclose(self) -> None:
        if self._client.is_closed:
            return
        await self._client.aclose()

    async def _headers(self, *, has_body: bool = False, streaming: bool = False) -> dict[str, str]:
        h = build_rest_headers(has_body=has_body, streaming=streaming)
        h["Authorization"] = f"Bearer {self.api_key}"
        h.update(localhost_extra_header(self.base_url))
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        q = {k: v for k, v in (params or {}).items() if v is not None}
        endpoint = f"{method} {path}"
        url = path if path.startswith("http") else path
        try:
            headers = await self._headers(has_body=json_body is not None)
            if extra_headers:
                headers.update(extra_headers)
            r = await self._client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=q or None,
            )
        except httpx.RequestError as e:
            raise errors.NetworkError(
                "Network request failed",
                cause=e,
                endpoint=endpoint,
                is_retryable=True,
            ) from e
        body_text = (await r.aread()).decode("utf-8", errors="replace")
        rid = request_id_from_response(r)
        if not r.is_success:
            errors.raise_for_cloud_api_status(r.status_code, body_text, endpoint=endpoint, request_id=rid)
        if not body_text:
            return {}
        try:
            return json.loads(body_text)
        except json.JSONDecodeError as e:
            raise errors.UnknownAgentError(
                "Failed to parse Cursor API response",
                status=r.status_code,
                endpoint=endpoint,
                request_id=rid,
                cause=e,
                is_retryable=False,
            ) from e

    async def create_agent(self, req: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return await self._request("POST", "/v1/agents", json_body=req, extra_headers=extra)

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/agents/{_encode(agent_id)}")

    async def list_agents(
        self,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("GET", "/v1/agents", params=params)

    async def archive_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/agents/{_encode(agent_id)}/archive")

    async def unarchive_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/agents/{_encode(agent_id)}/unarchive")

    async def delete_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/v1/agents/{_encode(agent_id)}")

    async def create_run(
        self,
        agent_id: str,
        req: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        extra = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return await self._request(
            "POST",
            f"/v1/agents/{_encode(agent_id)}/runs",
            json_body=req,
            extra_headers=extra,
        )

    async def list_runs(self, agent_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("GET", f"/v1/agents/{_encode(agent_id)}/runs", params=params)

    async def get_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/v1/agents/{_encode(agent_id)}/runs/{_encode(run_id)}",
        )

    async def cancel_run(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v1/agents/{_encode(agent_id)}/runs/{_encode(run_id)}/cancel",
        )

    @asynccontextmanager
    async def stream_run(
        self,
        agent_id: str,
        run_id: str,
        *,
        last_event_id: str | None = None,
    ) -> AsyncIterator[httpx.Response]:
        path = f"/v1/agents/{_encode(agent_id)}/runs/{_encode(run_id)}/stream"
        headers = await self._headers(streaming=True)
        headers["Accept"] = "text/event-stream"
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        endpoint = f"GET {path}"
        try:
            async with self._client.stream("GET", path, headers=headers) as r:
                if r.status_code < 200 or r.status_code >= 300:
                    body_text = (await r.aread()).decode("utf-8", errors="replace")
                    rid = request_id_from_response(r)
                    errors.raise_for_cloud_api_status(r.status_code, body_text, endpoint=endpoint, request_id=rid)
                yield r
        except httpx.RequestError as e:
            raise errors.NetworkError(
                "Network request failed",
                endpoint=endpoint,
                cause=e,
                is_retryable=True,
            ) from e

    async def list_artifacts(self, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/agents/{_encode(agent_id)}/artifacts")

    async def get_artifact_download_url(self, agent_id: str, path: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/v1/agents/{_encode(agent_id)}/artifacts/download",
            params={"path": path},
        )

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/me")

    async def list_models(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/models")

    async def list_repositories(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/repositories")

    async def get_agent_usage(self, agent_id: str, run_id: str | None = None) -> dict[str, Any]:
        params = {"runId": run_id} if run_id else None
        return await self._request("GET", f"/v1/agents/{_encode(agent_id)}/usage", params=params)

    async def list_agent_messages(
        self,
        agent_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        params = {"limit": limit, "offset": offset}
        for path in (
            f"/v1/agents/{_encode(agent_id)}/messages",
            f"/v1/agents/{_encode(agent_id)}/conversation",
        ):
            try:
                data = await self._request("GET", path, params=params)
            except errors.CursorSdkError as exc:
                if exc.status == 404:
                    continue
                raise
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                items = data.get("messages") if isinstance(data, dict) else []
            if not isinstance(items, list):
                return []
            out: list[dict[str, Any]] = []
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                msg_type = item.get("type") or "assistant"
                if msg_type not in ("user", "assistant"):
                    continue
                out.append(
                    {
                        "type": msg_type,
                        "uuid": str(item.get("uuid") or f"{agent_id}:{i}"),
                        "agent_id": str(item.get("agent_id") or agent_id),
                        "message": item.get("message") or item,
                    }
                )
            return out
        return []
