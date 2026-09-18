"""Offline tests for cookbook Python ports and the live SDK shim."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_EXAMPLES = _ROOT / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))


def test_rewrite_is_unchanged_for_catalog() -> None:
    from stress_rewrite import rewrite_source

    src = "from cursor_sdk import Agent, LocalAgentOptions\n"
    assert "from opencursor import Agent, LocalAgentOptions" in rewrite_source(src)


def test_selected_sdk_defaults_to_opencursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCURSOR_LIVE_SDK", raising=False)
    from cookbook import _sdk

    importlib.reload(_sdk)
    assert _sdk.selected_sdk() == "opencursor"
    assert _sdk.SDK_NAME == "opencursor"
    assert hasattr(_sdk.Agent, "create")
    assert hasattr(_sdk.Cursor, "me")


def test_selected_sdk_official_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    from cookbook import _sdk

    assert _sdk.selected_sdk({"OPENCURSOR_LIVE_SDK": "official"}) == "official"
    assert _sdk.selected_sdk({"OPENCURSOR_LIVE_SDK": "cursor_sdk"}) == "official"
    assert _sdk.selected_sdk({"OPENCURSOR_LIVE_SDK": "cursor-sdk"}) == "official"
    assert _sdk.selected_sdk({"OPENCURSOR_LIVE_SDK": "opencursor"}) == "opencursor"
    assert _sdk.selected_sdk({"OPENCURSOR_LIVE_SDK": "open"}) == "opencursor"
    assert _sdk.selected_sdk({"OPENCURSOR_LIVE_SDK": ""}) == "opencursor"


def test_shim_official_exports_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCURSOR_LIVE_SDK", "official")
    from cookbook import _sdk

    try:
        importlib.reload(_sdk)
    except Exception as exc:
        pytest.skip(f"official cursor_sdk unavailable: {exc}")
    try:
        assert _sdk.SDK_NAME == "official"
        assert hasattr(_sdk.Agent, "create")
        assert hasattr(_sdk.Cursor, "models")
    finally:
        monkeypatch.delenv("OPENCURSOR_LIVE_SDK", raising=False)
        importlib.reload(_sdk)


def test_debug_logger_wraps_fake_agent(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OPENCURSOR_LIVE_DEBUG", "1")
    from cookbook import _sdk

    importlib.reload(_sdk)

    class FakeRun:
        id = "run-1"

        def stream(self):
            yield type("Event", (), {"type": "assistant"})()

        def wait(self):
            return type("Result", (), {"status": "finished", "durationMs": 12, "usage": None})()

        def cancel(self) -> None:
            return None

    class FakeAgent:
        agent_id = "agent-1"

        def send(self, prompt: str):
            assert prompt
            return FakeRun()

        def close(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    try:
        wrapped = _sdk.wrap_debug_agent(FakeAgent())
        run = wrapped.send("hello")
        events = list(run.stream())
        result = run.wait()
        assert events
        assert result.status == "finished"
        err = capsys.readouterr().err
        assert "[cookbook-sdk opencursor]" in err
        assert "send" in err
        assert "stream event type=assistant" in err
        assert "wait status=finished" in err
    finally:
        monkeypatch.delenv("OPENCURSOR_LIVE_DEBUG", raising=False)
        importlib.reload(_sdk)


def test_dag_parse_and_ranks() -> None:
    from cookbook.dag_task_runner.dag import compute_ranks, parse_dag

    dag = parse_dag(
        {
            "title": "demo",
            "tasks": [
                {"id": "a", "depends_on": [], "complexity": "LOW", "subtask_prompt": "one"},
                {"id": "b", "depends_on": ["a"], "complexity": "MED", "subtask_prompt": "two"},
                {"id": "c", "depends_on": ["a"], "complexity": "LOW", "subtask_prompt": "three"},
            ],
        }
    )
    ranks = compute_ranks(dag)
    assert [task.id for task in ranks[0]] == ["a"]
    assert {task.id for task in ranks[1]} == {"b", "c"}


def test_dag_detects_cycle() -> None:
    from cookbook.dag_task_runner.dag import parse_dag

    with pytest.raises(ValueError, match="Cycle"):
        parse_dag(
            {
                "title": "loop",
                "tasks": [
                    {"id": "a", "depends_on": ["b"], "complexity": "LOW", "subtask_prompt": "one"},
                    {"id": "b", "depends_on": ["a"], "complexity": "LOW", "subtask_prompt": "two"},
                ],
            }
        )


def test_canvas_writer_inlines_state(tmp_path: Path) -> None:
    from cookbook.dag_task_runner.canvas_writer import CanvasWriter, initial_run_state
    from cookbook.dag_task_runner.dag import create_model_resolver, parse_dag

    dag = parse_dag(
        {
            "title": "Canvas demo",
            "tasks": [{"id": "ping", "depends_on": [], "complexity": "LOW", "subtask_prompt": "pong"}],
        }
    )
    state = initial_run_state(dag, create_model_resolver({"LOW": "composer-2"}))
    path = tmp_path / "demo.canvas.tsx"
    writer = CanvasWriter(str(path), debounce_ms=1)
    writer.schedule(state)
    writer.flush()
    source = path.read_text(encoding="utf-8")
    assert "const STATE: RunState =" in source
    assert "Canvas demo" in source
    payload = source.split("const STATE: RunState = ", 1)[1].split(";\n", 1)[0]
    data = json.loads(payload)
    assert data["title"] == "Canvas demo"
    assert data["tasks"][0]["id"] == "ping"


def test_dag_init_only(tmp_path: Path) -> None:
    from cookbook.dag_task_runner.run_dag import run_dag

    dag_path = _EXAMPLES / "cookbook" / "dag_task_runner" / "examples" / "live_dag.json"
    canvas = tmp_path / "live.canvas.tsx"
    code = run_dag(
        {
            "dag": str(dag_path),
            "canvas_path": str(canvas),
            "cwd": str(tmp_path),
            "models_file": None,
            "debounce_ms": 1,
            "task_timeout_ms": 1000,
            "stream_publish_ms": 50,
            "stream_idle_timeout_ms": 1000,
            "init_only": True,
        }
    )
    assert code == 0
    assert "const STATE: RunState =" in canvas.read_text(encoding="utf-8")


def test_format_model_label_and_github_remote() -> None:
    from cookbook.coding_agent_cli.agent import format_duration, format_model_label, normalize_github_remote

    assert format_model_label(type("M", (), {"id": "composer-2", "params": None})()) == "composer-2"
    assert normalize_github_remote("git@github.com:acme/app.git") == "https://github.com/acme/app"
    assert normalize_github_remote("https://github.com/acme/app.git") == "https://github.com/acme/app"
    assert format_duration(250) == "250ms"
    assert format_duration(1500) == "1.5s"


def test_slash_commands() -> None:
    from cookbook.coding_agent_cli.commands import format_slash_command_help, get_slash_command

    assert get_slash_command("/help extra") == "/help"
    assert get_slash_command("not-a-command") is None
    help_text = format_slash_command_help()
    assert "/local" in help_text
    assert "/cloud" in help_text


def test_cli_parse_args() -> None:
    from cookbook.coding_agent_cli.cli import parse_args

    options = parse_args(["--cwd", "/tmp", "--model", "composer-2", "--force", "Explain it"])
    assert options.force is True
    assert options.model == "composer-2"
    assert options.prompt == "Explain it"


def test_project_name_xml() -> None:
    from cookbook.app_builder.server import extract_project_name_xml, generate_fallback_project_name, sanitize_project_name

    assert extract_project_name_xml("Hello <projectName>Task Board</projectName>") == "Task Board"
    assert "Dashboard" in sanitize_project_name("Project name: Fancy Dashboard")
    assert "weather" in generate_fallback_project_name("Please build me a weather dashboard app").lower()


def test_live_dag_fixture_is_read_only() -> None:
    raw = json.loads(
        (_EXAMPLES / "cookbook" / "dag_task_runner" / "examples" / "live_dag.json").read_text(encoding="utf-8")
    )
    assert raw["models"]["LOW"] == "composer-2"
    for task in raw["tasks"]:
        assert "pong" in task["subtask_prompt"].lower()
        assert "write" not in task["subtask_prompt"].lower()
