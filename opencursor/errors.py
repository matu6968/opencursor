from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass
class SdkErrorContext:
    operation: str | None = None
    endpoint: str | None = None
    status: int | None = None
    code: str | None = None
    request_id: str | None = None
    is_retryable: bool | None = None


class CursorSdkError(Exception):
    """Base for Open Cursor / SDK-shaped errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        is_retryable: bool = False,
        cause: BaseException | None = None,
        endpoint: str | None = None,
        request_id: str | None = None,
        operation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.is_retryable = is_retryable
        self.cause = cause
        self.endpoint = endpoint
        self.request_id = request_id
        self.operation = operation

    def __str__(self) -> str:
        if self.code:
            return f"{self.code}: {self.message}"
        return self.message

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}({self.message!r}, "
            f"code={self.code!r}, status={self.status!r}, "
            f"request_id={self.request_id!r})"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "name": type(self).__name__,
            "message": str(self),
            "code": self.code,
            "status": self.status,
            "isRetryable": self.is_retryable,
            "endpoint": self.endpoint,
            "requestId": self.request_id,
            "operation": self.operation,
        }


class CursorAgentError(CursorSdkError):
    """Backward-compatible alias for agent errors."""

    pass


class AuthenticationError(CursorAgentError):
    pass


class RateLimitError(CursorAgentError):
    pass


class ConfigurationError(CursorAgentError):
    pass


class AgentBusyError(CursorAgentError):
    pass


class IntegrationNotConnectedError(ConfigurationError):
    def __init__(
        self,
        message: str,
        *,
        help_url: str,
        provider: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.help_url = help_url
        self.provider = provider

    def to_json(self) -> dict[str, Any]:
        d = super().to_json()
        d["helpUrl"] = self.help_url
        d["provider"] = self.provider
        return d


class NetworkError(CursorAgentError):
    pass


class APITimeoutError(NetworkError):
    pass


class UnknownAgentError(CursorAgentError):
    pass


class AgentNotFoundError(CursorAgentError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("code", "agent_not_found")
        super().__init__(message, **kwargs)


class UnsupportedRunOperationError(ConfigurationError):
    def __init__(self, operation: str, reason: str | None = None) -> None:
        super().__init__(
            reason or f'Run operation "{operation}" is not supported',
            code="unsupported_run_operation",
            is_retryable=False,
        )
        self.operation = operation


CursorSDKError = CursorSdkError
PermissionDeniedError = AuthenticationError
BadRequestError = ConfigurationError
NotFoundError = AgentNotFoundError


class InternalServerError(CursorSdkError):
    pass


# aiserver.v1.ErrorDetails.Error names as used by @cursor/sdk convertConnectError
# (protobuf-es strips the ERROR_ prefix).
_AUTH_DETAILS = {
    "NOT_LOGGED_IN",
    "INVALID_AUTH_ID",
    "NOT_HIGH_ENOUGH_PERMISSIONS",
    "AGENT_REQUIRES_LOGIN",
    "AUTH_TOKEN_NOT_FOUND",
    "AUTH_TOKEN_EXPIRED",
    "UNAUTHORIZED",
}
_RATE_DETAILS = {
    "FREE_USER_RATE_LIMIT_EXCEEDED",
    "PRO_USER_RATE_LIMIT_EXCEEDED",
    "FREE_USER_USAGE_LIMIT",
    "PRO_USER_USAGE_LIMIT",
    "RESOURCE_EXHAUSTED",
    "OPENAI_RATE_LIMIT_EXCEEDED",
    "GENERIC_RATE_LIMIT_EXCEEDED",
    "GPT_4_VISION_PREVIEW_RATE_LIMIT",
    "API_KEY_RATE_LIMIT",
    "RATE_LIMITED",
    "RATE_LIMITED_CHANGEABLE",
}
_CONFIG_DETAILS = {
    "BAD_API_KEY",
    "BAD_USER_API_KEY",
    "BAD_MODEL_NAME",
    "MODEL_BLOCKED",
    "NOT_FOUND",
    "DEPRECATED",
    "USER_NOT_FOUND",
    "BAD_REQUEST",
    "FILE_NOT_FOUND",
}
_NETWORK_DETAILS = {
    "TIMEOUT",
}

_CONNECT_CODE_CLASS: dict[str, type[CursorAgentError]] = {
    "unauthenticated": AuthenticationError,
    "resource_exhausted": RateLimitError,
    "invalid_argument": ConfigurationError,
    "not_found": ConfigurationError,
    "unavailable": NetworkError,
    "deadline_exceeded": NetworkError,
    "internal": NetworkError,
}

_HTTP_TO_CONNECT = {
    400: "invalid_argument",
    401: "unauthenticated",
    403: "permission_denied",
    404: "not_found",
    409: "already_exists",
    429: "resource_exhausted",
    499: "canceled",
    500: "internal",
    501: "unimplemented",
    503: "unavailable",
    504: "deadline_exceeded",
}

_GENERIC_CONNECT_CODES = frozenset({"error", "unknown", "unknown_error"})


def wrap_sdk_error(err: object, context: SdkErrorContext | None = None) -> CursorSdkError:
    ctx = context or SdkErrorContext()
    converted = convert_error(err)
    if ctx.code and converted.code is None:
        converted.code = ctx.code
    if ctx.status is not None and converted.status is None:
        converted.status = ctx.status
    if ctx.endpoint and converted.endpoint is None:
        converted.endpoint = ctx.endpoint
    if ctx.request_id and converted.request_id is None:
        converted.request_id = ctx.request_id
    if ctx.operation and converted.operation is None:
        converted.operation = ctx.operation
    if ctx.is_retryable is not None:
        converted.is_retryable = ctx.is_retryable
    return converted


def to_run_error(err: object) -> dict[str, str]:
    sdk = convert_error(err)
    message = getattr(sdk, "message", None) or str(sdk)
    if sdk.code:
        prefix = f"[{sdk.code}] "
        if message.startswith(prefix):
            message = message[len(prefix) :]
    out: dict[str, str] = {"message": message or type(sdk).__name__}
    if sdk.code:
        out["code"] = sdk.code
    return out


def convert_error(err: object) -> CursorAgentError:
    if isinstance(err, CursorAgentError):
        return err
    if isinstance(err, CursorSdkError):
        return CursorAgentError(
            str(err),
            code=err.code,
            status=err.status,
            is_retryable=err.is_retryable,
            cause=err,
            endpoint=err.endpoint,
            request_id=err.request_id,
            operation=err.operation,
        )
    payload = _as_connect_payload(err)
    if payload is not None:
        return convert_connect_error(payload)
    if isinstance(err, BaseException):
        return UnknownAgentError(str(err) or type(err).__name__, cause=err)
    return UnknownAgentError(str(err))


def convert_connect_error(
    error: Mapping[str, Any] | str | bytes,
    *,
    status: int | None = None,
    endpoint: str | None = None,
    request_id: str | None = None,
    cause: BaseException | None = None,
) -> CursorAgentError:
    blob = _as_connect_payload(error) or {}
    details = _extract_error_details(blob)
    connect_code = _normalize_connect_code(blob.get("code"))
    http_code = _HTTP_TO_CONNECT.get(status) if status is not None else None
    if not connect_code or (http_code and connect_code in _GENERIC_CONNECT_CODES):
        connect_code = http_code or connect_code
    message = str(blob.get("message") or "") or (f"HTTP {status}" if status else "Request failed")
    is_retryable = False
    details_code: str | None = None
    if details is not None:
        custom = details.get("details") if isinstance(details.get("details"), Mapping) else None
        if isinstance(custom, Mapping):
            title = str(custom.get("title") or "")
            detail = str(custom.get("detail") or "")
            if title and detail:
                message = f"{title} {detail}"
            elif title or detail:
                message = title or detail
            is_retryable = bool(custom.get("is_retryable") or custom.get("isRetryable"))
        details_code = _error_details_code_name(details.get("error"))
    kwargs: dict[str, Any] = {
        "code": details_code or connect_code,
        "status": status,
        "is_retryable": is_retryable,
        "endpoint": endpoint,
        "request_id": request_id,
        "cause": cause,
    }
    if details_code:
        cls = _class_for_error_details(details_code)
        return cls(message, **kwargs)
    cls = _CONNECT_CODE_CLASS.get(connect_code or "", UnknownAgentError)
    if cls is NetworkError and not is_retryable:
        kwargs["is_retryable"] = connect_code in {"unavailable", "deadline_exceeded", "internal"} or (
            status is not None and status >= 500
        )
    if cls is RateLimitError:
        kwargs["is_retryable"] = True
    if cls is AuthenticationError or cls is ConfigurationError:
        kwargs["is_retryable"] = is_retryable
    return cls(message, **kwargs)


def raise_for_connect_http(
    status: int,
    body_text: str,
    *,
    endpoint: str,
    request_id: str | None = None,
) -> None:
    raise convert_connect_error(body_text, status=status, endpoint=endpoint, request_id=request_id)


def error_from_sse_error_event(code: str, message: str) -> CursorAgentError:
    detail = f"[{code}] {message}"
    if code == "unauthorized":
        return AuthenticationError(detail, code=code, is_retryable=False)
    if code in {"forbidden", "not_found"}:
        return ConfigurationError(detail, code=code, is_retryable=False)
    return NetworkError(detail, code=code, is_retryable=False)


def _parse_api_error_body(text: str) -> tuple[str, str, str | None, str | None]:
    code, message, help_url, provider = "unknown", text or "Request failed", None, None
    try:
        raw = json.loads(text)
        err = raw.get("error") if isinstance(raw, Mapping) else None
        blob: Mapping[str, Any] = err if isinstance(err, Mapping) else raw if isinstance(raw, Mapping) else {}
        if isinstance(blob, Mapping):
            code = str(blob.get("code") or code)
            message = str(blob.get("message") or message)
            hu = blob.get("helpUrl")
            pr = blob.get("provider")
            help_url = str(hu) if isinstance(hu, str) else None
            provider = str(pr) if isinstance(pr, str) else None
    except Exception:
        pass
    return code, message, help_url, provider


def raise_for_cloud_api_status(
    status: int,
    body_text: str,
    *,
    endpoint: str,
    request_id: str | None,
) -> None:
    code, message, help_url, provider = _parse_api_error_body(body_text)
    detail = f"[{code}] {message}"
    base_kw: dict[str, Any] = {
        "status": status,
        "code": code,
        "endpoint": endpoint,
        "request_id": request_id,
    }
    if code == "integration_not_connected" and help_url and provider:
        raise IntegrationNotConnectedError(detail, help_url=help_url, provider=provider, **base_kw, is_retryable=False)
    if code == "agent_busy":
        raise AgentBusyError(detail, **base_kw, is_retryable=False)
    if code == "agent_not_found":
        raise AgentNotFoundError(detail, **base_kw, is_retryable=False)
    if status == 401:
        raise AuthenticationError(message, **base_kw, is_retryable=False)
    if status == 429:
        raise RateLimitError(message, **base_kw, is_retryable=True)
    if status in (400, 404, 409):
        raise ConfigurationError(detail, **base_kw, is_retryable=False)
    if status >= 500:
        raise NetworkError(detail, **base_kw, is_retryable=True)
    raise UnknownAgentError(detail, **base_kw, is_retryable=False)


def _as_connect_payload(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        err = value.get("error") if isinstance(value.get("error"), Mapping) else None
        blob = err if isinstance(err, Mapping) else value
        return dict(blob) if isinstance(blob, Mapping) else None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            raw = json.loads(text)
        except Exception:
            return {"message": text}
        if isinstance(raw, Mapping):
            return _as_connect_payload(raw)
    return None


def _extract_error_details(blob: Mapping[str, Any]) -> dict[str, Any] | None:
    details = blob.get("details")
    if not isinstance(details, list):
        return None
    for item in details:
        parsed = _detail_as_error_details(item)
        if parsed is not None:
            return parsed
    return None


def _detail_as_error_details(item: object) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    type_name = str(item.get("type") or item.get("typeUrl") or item.get("@type") or "")
    debug = item.get("debug")
    if isinstance(debug, Mapping) and ("error" in debug or "details" in debug):
        if "ErrorDetails" in type_name or not type_name or type_name.endswith("ErrorDetails"):
            return dict(debug)
    if "ErrorDetails" not in type_name:
        return None
    value = item.get("value")
    if isinstance(value, Mapping) and ("error" in value or "details" in value):
        return dict(value)
    raw = _b64_bytes(value)
    if raw is None:
        return None
    try:
        from opencursor._protobuf import decode_message

        decoded = decode_message("aiserver.v1.ErrorDetails", raw)
        return decoded if isinstance(decoded, dict) else None
    except Exception:
        return None


def _b64_bytes(value: object) -> bytes | None:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str) or not value:
        return None
    pad = "=" * ((4 - len(value) % 4) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(value + pad)
        except Exception:
            continue
    return None


def _error_details_code_name(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        name = _enum_name("aiserver.v1.ErrorDetails.Error", value)
        return _strip_error_prefix(name) if name else str(value)
    text = str(value)
    if text.isdigit():
        return _error_details_code_name(int(text))
    return _strip_error_prefix(text)


def _enum_name(type_name: str, number: int) -> str | None:
    try:
        from opencursor._protobuf import load_descriptors

        desc = load_descriptors()["enums"].get(type_name)
    except Exception:
        return None
    if not desc:
        return None
    for item in desc.get("values") or []:
        if int(item.get("no") or -1) == number:
            return str(item.get("name") or "")
    return None


def _strip_error_prefix(name: str) -> str:
    if name.startswith("ERROR_"):
        return name[len("ERROR_") :]
    return name


def _class_for_error_details(code: str) -> type[CursorAgentError]:
    if code in _AUTH_DETAILS:
        return AuthenticationError
    if code in _RATE_DETAILS:
        return RateLimitError
    if code in _CONFIG_DETAILS:
        return ConfigurationError
    if code in _NETWORK_DETAILS:
        return NetworkError
    return UnknownAgentError


def _normalize_connect_code(code: object) -> str | None:
    if code is None or code == "":
        return None
    text = str(code).strip()
    if not text:
        return None
    if text.startswith("Code."):
        text = text[len("Code.") :]
    snake = []
    for i, ch in enumerate(text):
        if ch.isupper() and i and (text[i - 1].islower() or (i + 1 < len(text) and text[i + 1].islower())):
            snake.append("_")
        snake.append(ch)
    return "".join(snake).replace("-", "_").lower()
