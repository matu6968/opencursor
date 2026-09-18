from __future__ import annotations

from opencursor._local_executor import _interaction_response_body, resolve_allowed_tools
from opencursor._protobuf import decode_message, encode_message, oneof_case
from opencursor.types import AgentOptions


def test_web_search_maps_to_proto_tool_name() -> None:
    opts = AgentOptions.model_validate({"local": {"cwd": "."}, "tools": ["webSearch"]})
    assert resolve_allowed_tools(opts) == ["web_search_tool_call"]


def test_web_search_interaction_response_encodes_approved() -> None:
    query = {
        "id": 4,
        "web_search_request_query": {"args": {"search_term": "python inventor", "tool_call_id": "t1"}},
        "_oneof_query": "web_search_request_query",
    }
    body = _interaction_response_body(query)
    raw = encode_message("agent.v1.AgentClientMessage", {"interaction_response": body})
    decoded = decode_message("agent.v1.AgentClientMessage", raw)
    assert oneof_case(decoded, "message") == "interaction_response"
    resp = decoded["interaction_response"]
    assert resp["id"] == 4
    assert oneof_case(resp, "result") == "web_search_request_response"
    assert oneof_case(resp["web_search_request_response"], "result") == "approved"


def test_web_search_result_roundtrip() -> None:
    msg = {
        "web_search_tool_call": {
            "args": {"search_term": "python inventor", "tool_call_id": "t1"},
            "result": {
                "success": {
                    "references": [
                        {"title": "Python", "url": "https://python.org", "chunk": "Guido van Rossum"}
                    ]
                }
            },
        }
    }
    raw = encode_message("agent.v1.ToolCall", msg)
    decoded = decode_message("agent.v1.ToolCall", raw)
    assert oneof_case(decoded, "tool") == "web_search_tool_call"
    refs = decoded["web_search_tool_call"]["result"]["success"]["references"]
    assert refs[0]["url"] == "https://python.org"
