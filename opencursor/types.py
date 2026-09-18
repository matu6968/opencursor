from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, TypedDict, Union

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

T_co = TypeVar("T_co")

RunStatus = Literal["running", "finished", "error", "cancelled", "expired"]
RunResultStatus = Literal["finished", "error", "cancelled", "expired"]
RunOperation = Literal["stream", "wait", "cancel", "conversation"]
SettingSource = Literal["project", "user", "team", "mdm", "plugins", "all"]
AgentModeOption = Literal["agent", "plan"]
# Public Run.steer outcomes. Agent-core also has "confirm_steering" (queued but
# not appended yet); the one-shot promise waits through that and never returns it.
SteerAckOutcome = Literal["complete_delivered", "revert_to_followup"]


class _OfficialDataclassRepr:
    def __repr__(self) -> str:
        names = list(type(self).model_fields)
        body = ", ".join(f"{name}={getattr(self, name)!r}" for name in names)
        return f"{type(self).__name__}({body})"

    def __str__(self) -> str:
        return self.__repr__()


class ModelParameterValue(_OfficialDataclassRepr, BaseModel):
    id: str
    value: str


class ModelSelection(BaseModel):
    id: str
    params: list[ModelParameterValue] | None = None

    @classmethod
    def from_value(cls, value: str | Mapping[str, Any] | ModelSelection) -> ModelSelection:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(id=value)
        mapping = dict(value)
        params_raw = mapping.get("params") or []
        return cls(
            id=str(mapping.get("id") or ""),
            params=[
                item if isinstance(item, ModelParameterValue) else ModelParameterValue.model_validate(dict(item))
                for item in params_raw
                if isinstance(item, (ModelParameterValue, Mapping))
            ]
            or None,
        )

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ModelSelection:
        return cls.from_value(value)


def _coerce_model_selection(value: Any) -> Any:
    if value is None or isinstance(value, ModelSelection) or value == "inherit":
        return value
    if isinstance(value, (str, Mapping)):
        return ModelSelection.from_value(value)
    return value


class SDKImageDimension(BaseModel):
    width: int
    height: int


class SDKImageUrl(BaseModel):
    url: str
    dimension: SDKImageDimension | None = None


class SDKImageData(BaseModel):
    data: str
    mimeType: str = Field(alias="mimeType")
    dimension: SDKImageDimension | None = None

    model_config = {"populate_by_name": True}


SDKImage = Union[SDKImageUrl, SDKImageData]


class SDKUserMessage(BaseModel):
    text: str
    images: list[SDKImageUrl | SDKImageData] | None = None

    @classmethod
    def from_value(cls, value: str | Mapping[str, Any] | SDKUserMessage) -> SDKUserMessage:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(text=value)
        return cls.model_validate(dict(value))

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": self.text}
        if self.images:
            payload["images"] = [
                image.model_dump(by_alias=True, exclude_none=True) if isinstance(image, BaseModel) else dict(image)
                for image in self.images
            ]
        return payload


UserMessage = SDKUserMessage


class AssistantMessage(BaseModel):
    text: str


class ThinkingMessage(BaseModel):
    text: str
    thinking_duration_ms: int | None = None


