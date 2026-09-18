#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""One-shot + REPL coding-agent CLI (no OpenTUI).

Mirrors cookbook/sdk/coding-agent-cli/src/index.ts one-shot path and slash commands.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_EXAMPLES = Path(__file__).resolve().parents[2]
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from cookbook.coding_agent_cli.agent import (  # noqa: E402
    CodingAgentSession,
    format_duration,
)
from cookbook.coding_agent_cli.commands import (  # noqa: E402
    format_slash_command_help,
    get_slash_command,
)

DEFAULT_MODEL = os.environ.get("CURSOR_MODEL") or "composer-2"


@dataclass
class CliOptions:
    cwd: str
    force: bool
    help: bool
    model: str
    prompt: str


def parse_args(argv: list[str]) -> CliOptions:
    prompt_parts: list[str] = []
    cwd = os.getcwd()
    force = False
    help_flag = False
    model = DEFAULT_MODEL
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            prompt_parts.extend(argv[index + 1 :])
            break
        if arg in {"--help", "-h"}:
            help_flag = True
            index += 1
            continue
        if arg == "--force":
            force = True
            index += 1
            continue
        if arg in {"--cwd", "-C"}:
            cwd = _read_option(argv, index, arg)
            index += 2
            continue
        if arg.startswith("--cwd="):
            cwd = arg[len("--cwd=") :]
            index += 1
            continue
        if arg in {"--model", "-m"}:
            model = _read_option(argv, index, arg)
            index += 2
            continue
        if arg.startswith("--model="):
            model = arg[len("--model=") :]
            index += 1
            continue
        if arg.startswith("-"):
            raise SystemExit(f"Unknown option: {arg}")
        prompt_parts.extend(argv[index:])
        break
    return CliOptions(
        cwd=str(Path(cwd).resolve()),
        force=force,
        help=help_flag,
        model=model,
        prompt=" ".join(prompt_parts).strip(),
    )


def _read_option(argv: list[str], index: int, option: str) -> str:
    if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
        raise SystemExit(f"Expected a value after {option}.")
    return argv[index + 1]


def print_help() -> None:
    print(
        """Lightweight coding agent CLI

Usage:
  coding_agent_cli [options] "your task"
  coding_agent_cli [options]

Options:
  -C, --cwd <path>       Workspace directory for the local agent. Defaults to cwd.
  -m, --model <id>      Model id. Defaults to CURSOR_MODEL or composer-2.
      --force           Expire a stuck active local run before starting.
  -h, --help            Show this help.

Interactive commands:
  /local                 Run future prompts in the local workspace.
  /cloud                 Run future prompts in Cursor cloud.
  /model                 List available models.
  /reset                 Start a fresh agent in the current execution mode.

Examples:
  coding_agent_cli "Explain the auth flow"
  coding_agent_cli --cwd ../my-app "Add a regression test for the parser"
"""
    )


def compact_text(text: str) -> str:
    return " ".join(text.split()).strip()


def render_plain_event(
    event: dict,
    annotate: callable,
    write_assistant: callable,
) -> None:
    kind = event.get("type")
    if kind == "assistant_delta":
        write_assistant(str(event.get("text") or ""))
        return
    if kind == "thinking":
        text = compact_text(str(event.get("text") or ""))
        if text:
            annotate(f"[thinking] {text}")
        return
    if kind == "tool":
        annotate(f"[tool] {event.get('status')} {event.get('name')}")
        return
    if kind == "status":
        status = event.get("status")
        if status != "FINISHED":
            extra = f" {event.get('message')}" if event.get("message") else ""
            annotate(f"[status] {status}{extra}")
        return
    if kind == "task":
        bits = [event.get("status"), event.get("text")]
        text = compact_text(" ".join(str(item) for item in bits if item))
        if text:
            annotate(f"[task] {text}")
        return
    if kind == "result":
        details = [f"status={event.get('status')}"]
        duration = event.get("durationMs")
        if duration:
            details.append(f"duration={format_duration(duration)}")
        usage = event.get("usage") or {}
        if usage.get("inputTokens"):
            details.append(f"input={usage['inputTokens']}")
        if usage.get("outputTokens"):
            details.append(f"output={usage['outputTokens']}")
        annotate(f"[done] {' '.join(details)}")


def run_plain_prompt(api_key: str, options: CliOptions, prompt: str) -> object:
    session = CodingAgentSession(
        api_key=api_key,
        cwd=options.cwd,
        force=options.force,
        model={"id": options.model},
    )
    assistant_ended_with_newline = True

    def annotate(message: str) -> None:
        nonlocal assistant_ended_with_newline
        if not assistant_ended_with_newline:
            sys.stderr.write("\n")
        sys.stderr.write(f"{message}\n")
        assistant_ended_with_newline = True

    def write_assistant(text: str) -> None:
        nonlocal assistant_ended_with_newline
        sys.stdout.write(text)
        sys.stdout.flush()
        assistant_ended_with_newline = text.endswith("\n")

    try:
        return session.send_prompt(
            prompt,
            lambda event: render_plain_event(event, annotate, write_assistant),
        )
    finally:
        session.dispose()


def _run_repl(api_key: str, options: CliOptions) -> int:
    session = CodingAgentSession(
        api_key=api_key,
        cwd=options.cwd,
        force=options.force,
        model={"id": options.model},
    )
    print(f"coding agent | model: {options.model} | workspace: {options.cwd}")
    print("Type /help for commands.")
    try:
        while True:
            try:
                raw = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not raw:
                continue
            command = get_slash_command(raw)
            if command in {"/exit", "/quit"}:
                return 0
            if command == "/help":
                print(format_slash_command_help())
                continue
            if command == "/local":
                session.set_execution_mode("local")
                print(f"execution: local {session.execution_target}")
                continue
            if command == "/cloud":
                session.set_execution_mode("cloud")
                print(f"execution: cloud {session.execution_target}")
                continue
            if command == "/reset":
                session.reset()
                print("session reset")
                continue
            if command == "/model":
                for choice in session.list_models():
                    print(f"  {choice.label}")
                continue
            if command:
                print(f"unknown command {command}")
                continue
            assistant_ended_with_newline = True

            def annotate(message: str) -> None:
                nonlocal assistant_ended_with_newline
                if not assistant_ended_with_newline:
                    sys.stderr.write("\n")
                sys.stderr.write(f"{message}\n")
                assistant_ended_with_newline = True

            def write_assistant(text: str) -> None:
                nonlocal assistant_ended_with_newline
                sys.stdout.write(text)
                sys.stdout.flush()
                assistant_ended_with_newline = text.endswith("\n")

            session.send_prompt(
                raw,
                lambda event: render_plain_event(event, annotate, write_assistant),
            )
            print()
    finally:
        session.dispose()


def main(argv: list[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    if options.help:
        print_help()
        return 0
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        print("Set CURSOR_API_KEY before running the CLI.", file=sys.stderr)
        return 2
    if options.prompt:
        run_plain_prompt(api_key, options, options.prompt)
        if not options.prompt.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    if not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        if not prompt:
            print("No prompt provided on stdin.", file=sys.stderr)
            return 2
        run_plain_prompt(api_key, options, prompt)
        return 0
    if not sys.stdout.isatty():
        print("Interactive mode requires a TTY stdout.", file=sys.stderr)
        return 2
    return _run_repl(api_key, options)


if __name__ == "__main__":
    raise SystemExit(main())
