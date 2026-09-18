from __future__ import annotations

from opencursor._protobuf import decode_message, encode_message, oneof_case


def test_agent_client_message_roundtrip() -> None:
    msg = {
        "run_request": {
            "conversation_id": "conv-1",
            "run_id": "run-1",
            "model_details": {"model_id": "composer-2.5", "display_model_id": "composer-2.5"},
            "requested_model": {"model_id": "composer-2.5", "max_mode": False, "built_in_model": True},
            "action": {
                "user_message_action": {
                    "user_message": {
                        "text": "hello",
                        "message_id": "m1",
                        "mode": "AGENT_MODE_AGENT",
                    }
                }
            },
        }
    }
    raw = encode_message("agent.v1.AgentClientMessage", msg)
    decoded = decode_message("agent.v1.AgentClientMessage", raw)
    assert oneof_case(decoded, "message") == "run_request"
    assert decoded["run_request"]["conversation_id"] == "conv-1"
    assert decoded["run_request"]["action"]["user_message_action"]["user_message"]["text"] == "hello"
    assert decoded["run_request"]["requested_model"]["model_id"] == "composer-2.5"


def test_bidi_append_request_roundtrip() -> None:
    msg = {
        "request_id": {"request_id": "req-1"},
        "append_seqno": 1,
        "data_binary": b"\x01\x02",
    }
    raw = encode_message("aiserver.v1.BidiAppendRequest", msg)
    decoded = decode_message("aiserver.v1.BidiAppendRequest", raw)
    assert decoded["request_id"]["request_id"] == "req-1"
    assert decoded["append_seqno"] == 1
    assert decoded["data_binary"] == b"\x01\x02"


def test_exec_server_message_read_args() -> None:
    msg = {"id": 7, "exec_id": "e1", "read_args": {"path": "README.md", "tool_call_id": "t1"}}
    raw = encode_message("agent.v1.ExecServerMessage", msg)
    decoded = decode_message("agent.v1.ExecServerMessage", raw)
    assert oneof_case(decoded, "message") == "read_args"
    assert decoded["read_args"]["path"] == "README.md"
    assert decoded["id"] == 7


def test_conversation_state_mode_is_present() -> None:
    raw = encode_message(
        "agent.v1.AgentRunRequest",
        {"conversation_state": {"mode": "AGENT_MODE_AGENT"}, "conversation_id": "c1"},
    )
    decoded = decode_message("agent.v1.AgentRunRequest", raw)
    assert decoded["conversation_state"]["mode"] == 1
    empty = encode_message("agent.v1.ConversationStateStructure", {})
    assert empty == b""


def test_inject_context_action_roundtrip() -> None:
    msg = {
        "conversation_action": {
            "inject_context_action": {
                "injection_id": "inj-1",
                "expected_run_id": "gen-1",
                "user_context": {
                    "user_message": {
                        "text": "steer me",
                        "message_id": "m-steer",
                    }
                },
            }
        }
    }
    raw = encode_message("agent.v1.AgentClientMessage", msg)
    decoded = decode_message("agent.v1.AgentClientMessage", raw)
    assert oneof_case(decoded, "message") == "conversation_action"
    action = decoded["conversation_action"]["inject_context_action"]
    assert action["injection_id"] == "inj-1"
    assert action["expected_run_id"] == "gen-1"
    assert action["user_context"]["user_message"]["text"] == "steer me"


def test_context_injection_state_roundtrip() -> None:
    msg = {
        "interaction_update": {
            "context_injection_state": {
                "injection_id": "inj-1",
                "state": {"delivered": {}},
            }
        }
    }
    raw = encode_message("agent.v1.AgentServerMessage", msg)
    decoded = decode_message("agent.v1.AgentServerMessage", raw)
    assert oneof_case(decoded, "message") == "interaction_update"
    update = decoded["interaction_update"]
    assert oneof_case(update, "message") == "context_injection_state"
    state = update["context_injection_state"]
    assert state["injection_id"] == "inj-1"
    assert oneof_case(state["state"], "state") == "delivered"