class SDKUserMessageContent(BaseModel):
    role: Literal["user"] = "user"
    content: list[Mapping[str, Any]] = Field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class SDKAssistantMessageContent(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: list[Mapping[str, Any]] = Field(default_factory=list)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class AgentMessage(BaseModel):
    type: str
    uuid: str
    agent_id: str
    message: Any = None

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> AgentMessage:
        return cls(
            type=str(value.get("type") or ""),
            uuid=str(value.get("uuid") or ""),
            agent_id=str(value.get("agentId") or value.get("agent_id") or ""),
            message=value.get("message"),
        )


class ShellCommand(BaseModel):
    command: str
    working_directory: str = Field(default="", alias="workingDirectory")

    model_config = {"populate_by_name": True}


class ShellOutput(BaseModel):
    stdout: str
    stderr: str
    exit_code: int = Field(alias="exitCode")

    model_config = {"populate_by_name": True}


class ShellOutputDeltaUpdate(BaseModel):
    type: Literal["shell-output-delta"] = "shell-output-delta"
    event: dict[str, Any] = Field(default_factory=dict)


def _as_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise TypeError(f"Expected a mapping, got {type(value).__name__}")


def _string(value: Mapping[str, Any], key: str, default: str = "") -> str:
    item = value.get(key, default)
    return str(item) if item is not None else default


def _int(value: Mapping[str, Any], key: str, default: int = 0) -> int:
    item = value.get(key)
    if item is None:
        return default
    try:
        return int(item)
    except (TypeError, ValueError):
        return default


def _string_any(value: Mapping[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        item = value.get(key)
        if item is not None:
            return str(item)
    return default


def _int_any(value: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        item = value.get(key)
        if item is None:
            continue
        try:
            return int(item)
        except (TypeError, ValueError):
            continue
    return default


def _optional_int_any(value: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        item = value.get(key)
        if item is None:
            continue
        try:
            return int(item)
        except (TypeError, ValueError):
            continue
    return None


def _has_reported_token_usage(value: Mapping[str, Any]) -> bool:
    return (
        _optional_int_any(value, "input_tokens", "inputTokens") is not None
        or _optional_int_any(value, "output_tokens", "outputTokens") is not None
        or _optional_int_any(value, "cache_read_tokens", "cacheReadTokens") is not None
        or _optional_int_any(value, "cache_write_tokens", "cacheWriteTokens") is not None
    )


def _enum(value: str | None, prefix: str) -> str | None:
    if value is None:
        return None
    if value.startswith(prefix):
        return value
    return f"{prefix}{value.upper()}"


class AssistantConversationStep(BaseModel):
    type: Literal["assistantMessage"] = "assistantMessage"
    message: AssistantMessage


class ToolCallConversationStep(BaseModel):
    type: Literal["toolCall"] = "toolCall"
    message: dict[str, Any]


class ThinkingConversationStep(BaseModel):
    type: Literal["thinkingMessage"] = "thinkingMessage"
    message: ThinkingMessage


ConversationStep = (
    AssistantConversationStep
    | ToolCallConversationStep
    | ThinkingConversationStep
    | Mapping[str, Any]
)


def parse_conversation_step(value: Mapping[str, Any]) -> ConversationStep:
    nested_step = value.get("step")
    payload = _as_mapping(nested_step) if isinstance(nested_step, Mapping) else value
    step_type = _string(payload, "type")
    message = payload.get("message")
    if step_type == "assistantMessage" and isinstance(message, Mapping):
        return AssistantConversationStep(
            type="assistantMessage",
            message=AssistantMessage(text=_string(message, "text")),
        )
    if step_type == "toolCall" and isinstance(message, Mapping):
        return ToolCallConversationStep(type="toolCall", message=dict(message))
    if step_type == "thinkingMessage" and isinstance(message, Mapping):
        return ThinkingConversationStep(
            type="thinkingMessage",
            message=ThinkingMessage(
                text=_string(message, "text"),
                thinking_duration_ms=(
                    _int(message, "thinkingDurationMs")
                    if message.get("thinkingDurationMs") is not None
                    else None
                ),
            ),
        )
    return dict(payload)


class AgentConversationTurn(BaseModel):
    user_message: dict[str, Any] | None = Field(default=None, alias="userMessage")
    steps: list[Any] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ShellConversationTurn(BaseModel):
    shell_command: ShellCommand | None = Field(default=None, alias="shellCommand")
    shell_output: ShellOutput | None = Field(default=None, alias="shellOutput")

    model_config = {"populate_by_name": True}


class ConversationTurn(BaseModel):
    type: str
    turn: Any

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ConversationTurn:
        turn_type = _string(value, "type")
        turn_raw = value.get("turn")
        turn = _as_mapping(turn_raw) if turn_raw else {}
        if turn_type == "agentConversationTurn":
            return cls(
                type=turn_type,
                turn=AgentConversationTurn(
                    user_message=dict(turn["userMessage"])
                    if isinstance(turn.get("userMessage"), Mapping)
                    else None,
                    steps=[
                        parse_conversation_step(_as_mapping(step))
                        for step in turn.get("steps", [])
                    ],
                ),
            )
        if turn_type == "shellConversationTurn":
            command = turn.get("shellCommand")
            output = turn.get("shellOutput")
            return cls(
                type=turn_type,
                turn=ShellConversationTurn(
                    shell_command=ShellCommand(
                        command=_string(_as_mapping(command), "command"),
                        working_directory=_string(_as_mapping(command), "workingDirectory"),
                    )
                    if isinstance(command, Mapping)
                    else None,
                    shell_output=ShellOutput(
                        stdout=_string(_as_mapping(output), "stdout"),
                        stderr=_string(_as_mapping(output), "stderr"),
                        exit_code=_int(_as_mapping(output), "exitCode"),
                    )
                    if isinstance(output, Mapping)
                    else None,
                ),
            )
        return cls(type=turn_type, turn=dict(turn))


class TextDeltaUpdate(BaseModel):
    type: Literal["text-delta"] = "text-delta"
    text: str


class ThinkingDeltaUpdate(BaseModel):
    type: Literal["thinking-delta"] = "thinking-delta"
    text: str


class ThinkingCompletedUpdate(BaseModel):
    type: Literal["thinking-completed"] = "thinking-completed"
    thinking_duration_ms: int = Field(default=0, alias="thinkingDurationMs")

    model_config = {"populate_by_name": True}


class ToolCallStartedUpdate(BaseModel):
    type: Literal["tool-call-started"] = "tool-call-started"
    call_id: str = Field(default="", alias="callId")
    tool_call: dict[str, Any] = Field(default_factory=dict, alias="toolCall")
    model_call_id: str = Field(default="", alias="modelCallId")

    model_config = {"populate_by_name": True}


class ToolCallCompletedUpdate(BaseModel):
    type: Literal["tool-call-completed"] = "tool-call-completed"
    call_id: str = Field(default="", alias="callId")
    tool_call: dict[str, Any] = Field(default_factory=dict, alias="toolCall")
    model_call_id: str = Field(default="", alias="modelCallId")

    model_config = {"populate_by_name": True}


class PartialToolCallUpdate(BaseModel):
    type: Literal["partial-tool-call"] = "partial-tool-call"
    call_id: str = Field(default="", alias="callId")
    tool_call: dict[str, Any] = Field(default_factory=dict, alias="toolCall")
    model_call_id: str = Field(default="", alias="modelCallId")

    model_config = {"populate_by_name": True}


class TokenDeltaUpdate(BaseModel):
    type: Literal["token-delta"] = "token-delta"
    tokens: int = 0


class StepStartedUpdate(BaseModel):
    type: Literal["step-started"] = "step-started"
    step_id: int = Field(default=0, alias="stepId")

    model_config = {"populate_by_name": True}


class StepCompletedUpdate(BaseModel):
    type: Literal["step-completed"] = "step-completed"
    step_id: int = Field(default=0, alias="stepId")
    step_duration_ms: int = Field(default=0, alias="stepDurationMs")

    model_config = {"populate_by_name": True}


class TurnEndedUpdate(BaseModel):
    type: Literal["turn-ended"] = "turn-ended"
    usage: dict[str, Any] | None = None


class UserMessageAppendedUpdate(BaseModel):
    type: Literal["user-message-appended"] = "user-message-appended"
    user_message: dict[str, Any] = Field(default_factory=dict, alias="userMessage")

    model_config = {"populate_by_name": True}


class SummaryUpdate(BaseModel):
    type: Literal["summary"] = "summary"
    summary: str = ""


class SummaryStartedUpdate(BaseModel):
    type: Literal["summary-started"] = "summary-started"


class SummaryCompletedUpdate(BaseModel):
    type: Literal["summary-completed"] = "summary-completed"


class UnknownInteractionUpdate(BaseModel):
    type: str
    update: dict[str, Any] = Field(default_factory=dict)


InteractionUpdate = (
    TextDeltaUpdate
    | ThinkingDeltaUpdate
    | ThinkingCompletedUpdate
    | ToolCallStartedUpdate
    | ToolCallCompletedUpdate
    | PartialToolCallUpdate
    | TokenDeltaUpdate
    | StepStartedUpdate
    | StepCompletedUpdate
    | TurnEndedUpdate
    | UserMessageAppendedUpdate
    | SummaryUpdate
    | SummaryStartedUpdate
    | SummaryCompletedUpdate
    | ShellOutputDeltaUpdate
    | UnknownInteractionUpdate
    | Mapping[str, Any]
)


def _interaction_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    update = value.get("update")
    if isinstance(update, Mapping):
        return update
    return value


def parse_interaction_update(value: Mapping[str, Any]) -> InteractionUpdate:
    payload = _interaction_payload(value)
    update_type = _string(payload, "type") or _string(value, "type")
    if update_type == "text-delta":
        return TextDeltaUpdate(type="text-delta", text=_string(payload, "text"))
    if update_type == "thinking-delta":
        return ThinkingDeltaUpdate(type="thinking-delta", text=_string(payload, "text"))
    if update_type == "thinking-completed":
        return ThinkingCompletedUpdate(
            type="thinking-completed",
            thinking_duration_ms=_int(payload, "thinkingDurationMs"),
        )
    if update_type == "tool-call-started":
        return ToolCallStartedUpdate(
            type="tool-call-started",
            call_id=_string(payload, "callId"),
            tool_call=dict(payload.get("toolCall") or {}),
            model_call_id=_string(payload, "modelCallId"),
        )
    if update_type == "tool-call-completed":
        return ToolCallCompletedUpdate(
            type="tool-call-completed",
            call_id=_string(payload, "callId"),
            tool_call=dict(payload.get("toolCall") or {}),
            model_call_id=_string(payload, "modelCallId"),
        )
    if update_type == "partial-tool-call":
        return PartialToolCallUpdate(
            type="partial-tool-call",
            call_id=_string(payload, "callId"),
            tool_call=dict(payload.get("toolCall") or {}),
            model_call_id=_string(payload, "modelCallId"),
        )
    if update_type == "token-delta":
        return TokenDeltaUpdate(type="token-delta", tokens=_int(payload, "tokens"))
    if update_type == "step-started":
        return StepStartedUpdate(type="step-started", step_id=_int(payload, "stepId"))
    if update_type == "step-completed":
        return StepCompletedUpdate(
            type="step-completed",
            step_id=_int(payload, "stepId"),
            step_duration_ms=_int(payload, "stepDurationMs"),
        )
    if update_type == "turn-ended":
        usage = payload.get("usage")
        return TurnEndedUpdate(
            type="turn-ended",
            usage=dict(usage) if isinstance(usage, Mapping) else None,
        )
    if update_type == "user-message-appended":
        return UserMessageAppendedUpdate(
            type="user-message-appended",
            user_message=dict(payload.get("userMessage") or {}),
        )
    if update_type == "summary":
        return SummaryUpdate(type="summary", summary=_string(payload, "summary"))
    if update_type == "summary-started":
        return SummaryStartedUpdate(type="summary-started")
    if update_type == "summary-completed":
        return SummaryCompletedUpdate(type="summary-completed")
    if update_type == "shell-output-delta":
        return ShellOutputDeltaUpdate(
            type="shell-output-delta",
            event=dict(payload.get("event") or {}),
        )
    return UnknownInteractionUpdate(type=update_type, update=dict(payload))


class McpAuth(BaseModel):
    client_id: str = Field(default="", alias="clientId")
    client_secret: str | None = Field(default=None, alias="clientSecret")
    scopes: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def _normalize_keys(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "clientId" not in data and "client_id" not in data and "CLIENT_ID" in data:
            data["clientId"] = data["CLIENT_ID"]
        if "clientSecret" not in data and "client_secret" not in data and "CLIENT_SECRET" in data:
            data["clientSecret"] = data["CLIENT_SECRET"]
        if "scopes" not in data and "SCOPES" in data:
            data["scopes"] = data["SCOPES"]
        scopes = data.get("scopes")
        if isinstance(scopes, str):
            data["scopes"] = [token for token in scopes.split() if token]
        return data

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.client_id:
            payload["clientId"] = self.client_id
        if self.client_secret:
            payload["clientSecret"] = self.client_secret
        if self.scopes:
            payload["scopes"] = list(self.scopes)
        extras = self.model_dump(exclude={"client_id", "client_secret", "scopes"}, exclude_none=True)
        for key, item in extras.items():
            if item not in (None, "", [], {}):
                payload[key] = item
        return payload


class StdioMcpServerConfig(BaseModel):
    type: Literal["stdio"] | None = None
    command: str
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None

    def to_json(self) -> dict[str, Any]:
        inner: dict[str, Any] = {"command": self.command}
        if self.args:
            inner["args"] = list(self.args)
        if self.env:
            inner["env"] = dict(self.env)
        if self.cwd:
            inner["cwd"] = os.fspath(self.cwd)
        return {"stdio": inner}


class HttpMcpServerConfig(BaseModel):
    url: str
    type: Literal["http", "sse"] | str | None = "http"
    headers: dict[str, str] | None = None
    auth: McpAuth | dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        kind = str(self.type) if self.type else "http"
        inner: dict[str, Any] = {
            "type": _enum(kind, "HTTP_MCP_TRANSPORT_TYPE_"),
            "url": self.url,
        }
        if self.headers:
            inner["headers"] = dict(self.headers)
        if self.auth:
            inner["auth"] = _normalize_mcp_auth(self.auth)
        return {"http": inner}


class SseMcpServerConfig(HttpMcpServerConfig):
    type: Literal["sse"] | str | None = "sse"


McpServerStdio = StdioMcpServerConfig
McpServerRemote = HttpMcpServerConfig
McpServerConfig = Union[HttpMcpServerConfig, SseMcpServerConfig, StdioMcpServerConfig]


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _coerce_scopes(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [token for token in value.split() if token]
    return [str(scope) for scope in value]


def _normalize_mcp_auth(auth: McpAuth | Mapping[str, Any] | None) -> dict[str, Any]:
    if auth is None:
        return {}
    if isinstance(auth, McpAuth):
        return auth.to_json()
    scopes_value = _first_present(auth, "scopes", "SCOPES")
    normalized: dict[str, Any] = {}
    client_id = _first_present(auth, "clientId", "CLIENT_ID", "client_id")
    client_secret = _first_present(auth, "clientSecret", "CLIENT_SECRET", "client_secret")
    if client_id:
        normalized["clientId"] = client_id
    if client_secret:
        normalized["clientSecret"] = client_secret
    scopes = _coerce_scopes(scopes_value)
    if scopes:
        normalized["scopes"] = scopes
    skip = {"clientId", "clientSecret", "scopes", "CLIENT_ID", "CLIENT_SECRET", "SCOPES", "client_id", "client_secret"}
    for key, value in auth.items():
        if key in skip:
            continue
        if key not in normalized and value not in (None, "", [], {}):
            normalized[key] = value
    return normalized


def _normalize_mcp_server(value: Any) -> dict[str, Any]:
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return dict(to_json())
    mapping = dict(value)
    if "http" in mapping or "stdio" in mapping:
        return mapping
    transport_type = str(mapping.get("type") or "")
    if transport_type in {"http", "sse"} or (not transport_type and "url" in mapping):
        auth = mapping.get("auth")
        inner: dict[str, Any] = {
            "type": _enum(transport_type if transport_type else "http", "HTTP_MCP_TRANSPORT_TYPE_"),
            "url": mapping.get("url") or "",
        }
        headers = mapping.get("headers")
        if headers:
            inner["headers"] = dict(headers)
        if isinstance(auth, (Mapping, McpAuth)):
            inner["auth"] = _normalize_mcp_auth(auth)
        return {"http": inner}
    inner_stdio: dict[str, Any] = {"command": mapping.get("command") or ""}
    if mapping.get("args"):
        inner_stdio["args"] = list(mapping.get("args") or [])
    if mapping.get("env"):
        inner_stdio["env"] = dict(mapping.get("env") or {})
    if mapping.get("cwd"):
        inner_stdio["cwd"] = os.fspath(mapping["cwd"])
    return {"stdio": inner_stdio}


class AgentDefinitionMcpServer(BaseModel):
    name: str | None = None
    inline_config: Any = Field(default=None, alias="inlineConfig")

    model_config = {"populate_by_name": True}

    @classmethod
    def named(cls, name: str) -> AgentDefinitionMcpServer:
        return cls(name=name)

    @classmethod
    def inline(cls, config: Any) -> AgentDefinitionMcpServer:
        return cls(inline_config=config)

    def to_json(self) -> dict[str, Any]:
        if self.name is not None:
            return {"name": self.name}
        if self.inline_config is not None:
            return {"inlineConfig": _normalize_mcp_server(self.inline_config)}
        return {}


class AgentDefinition(BaseModel):
    description: str
    prompt: str
    model: ModelSelection | Literal["inherit"] | None = None
    mcpServers: list[str | AgentDefinitionMcpServer | dict[str, Any]] | None = Field(default=None, alias="mcpServers")

    model_config = {"populate_by_name": True}

    @field_validator("model", mode="before")
    @classmethod
    def _coerce_model(cls, value: Any) -> Any:
        return _coerce_model_selection(value)


class CloudEnvironment(BaseModel):
    type: Literal["cloud", "pool", "machine"] | str = "cloud"
    name: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value.startswith("CLOUD_ENVIRONMENT_TYPE_"):
            return value.removeprefix("CLOUD_ENVIRONMENT_TYPE_").lower()
        return value

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> CloudEnvironment:
        name = value.get("name")
        return cls(type=value.get("type") or "cloud", name=str(name) if name else None)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        kind = str(self.type) if self.type else ""
        if kind:
            if not kind.startswith("CLOUD_ENVIRONMENT_TYPE_"):
                kind = f"CLOUD_ENVIRONMENT_TYPE_{kind.upper()}"
            payload["type"] = kind
        if self.name:
            payload["name"] = self.name
        return payload


class CloudRepository(BaseModel):
    url: str
    starting_ref: str | None = Field(default=None, alias="startingRef")
    pr_url: str | None = Field(default=None, alias="prUrl")

    model_config = {"populate_by_name": True}

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"url": self.url}
        if self.starting_ref:
            payload["startingRef"] = self.starting_ref
        if self.pr_url:
            payload["prUrl"] = self.pr_url
        return payload


CloudEnv = CloudEnvironment
CloudRepo = CloudRepository


class CloudAgentOptions(BaseModel):
    env: CloudEnvironment | None = None
    repos: list[CloudRepository] | None = None
    workOnCurrentBranch: bool | None = Field(default=None, alias="workOnCurrentBranch")
    autoCreatePR: bool | None = Field(default=None, alias="autoCreatePR")
    skipReviewerRequest: bool | None = Field(default=None, alias="skipReviewerRequest")
    openAsCursorGithubApp: bool | None = Field(default=None, alias="openAsCursorGithubApp")
    envVars: dict[str, str] | None = Field(default=None, alias="envVars")
    metadata: dict[str, str] | None = None
    agentServeAgent: str | None = Field(default=None, alias="agentServeAgent")

    model_config = {"populate_by_name": True}


class SandboxOptions(BaseModel):
    enabled: bool


class SDKToolAnnotations(BaseModel):
    title: str | None = None
    readOnlyHint: bool | None = Field(default=None, alias="readOnlyHint")
    destructiveHint: bool | None = Field(default=None, alias="destructiveHint")
    idempotentHint: bool | None = Field(default=None, alias="idempotentHint")
    openWorldHint: bool | None = Field(default=None, alias="openWorldHint")

    model_config = {"populate_by_name": True}


class CustomToolContext(BaseModel):
    tool_call_id: str | None = Field(default=None, alias="toolCallId")

    model_config = {"populate_by_name": True}


class CustomTool(BaseModel):
    description: str | None = None
    inputSchema: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("inputSchema", "input_schema"),
        serialization_alias="inputSchema",
    )
    outputSchema: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("outputSchema", "output_schema"),
        serialization_alias="outputSchema",
    )
    annotations: SDKToolAnnotations | None = None
    execute: Any = None

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}

    @property
    def input_schema(self) -> dict[str, Any] | None:
        return self.inputSchema

    @property
    def output_schema(self) -> dict[str, Any] | None:
        return self.outputSchema


SDKCustomTool = CustomTool


class LocalAgentOptions(BaseModel):
    cwd: str | list[str] | None = None
    dirs: list[str] | None = None
    settingSources: list[SettingSource] | None = Field(
        default=None,
        validation_alias=AliasChoices("settingSources", "setting_sources"),
        serialization_alias="settingSources",
    )
    sandboxOptions: SandboxOptions | None = Field(default=None, alias="sandboxOptions")
    useHttp1ForAgent: bool | None = Field(default=None, alias="useHttp1ForAgent")
    autoReview: bool | None = Field(default=None, alias="autoReview")
    store: Any | None = None
    customTools: dict[str, Any] | None = Field(default=None, alias="customTools")
    enableAgentRetries: bool | None = Field(default=None, alias="enableAgentRetries")

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}


class AgentOptions(BaseModel):
    model: ModelSelection | None = None
    apiKey: str | None = Field(
        default=None,
        validation_alias=AliasChoices("apiKey", "api_key"),
        serialization_alias="apiKey",
    )
    name: str | None = None
    local: LocalAgentOptions | None = None
    cloud: CloudAgentOptions | None = None
    mcpServers: dict[str, McpServerStdio | McpServerRemote] | None = Field(
        default=None,
        validation_alias=AliasChoices("mcpServers", "mcp_servers"),
        serialization_alias="mcpServers",
    )
    agents: dict[str, AgentDefinition] | None = None
    agentId: str | None = Field(
        default=None,
        validation_alias=AliasChoices("agentId", "agent_id"),
        serialization_alias="agentId",
    )
    platform: dict[str, Any] | None = None
    tools: list[str] | None = None
    disallowedTools: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("disallowedTools", "disallowed_tools"),
        serialization_alias="disallowedTools",
    )
    mode: AgentModeOption | None = None
    idempotencyKey: str | None = Field(
        default=None,
        validation_alias=AliasChoices("idempotencyKey", "idempotency_key"),
        serialization_alias="idempotencyKey",
    )

    model_config = {"populate_by_name": True}

    @field_validator("model", mode="before")
    @classmethod
    def _coerce_model(cls, value: Any) -> Any:
        return _coerce_model_selection(value)


class LocalSendOptions(BaseModel):
    force: bool | None = None
    customTools: dict[str, Any] | None = Field(default=None, alias="customTools")

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.force is not None:
            payload["force"] = self.force
        if self.customTools is not None:
            payload["customTools"] = self.customTools
        return payload


SendOptionsLocal = LocalSendOptions


class CloudSendOptions(BaseModel):
    envVars: dict[str, str] | None = Field(default=None, alias="envVars")

    model_config = {"populate_by_name": True}


class SendOptions(BaseModel):
    model: ModelSelection | None = None
    mcpServers: dict[str, McpServerStdio | McpServerRemote] | None = None
    mode: AgentModeOption | None = None
    onStep: Any | None = None
    onDelta: Any | None = None
    local: LocalSendOptions | None = None
    cloud: CloudSendOptions | None = None
    idempotencyKey: str | None = Field(default=None, alias="idempotencyKey")

    model_config = {"populate_by_name": True}

    @field_validator("model", mode="before")
    @classmethod
    def _coerce_model(cls, value: Any) -> Any:
        return _coerce_model_selection(value)


class ModelSelectionDict(TypedDict, total=False):
    id: str
    params: Sequence[Mapping[str, Any] | ModelParameterValue]


class LocalAgentStoreConfigDict(TypedDict, total=False):
    type: str
    root_dir: str
    rootDir: str


class LocalAgentStoreConfig(BaseModel):
    type: str
    root_dir: str | None = Field(default=None, alias="rootDir")

    model_config = {"populate_by_name": True}

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.root_dir:
            payload["rootDir"] = self.root_dir
        return payload


class CloudRepositoryDict(TypedDict, total=False):
    url: str
    starting_ref: str
    startingRef: str
    pr_url: str
    prUrl: str


class LocalAgentOptionsDict(TypedDict, total=False):
    cwd: str | os.PathLike[str]
    dirs: Sequence[str | os.PathLike[str]]
    setting_sources: Sequence[SettingSource | str]
    sandbox_options: Mapping[str, Any] | SandboxOptions
    store: LocalAgentStoreConfigDict | LocalAgentStoreConfig | Mapping[str, Any]
    auto_review: bool
    autoReview: bool
    custom_tools: Mapping[str, CustomTool | Mapping[str, Any]]
    useHttp1ForAgent: bool
    enableAgentRetries: bool


class CloudAgentOptionsDict(TypedDict, total=False):
    env: Mapping[str, Any] | CloudEnvironment
    repos: Sequence[CloudRepositoryDict | CloudRepository | Mapping[str, Any]]
    work_on_current_branch: bool
    workOnCurrentBranch: bool
    auto_create_pr: bool
    autoCreatePr: bool
    autoCreatePR: bool
    open_as_cursor_github_app: bool
    openAsCursorGithubApp: bool
    skip_reviewer_request: bool
    skipReviewerRequest: bool
    env_vars: Mapping[str, str]
    envVars: Mapping[str, str]
    metadata: Mapping[str, str]
    agentServeAgent: str


class AgentOptionsDict(TypedDict, total=False):
    model: str | ModelSelectionDict | ModelSelection | Mapping[str, Any]
    api_key: str
    apiKey: str
    name: str
    local: LocalAgentOptionsDict | LocalAgentOptions | Mapping[str, Any]
    cloud: CloudAgentOptionsDict | CloudAgentOptions | Mapping[str, Any]
    mcp_servers: Mapping[str, McpServerConfig]
    mcpServers: Mapping[str, McpServerConfig]
    agents: Mapping[str, AgentDefinition | Mapping[str, Any]]
    agent_id: str
    agentId: str
    idempotency_key: str
    idempotencyKey: str
    mode: AgentModeOption
    tools: Sequence[str]
    disallowed_tools: Sequence[str]
    disallowedTools: Sequence[str]
    platform: dict[str, Any]


class LocalSendOptionsDict(TypedDict, total=False):
    force: bool
    custom_tools: Mapping[str, Any]
    customTools: Mapping[str, Any]


class CloudSendOptionsDict(TypedDict, total=False):
    env_vars: Mapping[str, str]
    envVars: Mapping[str, str]


class SendOptionsDict(TypedDict, total=False):
    model: str | ModelSelectionDict | ModelSelection | Mapping[str, Any]
    mcp_servers: Mapping[str, McpServerConfig]
    mcpServers: Mapping[str, McpServerConfig]
    local: LocalSendOptionsDict | LocalSendOptions | Mapping[str, Any]
    cloud: CloudSendOptionsDict | CloudSendOptions | Mapping[str, Any]
    idempotency_key: str
    idempotencyKey: str
    mode: AgentModeOption


class UserMessageDict(TypedDict, total=False):
    text: str
    images: Sequence[SDKImage | Mapping[str, Any]]


class RunGitBranchInfo(BaseModel):
    repoUrl: str = Field(alias="repoUrl")
    branch: str | None = None
    prUrl: str | None = Field(default=None, alias="prUrl")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RunGitBranchInfo:
        return cls(
            repoUrl=_string(value, "repoUrl") or _string(value, "repo_url"),
            branch=_string(value, "branch") or None,
            prUrl=_string(value, "prUrl") or _string(value, "pr_url") or None,
        )


class RunGitInfo(BaseModel):
    branches: list[RunGitBranchInfo] = Field(default_factory=list)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RunGitInfo:
        return cls(
            branches=[
                item if isinstance(item, RunGitBranchInfo) else RunGitBranchInfo.from_json(_as_mapping(item))
                for item in value.get("branches", [])
            ]
        )


class RunError(BaseModel):
    message: str
    code: str | None = None


class TokenUsage(_OfficialDataclassRepr, BaseModel):
    input_tokens: int = Field(
        default=0,
        validation_alias=AliasChoices("input_tokens", "inputTokens"),
        serialization_alias="inputTokens",
    )
    output_tokens: int = Field(
        default=0,
        validation_alias=AliasChoices("output_tokens", "outputTokens"),
        serialization_alias="outputTokens",
    )
    cache_read_tokens: int = Field(
        default=0,
        validation_alias=AliasChoices("cache_read_tokens", "cacheReadTokens"),
        serialization_alias="cacheReadTokens",
    )
    cache_write_tokens: int = Field(
        default=0,
        validation_alias=AliasChoices("cache_write_tokens", "cacheWriteTokens"),
        serialization_alias="cacheWriteTokens",
    )
    total_tokens: int = Field(
        default=0,
        validation_alias=AliasChoices("total_tokens", "totalTokens"),
        serialization_alias="totalTokens",
    )
    reasoning_tokens: int | None = Field(
        default=None,
        validation_alias=AliasChoices("reasoning_tokens", "reasoningTokens"),
        serialization_alias="reasoningTokens",
    )

    model_config = {"populate_by_name": True}

    @property
    def inputTokens(self) -> int:  # noqa: N802
        return self.input_tokens

    @inputTokens.setter
    def inputTokens(self, value: int) -> None:  # noqa: N802
        self.input_tokens = int(value)

    @property
    def outputTokens(self) -> int:  # noqa: N802
        return self.output_tokens

    @outputTokens.setter
    def outputTokens(self, value: int) -> None:  # noqa: N802
        self.output_tokens = int(value)

    @property
    def cacheReadTokens(self) -> int:  # noqa: N802
        return self.cache_read_tokens

    @cacheReadTokens.setter
    def cacheReadTokens(self, value: int) -> None:  # noqa: N802
        self.cache_read_tokens = int(value)

    @property
    def cacheWriteTokens(self) -> int:  # noqa: N802
        return self.cache_write_tokens

    @cacheWriteTokens.setter
    def cacheWriteTokens(self, value: int) -> None:  # noqa: N802
        self.cache_write_tokens = int(value)

    @property
    def totalTokens(self) -> int:  # noqa: N802
        return self.total_tokens

    @totalTokens.setter
    def totalTokens(self, value: int) -> None:  # noqa: N802
        self.total_tokens = int(value)

    @property
    def reasoningTokens(self) -> int | None:  # noqa: N802
        return self.reasoning_tokens

    @reasoningTokens.setter
    def reasoningTokens(self, value: int | None) -> None:  # noqa: N802
        self.reasoning_tokens = None if value is None else int(value)

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> TokenUsage:
        input_tokens = _int_any(value, "input_tokens", "inputTokens")
        output_tokens = _int_any(value, "output_tokens", "outputTokens")
        cache_read_tokens = _int_any(value, "cache_read_tokens", "cacheReadTokens")
        cache_write_tokens = _int_any(value, "cache_write_tokens", "cacheWriteTokens")
        summed = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
        wire_total = _optional_int_any(value, "total_tokens", "totalTokens")
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            total_tokens=summed if wire_total is None else wire_total,
            reasoning_tokens=_optional_int_any(value, "reasoning_tokens", "reasoningTokens"),
        )

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "cacheWriteTokens": self.cache_write_tokens,
            "totalTokens": self.total_tokens,
        }
        if self.reasoning_tokens is not None:
            payload["reasoningTokens"] = self.reasoning_tokens
        return payload


