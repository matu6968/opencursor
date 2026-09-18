# SPDX-License-Identifier: MIT-0
"""SDK layer for the agent-kanban cookbook example.

Mirrors cookbook/sdk/agent-kanban/src/lib/agents/server.ts (no React/Next.js).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from cookbook._sdk import Agent, CloudAgentOptions, CloudRepository, Cursor

_REPO_CACHE_TTL_MS = 55_000
_repository_cache: dict[str, dict[str, Any]] = {}


class InvalidCursorApiKeyError(Exception):
    code = "invalid_api_key"


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


def _as_record(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    dumped = getattr(value, "model_dump", None)
    if callable(dumped):
        try:
            return dict(dumped(by_alias=True))
        except TypeError:
            return dict(dumped())
    data: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        item = getattr(value, name)
        if callable(item):
            continue
        data[name] = item
    return data


def _first_string(record: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        nested = getattr(record.get("__obj__"), key, None) if False else None
        _ = nested
    for key in keys:
        value = record.get(key)
        if value is not None and not isinstance(value, (dict, list)) and str(value).strip():
            if isinstance(value, bool):
                continue
            return str(value).strip()
    return None


def _extract_array(value: Any, keys: list[str]) -> list[Any]:
    if isinstance(value, list):
        return value
    items = getattr(value, "items", None)
    if isinstance(items, list):
        return items
    if callable(items) and not isinstance(value, dict):
        # list-like result objects expose .items as the collection, not dict.items
        maybe = getattr(value, "items", None)
        if isinstance(maybe, list):
            return maybe
    record = _as_record(value)
    for key in keys:
        candidate = record.get(key)
        if isinstance(candidate, list):
            return candidate
    maybe_items = record.get("items")
    if isinstance(maybe_items, list):
        return maybe_items
    return []


def validate_cursor_api_key(api_key: str) -> None:
    try:
        Cursor.me(api_key=api_key)
    except Exception as exc:
        raise InvalidCursorApiKeyError("The Cursor API key could not be validated.") from exc


def get_current_user(api_key: str) -> dict[str, Any] | None:
    try:
        user = Cursor.me(api_key=api_key)
    except Exception:
        return None
    record = _as_record(user)
    name = _first_string(record, ["name", "displayName", "username", "api_key_name", "apiKeyName", "user_email"]) or "Cursor user"
    return {"name": name, "email": _first_string(record, ["email", "user_email", "userEmail"])}


def list_models(api_key: str) -> list[dict[str, str]]:
    try:
        models = Cursor.models.list(api_key=api_key)
    except Exception:
        return []
    result: list[dict[str, str]] = []
    for model in _extract_array(models, ["models", "items", "data"]) or list(models):
        record = _as_record(model)
        ident = _first_string(record, ["id", "name"])
        if not ident:
            continue
        result.append(
            {
                "id": ident,
                "label": _first_string(record, ["displayName", "display_name", "label", "name"]) or ident,
                "description": _first_string(record, ["description"]) or "",
            }
        )
    return result


def list_repositories(api_key: str) -> list[dict[str, Any]]:
    cached = _repository_cache.get(api_key)
    now = time.time() * 1000
    if cached and now - cached["loaded_at"] < _REPO_CACHE_TTL_MS:
        return list(cached["repositories"])
    try:
        response = Cursor.repositories.list(api_key=api_key)
    except Exception:
        return []
    repositories = []
    for raw in _extract_array(response, ["repositories", "repos", "items", "data"]) or list(response):
        normalized = _normalize_repository(raw)
        if normalized:
            repositories.append(normalized)
    _repository_cache[api_key] = {"loaded_at": now, "repositories": repositories}
    return list(repositories)


def _normalize_repository_url(url: str | None) -> str | None:
    if not url:
        return None
    trimmed = url.strip().removesuffix(".git")
    if trimmed.startswith("git@github.com:"):
        return "https://github.com/" + trimmed[len("git@github.com:") :]
    if trimmed.startswith("ssh://git@github.com/"):
        return "https://github.com/" + trimmed[len("ssh://git@github.com/") :]
    return trimmed


def _label_from_repository_url(url: str) -> str:
    path = url.rstrip("/").split("github.com/")[-1]
    return path or url


def _normalize_repository(raw: Any) -> dict[str, Any] | None:
    record = _as_record(raw)
    url = _normalize_repository_url(
        _first_string(record, ["url", "htmlUrl", "remoteUrl", "cloneUrl", "sshUrl"])
    )
    if not url:
        return None
    label = _first_string(record, ["fullName", "slug", "label", "name"]) or _label_from_repository_url(url)
    owner, _, name = label.partition("/")
    return {
        "id": _first_string(record, ["id"]) or url,
        "label": label,
        "url": url,
        "owner": owner or None,
        "name": name or None,
        "defaultBranch": _first_string(record, ["defaultBranch", "default_branch", "branch"]),
    }


def _normalize_agent(raw: Any) -> dict[str, Any]:
    record = _as_record(raw)
    ident = _first_string(record, ["id", "agentId", "agent_id", "uuid"]) or f"agent-{uuid.uuid4()}"
    repos = record.get("repos") or []
    repo_url = None
    if isinstance(repos, (list, tuple)) and repos:
        first = repos[0]
        repo_url = first if isinstance(first, str) else _attr(first, "url")
    return {
        "id": ident,
        "title": _first_string(record, ["name", "title", "summary"]) or f"Agent {ident[:8]}",
        "status": str(_first_string(record, ["status", "state"]) or ("archived" if record.get("archived") else "no_status")),
        "repository": str(repo_url or _first_string(record, ["repository", "repo"]) or "No repository"),
        "repositoryUrl": repo_url,
        "branch": _first_string(record, ["branch", "startingRef", "starting_ref"]),
        "createdAt": _first_string(record, ["createdAt", "created_at"]),
        "updatedAt": _first_string(record, ["lastModified", "last_modified", "updatedAt", "updated_at"]),
        "prUrl": _first_string(record, ["prUrl", "pr_url"]),
        "artifacts": [],
    }


def list_cloud_agents(
    api_key: str,
    *,
    cursor: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
    pr_url: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "api_key": api_key,
        "runtime": "cloud",
        "limit": limit,
        "include_archived": include_archived,
    }
    if cursor:
        payload["cursor"] = cursor
    if pr_url:
        payload["pr_url"] = pr_url
    response = Agent.list(payload)
    raw_agents = _extract_array(response, ["agents", "items", "data", "results"]) or list(response)
    agents = []
    for raw in raw_agents:
        card = _normalize_agent(raw)
        try:
            card["artifacts"] = list_artifacts_for_agent(api_key, card["id"])
        except Exception:
            card["artifacts"] = []
        agents.append(card)
    next_cursor = _attr(response, "next_cursor", "nextCursor")
    return {"agents": agents, "nextCursor": next_cursor}


def list_runs_for_agent(api_key: str, agent_id: str) -> list[dict[str, Any]]:
    list_runs = getattr(Agent, "list_runs", None) or getattr(Agent, "listRuns", None)
    if list_runs is None:
        return []
    response = list_runs(agent_id, {"runtime": "cloud", "api_key": api_key, "limit": 10})
    runs = []
    for raw in _extract_array(response, ["items", "runs", "data", "results"]) or list(response):
        record = _as_record(raw)
        runs.append(
            {
                "id": _first_string(record, ["id", "runId", "run_id"]),
                "status": _first_string(record, ["status"]),
                "durationMs": _attr(raw, "duration_ms", "durationMs"),
                "result": _first_string(record, ["result"]),
            }
        )
    return runs


def attach_agent(api_key: str, agent_id: str) -> Any:
    resume = getattr(Agent, "resume", None)
    if resume is not None:
        try:
            return resume(agent_id, {"api_key": api_key})
        except TypeError:
            return resume(agent_id, api_key=api_key)
    getter = getattr(Agent, "get", None)
    if getter is None:
        raise RuntimeError("This version of the SDK cannot attach to cloud agents.")
    return getter(agent_id, api_key=api_key)


def list_artifacts_for_agent(api_key: str, agent_id: str) -> list[dict[str, Any]]:
    agent = attach_agent(api_key, agent_id)
    lister = getattr(agent, "list_artifacts", None) or getattr(agent, "listArtifacts", None)
    if lister is None:
        _dispose(agent)
        return []
    try:
        response = lister()
    finally:
        _dispose(agent)
    previews = []
    for raw in _extract_array(response, ["artifacts", "items", "files", "data"]) or list(response or []):
        record = _as_record(raw)
        path = _first_string(record, ["path", "name", "filename", "filePath"]) or "artifact"
        previews.append({"path": path, "name": path.rsplit("/", 1)[-1], "size": _attr(raw, "size")})
    return previews[:4]


def download_artifact(api_key: str, agent_id: str, artifact_path: str) -> bytes | dict[str, Any]:
    agent = attach_agent(api_key, agent_id)
    downloader = getattr(agent, "download_artifact", None) or getattr(agent, "downloadArtifact", None)
    if downloader is None:
        _dispose(agent)
        return {}
    try:
        return downloader(artifact_path)
    finally:
        _dispose(agent)


def create_cloud_agent(
    api_key: str,
    *,
    prompt: str,
    repository_id: str,
    name: str | None = None,
    model_id: str | None = None,
    branch: str | None = None,
    auto_create_pr: bool = False,
) -> dict[str, Any]:
    text = prompt.strip()
    if not text:
        raise ValueError("A prompt is required to create a cloud agent.")
    repositories = list_repositories(api_key)
    repository = next((item for item in repositories if item["id"] == repository_id or item["url"] == repository_id), None)
    if repository is None:
        url = _normalize_repository_url(repository_id)
        if not url:
            raise ValueError("Select a repository before creating an agent.")
        repository = {"id": url, "label": _label_from_repository_url(url), "url": url, "defaultBranch": None}
    cloud_repo = CloudRepository(url=repository["url"], starting_ref=branch or None)
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "name": (name or text)[:80],
        "cloud": CloudAgentOptions(repos=[cloud_repo], auto_create_pr=auto_create_pr),
    }
    if model_id and model_id != "auto":
        kwargs["model"] = model_id
    created = Agent.create(**kwargs)
    sender = getattr(created, "send", None)
    if sender is not None:
        sender(text)
    card = _normalize_agent(created)
    card["repository"] = repository.get("label")
    card["repositoryUrl"] = repository.get("url")
    card["latestMessage"] = text
    try:
        card["artifacts"] = list_artifacts_for_agent(api_key, card["id"])
    except Exception:
        card["artifacts"] = []
    _dispose(created)
    return {"agent": card}


def _dispose(agent: Any) -> None:
    closer = getattr(agent, "close", None)
    if closer is not None:
        try:
            closer()
        except Exception:
            pass
