from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from opencursor._sandbox import (
    SandboxRuntime,
    effective_js_policy,
    locate_ripgrep,
    run_sandboxed_argv,
    sandbox_is_on,
)

GREP_TIMEOUT_S = 25.0
CLIENT_LIMIT_LINES = 2000
HARD_MAX_OUTPUT_LINES = 10_000
MAX_OUTPUT_BYTES = 8_388_608
MAX_COLUMNS = 1000
MISSING_RG = (
    "Ripgrep path not configured. Set CURSOR_RIPGREP_PATH or install the "
    "@cursor/sdk-<platform> optional package."
)

_cursor_ignore_cache: dict[str, bool] = {}


def exec_grep(
    cwd: Path,
    args: dict[str, Any],
    sandbox: SandboxRuntime | None = None,
) -> dict[str, Any]:
    pattern = str(args.get("pattern") or "")
    output_mode = str(args.get("output_mode") or "content")
    target = _rg_target(cwd, args.get("path"))
    raw_path = args.get("path")
    if raw_path:
        resolved = _resolve_path(cwd, str(raw_path))
        if not resolved.exists():
            return {"error": {"error": f"Path does not exist: {resolved}"}}
    rg = locate_ripgrep(excluded_workspace=cwd)
    if rg is None:
        return {"error": {"error": MISSING_RG}}
    try:
        rg_args = build_rg_args(cwd, args, output_mode, target, rg)
    except ValueError as exc:
        return {"error": {"error": str(exc)}}
    argv = [str(rg), *rg_args]
    env = _rg_env(os.environ, rg)
    policy = effective_js_policy(sandbox, args.get("sandbox_policy"))
    try:
        if sandbox_is_on(policy):
            binary = sandbox.binary if sandbox is not None else None
            if binary is None:
                return {"error": {"error": "Sandbox binary path was not configured"}}
            proc = run_sandboxed_argv(
                argv=argv,
                working_directory=cwd,
                workspace=sandbox.workspace if sandbox is not None else cwd,
                policy=policy,
                binary=binary,
                timeout_s=GREP_TIMEOUT_S,
                env=env,
            )
        else:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                timeout=GREP_TIMEOUT_S,
                stdin=subprocess.DEVNULL,
            )
    except subprocess.TimeoutExpired:
        return {"error": {"error": f"Timed out after {int(GREP_TIMEOUT_S)}s"}}
    except FileNotFoundError:
        return {"error": {"error": MISSING_RG}}
    except OSError as exc:
        return {"error": {"error": str(exc)}}
    stdout = proc.stdout or b""
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if len(stdout) > MAX_OUTPUT_BYTES:
        stdout = stdout[:MAX_OUTPUT_BYTES]
    if (stderr or proc.returncode == 2) and not stdout.strip():
        return {"error": {"error": stderr or f"ripgrep exited with code {proc.returncode}"}}
    text = stdout.decode("utf-8", errors="surrogateescape")
    if output_mode == "content":
        parsed = parse_content_output(text, CLIENT_LIMIT_LINES, HARD_MAX_OUTPUT_LINES)
        union = {"content": parsed}
    elif output_mode == "files_with_matches":
        parsed_files = parse_files_output(text, CLIENT_LIMIT_LINES, HARD_MAX_OUTPUT_LINES)
        union = {"files": parsed_files}
    elif output_mode == "count":
        parsed_count = parse_count_output(text, CLIENT_LIMIT_LINES, HARD_MAX_OUTPUT_LINES)
        union = {"count": parsed_count}
    else:
        return {"error": {"error": f"Unknown output mode: {output_mode}"}}
    path_out = "" if raw_path is None else str(raw_path)
    return {
        "success": {
            "pattern": pattern,
            "path": path_out,
            "output_mode": output_mode,
            "workspace_results": {str(cwd): union},
        }
    }


def build_rg_args(
    cwd: Path,
    args: dict[str, Any],
    output_mode: str,
    target: str,
    rg: Path,
) -> list[str]:
    argv: list[str] = []
    if supports_cursor_ignore(rg):
        for ignore in cursor_ignore_files(cwd):
            argv.extend(["--cursor-ignore", ignore])
    if output_mode == "content":
        argv.extend(["--line-number", "--with-filename", "--no-heading", "-0"])
        if args.get("context_before") is not None:
            argv.extend(["--before-context", str(int(args["context_before"]))])
        if args.get("context_after") is not None:
            argv.extend(["--after-context", str(int(args["context_after"]))])
        argv.extend(["--max-columns", str(MAX_COLUMNS), "--max-columns-preview"])
        if args.get("context") is not None and args.get("context_before") is None and args.get("context_after") is None:
            ctx = str(int(args["context"]))
            argv.extend(["--before-context", ctx, "--after-context", ctx])
    elif output_mode == "files_with_matches":
        argv.append("-l")
    elif output_mode == "count":
        argv.extend(["-c", "--with-filename"])
    else:
        raise ValueError(f"Unknown output mode: {output_mode}")
    if args.get("case_insensitive") is True:
        argv.append("--ignore-case")
    else:
        argv.append("--case-sensitive")
    if args.get("type"):
        argv.extend(["--type", str(args["type"])])
    if args.get("glob"):
        argv.extend(["--iglob", str(args["glob"])])
    if args.get("multiline"):
        argv.extend(["--multiline", "--multiline-dotall"])
    sort = args.get("sort") if args.get("sort") is not None else "modified"
    if sort != "none":
        flag = "--sort" if args.get("sort_ascending") is True else "--sortr"
        argv.extend([flag, str(sort)])
    argv.extend(["--no-config", "--color=never", "--hidden", "--follow"])
    argv.extend(["--regexp", str(args.get("pattern") or "")])
    argv.extend(["--", target])
    return argv


