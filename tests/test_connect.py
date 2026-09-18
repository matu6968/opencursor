from __future__ import annotations

import asyncio
import struct

from opencursor._connect import (
    HTTP2_CONFIG_FORCE_ALL_DISABLED,
    HTTP2_CONFIG_FORCE_ALL_ENABLED,
    HTTP2_CONFIG_FORCE_BIDI_DISABLED,
    HTTP2_CONFIG_FORCE_BIDI_ENABLED,
    HTTP2_CONFIG_UNSPECIFIED,
    ConnectClient,
    Http2Unavailable,
    _Http2Run,
    _connect_frame,
    env_use_http1,
    local_prefer_http1,
    parse_http2_config,
    reset_http2_config_cache,
    select_use_http1,
    should_fallback_to_http1,
)
from opencursor import errors


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def test_select_use_http1_server_config_wins() -> None:
    assert select_use_http1(http2_config=HTTP2_CONFIG_FORCE_BIDI_DISABLED, local_prefer=False) is True
    assert select_use_http1(http2_config=HTTP2_CONFIG_FORCE_ALL_DISABLED, local_prefer=False) is True
    assert select_use_http1(http2_config=HTTP2_CONFIG_FORCE_BIDI_ENABLED, local_prefer=True) is False
    assert select_use_http1(http2_config=HTTP2_CONFIG_FORCE_ALL_ENABLED, local_prefer=True) is False
    assert select_use_http1(http2_config=HTTP2_CONFIG_UNSPECIFIED, local_prefer=True) is True
    assert select_use_http1(http2_config=None, local_prefer=False) is False


def test_local_prefer_http1_url_and_option_and_env(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_USE_HTTP1", raising=False)
    assert local_prefer_http1("http://localhost:8080", False) is True
    assert local_prefer_http1("https://api2.cursor.sh", True) is True
    assert local_prefer_http1("https://api2.cursor.sh", False) is False
    monkeypatch.setenv("CURSOR_USE_HTTP1", "true")
    assert local_prefer_http1("https://api2.cursor.sh", None) is True
    assert env_use_http1() is True


def test_parse_http2_config_field_7() -> None:
    tag = _varint((7 << 3) | 0)
    buf = tag + _varint(HTTP2_CONFIG_FORCE_BIDI_ENABLED)
    assert parse_http2_config(buf) == HTTP2_CONFIG_FORCE_BIDI_ENABLED
    other = _varint((1 << 3) | 0) + _varint(1)
    assert parse_http2_config(other) is None
    nested = (
        _varint((1 << 3) | 2)
        + _varint(3)
        + b"abc"
        + _varint((7 << 3) | 0)
        + _varint(HTTP2_CONFIG_FORCE_BIDI_DISABLED)
    )
    assert parse_http2_config(nested) == HTTP2_CONFIG_FORCE_BIDI_DISABLED


def test_connect_frame_roundtrip() -> None:
    payload = b"hello"
    framed = _connect_frame(payload, flags=0)
    flags, length = struct.unpack(">BI", framed[:5])
    assert flags == 0
    assert length == 5
    assert framed[5:] == payload


def test_should_fallback_rules() -> None:
    assert should_fallback_to_http1(Http2Unavailable("no h2")) is True
    assert should_fallback_to_http1(errors.AuthenticationError("nope", status=401)) is False
    assert should_fallback_to_http1(errors.CursorAgentError("bad", status=400)) is False
    assert should_fallback_to_http1(errors.CursorAgentError("missing", status=404)) is True
    assert should_fallback_to_http1(errors.CursorAgentError("boom", status=503)) is True


def test_decide_http1_when_h2_missing(monkeypatch) -> None:
    reset_http2_config_cache()
    monkeypatch.setattr("opencursor._connect.http2_available", lambda: False)
    client = ConnectClient(api_key="k", use_http1=False)
    assert asyncio.run(client.decide_use_http1()) is True
    asyncio.run(client.aclose())


def test_open_run_falls_back_to_sse(monkeypatch) -> None:
    reset_http2_config_cache()
    client = ConnectClient(api_key="k")

    async def force_http2(_self=None) -> bool:
        return False

    async def fail_start(self) -> None:
        raise Http2Unavailable("negotiated HTTP/1.1")

    async def fake_sse(self, request_id, *, allowed_tools=None, ready=None):
        if ready is not None:
            ready.set()
        if False:
            yield {}

    monkeypatch.setattr(ConnectClient, "decide_use_http1", force_http2)
    monkeypatch.setattr(_Http2Run, "start", fail_start)
    monkeypatch.setattr(ConnectClient, "run_sse", fake_sse)

    async def _run() -> str:
        session = await client.open_run("req-1")
        name = session.transport
        await session.aclose()
        await client.aclose()
        return name

    assert asyncio.run(_run()) == "http1-sse"
    assert client._http2_failed is True


def test_open_run_http1_option_skips_http2(monkeypatch) -> None:
    reset_http2_config_cache()
    client = ConnectClient(api_key="k", use_http1=True)
    started = {"http2": False}

    async def fail_start(self) -> None:
        started["http2"] = True
        raise Http2Unavailable("should not run")

    async def fake_sse(self, request_id, *, allowed_tools=None, ready=None):
        if ready is not None:
            ready.set()
        if False:
            yield {}

    monkeypatch.setattr(_Http2Run, "start", fail_start)
    monkeypatch.setattr(ConnectClient, "run_sse", fake_sse)

    async def _run() -> str:
        session = await client.open_run("req-1")
        name = session.transport
        await session.aclose()
        await client.aclose()
        return name

    assert asyncio.run(_run()) == "http1-sse"
    assert started["http2"] is False
    assert client._http2_failed is False


def test_open_run_does_not_fallback_on_auth(monkeypatch) -> None:
    reset_http2_config_cache()
    client = ConnectClient(api_key="k")

    async def force_http2(_self=None) -> bool:
        return False

    async def fail_start(self) -> None:
        raise errors.AuthenticationError("Run unauthorized", status=401)

    monkeypatch.setattr(ConnectClient, "decide_use_http1", force_http2)
    monkeypatch.setattr(_Http2Run, "start", fail_start)

    async def _run() -> None:
        try:
            await client.open_run("req-1")
        finally:
            await client.aclose()

    try:
        asyncio.run(_run())
        raise AssertionError("expected AuthenticationError")
    except errors.AuthenticationError:
        pass
    assert client._http2_failed is False
