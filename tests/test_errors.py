from __future__ import annotations

import base64

from opencursor import errors
from opencursor._connect import should_fallback_to_http1
from opencursor._protobuf import encode_message


def _details_b64(*, error: str, title: str = "", detail: str = "", retryable: bool = False) -> str:
    custom: dict = {}
    if title:
        custom["title"] = title
    if detail:
        custom["detail"] = detail
    if retryable:
        custom["is_retryable"] = True
    msg = {"error": error}
    if custom:
        msg["details"] = custom
    raw = encode_message("aiserver.v1.ErrorDetails", msg)
    return base64.b64encode(raw).decode("ascii")


def test_error_details_auth_rate_config_timeout() -> None:
    auth = errors.convert_connect_error(
        {
            "code": "unknown",
            "message": "ignored",
            "details": [
                {
                    "type": "aiserver.v1.ErrorDetails",
                    "value": _details_b64(
                        error="ERROR_NOT_LOGGED_IN",
                        title="Sign in",
                        detail="API key is invalid",
                    ),
                }
            ],
        }
    )
    assert isinstance(auth, errors.AuthenticationError)
    assert auth.code == "NOT_LOGGED_IN"
    assert auth.message == "Sign in API key is invalid"
    assert str(auth) == "NOT_LOGGED_IN: Sign in API key is invalid"

    rate = errors.convert_connect_error(
        {
            "details": [
                {
                    "type": "aiserver.v1.ErrorDetails",
                    "debug": {"error": "RATE_LIMITED", "details": {"detail": "slow down", "isRetryable": True}},
                }
            ]
        }
    )
    assert isinstance(rate, errors.RateLimitError)
    assert rate.code == "RATE_LIMITED"
    assert rate.is_retryable is True

    cfg = errors.convert_connect_error(
        {
            "details": [
                {
                    "type": "aiserver.v1.ErrorDetails",
                    "debug": {"error": "BAD_MODEL_NAME", "details": {"title": "Unknown model"}},
                }
            ]
        }
    )
    assert isinstance(cfg, errors.ConfigurationError)
    assert cfg.code == "BAD_MODEL_NAME"

    timeout = errors.convert_connect_error(
        {"details": [{"type": "aiserver.v1.ErrorDetails", "debug": {"error": "TIMEOUT"}}]}
    )
    assert isinstance(timeout, errors.NetworkError)
    assert timeout.code == "TIMEOUT"


def test_connect_code_fallback_and_http_status() -> None:
    unauth = errors.convert_connect_error({"code": "unauthenticated", "message": "nope"}, status=401)
    assert isinstance(unauth, errors.AuthenticationError)
    assert unauth.status == 401
    assert unauth.message == "nope"
    assert str(unauth) == "unauthenticated: nope"

    missing = errors.convert_connect_error("plain failure", status=503)
    assert isinstance(missing, errors.NetworkError)
    assert missing.code == "unavailable"
    assert missing.is_retryable is True

    not_found = errors.convert_connect_error({"code": "not_found", "message": "missing"})
    assert isinstance(not_found, errors.ConfigurationError)


def test_to_run_error_strips_code_prefix() -> None:
    err = errors.ConfigurationError("[bad_request] boom", code="bad_request")
    assert errors.to_run_error(err) == {"message": "boom", "code": "bad_request"}


def test_cursor_agent_error_message_matches_official() -> None:
    err = errors.AuthenticationError("Invalid User API Key", code="unauthenticated", is_retryable=False)
    assert err.message == "Invalid User API Key"
    assert str(err) == "unauthenticated: Invalid User API Key"
    assert err.is_retryable is False


def test_connect_http_401_preserves_server_message() -> None:
    try:
        errors.raise_for_connect_http(
            401,
            '{"code":"unauthenticated","message":"Invalid User API Key"}',
            endpoint="https://api2.cursor.sh/auth/exchange_user_api_key",
        )
        raise AssertionError("expected AuthenticationError")
    except errors.AuthenticationError as exc:
        assert exc.message == "Invalid User API Key"
        assert exc.code == "unauthenticated"
        assert str(exc) == "unauthenticated: Invalid User API Key"
        assert exc.is_retryable is False


def test_connect_http_401_generic_error_code_maps_to_unauthenticated() -> None:
    try:
        errors.raise_for_connect_http(
            401,
            '{"code":"error","message":"Invalid User API Key"}',
            endpoint="https://api2.cursor.sh/auth/exchange_user_api_key",
        )
        raise AssertionError("expected AuthenticationError")
    except errors.AuthenticationError as exc:
        assert exc.message == "Invalid User API Key"
        assert exc.code == "unauthenticated"
        assert str(exc) == "unauthenticated: Invalid User API Key"
        assert exc.is_retryable is False


def test_cloud_api_agent_codes() -> None:
    try:
        errors.raise_for_cloud_api_status(
            409,
            '{"code":"agent_busy","message":"active run"}',
            endpoint="/v1/agents",
            request_id="r1",
        )
        raise AssertionError("expected AgentBusyError")
    except errors.AgentBusyError as exc:
        assert exc.code == "agent_busy"
        assert exc.is_retryable is False

    try:
        errors.raise_for_cloud_api_status(
            404,
            '{"code":"agent_not_found","message":"gone"}',
            endpoint="/v1/agents/x",
            request_id=None,
        )
        raise AssertionError("expected AgentNotFoundError")
    except errors.AgentNotFoundError as exc:
        assert exc.code == "agent_not_found"


def test_sse_error_event_mapping() -> None:
    assert isinstance(errors.error_from_sse_error_event("unauthorized", "nope"), errors.AuthenticationError)
    assert isinstance(errors.error_from_sse_error_event("not_found", "missing"), errors.ConfigurationError)
    net = errors.error_from_sse_error_event("upstream", "boom")
    assert isinstance(net, errors.NetworkError)
    assert net.is_retryable is False


def test_should_not_fallback_typed_client_errors() -> None:
    assert should_fallback_to_http1(errors.AgentBusyError("busy", code="agent_busy")) is False
    assert should_fallback_to_http1(errors.AgentNotFoundError("gone")) is False
    assert should_fallback_to_http1(errors.RateLimitError("slow", status=429, is_retryable=True)) is False
    assert should_fallback_to_http1(errors.NetworkError("down", status=503, is_retryable=True)) is True
