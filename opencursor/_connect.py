from __future__ import annotations

import asyncio
import json
import os
import struct
import uuid
from typing import Any, AsyncIterator, Protocol
from urllib.parse import urlparse

import httpx

from opencursor import errors
from opencursor._protobuf import decode_message, encode_message

CONNECT_PROTO_VERSION = "1"
DEFAULT_AGENT_BACKEND = "https://api2.cursor.sh"
PROTOCOL_SDK_VERSION = "1.0.31"

HTTP2_CONFIG_UNSPECIFIED = 0
HTTP2_CONFIG_FORCE_ALL_DISABLED = 1
HTTP2_CONFIG_FORCE_ALL_ENABLED = 2
HTTP2_CONFIG_FORCE_BIDI_DISABLED = 3
HTTP2_CONFIG_FORCE_BIDI_ENABLED = 4

_HTTP2_CONFIG_CACHE: dict[str, int | None] = {}


def reset_http2_config_cache() -> None:
    _HTTP2_CONFIG_CACHE.clear()


class Http2Unavailable(RuntimeError):
    """HTTP/2 bidi Run could not be used; callers should fall back to RunSSE."""


class AgentRunSession(Protocol):
    transport: str

    async def send(self, payload: bytes) -> None: ...
    def __aiter__(self) -> AsyncIterator[dict[str, Any]]: ...
    async def aclose(self) -> None: ...


def default_agent_base_url() -> str:
    explicit = os.environ.get("CURSOR_AGENT_BACKEND_URL")
    if explicit:
        return explicit.rstrip("/")
    backend = os.environ.get("CURSOR_BACKEND_URL")
    if backend and "api.cursor.com" not in backend:
        return backend.rstrip("/")
    return DEFAULT_AGENT_BACKEND


