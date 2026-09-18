from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from opencursor._local_executor import (
    _exec_fetch,
    _fetch_url_error,
    _interaction_response_body,
    resolve_allowed_tools,
)
from opencursor._protobuf import decode_message, encode_message, oneof_case
from opencursor.types import AgentOptions


def test_web_fetch_maps_to_proto_tool_name() -> None:
    opts = AgentOptions.model_validate({"local": {"cwd": "."}, "tools": ["webFetch"]})
    assert resolve_allowed_tools(opts) == ["web_fetch_tool_call"]


def test_fetch_url_error_rejects_non_http() -> None:
    assert _fetch_url_error("") == "missing url"
    assert _fetch_url_error("file:///etc/passwd") is not None
    assert _fetch_url_error("https://example.com/page") is None


def test_fetch_result_roundtrip() -> None:
    msg = {
        "id": 3,
        "exec_id": "e-fetch",
        "fetch_result": {
            "success": {
                "url": "https://example.com/",
                "content": "<html>ok</html>",
                "status_code": 200,
                "content_type": "text/html",
            }
        },
    }
    raw = encode_message("agent.v1.ExecClientMessage", msg)
    decoded = decode_message("agent.v1.ExecClientMessage", raw)
    assert oneof_case(decoded, "message") == "fetch_result"
    assert decoded["fetch_result"]["success"]["status_code"] == 200
    assert decoded["fetch_result"]["success"]["content"] == "<html>ok</html>"


def test_web_fetch_interaction_response_encodes_approved() -> None:
    query = {
        "id": 9,
        "web_fetch_request_query": {"args": {"url": "https://example.com"}, "skip_approval": False},
        "_oneof_query": "web_fetch_request_query",
    }
    body = _interaction_response_body(query)
    raw = encode_message("agent.v1.AgentClientMessage", {"interaction_response": body})
    decoded = decode_message("agent.v1.AgentClientMessage", raw)
    assert oneof_case(decoded, "message") == "interaction_response"
    resp = decoded["interaction_response"]
    assert resp["id"] == 9
    assert oneof_case(resp, "result") == "web_fetch_request_response"
    assert oneof_case(resp["web_fetch_request_response"], "result") == "approved"


def test_exec_fetch_local_http() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<html><title>Hello Fetch</title></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = asyncio.run(_exec_fetch({"url": f"http://127.0.0.1:{port}/"}))
        assert "success" in result
        assert result["success"]["status_code"] == 200
        assert "Hello Fetch" in result["success"]["content"]
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_exec_fetch_rejects_file_url() -> None:
    result = asyncio.run(_exec_fetch({"url": "file:///etc/passwd"}))
    assert "error" in result
    assert "scheme" in result["error"]["error"]
