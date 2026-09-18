"""Stress official Python dependents by rewriting `cursor_sdk` imports.

Catalog: tests/stress/catalog.json (GitHub code search of cursor-sdk dependents).
Live clone+compile is opt-in via OPENCURSOR_STRESS_CLONE=1.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import opencursor
from stress_rewrite import rewrite_module_name, rewrite_source
from test_api_visibility import INTENTIONALLY_SPLIT, INTENTIONALLY_UNPORTED

_CATALOG = Path(__file__).resolve().parent / "stress" / "catalog.json"

# Official names used by dependents but not re-exported from the package root,
# or that live only on the Node bridge.
STRESS_BRIDGE_INTERNALS = frozenset(
    {
        "parse_discovery_line",
        "resolve_bridge_path",
        "READY_LINE_PREFIX",
        "_bridge_subprocess_env",
        "_tool_callback",
        "_store_callback",
        "_bridge",
        "_client",
    }
)


def _catalog() -> dict:
    return json.loads(_CATALOG.read_text(encoding="utf-8"))


def test_rewrite_import_lines() -> None:
    src = (
        "from cursor_sdk import Agent, AgentOptions\n"
        "from cursor_sdk.types import LocalAgentOptions\n"
        "from cursor_sdk.errors import CursorAgentError\n"
        "from cursor_sdk.events import TurnEndedUpdate\n"
        "from cursor_sdk.asyncio import AsyncAgent\n"
        "from cursor_sdk._bridge import parse_discovery_line\n"
        "import cursor_sdk\n"
        "import cursor_sdk as sdk\n"
    )
    out = rewrite_source(src)
    assert "from opencursor import Agent, AgentOptions" in out
    assert "from opencursor.types import LocalAgentOptions" in out
    assert "from opencursor.errors import CursorAgentError" in out
    assert "from opencursor.events import TurnEndedUpdate" in out
    assert "from cursor_sdk.asyncio import AsyncAgent" in out
    assert "from cursor_sdk._bridge import parse_discovery_line" in out
    assert "import opencursor as cursor_sdk" in out
    assert "import opencursor as sdk" in out


def test_next_clone_batch_skips_existing_checkouts(tmp_path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_sdk_stress as stress_run

    candidates = ["acme/one", "acme/two", "acme/three", "acme/four"]
    cloned = tmp_path / "acme__one"
    cloned.mkdir()
    (cloned / ".git").mkdir()
    leftover = tmp_path / "acme__two"
    leftover.mkdir()
    batch, skipped = stress_run.next_clone_batch(candidates, tmp_path, limit=2)
    assert skipped == 1
    assert batch == ["acme/two", "acme/three"]

    for name in ("acme/two", "acme/three"):
        dest = tmp_path / name.replace("/", "__")
        dest.mkdir(exist_ok=True)
        (dest / ".git").mkdir(exist_ok=True)
    batch2, skipped2 = stress_run.next_clone_batch(candidates, tmp_path, limit=2)
    assert skipped2 == 3
    assert batch2 == ["acme/four"]


def test_catalog_declared_dependents_are_present() -> None:
    stats = _catalog()["stats"]
    assert stats["requirements_txt_repos"] >= 50
    assert stats["declared_dep_repos"] >= 100
    assert stats["file_count"] >= 200


def test_dependent_public_imports_resolve_after_rewrite() -> None:
    missing: list[str] = []
    seen: set[tuple[str, str]] = set()
    for repo in _catalog()["repos"]:
        for file in repo["files"]:
            for imp in file["imports"]:
                if imp["kind"] != "from":
                    continue
                dest = rewrite_module_name(imp["module"])
                if dest is None:
                    continue
                for name in imp["names"]:
                    if name == "*":
                        continue
                    key = (dest, name)
                    if key in seen:
                        continue
                    seen.add(key)
                    if name in INTENTIONALLY_UNPORTED or name in INTENTIONALLY_SPLIT or name in STRESS_BRIDGE_INTERNALS:
                        continue
                    module = importlib.import_module(dest)
                    if not hasattr(module, name):
                        missing.append(f"{repo['full_name']}:{file['path']} {dest}.{name}")
    assert missing == [], "dependents import names missing from opencursor after rewrite:\n" + "\n".join(missing)


def test_rewritten_public_from_imports_execute() -> None:
    """`from opencursor import Name` for every public dependent symbol."""
    failures: list[str] = []
    seen: set[tuple[str, str]] = set()
    for repo in _catalog()["repos"]:
        for file in repo["files"]:
            for imp in file["imports"]:
                if imp["kind"] != "from":
                    continue
                dest = rewrite_module_name(imp["module"])
                if dest is None:
                    continue
                names = [n for n in imp["names"] if n != "*"]
                public = [
                    n
                    for n in names
                    if n not in INTENTIONALLY_UNPORTED
                    and n not in INTENTIONALLY_SPLIT
                    and n not in STRESS_BRIDGE_INTERNALS
                ]
                if not public:
                    continue
                key = (dest, tuple(sorted(public)))
                if key in seen:
                    continue
                seen.add(key)
                stmt = f"from {dest} import {', '.join(public)}"
                try:
                    exec(stmt, {})  # noqa: S102
                except Exception as exc:
                    failures.append(f"{stmt} ({exc})")
    assert failures == [], "rewritten import statements failed:\n" + "\n".join(failures)


def test_module_import_alias_exposes_agent() -> None:
    ns: dict[str, object] = {}
    exec(rewrite_source("import cursor_sdk\n"), ns)  # noqa: S102
    assert ns["cursor_sdk"] is opencursor
    assert ns["cursor_sdk"].Agent is opencursor.Agent


def test_events_submodule_matches_official_names() -> None:
    events = importlib.import_module("opencursor.events")
    for name in (
        "TurnEndedUpdate",
        "TextDeltaUpdate",
        "ThinkingDeltaUpdate",
        "ToolCallStartedUpdate",
        "AssistantConversationStep",
    ):
        assert hasattr(events, name)


@pytest.mark.skipif(os.environ.get("OPENCURSOR_STRESS_CLONE") != "1", reason="set OPENCURSOR_STRESS_CLONE=1 to clone dependents")
def test_clone_rewrite_compile_sample() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_sdk_stress.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--clone", "--limit", "3"],
        cwd=str(Path(__file__).resolve().parents[1]),
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
