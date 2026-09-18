# SPDX-License-Identifier: MIT-0
"""Select opencursor or official cursor_sdk for cookbook ports.

Environment:

  OPENCURSOR_LIVE_SDK=opencursor|official
  OPENCURSOR_LIVE_DEBUG=1
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

SDK_ENV = "OPENCURSOR_LIVE_SDK"
DEBUG_ENV = "OPENCURSOR_LIVE_DEBUG"

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "token",
        "secret",
        "password",
        "auth_token",
        "authtoken",
    }
)

_EXPORT_NAMES = (
    "Agent",
    "Cursor",
    "LocalAgentOptions",
    "CloudAgentOptions",
    "CloudRepository",
    "CursorAgentError",
    "SendOptions",
    "LocalSendOptions",
    "ModelSelection",
    "ModelParameterValue",
)

backend: Any
SDK_NAME: str
Agent: Any
Cursor: Any
LocalAgentOptions: Any
CloudAgentOptions: Any
CloudRepository: Any
CursorAgentError: Any
SendOptions: Any
LocalSendOptions: Any
ModelSelection: Any
ModelParameterValue: Any


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def selected_sdk(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    raw = env.get(SDK_ENV, "opencursor").strip().lower()
    if raw in {"official", "cursor_sdk", "cursor-sdk"}:
        return "official"
    return "opencursor"


def debug_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _truthy(env.get(DEBUG_ENV))


def official_root() -> Path:
    return Path(__file__).resolve().parents[2] / "official"


def debug_log(message: str, *, sdk_name: str | None = None) -> None:
    tag = sdk_name or SDK_NAME
    print(f"[cookbook-sdk {tag}] {message}", file=sys.stderr, flush=True)


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


def _is_secret_key(key: str) -> bool:
    compact = key.replace("-", "").replace("_", "").lower()
    return compact in _SECRET_KEYS or "apikey" in compact


def _summarize(value: Any, *, depth: int = 0) -> str:
    if depth > 2:
        return "..."
    if value is None:
        return "None"
    if isinstance(value, (bool, int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.replace("\n", " ")
        if len(text) > 80:
            text = text[:77] + "..."
        return repr(text)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        parts = []
        for key, item in list(value.items())[:8]:
            name = str(key)
            if _is_secret_key(name):
                parts.append(f"{name}=<redacted>")
            else:
                parts.append(f"{name}={_summarize(item, depth=depth + 1)}")
        return "{" + ", ".join(parts) + "}"
    cwd = getattr(value, "cwd", None)
    if cwd is not None:
        return f"{type(value).__name__}(cwd={cwd!r})"
    ident = _attr(value, "id", default=None)
    if ident is not None:
        return f"{type(value).__name__}(id={ident!r})"
    return type(value).__name__


def _summarize_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    parts = [_summarize(item) for item in args]
    for key, value in kwargs.items():
        if _is_secret_key(key):
            parts.append(f"{key}=<redacted>")
        else:
            parts.append(f"{key}={_summarize(value)}")
    return " ".join(parts)


def _agent_id(agent: Any) -> str:
    return str(_attr(agent, "agent_id", "agentId", "id", default=""))


def _run_id(run: Any) -> str:
    return str(_attr(run, "id", "run_id", "runId", default=""))


def _event_type(event: Any) -> str:
    return str(_attr(event, "type", default=type(event).__name__))


def _error_fields(exc: BaseException) -> str:
    retryable = _attr(exc, "is_retryable", "isRetryable", default="")
    message = getattr(exc, "message", None) or str(exc)
    return f"{type(exc).__name__} retryable={retryable}: {message}"


class _DebugRun:
    def __init__(self, inner: Any, *, sdk_name: str) -> None:
        self._inner = inner
        self._sdk_name = sdk_name

    def stream(self) -> Iterator[Any]:
        return self._iter("stream", self._inner.stream())

    def messages(self) -> Iterator[Any]:
        target = getattr(self._inner, "messages", None)
        if target is None:
            target = self._inner.stream
        return self._iter("messages", target())

    def events(self) -> Iterator[Any]:
        return self._iter("events", self._inner.events())

    def _iter(self, label: str, iterator: Any) -> Iterator[Any]:
        debug_log(f"{label} start run_id={_run_id(self._inner)}", sdk_name=self._sdk_name)
        counts: dict[str, int] = {}
        try:
            for event in iterator:
                kind = _event_type(event)
                counts[kind] = counts.get(kind, 0) + 1
                debug_log(f"{label} event type={kind}", sdk_name=self._sdk_name)
                yield event
        except Exception as exc:
            debug_log(f"{label} error {_error_fields(exc)}", sdk_name=self._sdk_name)
            raise
        summary = " ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"
        debug_log(f"{label} done run_id={_run_id(self._inner)} counts={summary}", sdk_name=self._sdk_name)

    def wait(self) -> Any:
        debug_log(f"wait run_id={_run_id(self._inner)}", sdk_name=self._sdk_name)
        try:
            result = self._inner.wait()
        except Exception as exc:
            debug_log(f"wait error {_error_fields(exc)}", sdk_name=self._sdk_name)
            raise
        status = _attr(result, "status", default="")
        duration = _attr(result, "duration_ms", "durationMs", default="")
        usage = _attr(result, "usage", default=None)
        total = _attr(usage, "total_tokens", "totalTokens", default="")
        debug_log(
            f"wait status={status} duration_ms={duration} usage.total_tokens={total}",
            sdk_name=self._sdk_name,
        )
        return result

    def cancel(self) -> Any:
        debug_log(f"cancel run_id={_run_id(self._inner)}", sdk_name=self._sdk_name)
        try:
            return self._inner.cancel()
        except Exception as exc:
            debug_log(f"cancel error {_error_fields(exc)}", sdk_name=self._sdk_name)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _DebugAgent:
    def __init__(self, inner: Any, *, sdk_name: str) -> None:
        self._inner = inner
        self._sdk_name = sdk_name

    def send(self, message: Any = None, options: Any = None, *args: Any, **kwargs: Any) -> Any:
        prompt = message if message is not None else (args[0] if args else "")
        chars = len(prompt) if isinstance(prompt, str) else "n/a"
        debug_log(
            f"send agent_id={_agent_id(self._inner)} prompt_chars={chars}",
            sdk_name=self._sdk_name,
        )
        try:
            if options is None and not args:
                run = self._inner.send(message, **kwargs) if message is not None else self._inner.send(**kwargs)
            elif options is None:
                run = self._inner.send(message, *args, **kwargs)
            else:
                run = self._inner.send(message, options, *args, **kwargs)
        except Exception as exc:
            debug_log(f"send error {_error_fields(exc)}", sdk_name=self._sdk_name)
            raise
        debug_log(f"send run_id={_run_id(run)}", sdk_name=self._sdk_name)
        return wrap_debug_run(run, sdk_name=self._sdk_name)

    def close(self) -> Any:
        closer = getattr(self._inner, "close", None)
        if closer is None:
            closer = getattr(self._inner, "aclose", None)
        if closer is None:
            return None
        debug_log(f"close agent_id={_agent_id(self._inner)}", sdk_name=self._sdk_name)
        return closer()

    def __enter__(self) -> _DebugAgent:
        entered = self._inner.__enter__()
        if entered is not self._inner:
            self._inner = entered
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Any:
        return self._inner.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _DebugModels:
    def __init__(self, inner: Any, *, sdk_name: str) -> None:
        self._inner = inner
        self._sdk_name = sdk_name

    def list(self, *args: Any, **kwargs: Any) -> Any:
        debug_log("Cursor.models.list", sdk_name=self._sdk_name)
        try:
            result = self._inner.list(*args, **kwargs)
        except Exception as exc:
            debug_log(f"Cursor.models.list error {_error_fields(exc)}", sdk_name=self._sdk_name)
            raise
        count = len(result) if hasattr(result, "__len__") else "?"
        debug_log(f"Cursor.models.list count={count}", sdk_name=self._sdk_name)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _DebugRepositories:
    def __init__(self, inner: Any, *, sdk_name: str) -> None:
        self._inner = inner
        self._sdk_name = sdk_name

    def list(self, *args: Any, **kwargs: Any) -> Any:
        debug_log("Cursor.repositories.list", sdk_name=self._sdk_name)
        try:
            result = self._inner.list(*args, **kwargs)
        except Exception as exc:
            debug_log(f"Cursor.repositories.list error {_error_fields(exc)}", sdk_name=self._sdk_name)
            raise
        count = len(result) if hasattr(result, "__len__") else "?"
        debug_log(f"Cursor.repositories.list count={count}", sdk_name=self._sdk_name)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _DebugCursor:
    def __init__(self, inner: Any, *, sdk_name: str) -> None:
        self._inner = inner
        self._sdk_name = sdk_name
        models = getattr(inner, "models", None)
        repos = getattr(inner, "repositories", None)
        self.models = _DebugModels(models, sdk_name=sdk_name) if models is not None else models
        self.repositories = (
            _DebugRepositories(repos, sdk_name=sdk_name) if repos is not None else repos
        )

    def me(self, *args: Any, **kwargs: Any) -> Any:
        debug_log("Cursor.me", sdk_name=self._sdk_name)
        try:
            result = self._inner.me(*args, **kwargs)
        except Exception as exc:
            debug_log(f"Cursor.me error {_error_fields(exc)}", sdk_name=self._sdk_name)
            raise
        debug_log("Cursor.me ok", sdk_name=self._sdk_name)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_debug_run(run: Any, *, sdk_name: str | None = None) -> Any:
    if not debug_enabled() or isinstance(run, _DebugRun):
        return run
    return _DebugRun(run, sdk_name=sdk_name or SDK_NAME)


def wrap_debug_agent(agent: Any, *, sdk_name: str | None = None) -> Any:
    if not debug_enabled() or isinstance(agent, _DebugAgent):
        return agent
    return _DebugAgent(agent, sdk_name=sdk_name or SDK_NAME)


def _call_factory(label: str, func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], *, wrap_agent: bool) -> Any:
    debug_log(f"{label} {_summarize_call(args, kwargs)}".rstrip())
    try:
        result = func(*args, **kwargs)
    except Exception as exc:
        debug_log(f"{label} error {_error_fields(exc)}")
        raise
    if wrap_agent:
        agent_id = _agent_id(result)
        prefix = "cloud" if str(agent_id).startswith("bc-") else "local"
        debug_log(f"created agent_id={agent_id} prefix={prefix}")
        return wrap_debug_agent(result)
    debug_log(f"{label} ok")
    return result


class _AgentProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(backend.Agent, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        if not debug_enabled():
            return backend.Agent.create(*args, **kwargs)
        return _call_factory("create", backend.Agent.create, args, kwargs, wrap_agent=True)

    def resume(self, *args: Any, **kwargs: Any) -> Any:
        if not debug_enabled():
            return backend.Agent.resume(*args, **kwargs)
        return _call_factory("resume", backend.Agent.resume, args, kwargs, wrap_agent=True)

    def prompt(self, *args: Any, **kwargs: Any) -> Any:
        if not debug_enabled():
            return backend.Agent.prompt(*args, **kwargs)
        return _call_factory("prompt", backend.Agent.prompt, args, kwargs, wrap_agent=False)

    def list(self, *args: Any, **kwargs: Any) -> Any:
        func = backend.Agent.list
        if not debug_enabled():
            return func(*args, **kwargs)
        return _call_factory("list", func, args, kwargs, wrap_agent=False)

    def list_runs(self, *args: Any, **kwargs: Any) -> Any:
        func = getattr(backend.Agent, "list_runs", None) or backend.Agent.listRuns
        if not debug_enabled():
            return func(*args, **kwargs)
        return _call_factory("list_runs", func, args, kwargs, wrap_agent=False)

    def listRuns(self, *args: Any, **kwargs: Any) -> Any:  # noqa: N802
        return self.list_runs(*args, **kwargs)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        if not debug_enabled():
            return backend.Agent.get(*args, **kwargs)
        return _call_factory("get", backend.Agent.get, args, kwargs, wrap_agent=False)

    def get_run(self, *args: Any, **kwargs: Any) -> Any:
        func = getattr(backend.Agent, "get_run", None) or backend.Agent.getRun
        if not debug_enabled():
            return func(*args, **kwargs)
        return _call_factory("get_run", func, args, kwargs, wrap_agent=False)

    def getRun(self, *args: Any, **kwargs: Any) -> Any:  # noqa: N802
        return self.get_run(*args, **kwargs)


def _import_official() -> Any:
    root = official_root()
    path = str(root)
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import cursor_sdk
    except Exception as exc:
        debug_log(f"import cursor_sdk error {type(exc).__name__}: {exc}", sdk_name="official")
        raise
    return cursor_sdk


def _import_opencursor() -> Any:
    import opencursor

    return opencursor


def _export_name(module: Any, name: str) -> Any:
    try:
        return getattr(module, name)
    except AttributeError:
        types_mod = getattr(module, "types", None)
        if types_mod is not None and hasattr(types_mod, name):
            return getattr(types_mod, name)
        errors_mod = getattr(module, "errors", None)
        if errors_mod is not None and hasattr(errors_mod, name):
            return getattr(errors_mod, name)
        debug_log(f"missing export {name} AttributeError")
        raise


def load_backend(environ: dict[str, str] | None = None) -> Any:
    name = selected_sdk(environ)
    if name == "official":
        return _import_official()
    return _import_opencursor()


def configure(environ: dict[str, str] | None = None) -> str:
    """Load the selected SDK into this module's public names. Returns SDK_NAME."""
    global backend, SDK_NAME, Agent, Cursor
    global LocalAgentOptions, CloudAgentOptions, CloudRepository
    global CursorAgentError, SendOptions, LocalSendOptions
    global ModelSelection, ModelParameterValue

    SDK_NAME = selected_sdk(environ)
    backend = load_backend(environ)
    raw_agent = _export_name(backend, "Agent")
    raw_cursor = _export_name(backend, "Cursor")
    Agent = _AgentProxy() if debug_enabled(environ) else raw_agent
    Cursor = _DebugCursor(raw_cursor, sdk_name=SDK_NAME) if debug_enabled(environ) else raw_cursor
    LocalAgentOptions = _export_name(backend, "LocalAgentOptions")
    CloudAgentOptions = _export_name(backend, "CloudAgentOptions")
    CloudRepository = _export_name(backend, "CloudRepository")
    CursorAgentError = _export_name(backend, "CursorAgentError")
    SendOptions = _export_name(backend, "SendOptions")
    LocalSendOptions = _export_name(backend, "LocalSendOptions")
    ModelSelection = _export_name(backend, "ModelSelection")
    ModelParameterValue = _export_name(backend, "ModelParameterValue")
    return SDK_NAME


configure()

__all__ = [
    "SDK_ENV",
    "DEBUG_ENV",
    "SDK_NAME",
    "backend",
    "selected_sdk",
    "debug_enabled",
    "debug_log",
    "configure",
    "wrap_debug_agent",
    "wrap_debug_run",
    *_EXPORT_NAMES,
]