def to_token_usage(
    usage: Mapping[str, Any] | TokenUsage | None,
) -> TokenUsage | None:
    if usage is None:
        return None
    if isinstance(usage, TokenUsage):
        return usage
    if not _has_reported_token_usage(usage):
        return None
    input_tokens = _int_any(usage, "input_tokens", "inputTokens")
    output_tokens = _int_any(usage, "output_tokens", "outputTokens")
    cache_read_tokens = _int_any(usage, "cache_read_tokens", "cacheReadTokens")
    cache_write_tokens = _int_any(usage, "cache_write_tokens", "cacheWriteTokens")
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=input_tokens + output_tokens + cache_read_tokens + cache_write_tokens,
        reasoning_tokens=_optional_int_any(usage, "reasoning_tokens", "reasoningTokens"),
    )


def sum_token_usage(
    usages: Sequence[Mapping[str, Any] | TokenUsage | None],
) -> TokenUsage | None:
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    reasoning_tokens = 0
    has_usage = False
    has_reasoning = False
    for entry in usages:
        usage = to_token_usage(entry)
        if usage is None:
            continue
        has_usage = True
        input_tokens += usage.inputTokens
        output_tokens += usage.outputTokens
        cache_read_tokens += usage.cacheReadTokens
        cache_write_tokens += usage.cacheWriteTokens
        if usage.reasoningTokens is not None:
            has_reasoning = True
            reasoning_tokens += usage.reasoningTokens
    if not has_usage:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=input_tokens + output_tokens + cache_read_tokens + cache_write_tokens,
        reasoning_tokens=reasoning_tokens if has_reasoning else None,
    )