def env_use_http1() -> bool:
    value = (os.environ.get("CURSOR_USE_HTTP1") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def is_plain_http_url(url: str) -> bool:
    try:
        return urlparse(url).scheme == "http"
    except Exception:
        return url.startswith("http://")


def local_prefer_http1(base_url: str, option: bool | None) -> bool:
    if is_plain_http_url(base_url):
        return True
    if option is not None:
        return bool(option)
    from opencursor._sdk_config import get_default_use_http1_for_agent

    configured = get_default_use_http1_for_agent()
    if configured is not None:
        return bool(configured)
    return env_use_http1()


def select_use_http1(*, http2_config: int | None, local_prefer: bool) -> bool:
    if http2_config in (HTTP2_CONFIG_FORCE_ALL_DISABLED, HTTP2_CONFIG_FORCE_BIDI_DISABLED):
        return True
    if http2_config in (HTTP2_CONFIG_FORCE_ALL_ENABLED, HTTP2_CONFIG_FORCE_BIDI_ENABLED):
        return False
    return local_prefer


def http2_available() -> bool:
    try:
        import h2  # noqa: F401
    except ImportError:
        return False
    return True


def parse_http2_config(buf: bytes) -> int | None:
    i = 0
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, i = _read_varint(buf, i)
            if field == 7:
                return int(value)
        elif wire == 1:
            i += 8
        elif wire == 2:
            length, i = _read_varint(buf, i)
            i += length
        elif wire == 5:
            i += 4
        else:
            break
    return None


def should_fallback_to_http1(exc: BaseException) -> bool:
    if isinstance(exc, Http2Unavailable):
        return True
    if isinstance(exc, (errors.AuthenticationError, errors.RateLimitError, errors.AgentBusyError, errors.AgentNotFoundError)):
        return False
    if isinstance(exc, errors.CursorAgentError):
        status = exc.status
        if status in {404, 415, 426, 505} or (status is not None and status >= 500):
            return True
        if status is not None and 400 <= status < 500:
            return False
    if isinstance(exc, (httpx.ProtocolError, httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException)):
        return True
    return True


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _stream_timeout() -> httpx.Timeout:
    return httpx.Timeout(120.0, connect=30.0, read=None, write=30.0, pool=30.0)


class ConnectClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 120.0,
        use_http1: bool | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = (base_url or default_agent_base_url()).rstrip("/")
        self._access_token: str | None = None
        self._use_http1_option = use_http1
        self._http2_failed = False
        self._http = httpx.AsyncClient(http2=False, timeout=_stream_timeout(), follow_redirects=True)
        self._http2: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._http2 is not None:
            await self._http2.aclose()
            self._http2 = None

    async def exchange_access_token(self) -> str:
        url = f"{self.base_url}/auth/exchange_user_api_key"
        resp = await self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "x-cursor-client-type": "sdk",
                "x-cursor-client-version": f"sdk-{PROTOCOL_SDK_VERSION}",
            },
            content=b"{}",
        )
        if resp.status_code >= 400:
            errors.raise_for_connect_http(resp.status_code, resp.text, endpoint=url)
        data = resp.json()
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise errors.AuthenticationError("API key exchange returned no accessToken", endpoint=url, is_retryable=False)
        self._access_token = str(token)
        return self._access_token

    async def _bearer(self) -> str:
        if not self._access_token:
            await self.exchange_access_token()
        assert self._access_token
        return self._access_token

    def _headers(
        self,
        *,
        token: str,
        content_type: str,
        streaming: bool,
        allowed_tools: list[str] | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {token}",
            "Connect-Protocol-Version": CONNECT_PROTO_VERSION,
            "Content-Type": content_type,
            "x-cursor-client-type": "sdk",
            "x-cursor-client-version": f"sdk-{PROTOCOL_SDK_VERSION}",
            "x-ghost-mode": "false",
            "x-request-id": str(uuid.uuid4()),
        }
        if streaming:
            h["x-cursor-streaming"] = "true"
        if allowed_tools is not None:
            h["x-cursor-agent-allowed-tools"] = ",".join(allowed_tools)
        if extra:
            h.update(extra)
        return h

    async def unary(self, service: str, method: str, type_name: str, message: dict[str, Any], response_type: str) -> dict[str, Any]:
        token = await self._bearer()
        url = f"{self.base_url}/{service}/{method}"
        body = encode_message(type_name, message)
        resp = await self._http.post(
            url,
            headers=self._headers(token=token, content_type="application/proto", streaming=False),
            content=body,
        )
        if resp.status_code == 401:
            await self.exchange_access_token()
            token = await self._bearer()
            resp = await self._http.post(
                url,
                headers=self._headers(token=token, content_type="application/proto", streaming=False),
                content=body,
            )
        if resp.status_code >= 400:
            self._raise_connect_error(resp, url)
        return decode_message(response_type, resp.content)

    async def bidi_append(self, request_id: str, seqno: int, payload: bytes) -> None:
        await self.unary(
            "aiserver.v1.BidiService",
            "BidiAppend",
            "aiserver.v1.BidiAppendRequest",
            {
                "request_id": {"request_id": request_id},
                "append_seqno": seqno,
                "data": payload.hex(),
            },
            "aiserver.v1.BidiAppendResponse",
        )

    async def fetch_http2_config(self) -> int | None:
        key = self.api_key
        if key in _HTTP2_CONFIG_CACHE:
            return _HTTP2_CONFIG_CACHE[key]
        url = f"{self.base_url}/aiserver.v1.ServerConfigService/GetServerConfig"
        try:
            token = await self._bearer()
            resp = await self._http.post(
                url,
                headers=self._headers(token=token, content_type="application/proto", streaming=False),
                content=b"",
            )
            if resp.status_code >= 400:
                _HTTP2_CONFIG_CACHE[key] = None
                return None
            config = parse_http2_config(resp.content)
            _HTTP2_CONFIG_CACHE[key] = config
            return config
        except Exception:
            _HTTP2_CONFIG_CACHE[key] = None
            return None

    async def decide_use_http1(self) -> bool:
        local = local_prefer_http1(self.base_url, self._use_http1_option)
        if self._http2_failed or not http2_available():
            return True
        config = await self.fetch_http2_config()
        return select_use_http1(http2_config=config, local_prefer=local)

    async def open_run(
        self,
        request_id: str,
        *,
        allowed_tools: list[str] | None = None,
    ) -> AgentRunSession:
        use_http1 = await self.decide_use_http1()
        if not use_http1:
            try:
                session = _Http2Run(self, request_id, allowed_tools)
                await session.start()
                return session
            except Exception as exc:
                if not should_fallback_to_http1(exc):
                    raise
                self._http2_failed = True
        session = _Http1Run(self, request_id, allowed_tools)
        await session.start()
        return session

    async def run_sse(
        self,
        request_id: str,
        *,
        allowed_tools: list[str] | None = None,
        ready: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        token = await self._bearer()
        url = f"{self.base_url}/agent.v1.AgentService/RunSSE"
        req = encode_message("aiserver.v1.BidiRequestId", {"request_id": request_id})
        body = _connect_frame(req, flags=0)
        headers = self._headers(
            token=token,
            content_type="application/connect+proto",
            streaming=True,
            allowed_tools=allowed_tools,
        )
        async with self._http.stream("POST", url, headers=headers, content=body) as resp:
            if resp.status_code == 401:
                await resp.aread()
                await self.exchange_access_token()
                raise errors.AuthenticationError("RunSSE unauthorized after token exchange", status=401, endpoint=url)
            if resp.status_code >= 400:
                raw = await resp.aread()
                self._raise_connect_error(httpx.Response(resp.status_code, content=raw, headers=resp.headers, request=resp.request), url)
            if ready is not None:
                ready.set()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                while True:
                    frame, buf = _pop_connect_frame(buf)
                    if frame is None:
                        break
                    flags, payload = frame
                    if flags & 0x02:
                        if payload:
                            _raise_end_stream_error(payload, url)
                        return
                    if not payload:
                        continue
                    yield decode_message("agent.v1.AgentServerMessage", payload)

    def _http2_client(self) -> httpx.AsyncClient:
        if self._http2 is None:
            self._http2 = httpx.AsyncClient(http2=True, timeout=_stream_timeout(), follow_redirects=True)
        return self._http2

    def _raise_connect_error(self, resp: httpx.Response, url: str) -> None:
        rid = resp.headers.get("x-request-id") or resp.headers.get("X-Request-Id")
        errors.raise_for_connect_http(resp.status_code, resp.text, endpoint=url, request_id=rid)


class _Http1Run:
    transport = "http1-sse"

    def __init__(self, client: ConnectClient, request_id: str, allowed_tools: list[str] | None) -> None:
        self._client = client
        self._request_id = request_id
        self._allowed_tools = allowed_tools
        self._seq = 0
        self._queue: asyncio.Queue[dict[str, Any] | BaseException | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._start_error: BaseException | None = None

    async def start(self) -> None:
        ready = asyncio.Event()
        self._task = asyncio.create_task(self._pump(ready))
        await asyncio.wait_for(ready.wait(), timeout=30)
        if self._start_error is not None:
            await self.aclose()
            raise self._start_error

    async def send(self, payload: bytes) -> None:
        seq = self._seq
        self._seq += 1
        await self._client.bidi_append(self._request_id, seq, payload)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self

    async def __anext__(self) -> dict[str, Any]:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _pump(self, ready: asyncio.Event) -> None:
        try:
            async for msg in self._client.run_sse(
                self._request_id, allowed_tools=self._allowed_tools, ready=ready
            ):
                await self._queue.put(msg)
            await self._queue.put(None)
        except asyncio.CancelledError:
            await self._queue.put(None)
            raise
        except Exception as exc:
            self._start_error = exc
            if not ready.is_set():
                ready.set()
            await self._queue.put(exc)


class _Http2Run:
    transport = "http2-bidi"

    def __init__(self, client: ConnectClient, request_id: str, allowed_tools: list[str] | None) -> None:
        self._client = client
        self._request_id = request_id
        self._allowed_tools = allowed_tools
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._queue: asyncio.Queue[dict[str, Any] | BaseException | None] = asyncio.Queue()
        self._started = asyncio.Event()
        self._start_error: BaseException | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._url = f"{client.base_url}/agent.v1.AgentService/Run"

    async def start(self) -> None:
        if not http2_available():
            raise Http2Unavailable("the h2 package is not installed")
        self._task = asyncio.create_task(self._loop())
        await asyncio.wait_for(self._started.wait(), timeout=30)
        if self._start_error is not None:
            await self.aclose()
            raise self._start_error

    async def send(self, payload: bytes) -> None:
        await self._out.put(_connect_frame(payload, flags=0))

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self

    async def __anext__(self) -> dict[str, Any]:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._out.put(None)
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    async def _loop(self) -> None:
        url = self._url
        try:
            token = await self._client._bearer()
            headers = self._client._headers(
                token=token,
                content_type="application/connect+proto",
                streaming=False,
                allowed_tools=self._allowed_tools,
                extra={"x-request-id": self._request_id},
            )
            http = self._client._http2_client()

            async def _body() -> AsyncIterator[bytes]:
                # Empty DATA unblocks clients that buffer until the first body
                # chunk without sending a Connect envelope the server would parse.
                yield b""
                while True:
                    chunk = await self._out.get()
                    if chunk is None:
                        return
                    yield chunk

            async with http.stream("POST", url, headers=headers, content=_body()) as resp:
                version = (resp.http_version or "").upper().replace("HTTP/", "")
                if not version.startswith("2"):
                    raise Http2Unavailable(f"server negotiated {resp.http_version or 'HTTP/1.1'}")
                if resp.status_code == 401:
                    await resp.aread()
                    await self._client.exchange_access_token()
                    raise errors.AuthenticationError("Run unauthorized after token exchange", status=401, endpoint=url)
                if resp.status_code >= 400:
                    raw = await resp.aread()
                    self._client._raise_connect_error(
                        httpx.Response(resp.status_code, content=raw, headers=resp.headers, request=resp.request),
                        url,
                    )
                self._started.set()
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    while True:
                        frame, buf = _pop_connect_frame(buf)
                        if frame is None:
                            break
                        flags, payload = frame
                        if flags & 0x02:
                            if payload:
                                _raise_end_stream_error(payload, url)
                            await self._queue.put(None)
                            return
                        if payload:
                            await self._queue.put(decode_message("agent.v1.AgentServerMessage", payload))
                await self._queue.put(None)
        except asyncio.CancelledError:
            await self._queue.put(None)
            raise
        except Exception as exc:
            self._start_error = exc
            self._started.set()
            await self._queue.put(exc)


def _connect_frame(payload: bytes, flags: int = 0) -> bytes:
    return struct.pack(">BI", flags, len(payload)) + payload


def _pop_connect_frame(buf: bytearray) -> tuple[tuple[int, bytes] | None, bytearray]:
    if len(buf) < 5:
        return None, buf
    flags, length = struct.unpack(">BI", bytes(buf[:5]))
    if len(buf) < 5 + length:
        return None, buf
    payload = bytes(buf[5 : 5 + length])
    del buf[: 5 + length]
    return (flags, payload), buf


def _raise_end_stream_error(payload: bytes, url: str) -> None:
    try:
        data = json.loads(payload.decode("utf-8") or "{}")
    except Exception:
        return
    if not data:
        return
    err = data.get("error") if isinstance(data, dict) else None
    blob = err if isinstance(err, dict) else data
    if not isinstance(blob, dict):
        return
    code = str(blob.get("code") or "")
    message = str(blob.get("message") or "")
    if code or message:
        raise errors.convert_connect_error(blob, endpoint=url)
