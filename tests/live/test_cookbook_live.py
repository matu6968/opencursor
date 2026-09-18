"""Opt-in live runs of cookbook Python ports.

Skip unless CURSOR_API_KEY is set. Honors OPENCURSOR_LIVE_SDK and
OPENCURSOR_LIVE_DEBUG. Set OPENCURSOR_LIVE_CLOUD=1 for cloud list.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = _ROOT / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

pytestmark = pytest.mark.skipif(
    not os.environ.get("CURSOR_API_KEY"),
    reason="set CURSOR_API_KEY to live-run cookbook ports",
)


@pytest.fixture(autouse=True)
def _require_cookbook_sdk() -> None:
    try:
        from cookbook import _sdk
    except Exception as exc:
        choice = (os.environ.get("OPENCURSOR_LIVE_SDK") or "").strip().lower()
        if choice in {"official", "cursor_sdk", "cursor-sdk"}:
            pytest.skip(f"official cursor_sdk unavailable: {exc}")
        raise
    if _sdk.selected_sdk() == "official" and _sdk.SDK_NAME != "official":
        pytest.skip("official cursor_sdk unavailable")


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


def _snapshot(result: Any, *, text: str, message_types: list[str], agent_id: str) -> dict[str, Any]:
    status = str(_attr(result, "status", default=""))
    return {
        "sdk": os.environ.get("OPENCURSOR_LIVE_SDK") or "opencursor",
        "status": status,
        "message_types": message_types,
        "has_pong": "pong" in (text or "").lower(),
        "agent_prefix": "cloud" if str(agent_id).startswith("bc-") else "local",
        "error_class": None,
    }


def test_quickstart_local_pong(tmp_path: Path) -> None:
    from cookbook.quickstart import run_quickstart

    events: list[Any] = []
    try:
        result = run_quickstart(
            prompt="Reply with exactly: pong",
            cwd=str(tmp_path),
            api_key=os.environ["CURSOR_API_KEY"],
            model="composer-2",
            on_event=events.append,
        )
        error_class = None
    except Exception as exc:
        result = None
        error_class = type(exc).__name__
        pytest.fail(
            f"cookbook live run failed: { {'sdk': os.environ.get('OPENCURSOR_LIVE_SDK') or 'opencursor', 'error_class': error_class, 'error': str(exc)} }"
        )
    status = str(_attr(result, "status", default=""))
    text = str(_attr(result, "result", default="") or "")
    types = [str(_attr(event, "type", default="")) for event in events]
    snap = _snapshot(
        result,
        text=text,
        message_types=types,
        agent_id=str(_attr(result, "agent_id", "agentId", default="")),
    )
    snap["error_class"] = error_class
    assert types, snap
    assert status in {"finished", "error", "cancelled", "expired"}, snap
    if status == "finished":
        assert snap["has_pong"] or "pong" in text.lower(), snap


def test_coding_cli_one_shot(tmp_path: Path) -> None:
    from cookbook.coding_agent_cli.agent import CodingAgentSession

    events: list[dict[str, Any]] = []
    session = CodingAgentSession(
        api_key=os.environ["CURSOR_API_KEY"],
        cwd=str(tmp_path),
        model={"id": "composer-2"},
    )
    try:
        try:
            result = session.send_prompt("Reply with exactly: pong", events.append)
        except Exception as exc:
            pytest.fail(
                f"cookbook live run failed: { {'sdk': os.environ.get('OPENCURSOR_LIVE_SDK') or 'opencursor', 'error_class': type(exc).__name__, 'error': str(exc)} }"
            )
    finally:
        session.dispose()
    types = [str(event.get("type")) for event in events]
    status = str(_attr(result, "status", default=""))
    text = "".join(str(event.get("text") or "") for event in events if event.get("type") == "assistant_delta")
    snap = _snapshot(result, text=text, message_types=types, agent_id=str(_attr(session.agent, "agent_id", "agentId", "id", default="")))
    assert types, snap
    assert status in {"finished", "error", "cancelled", "expired"}, snap
    if status == "finished":
        assert "pong" in text.lower() or any(event.get("type") == "result" for event in events), snap


def test_live_dag_pong(tmp_path: Path) -> None:
    from cookbook.dag_task_runner.run_dag import run_dag

    dag = _EXAMPLES / "cookbook" / "dag_task_runner" / "examples" / "live_dag.json"
    canvas = tmp_path / "live.canvas.tsx"
    code = run_dag(
        {
            "dag": str(dag),
            "canvas_path": str(canvas),
            "cwd": str(tmp_path),
            "models_file": None,
            "debounce_ms": 50,
            "task_timeout_ms": 180_000,
            "stream_publish_ms": 200,
            "stream_idle_timeout_ms": 120_000,
            "init_only": False,
        }
    )
    source = canvas.read_text(encoding="utf-8")
    assert "const STATE: RunState =" in source
    assert code in {0, 1}


def test_app_builder_session_and_stream(tmp_path: Path) -> None:
    from cookbook.app_builder.server import (
        close_session,
        create_session,
        list_models,
        stream_agent_response,
        validate_cursor_api_key,
    )

    api_key = os.environ["CURSOR_API_KEY"]
    validate_cursor_api_key(api_key)
    models = list_models(api_key)
    assert models
    public = create_session(api_key, project_path=str(tmp_path / "app"))
    events: list[dict[str, Any]] = []
    try:
        result = stream_agent_response(public["id"], "Reply with exactly: pong", events.append)
    finally:
        close_session(public["id"])
    status = str(_attr(result, "status", default=""))
    snap = _snapshot(result, text=" ".join(str(event.get("text") or "") for event in events), message_types=[str(event.get("type")) for event in events], agent_id=str(public.get("id") or ""))
    assert events, snap
    assert status in {"finished", "error", "cancelled", "expired"}, snap


def test_kanban_catalog() -> None:
    from cookbook.agent_kanban.server import get_current_user, list_models, list_repositories

    api_key = os.environ["CURSOR_API_KEY"]
    user = get_current_user(api_key)
    assert user is None or user.get("name")
    models = list_models(api_key)
    assert isinstance(models, list)
    repos = list_repositories(api_key)
    assert isinstance(repos, list)


@pytest.mark.skipif(os.environ.get("OPENCURSOR_LIVE_CLOUD") != "1", reason="set OPENCURSOR_LIVE_CLOUD=1 for cloud Agent.list")
def test_kanban_list_cloud_agents() -> None:
    from cookbook.agent_kanban.server import list_cloud_agents

    page = list_cloud_agents(os.environ["CURSOR_API_KEY"], limit=5)
    assert "agents" in page
    assert isinstance(page["agents"], list)
