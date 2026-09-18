#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Run a JSON DAG as local Cursor SDK subagents.

Mirrors cookbook/sdk/dag-task-runner/src/run_dag.ts.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

_EXAMPLES = Path(__file__).resolve().parents[2]
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from cookbook._sdk import Agent, LocalAgentOptions  # noqa: E402
from cookbook.dag_task_runner.canvas_writer import (  # noqa: E402
    CanvasWriter,
    RunState,
    TaskState,
    clone_state,
    initial_run_state,
)
from cookbook.dag_task_runner.dag import (  # noqa: E402
    RawTask,
    create_model_resolver,
    compute_ranks,
    parse_dag,
    validate_model_map,
)

STREAM_CAP = 4000
DEFAULT_TASK_TIMEOUT_MS = 20 * 60 * 1000
DEFAULT_STREAM_PUBLISH_MS = 500
DEFAULT_STREAM_IDLE_TIMEOUT_MS = 5 * 60 * 1000
WAIT_AFTER_STREAM_GRACE_MS = 15 * 1000
UPSTREAM_SNIPPET_CAP = 2000


class TimeoutError_(TimeoutError):
    pass


class BoundedTextBuffer:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.data = ""
        self.dropped_chars = 0

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        self.data += chunk
        if len(self.data) <= self.cap:
            return
        overflow = len(self.data) - self.cap
        self.dropped_chars += overflow
        self.data = self.data[overflow:]

    def render(self) -> str:
        if self.dropped_chars == 0:
            return self.data
        return f"[...truncated {self.dropped_chars} earlier chars...]\n{self.data}"


def parse_args(argv: list[str]) -> dict[str, Any]:
    args: dict[str, str] = {}
    index = 0
    while index < len(argv):
        item = argv[index]
        if not item.startswith("--"):
            index += 1
            continue
        key = item[2:]
        nxt = argv[index + 1] if index + 1 < len(argv) else None
        if nxt and not nxt.startswith("--"):
            args[key] = nxt
            index += 2
        else:
            args[key] = "true"
            index += 1
    if "dag" not in args:
        raise SystemExit("--dag <path> is required")
    cwd = args.get("cwd") or os.getcwd()
    canvas_path = args.get("canvas-path")
    if not canvas_path:
        if "canvas" not in args:
            raise SystemExit("Provide either --canvas-path <abs-path> or --canvas <name>")
        canvases_dir = args.get("canvases-dir") or _default_canvases_dir(cwd)
        stem = args["canvas"].removesuffix(".canvas.tsx")
        canvas_path = str(Path(canvases_dir) / f"{stem}.canvas.tsx")
    if not canvas_path.endswith(".canvas.tsx"):
        canvas_path = canvas_path.removesuffix(".tsx") + ".canvas.tsx"
    return {
        "dag": args["dag"],
        "canvas_path": canvas_path,
        "cwd": cwd,
        "models_file": args.get("models-file"),
        "debounce_ms": _positive_int(args.get("debounce"), 200, "--debounce"),
        "task_timeout_ms": _positive_int(args.get("task-timeout-ms"), DEFAULT_TASK_TIMEOUT_MS, "--task-timeout-ms"),
        "stream_publish_ms": _positive_int(args.get("stream-publish-ms"), DEFAULT_STREAM_PUBLISH_MS, "--stream-publish-ms"),
        "stream_idle_timeout_ms": _positive_int(
            args.get("stream-idle-timeout-ms"),
            DEFAULT_STREAM_IDLE_TIMEOUT_MS,
            "--stream-idle-timeout-ms",
        ),
        "init_only": args.get("init-only") == "true",
    }


def _positive_int(raw: str | None, fallback: int, flag: str) -> int:
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{flag} must be a positive integer") from exc
    if value <= 0:
        raise SystemExit(f"{flag} must be a positive integer")
    return value


def _default_canvases_dir(cwd: str) -> str:
    slug = "-".join(
        "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in part)
        for part in Path(cwd).as_posix().strip("/").split("/")
    )
    return str(Path.home() / ".cursor" / "projects" / slug / "canvases")


