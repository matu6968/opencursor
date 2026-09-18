# SPDX-License-Identifier: MIT-0
"""Slash commands for the coding-agent CLI REPL.

Mirrors cookbook/sdk/coding-agent-cli/src/commands.ts (minus TUI picker UI).
"""

from __future__ import annotations

from typing import Iterable

SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "Show available commands."),
    ("/local", "Run future prompts in the local workspace."),
    ("/cloud", "Run future prompts in Cursor cloud."),
    ("/model", "List available Cursor models."),
    ("/reset", "Start a fresh agent and clear context."),
    ("/exit", "Exit the REPL."),
    ("/quit", "Exit the REPL."),
)

_COMMAND_NAMES = {name for name, _ in SLASH_COMMANDS}


def get_slash_command(text: str) -> str | None:
    command = text.strip().split(None, 1)[0] if text.strip() else ""
    return command if command in _COMMAND_NAMES else None


def format_slash_command_help() -> str:
    return "  ".join(f"{name} - {summary}" for name, summary in SLASH_COMMANDS)


def get_slash_command_items(query: str) -> list[dict[str, str]]:
    needle = query.strip().lower() or "/"
    return [
        {"key": name, "label": f"{name}  {summary}", "value": name}
        for name, summary in SLASH_COMMANDS
        if name.startswith(needle)
    ]


def iter_slash_commands() -> Iterable[tuple[str, str]]:
    return SLASH_COMMANDS