class UsageCost(_OfficialDataclassRepr, BaseModel):
    raw_cost_cents: float = Field(
        default=0.0,
        validation_alias=AliasChoices("raw_cost_cents", "rawCostCents"),
        serialization_alias="rawCostCents",
    )
    charged_cents: float = Field(
        default=0.0,
        validation_alias=AliasChoices("charged_cents", "chargedCents"),
        serialization_alias="chargedCents",
    )

    model_config = {"populate_by_name": True}

    @property
    def rawCostCents(self) -> float:  # noqa: N802
        return self.raw_cost_cents

    @property
    def chargedCents(self) -> float:  # noqa: N802
        return self.charged_cents


class RunUsage(_OfficialDataclassRepr, BaseModel):
    run_id: str = Field(validation_alias=AliasChoices("run_id", "runId"), serialization_alias="runId")
    usage: TokenUsage
    cost: UsageCost | None = None

    model_config = {"populate_by_name": True}

    @property
    def runId(self) -> str:  # noqa: N802
        return self.run_id


class AgentUsage(_OfficialDataclassRepr, BaseModel):
    usage: TokenUsage
    cost: UsageCost | None = None
    runs: list[RunUsage] = Field(default_factory=list)


class RunResult(_OfficialDataclassRepr, BaseModel):
    id: str
    agent_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("agent_id", "agentId"),
        serialization_alias="agentId",
    )
    status: RunResultStatus | RunStatus | str
    result: str = ""
    model: ModelSelection | None = None
    duration_ms: int | None = Field(
        default=0,
        validation_alias=AliasChoices("duration_ms", "durationMs"),
        serialization_alias="durationMs",
    )
    git: RunGitInfo | None = None
    created_at: str | None = Field(
        default=None,
        validation_alias=AliasChoices("created_at", "createdAt"),
        serialization_alias="createdAt",
    )
    usage: TokenUsage | None = None
    request_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("request_id", "requestId"),
        serialization_alias="requestId",
    )
    error: RunError | None = None

    model_config = {"populate_by_name": True}

    @field_validator("result", mode="before")
    @classmethod
    def _result_str(cls, value: object) -> str:
        return "" if value is None else str(value)

    @property
    def durationMs(self) -> int | None:  # noqa: N802
        return self.duration_ms

    @property
    def requestId(self) -> str | None:  # noqa: N802
        return self.request_id

    @property
    def agentId(self) -> str | None:  # noqa: N802
        return self.agent_id

    @property
    def run_id(self) -> str:
        import warnings

        warnings.warn(
            "RunResult.run_id is deprecated; use RunResult.id instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.id

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RunResult:
        created_at_raw = value.get("createdAt") or value.get("created_at")
        usage_raw = value.get("usage")
        status_raw = _string(value, "status")
        normalized = status_raw.removeprefix("RUN_LIFECYCLE_STATUS_").lower()
        if normalized == "unspecified":
            normalized = "error"
        elif normalized == "creating":
            normalized = "running"
        return cls(
            id=_string(value, "runId") or _string(value, "id"),
            agent_id=_string(value, "agentId") or _string(value, "agent_id") or None,
            status=normalized or "error",
            result=_string(value, "result") or "",
            model=ModelSelection.from_json(_as_mapping(value["model"])) if value.get("model") else None,
            duration_ms=_optional_int_any(value, "durationMs", "duration_ms"),
            git=RunGitInfo.from_json(_as_mapping(value["git"])) if value.get("git") else None,
            created_at=str(created_at_raw) if created_at_raw else None,
            usage=to_token_usage(usage_raw) if isinstance(usage_raw, Mapping) else None,
            request_id=_string(value, "requestId") or _string(value, "request_id") or None,
        )


class RunSnapshot(RunResult):
    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RunSnapshot:
        base = RunResult.from_json(value)
        return cls(
            id=base.id,
            agent_id=base.agent_id,
            status=base.status,
            result=base.result,
            model=base.model,
            duration_ms=base.duration_ms,
            git=base.git,
            created_at=base.created_at,
            usage=base.usage,
            request_id=base.request_id,
            error=base.error,
        )


def _sdk_message_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    message = value.get("message")
    if isinstance(message, Mapping) and "type" in message:
        return message
    return value


def _user_message_content_from_json(value: Any) -> SDKUserMessageContent:
    mapping = _as_mapping(value or {})
    content: list[Mapping[str, Any]] = []
    for block in mapping.get("content", []) or []:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text":
            content.append({"type": "text", "text": _string(block, "text")})
        else:
            content.append(dict(block))
    return SDKUserMessageContent(role="user", content=content)


def _assistant_message_content_from_json(value: Any) -> SDKAssistantMessageContent:
    mapping = _as_mapping(value or {})
    content: list[Mapping[str, Any]] = []
    for block in mapping.get("content", []) or []:
        if isinstance(block, Mapping):
            content.append(dict(block))
    return SDKAssistantMessageContent(role="assistant", content=content)


def sdk_message_from_json(value: Mapping[str, Any]) -> Any:
    payload = _sdk_message_payload(value)
    message_type = _string(payload, "type") or _string(value, "type")
    agent_id = _string_any(payload, "agent_id", "agentId")
    run_id = _string_any(payload, "run_id", "runId")
    if message_type == "system":
        from opencursor.messages import SDKSystemMessage

        return SDKSystemMessage(
            type="system",
            agent_id=agent_id,
            run_id=run_id,
            subtype=_string(payload, "subtype"),  # type: ignore[typeddict-item]
            model=ModelSelection.from_json(_as_mapping(payload["model"])) if payload.get("model") else None,
            tools=[str(tool) for tool in payload.get("tools", [])],
        )
    if message_type == "user":
        from opencursor.messages import SDKUserMessageEvent

        content = _user_message_content_from_json(payload.get("message"))
        return SDKUserMessageEvent(
            type="user",
            agent_id=agent_id,
            run_id=run_id,
            message=content,
        )
    if message_type == "assistant":
        from opencursor.messages import SDKAssistantMessage

        content = _assistant_message_content_from_json(payload.get("message"))
        return SDKAssistantMessage(
            type="assistant",
            agent_id=agent_id,
            run_id=run_id,
            message=content,
        )
    if message_type == "thinking":
        from opencursor.messages import SDKThinkingMessage

        duration_value = payload.get("thinkingDurationMs")
        if duration_value is None:
            duration_value = payload.get("thinking_duration_ms")
        return SDKThinkingMessage(
            type="thinking",
            agent_id=agent_id,
            run_id=run_id,
            text=_string(payload, "text"),
            thinking_duration_ms=_int({"thinkingDurationMs": duration_value}, "thinkingDurationMs")
            if duration_value is not None
            else None,
        )
    if message_type == "tool_call":
        from opencursor.messages import SDKToolUseMessage

        truncated = payload.get("truncated")
        return SDKToolUseMessage(
            type="tool_call",
            agent_id=agent_id,
            run_id=run_id,
            call_id=_string_any(payload, "call_id", "callId"),
            name=_string(payload, "name"),
            status=_string(payload, "status"),
            args=payload.get("args"),
            result=payload.get("result"),
            truncated=dict(truncated) if isinstance(truncated, Mapping) else None,
        )
    if message_type == "status":
        from opencursor.messages import SDKStatusMessage

        return SDKStatusMessage(
            type="status",
            agent_id=agent_id,
            run_id=run_id,
            status=_string(payload, "status"),  # type: ignore[typeddict-item]
            message=_string(payload, "message"),
        )
    if message_type == "task":
        from opencursor.messages import SDKTaskMessage

        return SDKTaskMessage(
            type="task",
            agent_id=agent_id,
            run_id=run_id,
            status=_string(payload, "status"),
            text=_string(payload, "text"),
        )
    if message_type == "request":
        from opencursor.messages import SDKRequestMessage

        return SDKRequestMessage(
            type="request",
            agent_id=agent_id,
            run_id=run_id,
            request_id=_string_any(payload, "request_id", "requestId"),
        )
    if message_type == "usage":
        from opencursor.messages import SDKUsageMessage

        usage_payload = payload.get("usage")
        if not isinstance(usage_payload, Mapping) or not _has_reported_token_usage(usage_payload):
            return dict(payload)
        return SDKUsageMessage(
            type="usage",
            agent_id=agent_id,
            run_id=run_id,
            usage=TokenUsage.from_json(usage_payload).to_json(),
        )
    return dict(payload)


class RunStreamEvent(BaseModel):
    kind: str
    offset: Any = None
    sdk_message: Any = None
    result: dict[str, Any] | None = None
    done: dict[str, Any] | None = None
    interaction_update: Any = None
    step: Any = None
    result_is_full: bool = False

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> RunStreamEvent:
        if "sdkMessage" in value:
            return cls(
                kind="sdk_message",
                offset=value.get("offset"),
                sdk_message=sdk_message_from_json(_as_mapping(value["sdkMessage"])),
            )
        if "interactionUpdate" in value:
            return cls(
                kind="interaction_update",
                offset=value.get("offset"),
                interaction_update=parse_interaction_update(_as_mapping(value["interactionUpdate"])),
            )
        if "step" in value:
            return cls(
                kind="step",
                offset=value.get("offset"),
                step=parse_conversation_step(_as_mapping(value["step"])),
            )
        if "result" in value:
            result_envelope = _as_mapping(value["result"])
            result = result_envelope.get("result")
            result_is_full = isinstance(result, Mapping)
            return cls(
                kind="result",
                offset=value.get("offset"),
                result=dict(result) if result_is_full else dict(result_envelope),
                result_is_full=result_is_full,
            )
        if "done" in value:
            done = value["done"]
            return cls(
                kind="done",
                offset=value.get("offset"),
                done=dict(done) if isinstance(done, Mapping) else {},
            )
        return cls(kind="unknown", offset=value.get("offset"))


class SDKArtifact(_OfficialDataclassRepr, BaseModel):
    path: str
    size_bytes: int = Field(
        default=0,
        validation_alias=AliasChoices("size_bytes", "sizeBytes"),
        serialization_alias="sizeBytes",
    )
    updated_at: str = Field(
        default="",
        validation_alias=AliasChoices("updated_at", "updatedAt"),
        serialization_alias="updatedAt",
    )

    model_config = {"populate_by_name": True}

    @property
    def sizeBytes(self) -> int:  # noqa: N802
        return self.size_bytes

    @property
    def updatedAt(self) -> str:  # noqa: N802
        return self.updated_at


SdkArtifact = SDKArtifact


class SDKUser(_OfficialDataclassRepr, BaseModel):
    api_key_name: str = Field(alias="apiKeyName")
    created_at: str = Field(alias="createdAt")
    user_id: int | None = Field(default=None, alias="userId")
    user_email: str = Field(default="", alias="userEmail")
    user_first_name: str = Field(default="", alias="userFirstName")
    user_last_name: str = Field(default="", alias="userLastName")

    model_config = {"populate_by_name": True}


class SDKRepository(_OfficialDataclassRepr, BaseModel):
    url: str


class ModelParameterDefinitionValue(_OfficialDataclassRepr, BaseModel):
    value: str
    display_name: str = Field(default="", alias="displayName")

    model_config = {"populate_by_name": True}

    @field_validator("display_name", mode="before")
    @classmethod
    def _display_name(cls, value: object) -> str:
        return "" if value is None else str(value)


ModelParameterDefValue = ModelParameterDefinitionValue


class ModelParameterDefinition(_OfficialDataclassRepr, BaseModel):
    id: str
    display_name: str = Field(default="", alias="displayName")
    values: tuple[ModelParameterDefinitionValue, ...] = ()

    model_config = {"populate_by_name": True}

    @field_validator("display_name", mode="before")
    @classmethod
    def _display_name(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("values", mode="before")
    @classmethod
    def _values(cls, value: object) -> object:
        return () if value is None else value


class ModelVariant(_OfficialDataclassRepr, BaseModel):
    params: tuple[ModelParameterValue, ...] = ()
    display_name: str = Field(default="", alias="displayName")
    description: str = ""
    is_default: bool = Field(default=False, alias="isDefault")

    model_config = {"populate_by_name": True}

    @field_validator("params", mode="before")
    @classmethod
    def _params(cls, value: object) -> object:
        return () if value is None else value

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("is_default", mode="before")
    @classmethod
    def _is_default(cls, value: object) -> bool:
        return bool(value)


class SDKModel(_OfficialDataclassRepr, BaseModel):
    id: str
    display_name: str = Field(alias="displayName")
    description: str = ""
    parameters: tuple[ModelParameterDefinition, ...] = ()
    variants: tuple[ModelVariant, ...] = ()

    model_config = {"populate_by_name": True}

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("parameters", "variants", mode="before")
    @classmethod
    def _seq(cls, value: object) -> object:
        return () if value is None else value


ModelListItem = SDKModel


class SDKAgentInfo(_OfficialDataclassRepr, BaseModel):
    agent_id: str = Field(validation_alias=AliasChoices("agent_id", "agentId"), serialization_alias="agentId")
    name: str
    summary: str
    last_modified: str | None = Field(
        default=None,
        validation_alias=AliasChoices("last_modified", "lastModified"),
        serialization_alias="lastModified",
    )
    status: Literal["running", "finished", "error"] | str | None = None
    created_at: str | None = Field(
        default=None,
        validation_alias=AliasChoices("created_at", "createdAt"),
        serialization_alias="createdAt",
    )
    archived: bool = False
    runtime: Literal["local", "cloud"] | None = None
    cwd: str = ""
    env: Any = None
    repos: Sequence[str] | None = ()
    metadata: dict[str, str] | None = None

    model_config = {"populate_by_name": True}

    @field_validator("last_modified", "created_at", mode="before")
    @classmethod
    def _ts_str(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        return str(value)

    @field_validator("archived", mode="before")
    @classmethod
    def _archived(cls, value: object) -> bool:
        return bool(value)

    @property
    def agentId(self) -> str:  # noqa: N802
        return self.agent_id

    @property
    def lastModified(self) -> int | str | None:  # noqa: N802
        if self.last_modified is None:
            return None
        if self.last_modified.isdigit():
            return int(self.last_modified)
        return self.last_modified

    @property
    def createdAt(self) -> int | str | None:  # noqa: N802
        if self.created_at is None:
            return None
        if self.created_at.isdigit():
            return int(self.created_at)
        return self.created_at


class SDKAgentInfoCloud(SDKAgentInfo):
    runtime: Literal["cloud"] = "cloud"


class SDKAgentInfoLocal(SDKAgentInfo):
    runtime: Literal["local"] = "local"


class ListResult(Generic[T_co]):
    def __init__(
        self,
        items: list[T_co],
        next_cursor: str | None = None,
        nextCursor: str | None = None,
        _get_next_page: Any = None,
    ) -> None:
        self.items = list(items)
        self.next_cursor = next_cursor or nextCursor or ""
        self._get_next_page = _get_next_page

    @property
    def nextCursor(self) -> str | None:  # noqa: N802
        return self.next_cursor or None

    def __repr__(self) -> str:
        return f"ListResult(items={self.items!r}, next_cursor={self.next_cursor!r})"

    def __iter__(self):
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> T_co:
        return self.items[index]

    def to_dict(self) -> dict[str, Any]:
        return {"items": self.items, "next_cursor": self.next_cursor}

    def has_next_page(self) -> bool:
        return bool(self.next_cursor)

    def next_page_info(self) -> dict[str, str]:
        return {"cursor": self.next_cursor} if self.next_cursor else {}

    def get_next_page(self) -> ListResult[T_co]:
        if not self.next_cursor:
            return ListResult(items=[])
        if self._get_next_page is None:
            return ListResult(items=[], next_cursor="")
        return self._get_next_page(self.next_cursor)

    def auto_paging_iter(self):
        page: ListResult[T_co] = self
        while True:
            yield from page.items
            if not page.next_cursor:
                return
            page = page.get_next_page()


class CursorRequestOptions(BaseModel):
    apiKey: str | None = Field(default=None, alias="apiKey")

    model_config = {"populate_by_name": True}


class GetAgentOptions(BaseModel):
    cwd: str | None = None
    apiKey: str | None = Field(default=None, alias="apiKey")
    store: Any | None = None

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}


class AgentOperationOptions(BaseModel):
    cwd: str | None = None
    apiKey: str | None = Field(default=None, alias="apiKey")
    store: Any | None = None

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}


class GetAgentMessagesOptions(BaseModel):
    limit: int | None = None
    offset: int | None = None
    runtime: Literal["local", "cloud"] | None = None
    cwd: str | None = None
    apiKey: str | None = Field(default=None, alias="apiKey")
    store: Any | None = None

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}


