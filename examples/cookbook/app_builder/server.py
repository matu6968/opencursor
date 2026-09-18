# SPDX-License-Identifier: MIT-0
"""SDK layer for the app-builder cookbook example.

Mirrors cookbook/sdk/app-builder/src/lib/app-builder/server.ts (no Next.js / Vite).
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cookbook._sdk import Agent, Cursor, LocalAgentOptions

APP_BUILDER_INSTRUCTIONS = [
    "You are building a local Vite React TypeScript application for a live preview product.",
    "Edit files directly in the current workspace. The dev server is already running and hot reloads when files change.",
    "Keep changes focused on the user's requested app. Prefer small, working iterations over broad rewrites.",
    "Do not start another long-running dev server unless the existing preview server is broken.",
]

FALLBACK_MODELS: list[dict[str, Any]] = [
    {"id": "auto", "label": "Auto", "parameters": [], "defaultParams": []},
    {"id": "composer-2", "label": "Composer 2", "parameters": [], "defaultParams": []},
]


class InvalidCursorApiKeyError(Exception):
    code = "invalid_api_key"


class UnknownAppBuilderSessionError(Exception):
    code = "unknown_session"


@dataclass
class BuilderSession:
    id: str
    api_key: str
    project_path: str
    models: list[dict[str, Any]] = field(default_factory=list)
    user: dict[str, Any] | None = None
    agent: Any = None


_SESSIONS: dict[str, BuilderSession] = {}


def _attr(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            item = value[name]
            if item is not None:
                return item
        if hasattr(value, name):
            item = getattr(value, name)
            if item is not None:
                return item
    return default


def validate_cursor_api_key(api_key: str) -> None:
    try:
        Cursor.me(api_key=api_key)
    except Exception as exc:
        raise InvalidCursorApiKeyError(
            "The Cursor API key could not be validated. Please check the key and try again."
        ) from exc


def get_current_user(api_key: str) -> dict[str, Any] | None:
    try:
        user = Cursor.me(api_key=api_key)
    except Exception:
        return None
    name = (
        _attr(user, "name", "display_name", "displayName", "username")
        or _attr(user, "api_key_name", "apiKeyName", "user_email", "userEmail")
        or "Cursor user"
    )
    email = _attr(user, "email", "user_email", "userEmail")
    return {"name": str(name), "email": str(email) if email else None}


def label_from_id(ident: str) -> str:
    parts = [part for part in ident.replace("-", " ").replace("_", " ").split() if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def list_models(api_key: str) -> list[dict[str, Any]]:
    try:
        models = Cursor.models.list(api_key=api_key)
        catalog = [_model_to_catalog_item(model) for model in models]
        seen: dict[str, dict[str, Any]] = {}
        for item in catalog:
            seen.setdefault(item["id"], item)
        return list(seen.values()) or list(FALLBACK_MODELS)
    except Exception:
        return list(FALLBACK_MODELS)


def _model_to_catalog_item(model: Any) -> dict[str, Any]:
    return {
        "id": _attr(model, "id"),
        "label": _attr(model, "display_name", "displayName") or _attr(model, "id"),
        "description": _attr(model, "description"),
        "parameters": _build_model_parameters(model),
        "defaultParams": _default_model_params(model),
    }


def _build_model_parameters(model: Any) -> list[dict[str, Any]]:
    parameters: dict[str, dict[str, Any]] = {}
    for parameter in _attr(model, "parameters", default=()) or ():
        parameters[_attr(parameter, "id")] = {
            "id": _attr(parameter, "id"),
            "label": _attr(parameter, "display_name", "displayName") or label_from_id(str(_attr(parameter, "id"))),
            "values": [
                {
                    "id": _attr(value, "value"),
                    "label": _attr(value, "display_name", "displayName") or label_from_id(str(_attr(value, "value"))),
                }
                for value in _attr(parameter, "values", default=()) or ()
            ],
        }
    return [item for item in parameters.values() if item["values"]]


def _default_model_params(model: Any) -> list[dict[str, str]]:
    variants = list(_attr(model, "variants", default=()) or ())
    default = next((item for item in variants if _attr(item, "is_default", "isDefault")), None)
    if default is None and variants:
        default = variants[0]
    params = _attr(default, "params", default=()) if default is not None else ()
    return [{"id": str(_attr(param, "id")), "value": str(_attr(param, "value"))} for param in params or ()]


def create_session(api_key: str, *, project_path: str | None = None) -> dict[str, Any]:
    validate_cursor_api_key(api_key)
    session_id = str(uuid.uuid4())
    path = project_path or str(Path.home() / ".app-builder" / "sessions" / session_id / "app")
    Path(path).mkdir(parents=True, exist_ok=True)
    session = BuilderSession(
        id=session_id,
        api_key=api_key,
        project_path=path,
        models=list_models(api_key),
        user=get_current_user(api_key),
    )
    _SESSIONS[session_id] = session
    return public_session(session)


def get_session(session_id: str) -> BuilderSession:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise UnknownAppBuilderSessionError("Unknown app builder session. Create a new session first.")
    return session


def public_session(session: BuilderSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "projectPath": session.project_path,
        "models": session.models,
        "user": session.user,
    }


def get_or_create_agent(session: BuilderSession) -> Any:
    if session.agent is not None:
        return session.agent
    session.agent = Agent.create(
        api_key=session.api_key,
        model=os.environ.get("CURSOR_MODEL") or "composer-2",
        local=LocalAgentOptions(cwd=session.project_path),
    )
    return session.agent


def build_prompt(user_message: str, session: BuilderSession) -> str:
    return "\n".join(
        [
            *APP_BUILDER_INSTRUCTIONS,
            "",
            f"Workspace: {session.project_path}",
            "",
            "User request:",
            user_message,
        ]
    )


def emit_sdk_message(event: Any, emit: Callable[[dict[str, Any]], None]) -> None:
    kind = _attr(event, "type")
    if kind == "assistant":
        message = _attr(event, "message")
        for block in _attr(message, "content", default=()) or ():
            if _attr(block, "type") == "text":
                emit({"type": "assistant_delta", "text": _attr(block, "text", default="")})
            else:
                emit(
                    {
                        "type": "tool_call",
                        "callId": _attr(block, "id"),
                        "name": _attr(block, "name"),
                        "status": "requested",
                        "args": _attr(block, "input"),
                    }
                )
        return
    if kind == "thinking":
        emit({"type": "thinking", "id": _attr(event, "id"), "text": _attr(event, "text", default="")})
        return
    if kind == "tool_call":
        emit(
            {
                "type": "tool_call",
                "callId": _attr(event, "call_id", "callId"),
                "name": _attr(event, "name"),
                "status": _attr(event, "status"),
                "args": _attr(event, "args"),
            }
        )
        return
    if kind == "status":
        emit({"type": "status", "status": _attr(event, "status"), "message": _attr(event, "message")})
        return
    if kind == "task":
        emit({"type": "task", "status": _attr(event, "status"), "text": _attr(event, "text")})


def stream_agent_response(
    session_id: str,
    user_message: str,
    emit: Callable[[dict[str, Any]], None],
    *,
    model: str | None = None,
) -> Any:
    session = get_session(session_id)
    agent = get_or_create_agent(session)
    options = {"model": {"id": model}} if model else None
    run = agent.send(build_prompt(user_message, session), options)
    for event in run.stream():
        emit_sdk_message(event, emit)
    return run.wait()


def extract_project_name_xml(value: str) -> str:
    match = re.search(r"<projectName>\s*([\s\S]*?)\s*</projectName>", value, re.I)
    return match.group(1).strip() if match else ""


def sanitize_project_name(value: str) -> str:
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    if not first_line:
        return ""
    cleaned = re.sub(r"^project\s*name\s*:\s*", "", first_line, flags=re.I)
    cleaned = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[`*_#>]+", "", cleaned)
    cleaned = re.sub(r'^[-\s"\']+|[-\s"\'.:]+$', "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    words = " ".join(cleaned.split(" ")[:5])
    return f"{words[:39].strip()}..." if len(words) > 42 else words


def generate_fallback_project_name(prompt: str, messages: list[dict[str, str]] | None = None) -> str:
    source = next((item["content"] for item in (messages or []) if item.get("role") == "user"), prompt)
    cleaned = sanitize_project_name(
        re.sub(
            r"\b(?:a|an|and|app|application|build|create|for|i|me|my|of|please|project|the|to|want|with)\b",
            " ",
            re.sub(r"[^a-z0-9\s-]", " ", re.sub(r"https?://\S+", "", source, flags=re.I), flags=re.I),
            flags=re.I,
        )
    )
    return cleaned or "Untitled App"


def generate_project_name(api_key: str, context: str | dict[str, Any]) -> str:
    if isinstance(context, str):
        prompt = context.strip()
        messages: list[dict[str, str]] = []
    else:
        prompt = str(context.get("prompt") or "").strip()
        messages = list(context.get("messages") or [])
    if not prompt and not messages:
        raise ValueError("Conversation context is required to generate a project name.")
    try:
        with Agent.create(api_key=api_key, model=os.environ.get("CURSOR_PROJECT_NAME_MODEL") or "composer-2") as agent:
            run = agent.send(_project_name_prompt(prompt, messages))
            assistant = ""
            for event in run.stream():
                if _attr(event, "type") != "assistant":
                    continue
                for block in _attr(_attr(event, "message"), "content", default=()) or ():
                    if _attr(block, "type") == "text":
                        assistant += str(_attr(block, "text") or "")
            result = run.wait()
            raw = extract_project_name_xml(assistant or str(_attr(result, "result") or ""))
            sanitized = sanitize_project_name(raw)
            if sanitized:
                return sanitized
    except Exception:
        pass
    return generate_fallback_project_name(prompt, messages)


def _project_name_prompt(prompt: str, messages: list[dict[str, str]]) -> str:
    lines = [
        "You name app-builder projects.",
        "Create a concise sidebar project name for this app-building conversation.",
        "Rules:",
        "- Return exactly one XML tag: <projectName>Concise Name</projectName>.",
        "- The project name inside the tag must use 2 to 5 words.",
        "- No quotes, markdown, emoji, trailing punctuation, or generic words like Project inside the tag.",
        "",
    ]
    if messages:
        lines.append("Conversation:")
        lines.extend(f"{'User' if item.get('role') == 'user' else 'Assistant'}: {item.get('content', '')}" for item in messages)
    elif prompt:
        lines.extend(["Initial user request:", prompt[:1200]])
    return "\n".join(lines)


def close_session(session_id: str) -> None:
    session = _SESSIONS.pop(session_id, None)
    if session is None:
        return
    closer = getattr(session.agent, "close", None) if session.agent is not None else None
    if closer is not None:
        closer()
