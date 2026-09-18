"""Opt-in live comparison of documented Python SDK fields vs official cursor_sdk.

Skip unless CURSOR_API_KEY is set. Optional OPENCURSOR_LIVE_CLOUD=1 enables
cloud list/get/list_runs (never archives or deletes pre-existing agents).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

_OFFICIAL_ROOT = Path(__file__).resolve().parents[2] / "official"
if str(_OFFICIAL_ROOT) not in sys.path:
    sys.path.insert(0, str(_OFFICIAL_ROOT))

pytestmark = pytest.mark.skipif(
    not os.environ.get("CURSOR_API_KEY"),
    reason="set CURSOR_API_KEY to compare opencursor against official cursor_sdk",
)


def _attr(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _user_fields(user: Any) -> dict[str, Any]:
    return {
        "api_key_name": _attr(user, "api_key_name"),
        "created_at": _attr(user, "created_at"),
        "user_id": _attr(user, "user_id"),
        "user_email": _attr(user, "user_email"),
        "user_first_name": _attr(user, "user_first_name"),
        "user_last_name": _attr(user, "user_last_name"),
    }


def _model_fields(model: Any) -> dict[str, Any]:
    parameters = []
    for parameter in _attr(model, "parameters", default=()) or ():
        values = []
        for item in _attr(parameter, "values", default=()) or ():
            values.append(
                {
                    "value": _attr(item, "value"),
                    "display_name": _attr(item, "display_name"),
                }
            )
        parameters.append(
            {
                "id": _attr(parameter, "id"),
                "display_name": _attr(parameter, "display_name"),
                "values": values,
            }
        )
    variants = []
    for variant in _attr(model, "variants", default=()) or ():
        variants.append({"display_name": _attr(variant, "display_name")})
    return {
        "id": _attr(model, "id"),
        "display_name": _attr(model, "display_name"),
        "parameters": parameters,
        "variants": variants,
    }


def _repo_urls(repos: Any) -> list[str]:
    return sorted(str(_attr(item, "url") or "") for item in (repos or []) if _attr(item, "url"))


def test_cursor_catalog_matches_official() -> None:
    import cursor_sdk
    import opencursor

    official_user = _user_fields(cursor_sdk.Cursor.me())
    open_user = _user_fields(opencursor.Cursor.me())
    assert official_user == open_user
    assert open_user["api_key_name"]
    assert open_user["created_at"]

    official_models = {item.id: _model_fields(item) for item in cursor_sdk.Cursor.models.list()}
    open_models = {item.id: _model_fields(item) for item in opencursor.Cursor.models.list()}
    assert official_models.keys() == open_models.keys()
    for model_id, fields in official_models.items():
        assert open_models[model_id]["display_name"] == fields["display_name"]
        assert open_models[model_id]["parameters"] == fields["parameters"]
        assert open_models[model_id]["variants"] == fields["variants"]

    official_repos = _repo_urls(cursor_sdk.Cursor.repositories.list())
    open_repos = _repo_urls(opencursor.Cursor.repositories.list())
    assert official_repos == open_repos


def _run_documented_local(
    agent_cls: Any,
    local_options_cls: Any,
    api_key: str,
    cwd: str,
    *,
    local_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra = dict(local_kwargs or {})
    with agent_cls.create(
        model="composer-2.5",
        api_key=api_key,
        local=local_options_cls(cwd=cwd, **extra),
    ) as agent:
        run = agent.send("Reply with exactly: pong")
        result = run.wait()
        text = run.text()
        messages = list(run.messages())
        usage_agent_total = None
        usage_error = None
        try:
            usage = agent.get_usage()
            usage_agent_total = _attr(_attr(usage, "usage"), "total_tokens")
        except Exception as exc:
            usage_error = type(exc).__name__
        artifacts = None
        artifacts_error = None
        try:
            artifacts = agent.list_artifacts()
        except Exception as exc:
            artifacts_error = type(exc).__name__
        download_error: str | None = None
        try:
            agent.download_artifact("missing.txt")
        except Exception as exc:
            download_error = type(exc).__name__
        agent_id = str(agent.agent_id)
        return {
            "agent_id": agent_id,
            "agent_prefix_local": not agent_id.startswith("bc-"),
            "run_id": str(run.id),
            "run_status": str(_attr(run, "status")),
            "result_status": str(_attr(result, "status")),
            "result_text": str(_attr(result, "result") or ""),
            "result_agent_id": _attr(result, "agent_id"),
            "usage_total": _attr(_attr(result, "usage"), "total_tokens"),
            "text": str(text or ""),
            "message_types": [str(_attr(item, "type")) for item in messages if _attr(item, "type")],
            "usage_agent_total": usage_agent_total,
            "usage_error": usage_error,
            "artifacts": artifacts,
            "artifacts_error": artifacts_error,
            "download_error": download_error,
        }


def test_local_agent_run_matches_official(tmp_path: Path) -> None:
    import cursor_sdk
    import opencursor
    from cursor_sdk import Agent as OfficialAgent
    from cursor_sdk import LocalAgentOptions as OfficialLocal
    from opencursor import Agent
    from opencursor.types import LocalAgentOptions

    api_key = os.environ["CURSOR_API_KEY"]
    official = _run_documented_local(OfficialAgent, OfficialLocal, api_key, str(tmp_path / "official"))
    ours = _run_documented_local(
        Agent,
        LocalAgentOptions,
        api_key,
        str(tmp_path / "opencursor"),
        local_kwargs={"useHttp1ForAgent": True},
    )

    assert official["agent_prefix_local"]
    assert ours["agent_prefix_local"]
    assert ours["agent_id"].startswith("agent-")
    assert official["run_id"]
    assert ours["run_id"]
    assert official["run_status"]
    assert ours["run_status"]
    assert official["result_status"] in {"finished", "error", "cancelled", "expired"}
    assert ours["result_status"] in {"finished", "error", "cancelled", "expired"}
    assert ours["result_agent_id"] == ours["agent_id"]
    assert "pong" in (ours["result_text"] or ours["text"]).lower()
    assert "pong" in (official["result_text"] or official["text"]).lower()
    assert ours["message_types"]
    if ours["artifacts_error"] is None:
        assert ours["artifacts"] == []
    if official["artifacts_error"] is None:
        assert official["artifacts"] == []
    assert official["download_error"]
    assert ours["download_error"]
    if ours["usage_error"] is None:
        assert ours["usage_agent_total"] is not None
        assert int(ours["usage_agent_total"]) >= 0
    if official["usage_error"] is None:
        assert official["usage_agent_total"] is not None
        assert int(official["usage_agent_total"]) >= 0


@pytest.mark.skipif(
    os.environ.get("OPENCURSOR_LIVE_CLOUD") != "1",
    reason="set OPENCURSOR_LIVE_CLOUD=1 to compare cloud list/get/list_runs",
)
def test_optional_cloud_list_get_runs() -> None:
    import cursor_sdk
    import opencursor
    from cursor_sdk import Agent as OfficialAgent
    from opencursor import Agent

    if not cursor_sdk.Cursor.repositories.list() and not opencursor.Cursor.repositories.list():
        pytest.skip("repositories.list() is empty")

    official_page = OfficialAgent.list({"runtime": "cloud"})
    open_page = Agent.list(runtime="cloud")
    official_ids = [_attr(item, "agent_id") for item in official_page]
    open_ids = [_attr(item, "agent_id") for item in open_page]
    assert official_ids == open_ids
    if not official_ids:
        return
    agent_id = official_ids[0]
    assert str(agent_id).startswith("bc-")
    official_info = OfficialAgent.get(agent_id)
    open_info = Agent.get(agent_id)
    assert _attr(official_info, "agent_id") == _attr(open_info, "agent_id")
    official_runs = OfficialAgent.list_runs(agent_id)
    open_runs = Agent.list_runs(agent_id)
    assert [_attr(item, "id") for item in official_runs] == [_attr(item, "id") for item in open_runs]
