from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterator, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opencursor import errors
from opencursor.types import SDKAssistantMessageContent, SDKUserMessageContent


class _SdkMessageBase(BaseModel):
    """Dataclass-style SDK stream message with dict access for store roundtrips."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __repr__(self) -> str:
        names = list(type(self).model_fields)
        body = ", ".join(f"{name}={getattr(self, name)!r}" for name in names)
        return f"{type(self).__name__}({body})"

    def __str__(self) -> str:
        return self.__repr__()

    def __getitem__(self, key: str) -> Any:
        fields = type(self).model_fields
        extra = self.model_extra or {}
        if key in fields or key in extra:
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return key in type(self).model_fields or key in (self.model_extra or {})

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self) -> Iterator[str]:
        yield from self.model_dump().keys()

    def __iter__(self) -> Iterator[str]:  # type: ignore[override]
        return iter(self.model_dump())


class TextBlock(_SdkMessageBase):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(_SdkMessageBase):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: Any = None


class SDKSystemMessage(_SdkMessageBase):
    type: Literal["system"] = "system"
    agent_id: str
    run_id: str
    subtype: Literal["init"] | str | None = None
    model: Any = None
    tools: list[str] | None = None


class SDKAssistantMessage(_SdkMessageBase):
    type: Literal["assistant"] = "assistant"
    agent_id: str
    run_id: str
    message: SDKAssistantMessageContent = Field(default_factory=SDKAssistantMessageContent)

    @field_validator("message", mode="before")
    @classmethod
    def _assistant_message(cls, value: Any) -> Any:
        if isinstance(value, SDKAssistantMessageContent):
            return value
        if isinstance(value, Mapping):
            return SDKAssistantMessageContent.model_validate(dict(value))
        return value


class SDKUserMessageEvent(_SdkMessageBase):
    type: Literal["user"] = "user"
    agent_id: str
    run_id: str
    message: SDKUserMessageContent = Field(default_factory=SDKUserMessageContent)

    @field_validator("message", mode="before")
    @classmethod
    def _user_message(cls, value: Any) -> Any:
        if isinstance(value, SDKUserMessageContent):
            return value
        if isinstance(value, Mapping):
            return SDKUserMessageContent.model_validate(dict(value))
        return value


class SDKToolUseMessage(_SdkMessageBase):
    type: Literal["tool_call"] = "tool_call"
    agent_id: str
    run_id: str
    call_id: str = ""
    name: str = ""
    status: Literal["running", "completed", "error"] | str = "running"
    args: Any = None
    result: Any = None
    truncated: dict[str, bool] | None = None


class SDKThinkingMessage(_SdkMessageBase):
    type: Literal["thinking"] = "thinking"
    agent_id: str
    run_id: str
    text: str = ""
    thinking_duration_ms: int | None = None


class SDKStatusMessage(_SdkMessageBase):
    type: Literal["status"] = "status"
    agent_id: str
    run_id: str
    status: str = ""
    message: str = ""


class SDKRequestMessage(_SdkMessageBase):
    type: Literal["request"] = "request"
    agent_id: str
    run_id: str
    request_id: str = ""


class SDKTaskMessage(_SdkMessageBase):
    type: Literal["task"] = "task"
    agent_id: str
    run_id: str
    status: str = ""
    text: str = ""


class SDKUsageMessage(_SdkMessageBase):
    type: Literal["usage"] = "usage"
    agent_id: str
    run_id: str
    usage: Any = None


SDKMessage = (
    SDKSystemMessage
    | SDKUserMessageEvent
    | SDKAssistantMessage
    | SDKToolUseMessage
    | SDKThinkingMessage
    | SDKStatusMessage
    | SDKRequestMessage
    | SDKTaskMessage
    | SDKUsageMessage
)

LOCAL_RUN_STREAM_SCHEMA_VERSION = 1
LOCAL_RUN_STREAM_EVENT_TYPE = "run_stream_event"


class LocalRunStreamSdkMessageEvent(TypedDict):
    schemaVersion: Literal[1]
    type: Literal["sdk_message"]
    agentId: str
    runId: str
    message: Any


class LocalRunStreamResultEvent(TypedDict, total=False):
    schemaVersion: Literal[1]
    type: Literal["result"]
    agentId: str
    runId: str
    status: Literal["finished", "error", "cancelled"]
    errorCode: str


class LocalRunStreamDoneEvent(TypedDict):
    schemaVersion: Literal[1]
    type: Literal["done"]
    agentId: str
    runId: str


LocalRunStreamEvent = LocalRunStreamSdkMessageEvent | LocalRunStreamResultEvent | LocalRunStreamDoneEvent


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _message_mapping(message: Any) -> dict[str, Any]:
    if isinstance(message, _SdkMessageBase):
        return message.model_dump()
    if isinstance(message, Mapping):
        return dict(message)
    return {}


def _is_sdk_message_like(value: Any) -> bool:
    if isinstance(value, _SdkMessageBase):
        return bool(value.type) and bool(value.agent_id) and bool(value.run_id)
    if _is_record(value):
        return isinstance(value.get("type"), str) and "agent_id" in value and "run_id" in value
    return False


def _is_run_result_status(value: Any) -> bool:
    return value in ("finished", "error", "cancelled")


def create_sdk_message_run_stream_event(message: SDKMessage | Mapping[str, Any]) -> LocalRunStreamSdkMessageEvent:
    payload = _message_mapping(message)
    return {
        "schemaVersion": LOCAL_RUN_STREAM_SCHEMA_VERSION,
        "type": "sdk_message",
        "agentId": str(payload.get("agent_id") or ""),
        "runId": str(payload.get("run_id") or ""),
        "message": payload,
    }


def decode_local_run_stream_event(payload: Any) -> LocalRunStreamEvent:
    if not _is_record(payload) or "schemaVersion" not in payload or "type" not in payload:
        raise errors.ConfigurationError("Invalid local run stream event payload", is_retryable=False)
    if payload["schemaVersion"] != LOCAL_RUN_STREAM_SCHEMA_VERSION:
        raise errors.ConfigurationError(
            f"Unsupported local run stream event schema version {payload['schemaVersion']}",
            is_retryable=False,
        )
    event_type = payload["type"]
    if event_type == "sdk_message" and _is_sdk_message_like(payload.get("message")):
        return cast(LocalRunStreamSdkMessageEvent, payload)
    if event_type == "result" and _is_run_result_status(payload.get("status")):
        return cast(LocalRunStreamResultEvent, payload)
    if event_type == "done":
        return cast(LocalRunStreamDoneEvent, payload)
    raise errors.ConfigurationError(f"Unsupported local run stream event type {event_type}", is_retryable=False)


def local_run_stream_event_to_sdk_message(event: LocalRunStreamEvent) -> SDKMessage | None:
    if event["type"] == "sdk_message":
        raw = event["message"]
        if isinstance(raw, _SdkMessageBase):
            return raw
        if isinstance(raw, Mapping):
            from opencursor.types import sdk_message_from_json

            return sdk_message_from_json(raw)
        return None
    return None


def decode_sdk_message_run_stream_event(payload: Any) -> SDKMessage:
    event = decode_local_run_stream_event(payload)
    message = local_run_stream_event_to_sdk_message(event)
    if message is None:
        raise errors.ConfigurationError(f"Local run stream event {event['type']} is not an SDK message", is_retryable=False)
    return message


def is_terminal_local_run_stream_event(event: LocalRunStreamEvent) -> bool:
    if event["type"] in ("done", "result"):
        return True
    message = local_run_stream_event_to_sdk_message(event)
    return bool(
        message
        and message.get("type") == "status"
        and message.get("status") in ("FINISHED", "ERROR", "CANCELLED", "EXPIRED")
    )


createSdkMessageRunStreamEvent = create_sdk_message_run_stream_event
decodeLocalRunStreamEvent = decode_local_run_stream_event
decodeSdkMessageRunStreamEvent = decode_sdk_message_run_stream_event
isTerminalLocalRunStreamEvent = is_terminal_local_run_stream_event
localRunStreamEventToSdkMessage = local_run_stream_event_to_sdk_message