class GetUsageOptions(BaseModel):
    runId: str | None = Field(default=None, alias="runId")
    apiKey: str | None = Field(default=None, alias="apiKey")
    cwd: str | None = None
    runtime: Literal["local", "cloud"] | None = None
    store: Any | None = None

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}


class CursorConfigureLocalOptions(BaseModel):
    store: Any | None = None
    useHttp1ForAgent: bool | None = Field(default=None, alias="useHttp1ForAgent")
    workspaceScanCacheTtlMs: int | None = Field(default=None, alias="workspaceScanCacheTtlMs")

    model_config = {"populate_by_name": True, "arbitrary_types_allowed": True}


class CursorConfigureOptions(BaseModel):
    local: CursorConfigureLocalOptions | None = None

    model_config = {"populate_by_name": True}


class GetRunOptionsCloud(BaseModel):
    runtime: Literal["cloud"] = "cloud"
    agentId: str = Field(alias="agentId")
    apiKey: str | None = Field(default=None, alias="apiKey")

    model_config = {"populate_by_name": True}


class GetRunOptionsLocal(BaseModel):
    runtime: Literal["local"] = "local"
    cwd: str | None = None
    store: Any | None = None

    model_config = {"arbitrary_types_allowed": True}


GetRunOptions = GetRunOptionsCloud | GetRunOptionsLocal