def format_ms(ms: float) -> str:
    if ms < 1000:
        return f"{int(ms)}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rem = round(seconds - minutes * 60)
    return f"{minutes}m {rem}s"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _event_text(event: Any) -> str:
    if getattr(event, "type", None) != "assistant":
        return ""
    message = getattr(event, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        kind = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if kind == "text":
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(str(text))
    return "".join(parts)


def build_upstream_context(task: RawTask, state_by_id: dict[str, TaskState]) -> str:
    if not task.depends_on:
        return ""
    lines = ["Upstream task results (for context — do not re-do this work):", ""]
    for dep_id in task.depends_on:
        dep = state_by_id.get(dep_id)
        if dep is None:
            continue
        snippet = (
            truncate(dep.resultText, UPSTREAM_SNIPPET_CAP)
            if dep.resultText
            else (f"(failed: {dep.errorMessage})" if dep.errorMessage else "(no output)")
        )
        lines.extend([f"### {dep_id} [{dep.status}]", snippet, ""])
    return "\n".join(lines)


def skip_task(task: RawTask, state_by_id: dict[str, TaskState], state: RunState, writer: CanvasWriter, failed_deps: list[str]) -> None:
    ts = state_by_id[task.id]
    now = int(time.time() * 1000)
    ts.status = "ERROR"
    ts.finishedAt = now
    ts.durationMs = 0
    ts.errorMessage = f"Skipped: upstream task(s) {', '.join(failed_deps)} failed"
    print(f"[dag-runner] skipping {task.id} — upstream {', '.join(failed_deps)} failed")
    writer.schedule(clone_state(state))


def mark_run_terminated(state: RunState, message: str, outcome: str) -> None:
    now = int(time.time() * 1000)
    state.runOutcome = outcome  # type: ignore[assignment]
    state.runMessage = message
    state.finishedAt = now
    for task in state.tasks:
        if task.status in {"FINISHED", "ERROR"}:
            continue
        task.status = "ERROR"
        task.errorMessage = "Runner interrupted" if outcome == "INTERRUPTED" else "Runner terminated"
        task.finishedAt = now
        task.durationMs = now - task.startedAt if task.startedAt else 0


def _best_effort_cancel(run: Any, task_id: str) -> None:
    cancel = getattr(run, "cancel", None)
    if not callable(cancel):
        return
    try:
        cancel()
    except Exception as exc:
        print(f"[dag-runner] failed to cancel timed-out task {task_id}: {exc}", file=sys.stderr)


def run_task(
    task: RawTask,
    state_by_id: dict[str, TaskState],
    state: RunState,
    writer: CanvasWriter,
    cwd: str,
    *,
    task_timeout_ms: int,
    stream_publish_ms: int,
    stream_idle_timeout_ms: int,
    stop: threading.Event,
) -> None:
    ts = state_by_id[task.id]
    ts.status = "RUNNING"
    ts.startedAt = int(time.time() * 1000)
    writer.schedule(clone_state(state))
    upstream = build_upstream_context(task, state_by_id)
    stitched = f"{upstream}\n\n---\n\n{task.subtask_prompt}" if upstream else task.subtask_prompt
    agent = Agent.create(
        api_key=os.environ.get("CURSOR_API_KEY"),
        model=ts.model,
        local=LocalAgentOptions(cwd=cwd),
    )
    run = None
    buffer = BoundedTextBuffer(STREAM_CAP)
    last_publish = 0.0
    deadline = time.time() * 1000 + task_timeout_ms

    def publish(force: bool = False) -> None:
        nonlocal last_publish
        now = time.time() * 1000
        if not force and now - last_publish < stream_publish_ms:
            return
        text = buffer.render()
        if text.strip():
            ts.resultText = text
        writer.schedule(clone_state(state))
        last_publish = now

    try:
        run = agent.send(stitched)
        stream = iter(run.stream())
        while True:
            if stop.is_set():
                raise TimeoutError_(f"Task {task.id} interrupted")
            remaining = deadline - time.time() * 1000
            timeout_for_next = min(remaining, stream_idle_timeout_ms)
            if timeout_for_next <= 0:
                raise TimeoutError_(f"Task {task.id} exceeded deadline of {format_ms(task_timeout_ms)}")
            try:
                event = _next_with_timeout(stream, timeout_for_next / 1000)
            except StopIteration:
                break
            chunk = _event_text(event)
            if chunk:
                buffer.append(chunk)
                publish()
        wait_grace = min(deadline - time.time() * 1000, WAIT_AFTER_STREAM_GRACE_MS)
        if wait_grace <= 0:
            raise TimeoutError_(f"Task {task.id} exceeded deadline of {format_ms(task_timeout_ms)}")
        result = run.wait()
        ts.finishedAt = int(time.time() * 1000)
        ts.durationMs = _attr(result, "duration_ms", "durationMs") or (ts.finishedAt - (ts.startedAt or ts.finishedAt))
        usage = _attr(result, "usage")
        ts.inputTokens = _attr(usage, "input_tokens", "inputTokens")
        ts.outputTokens = _attr(usage, "output_tokens", "outputTokens")
        rendered = buffer.render().strip()
        if rendered:
            ts.resultText = rendered
        status = str(_attr(result, "status") or "")
        if status == "finished":
            ts.status = "FINISHED"
        else:
            ts.status = "ERROR"
            ts.errorMessage = f"Run {status}"
    except Exception as exc:
        if run is not None and isinstance(exc, TimeoutError_):
            _best_effort_cancel(run, task.id)
        ts.finishedAt = int(time.time() * 1000)
        ts.durationMs = ts.finishedAt - (ts.startedAt or ts.finishedAt)
        ts.status = "ERROR"
        ts.errorMessage = str(exc)
        rendered = buffer.render().strip()
        if rendered:
            ts.resultText = rendered
    finally:
        if run is not None and getattr(run, "status", None) == "running":
            _best_effort_cancel(run, task.id)
        publish(True)
        closer = getattr(agent, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:
                pass
        writer.schedule(clone_state(state))


def _attr(value: Any, *names: str, default: Any = None) -> Any:
    if value is None:
        return default
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


def _next_with_timeout(iterator: Any, timeout_s: float) -> Any:
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = next(iterator)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError_("stream idle timeout")
    if "error" in box:
        raise box["error"]
    return box["value"]


def run_dag(args: dict[str, Any] | None = None, argv: list[str] | None = None) -> int:
    parsed = args or parse_args(sys.argv[1:] if argv is None else argv)
    if not parsed["init_only"] and not os.environ.get("CURSOR_API_KEY"):
        raise SystemExit("CURSOR_API_KEY is not set.")
    raw = json.loads(Path(parsed["dag"]).read_text(encoding="utf-8"))
    dag = parse_dag(raw)
    file_models = None
    if parsed["models_file"]:
        file_models = validate_model_map(
            json.loads(Path(parsed["models_file"]).read_text(encoding="utf-8")),
            f"--models-file {parsed['models_file']}",
        )
    model_for = create_model_resolver({**(dag.models or {}), **(file_models or {})})
    ranks = compute_ranks(dag)
    state = initial_run_state(dag, model_for)
    state_by_id = {task.id: task for task in state.tasks}
    writer = CanvasWriter(parsed["canvas_path"], parsed["debounce_ms"])
    print(f'[dag-runner] DAG "{dag.title}" — {len(dag.tasks)} tasks across {len(ranks)} rank(s)')
    print(f"[dag-runner] canvas → {parsed['canvas_path']}")
    writer.schedule(clone_state(state))
    writer.flush()
    if parsed["init_only"]:
        print("[dag-runner] --init-only: initial canvas written, exiting")
        return 0

    stop = threading.Event()

    def on_signal(_signum: int, _frame: Any) -> None:
        print("[dag-runner] received signal; finalizing canvas before exit", file=sys.stderr)
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        for index, rank in enumerate(ranks):
            print(f"[dag-runner] rank {index + 1}/{len(ranks)}: {', '.join(task.id for task in rank)}")
            with ThreadPoolExecutor(max_workers=max(1, len(rank))) as pool:
                futures = []
                for task in rank:
                    failed = [
                        dep
                        for dep in task.depends_on
                        if state_by_id.get(dep) is not None and state_by_id[dep].status == "ERROR"
                    ]
                    if failed:
                        skip_task(task, state_by_id, state, writer, failed)
                        continue
                    futures.append(
                        pool.submit(
                            run_task,
                            task,
                            state_by_id,
                            state,
                            writer,
                            parsed["cwd"],
                            task_timeout_ms=parsed["task_timeout_ms"],
                            stream_publish_ms=parsed["stream_publish_ms"],
                            stream_idle_timeout_ms=parsed["stream_idle_timeout_ms"],
                            stop=stop,
                        )
                    )
                wait(futures)
                for future in futures:
                    future.result()
        state.finishedAt = int(time.time() * 1000)
        errors = [task for task in state.tasks if task.status == "ERROR"]
        state.runOutcome = "FAILED" if errors else "SUCCESS"
        if errors:
            state.runMessage = "Some tasks failed: " + ", ".join(task.id for task in errors)
        writer.schedule(clone_state(state))
        writer.flush()
        elapsed = (state.finishedAt or 0) - state.startedAt
        print(
            f"[dag-runner] done — {len(state.tasks) - len(errors)}/{len(state.tasks)} succeeded in {format_ms(elapsed)}"
        )
        if errors:
            print("[dag-runner] errors: " + ", ".join(task.id for task in errors))
            return 1
        return 0
    except Exception as exc:
        mark_run_terminated(state, f"Runner failed: {exc}", "FAILED")
        writer.schedule(clone_state(state))
        writer.flush()
        raise
    finally:
        if state.finishedAt is None:
            mark_run_terminated(state, "Runner exited before finalization", "FAILED")
            writer.schedule(clone_state(state))
            writer.flush()


def main() -> int:
    try:
        return run_dag()
    except Exception as exc:
        print(f"[dag-runner] fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