def parse_content_output(text: str, client_limit: int, hard_max: int) -> dict[str, Any]:
    by_file: dict[str, dict[str, Any]] = {}
    total_lines = 0
    total_matched = 0
    kept = 0
    for line in _iter_lines(text):
        total_lines += 1
        if line == "--":
            continue
        nul = line.find("\x00")
        if nul < 0:
            continue
        rest = line[nul + 1 :]
        digits = 0
        line_no = 0
        while digits < len(rest) and rest[digits].isdigit():
            line_no = line_no * 10 + (ord(rest[digits]) - 48)
            digits += 1
        if digits == 0 or digits >= len(rest):
            continue
        sep = rest[digits]
        if sep not in ":-":
            continue
        is_context = sep == "-"
        if not is_context:
            total_matched += 1
        if kept >= client_limit:
            continue
        path = line[:nul]
        content = rest[digits + 1 :]
        entry = by_file.get(path)
        if entry is None:
            entry = {"file": path, "matches": []}
            by_file[path] = entry
        entry["matches"].append(
            {
                "line_number": line_no,
                "content": content,
                "is_context_line": is_context,
            }
        )
        kept += 1
    return {
        "matches": list(by_file.values()),
        "total_lines": total_lines,
        "total_matched_lines": total_matched,
        "client_truncated": kept >= client_limit and total_lines > 0,
        "ripgrep_truncated": total_lines >= hard_max,
    }


def parse_files_output(text: str, client_limit: int, hard_max: int) -> dict[str, Any]:
    files: list[str] = []
    total = 0
    for line in _iter_lines(text):
        if not line:
            continue
        if total < client_limit:
            files.append(line)
        total += 1
    return {
        "files": files,
        "total_files": total,
        "client_truncated": total > client_limit,
        "ripgrep_truncated": total >= hard_max,
    }


def parse_count_output(text: str, client_limit: int, hard_max: int) -> dict[str, Any]:
    counts: list[dict[str, Any]] = []
    for line in _iter_lines(text):
        if not line:
            continue
        sep = line.rfind(":")
        if sep <= 0:
            continue
        try:
            count = int(line[sep + 1 :].strip())
        except ValueError:
            continue
        counts.append({"file": line[:sep], "count": count})
    total_files = len(counts)
    total_matches = sum(item["count"] for item in counts)
    kept = counts[:client_limit]
    return {
        "counts": kept,
        "total_files": total_files,
        "total_matches": total_matches,
        "client_truncated": total_files > client_limit,
        "ripgrep_truncated": total_files >= hard_max,
    }


def supports_cursor_ignore(rg: Path) -> bool:
    key = str(rg)
    cached = _cursor_ignore_cache.get(key)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [str(rg), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        ok = "cursor" in (proc.stdout or "").lower()
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _cursor_ignore_cache[key] = ok
    return ok


def cursor_ignore_files(root: Path) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    try:
        current = root.resolve()
    except OSError:
        return found
    for candidate in (current, *current.parents):
        path = candidate / ".cursorignore"
        try:
            if path.is_file():
                text = str(path)
                if text not in seen:
                    seen.add(text)
                    found.append(text)
        except OSError:
            continue
    return found


def _rg_target(cwd: Path, raw: str | None) -> str:
    if not raw:
        return "."
    path = _resolve_path(cwd, raw)
    try:
        rel = path.relative_to(cwd.resolve())
        return "." if str(rel) == "." else str(rel)
    except ValueError:
        return str(path)


def _resolve_path(cwd: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return path.expanduser().resolve()


def _rg_env(base: Mapping[str, str], rg: Path) -> dict[str, str]:
    env = dict(base)
    env["CURSOR_RIPGREP_PATH"] = str(rg)
    path_key = "PATH"
    if sys.platform == "win32":
        path_key = next((key for key in env if key.lower() == "path"), "Path")
    prefix = str(rg.parent)
    current = env.get(path_key, "")
    parts = [part for part in current.split(os.pathsep) if part and part != prefix]
    env[path_key] = os.pathsep.join([prefix, *parts])
    return env


def _iter_lines(text: str):
    start = 0
    length = len(text)
    while start < length:
        end = text.find("\n", start)
        if end < 0:
            line = text[start:]
            if line.endswith("\r"):
                line = line[:-1]
            if line:
                yield line
            return
        line = text[start:end]
        if line.endswith("\r"):
            line = line[:-1]
        yield line
        start = end + 1