class ListAgentsCloudOptions(BaseModel):
    runtime: Literal["cloud"] = "cloud"
    limit: int | None = None
    cursor: str | None = None
    prUrl: str | None = Field(default=None, alias="prUrl")
    includeArchived: bool | None = Field(default=None, alias="includeArchived")
    apiKey: str | None = Field(default=None, alias="apiKey")

    model_config = {"populate_by_name": True}


class ListAgentsLocalOptions(BaseModel):
    runtime: Literal["local"] = "local"
    limit: int | None = None
    cursor: str | None = None
    cwd: str | None = None
    store: Any | None = None

    model_config = {"arbitrary_types_allowed": True}


ListAgentsOptions = ListAgentsCloudOptions | ListAgentsLocalOptions


class ListRunsCloudOptions(BaseModel):
    runtime: Literal["cloud"] = "cloud"
    limit: int | None = None
    cursor: str | None = None
    apiKey: str | None = Field(default=None, alias="apiKey")

    model_config = {"populate_by_name": True}


class ListRunsLocalOptions(BaseModel):
    runtime: Literal["local"] = "local"
    limit: int | None = None
    cursor: str | None = None
    cwd: str | None = None
    store: Any | None = None

    model_config = {"arbitrary_types_allowed": True}


ListRunsOptions = ListRunsCloudOptions | ListRunsLocalOptions

