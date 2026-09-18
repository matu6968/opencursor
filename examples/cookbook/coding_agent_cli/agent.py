# SPDX-License-Identifier: MIT-0
"""Coding-agent session over the cookbook SDK shim.

Mirrors cookbook/sdk/coding-agent-cli/src/agent.ts.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cookbook._sdk import (
    Agent,
    CloudAgentOptions,
    CloudRepository,
    Cursor,
    LocalAgentOptions,
    LocalSendOptions,
    SendOptions,
)

AgentEvent = dict[str, Any]
ExecutionMode = Literal["cloud", "local"]


@dataclass
class ModelChoice:
    label: str
    value: Any
    description: str | None = None


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class CancelRunResult:
    cancelled: bool
    reason: str | None = None


@dataclass
class CloudRepo:
    url: str
    starting_ref: str | None = None


AGENT_INSTRUCTIONS = "\n".join(
    [
        "You are a lightweight coding agent running from a terminal.",
        "Work in the configured workspace.",
        "Help the user inspect, edit, and validate code with small focused changes.",
        "Before changing files, understand the surrounding code and preserve unrelated user work.",
        "Keep progress updates concise and summarize the result clearly.",
    ]
)


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


def build_prompt(prompt: str) -> str:
    return "\n".join([AGENT_INSTRUCTIONS, "", "User task:", prompt])


def format_model_label(model: Any) -> str:
    model_id = _attr(model, "id", default="")
    params = _attr(model, "params", default=None) or []
    values = [str(_attr(param, "value", default="")) for param in params if _attr(param, "value")]
    values = [item for item in values if item]
    return f"{model_id} ({', '.join(values)})" if values else str(model_id)


def format_duration(ms: int | float) -> str:
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.1f}s"


def model_selection_key(selection: Any) -> str:
    model_id = str(_attr(selection, "id", default=""))
    params = list(_attr(selection, "params", default=None) or [])
    params.sort(key=lambda item: str(_attr(item, "id", default="")))
    encoded = "&".join(
        f"{_attr(item, 'id')}={_attr(item, 'value')}" for item in params if _attr(item, "id")
    )
    return f"{model_id}?{encoded}" if encoded else model_id


def run_git(cwd: str, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def normalize_github_remote(remote: str) -> str | None:
    trimmed = remote.strip().removesuffix(".git")
    ssh_match = None
    for pattern, prefix in (
        ("git@github.com:", "git@github.com:"),
        ("ssh://git@github.com/", "ssh://git@github.com/"),
        ("https://github.com/", "https://github.com/"),
    ):
        if trimmed.startswith(prefix):
            ssh_match = trimmed[len(prefix) :]
            break
    return f"https://github.com/{ssh_match}" if ssh_match else None


def detect_cloud_repository(cwd: str) -> CloudRepo:
    remote = run_git(cwd, ["config", "--get", "remote.origin.url"])
    if not remote:
        raise RuntimeError("Cloud mode requires a git repository with remote.origin.url set.")
    url = normalize_github_remote(remote)
    if not url:
        raise RuntimeError("Cloud mode currently expects remote.origin.url to point at GitHub.")
    branch = run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    starting_ref = branch if branch and branch != "HEAD" else None
    return CloudRepo(url=url, starting_ref=starting_ref)


def format_cloud_repository(repository: CloudRepo) -> str:
    return f"{repository.url}#{repository.starting_ref}" if repository.starting_ref else repository.url


def labels_match(left: str, right: str) -> bool:
    return _normalize_label(left) == _normalize_label(right)


def _normalize_label(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def label_from_id(ident: str) -> str:
    parts = [part for part in ident.replace("-", " ").replace("_", " ").split() if part]
    return " ".join(part[:1].upper() + part[1:] for part in parts)


def _model_to_choices(model: Any) -> list[ModelChoice]:
    base_label = str(_attr(model, "display_name", "displayName", default="") or _attr(model, "id"))
    variants = list(_attr(model, "variants", default=()) or ())
    description = _attr(model, "description", default=None)
    model_id = _attr(model, "id")
    if not variants:
        return [ModelChoice(label=base_label, value={"id": model_id}, description=description)]
    choices = []
    for variant in variants:
        variant_name = str(_attr(variant, "display_name", "displayName", default="") or "")
        label = base_label
        if variant_name.strip() and not labels_match(base_label, variant_name):
            label = f"{base_label} - {variant_name.strip()}"
        choices.append(
            ModelChoice(
                label=label,
                value={"id": model_id, "params": _attr(variant, "params", default=[])},
                description=_attr(variant, "description", default=description),
            )
        )
    return _disambiguate_duplicate_labels(_dedupe_model_choices(choices), model)


def _disambiguate_duplicate_labels(choices: list[ModelChoice], model: Any) -> list[ModelChoice]:
    counts: dict[str, int] = {}
    for choice in choices:
        counts[choice.label] = counts.get(choice.label, 0) + 1
    result: list[ModelChoice] = []
    for choice in choices:
        if counts.get(choice.label, 0) <= 1:
            result.append(choice)
            continue
        params_label = _format_params_label(_attr(choice.value, "params") if not isinstance(choice.value, dict) else choice.value.get("params") or [], model)
        if isinstance(choice.value, dict):
            params = choice.value.get("params") or []
            params_label = _format_params_label(params, model)
        result.append(
            ModelChoice(label=f"{choice.label} - {params_label}" if params_label else choice.label, value=choice.value, description=choice.description)
            if params_label
            else choice
        )
    return result


def _dedupe_model_choices(choices: list[ModelChoice]) -> list[ModelChoice]:
    by_key: dict[str, ModelChoice] = {}
    for choice in choices:
        key = model_selection_key(choice.value)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = choice
            continue
        by_key[key] = ModelChoice(
            label=existing.label,
            value=existing.value,
            description=existing.description or choice.description,
        )
    return list(by_key.values())


def _disambiguate_global_duplicate_labels(choices: list[ModelChoice]) -> list[ModelChoice]:
    counts: dict[str, int] = {}
    for choice in choices:
        counts[choice.label] = counts.get(choice.label, 0) + 1
    readable: set[str] = set()
    result: list[ModelChoice] = []
    for choice in choices:
        label = choice.label
        if counts.get(label, 0) > 1:
            detail = _selection_detail(choice.value)
            if detail and not labels_match(label, detail):
                label = f"{label} - {detail}"
        key = _normalize_label(label)
        if key in readable:
            continue
        readable.add(key)
        result.append(ModelChoice(label=label, value=choice.value, description=choice.description))
    return result


def _selection_detail(selection: Any) -> str:
    params = _attr(selection, "params", default=None)
    if isinstance(selection, dict):
        params = selection.get("params")
    if params:
        labels = [label_from_id(str(_attr(param, "value", default=""))) for param in params]
        return ", ".join(item for item in labels if item)
    return str(_attr(selection, "id", default=selection.get("id") if isinstance(selection, dict) else ""))


def _format_params_label(params: Any, model: Any) -> str:
    parts: list[str] = []
    parameters = list(_attr(model, "parameters", default=()) or ())
    for param in params or []:
        param_id = _attr(param, "id")
        param_value = _attr(param, "value")
        parameter = next((item for item in parameters if _attr(item, "id") == param_id), None)
        values = list(_attr(parameter, "values", default=()) or ()) if parameter else []
        value = next((item for item in values if _attr(item, "value") == param_value), None)
        parameter_label = str(
            _attr(parameter, "display_name", "displayName", default="") or label_from_id(str(param_id or ""))
        )
        value_label = str(
            _attr(value, "display_name", "displayName", default="") or label_from_id(str(param_value or ""))
        )
        if labels_match(parameter_label, value_label):
            parts.append(value_label)
        else:
            parts.append(f"{parameter_label}: {value_label}")
    return ", ".join(item for item in parts if item)


def _summarize_tool_args(tool_name: str, args: Any) -> str | None:
    if not args or not isinstance(args, Mapping):
        return None
    parts: list[str] = []
    for keys in _tool_summary_keys(tool_name):
        part = _summarize_first_value(args, keys)
        if part:
            parts.append(part)
    return " ".join(parts) if parts else None


def _tool_summary_keys(tool_name: str) -> list[list[str]]:
    name = tool_name.lower()
    if "read" in name:
        return [["path", "filePath", "target_file", "absolutePath"], ["offset"], ["limit"]]
    if "glob" in name:
        return [["pattern", "glob", "glob_pattern"], ["path", "cwd", "target_directory"]]
    if "grep" in name or "search" in name:
        return [["pattern", "query"], ["path"], ["glob"], ["type"]]
    if "shell" in name or "terminal" in name or "command" in name:
        return [["command", "cmd"], ["cwd", "working_directory"]]
    if "edit" in name or "write" in name or "patch" in name:
        return [["path", "target_file", "file"], ["instruction"]]
    return [["path", "file", "target_file"], ["pattern", "query", "command"]]


def _summarize_first_value(record: Mapping[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        formatted = _format_arg_value(record.get(key))
        if formatted:
            return f"{key}={formatted}"
    return None


def _format_arg_value(value: Any) -> str | None:
    if isinstance(value, str):
        return _shorten(value.replace("\n", " ").strip())
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        items = [item for item in (_format_arg_value(entry) for entry in value[:3]) if item]
        return f"[{','.join(items)}]" if items else None
    return None


def _shorten(value: str, max_length: int = 80) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."


def emit_sdk_message(event: Any, emit: Callable[[AgentEvent], None]) -> None:
    kind = _attr(event, "type", default="")
    if kind == "assistant":
        message = _attr(event, "message")
        content = _attr(message, "content", default=()) or ()
        for block in content:
            if _attr(block, "type") == "text":
                emit({"type": "assistant_delta", "text": _attr(block, "text", default="")})
            else:
                emit(
                    {
                        "type": "tool",
                        "callId": _attr(block, "id"),
                        "name": _attr(block, "name", default=""),
                        "params": _summarize_tool_args(
                            str(_attr(block, "name", default="")),
                            _attr(block, "input"),
                        ),
                        "status": "requested",
                    }
                )
        return
    if kind == "thinking":
        emit({"type": "thinking", "text": _attr(event, "text", default="")})
        return
    if kind == "tool_call":
        emit(
            {
                "type": "tool",
                "callId": _attr(event, "call_id", "callId"),
                "name": _attr(event, "name", default=""),
                "params": _summarize_tool_args(str(_attr(event, "name", default="")), _attr(event, "args")),
                "status": _attr(event, "status", default=""),
            }
        )
        return
    if kind == "status":
        emit({"type": "status", "status": _attr(event, "status", default=""), "message": _attr(event, "message")})
        return
    if kind == "task":
        emit({"type": "task", "status": _attr(event, "status"), "text": _attr(event, "text")})


class CodingAgentSession:
    def __init__(
        self,
        *,
        api_key: str,
        cwd: str,
        model: Any,
        force: bool = False,
        execution_mode: ExecutionMode = "local",
    ) -> None:
        self.api_key = api_key
        self.cwd = str(Path(cwd).resolve())
        self.force = force
        self.mode: ExecutionMode = execution_mode
        self.model_selection = model if not isinstance(model, str) else {"id": model}
        self.cloud_repository: CloudRepo | None = None
        self.current_run: Any = None
        self.agent = self._create_agent()
        self.agent_key = self._current_agent_key()

    @property
    def model(self) -> Any:
        return self.model_selection

    @property
    def execution_mode(self) -> ExecutionMode:
        return self.mode

    @property
    def execution_target(self) -> str:
        if self.mode == "local":
            return self.cwd
        repo = self.cloud_repository or detect_cloud_repository(self.cwd)
        return format_cloud_repository(repo)

    def set_model(self, model: Any) -> None:
        self.model_selection = model

    def list_models(self) -> list[ModelChoice]:
        models = Cursor.models.list(api_key=self.api_key)
        items = list(models) if not isinstance(models, list) else models
        if hasattr(models, "items") and not isinstance(models, list):
            maybe = _attr(models, "items", default=None)
            if maybe is not None:
                items = list(maybe)
        choices = _disambiguate_global_duplicate_labels(
            _dedupe_model_choices([choice for model in items for choice in _model_to_choices(model)])
        )
        if choices:
            return choices
        model_id = _attr(self.model_selection, "id", default=self.model_selection)
        return [ModelChoice(label=str(model_id), value=self.model_selection)]

    def reset(self) -> None:
        self._replace_agent()

    def set_execution_mode(self, mode: ExecutionMode) -> None:
        if self.current_run is not None:
            raise RuntimeError("Wait for the current run to finish before switching execution mode.")
        if self.mode == mode:
            return
        previous = self.mode
        self.mode = mode
        try:
            self._replace_agent()
        except Exception:
            self.mode = previous
            raise

    def dispose(self) -> None:
        closer = getattr(self.agent, "close", None)
        if closer is not None:
            closer()

    def cancel_current_run(self) -> CancelRunResult:
        run = self.current_run
        if run is None:
            return CancelRunResult(cancelled=False, reason="No active run to cancel.")
        supports = getattr(run, "supports", None)
        if supports is not None and not supports("cancel"):
            reason_fn = getattr(run, "unsupported_reason", None) or getattr(run, "unsupportedReason", None)
            reason = reason_fn("cancel") if reason_fn else "This run cannot be cancelled."
            return CancelRunResult(cancelled=False, reason=reason)
        run.cancel()
        return CancelRunResult(cancelled=True)

    def send_prompt(self, prompt: str, on_event: Callable[[AgentEvent], None]) -> Any:
        self._ensure_agent_fresh()
        send_kwargs: dict[str, Any] = {}
        if self.mode == "local":
            send_kwargs["model"] = self.model_selection
            if self.force:
                send_kwargs["local"] = LocalSendOptions(force=True)
        try:
            run = self.agent.send(build_prompt(prompt), SendOptions(**send_kwargs) if send_kwargs else None)
        except TypeError:
            run = self.agent.send(build_prompt(prompt), send_kwargs or None)
        self.current_run = run
        try:
            for event in run.stream():
                emit_sdk_message(event, on_event)
            result = run.wait()
            usage = _attr(result, "usage")
            on_event(
                {
                    "type": "result",
                    "status": _attr(result, "status", default=""),
                    "durationMs": _attr(result, "duration_ms", "durationMs"),
                    "usage": {
                        "inputTokens": _attr(usage, "input_tokens", "inputTokens"),
                        "outputTokens": _attr(usage, "output_tokens", "outputTokens"),
                    }
                    if usage is not None
                    else None,
                }
            )
            return result
        finally:
            if self.current_run is run:
                self.current_run = None

    def _create_agent(self) -> Any:
        options: dict[str, Any] = {
            "api_key": self.api_key,
            "name": "Lightweight coding agent",
            "model": self.model_selection,
        }
        if self.mode == "cloud":
            repository = detect_cloud_repository(self.cwd)
            self.cloud_repository = repository
            cloud = CloudRepository(url=repository.url, starting_ref=repository.starting_ref)
            return Agent.create(**options, cloud=CloudAgentOptions(repos=[cloud]))
        self.cloud_repository = None
        return Agent.create(**options, local=LocalAgentOptions(cwd=self.cwd))

    def _ensure_agent_fresh(self) -> None:
        if self.agent_key != self._current_agent_key():
            self._replace_agent()

    def _replace_agent(self) -> None:
        previous = self.agent
        self.agent = self._create_agent()
        self.agent_key = self._current_agent_key()
        closer = getattr(previous, "close", None)
        if closer is not None:
            closer()

    def _current_agent_key(self) -> str:
        model_key = model_selection_key(self.model_selection) if self.mode == "cloud" else None
        return json.dumps({"mode": self.mode, "model": model_key})
