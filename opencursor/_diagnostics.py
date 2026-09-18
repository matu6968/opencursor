from __future__ import annotations

import ast
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterable

DIAGNOSTIC_SEVERITY_ERROR = 1
DIAGNOSTIC_SEVERITY_WARNING = 2
DIAGNOSTIC_SEVERITY_INFORMATION = 3
DIAGNOSTIC_SEVERITY_HINT = 4

TIMEOUT_S = 10.0
MAX_FILES = 40
MAX_DIAGNOSTICS = 200

SOURCE_EXTS = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".json"}
)
SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".cursor",
    }
)

_TSC_RE = re.compile(
    r"^(?P<file>.+)\((?P<line>\d+),(?P<col>\d+)\): (?P<level>error|warning) (?P<code>TS\d+): (?P<msg>.*)$"
)
_NODE_LINE_RE = re.compile(r":(\d+)\r?$")


def exec_diagnostics(cwd: Path, args: dict[str, Any]) -> dict[str, Any]:
    requested = args.get("path") or ""
    path = _resolve(cwd, requested)
    display = requested or str(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"file_not_found": {"path": display}}
    except PermissionError:
        return {"permission_denied": {"path": display}}
    except OSError as exc:
        return {"error": {"path": display, "error": str(exc)}}
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        return {"file_not_found": {"path": display}}
    try:
        diagnostics = collect_diagnostics(path, cwd)
    except subprocess.TimeoutExpired:
        return {"error": {"path": display, "error": "Request timed out after 10 seconds"}}
    except Exception as exc:
        return {"error": {"path": display, "error": str(exc)}}
    if len(diagnostics) > MAX_DIAGNOSTICS:
        diagnostics = diagnostics[:MAX_DIAGNOSTICS]
    return {
        "success": {
            "path": display,
            "diagnostics": diagnostics,
            "total_diagnostics": len(diagnostics),
        }
    }


def collect_diagnostics(path: Path, cwd: Path) -> list[dict[str, Any]]:
    files = list(_files_for(path))
    out: list[dict[str, Any]] = []
    for file in files:
        items = _diagnostics_for_file(file, cwd)
        prefix = _rel(file, path) if path.is_dir() else None
        for item in items:
            if prefix:
                item = dict(item)
                item["message"] = f"{prefix}: {item.get('message') or ''}"
            out.append(item)
            if len(out) >= MAX_DIAGNOSTICS:
                return out
    return out


def make_diagnostic(
    *,
    severity: int,
    message: str,
    source: str,
    line: int = 1,
    column: int = 1,
    end_line: int | None = None,
    end_column: int | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "severity": severity,
        "range": _range(line, column, end_line, end_column),
        "message": message,
        "source": source,
    }
    if code:
        body["code"] = str(code)
    return body


def _resolve(cwd: Path, raw: str) -> Path:
    path = Path(raw or ".")
    if not path.is_absolute():
        path = cwd / path
    return path.expanduser().resolve()


def _files_for(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    count = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith(".")]
        for name in files:
            file = Path(root) / name
            if file.suffix.lower() not in SOURCE_EXTS:
                continue
            yield file
            count += 1
            if count >= MAX_FILES:
                return


def _diagnostics_for_file(path: Path, cwd: Path) -> list[dict[str, Any]]:
    ext = path.suffix.lower()
    if ext in {".py", ".pyi"}:
        return _python_diagnostics(path, cwd)
    if ext == ".json":
        return _json_diagnostics(path)
    if ext in {".js", ".jsx", ".mjs", ".cjs"}:
        return _javascript_diagnostics(path, cwd)
    if ext in {".ts", ".tsx"}:
        return _typescript_diagnostics(path, cwd)
    return []


def _python_diagnostics(path: Path, cwd: Path) -> list[dict[str, Any]]:
    ruff = shutil.which("ruff")
    if ruff:
        return _ruff_diagnostics(ruff, path, cwd)
    return _python_syntax(path)


def _python_syntax(path: Path) -> list[dict[str, Any]]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            make_diagnostic(
                severity=DIAGNOSTIC_SEVERITY_ERROR,
                message=str(exc),
                source="python",
            )
        ]
    try:
        ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [
            make_diagnostic(
                severity=DIAGNOSTIC_SEVERITY_ERROR,
                message=exc.msg or "invalid syntax",
                source="python",
                line=exc.lineno or 1,
                column=exc.offset or 1,
                code="syntax",
            )
        ]
    return []


def _ruff_diagnostics(ruff: str, path: Path, cwd: Path) -> list[dict[str, Any]]:
    proc = _run([ruff, "check", "--output-format", "json", str(path)], cwd)
    if not (proc.stdout or "").strip():
        return _python_syntax(path) if proc.returncode not in (0, 1) else []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _python_syntax(path)
    out: list[dict[str, Any]] = []
    for item in payload if isinstance(payload, list) else []:
        loc = item.get("location") or {}
        end = item.get("end_location") or {}
        code = item.get("code") or ""
        severity = (
            DIAGNOSTIC_SEVERITY_ERROR
            if str(code).startswith(("E", "F8", "S"))
            else DIAGNOSTIC_SEVERITY_WARNING
        )
        if str(code) in {"E999", "invalid-syntax"} or "SyntaxError" in str(item.get("message") or ""):
            severity = DIAGNOSTIC_SEVERITY_ERROR
        out.append(
            make_diagnostic(
                severity=severity,
                message=item.get("message") or str(code) or "ruff",
                source="ruff",
                line=int(loc.get("row") or 1),
                column=int(loc.get("column") or 1),
                end_line=int(end.get("row") or loc.get("row") or 1),
                end_column=int(end.get("column") or loc.get("column") or 1),
                code=str(code) if code else None,
            )
        )
    return out


def _json_diagnostics(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [make_diagnostic(severity=DIAGNOSTIC_SEVERITY_ERROR, message=str(exc), source="json")]
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return [
            make_diagnostic(
                severity=DIAGNOSTIC_SEVERITY_ERROR,
                message=exc.msg,
                source="json",
                line=exc.lineno,
                column=exc.colno,
                code="parse",
            )
        ]
    return []


def _javascript_diagnostics(path: Path, cwd: Path) -> list[dict[str, Any]]:
    eslint = shutil.which("eslint")
    if eslint:
        items = _eslint_diagnostics(eslint, path, cwd)
        if items is not None:
            return items
    node = shutil.which("node")
    if node:
        return _node_check(node, path, cwd)
    return []


def _typescript_diagnostics(path: Path, cwd: Path) -> list[dict[str, Any]]:
    eslint = shutil.which("eslint")
    if eslint:
        items = _eslint_diagnostics(eslint, path, cwd)
        if items is not None:
            return items
    tsc = shutil.which("tsc")
    if tsc:
        return _tsc_diagnostics(tsc, path, cwd)
    return []


def _eslint_diagnostics(eslint: str, path: Path, cwd: Path) -> list[dict[str, Any]] | None:
    proc = _run([eslint, "-f", "json", "--no-error-on-unmatched-pattern", str(path)], cwd)
    raw = (proc.stdout or "").strip()
    if not raw.startswith("["):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    out: list[dict[str, Any]] = []
    for file_result in payload if isinstance(payload, list) else []:
        for msg in file_result.get("messages") or []:
            severity = (
                DIAGNOSTIC_SEVERITY_ERROR
                if int(msg.get("severity") or 0) >= 2
                else DIAGNOSTIC_SEVERITY_WARNING
            )
            out.append(
                make_diagnostic(
                    severity=severity,
                    message=msg.get("message") or "eslint",
                    source="eslint",
                    line=int(msg.get("line") or 1),
                    column=int(msg.get("column") or 1),
                    end_line=int(msg.get("endLine") or msg.get("line") or 1),
                    end_column=int(msg.get("endColumn") or msg.get("column") or 1),
                    code=str(msg["ruleId"]) if msg.get("ruleId") else None,
                )
            )
    return out


def _node_check(node: str, path: Path, cwd: Path) -> list[dict[str, Any]]:
    proc = _run([node, "--check", str(path)], cwd)
    if proc.returncode == 0:
        return []
    stderr = proc.stderr or proc.stdout or "invalid syntax"
    line = 1
    first = stderr.splitlines()[0] if stderr.splitlines() else ""
    match = _NODE_LINE_RE.search(first)
    if match:
        line = int(match.group(1))
    message = stderr.strip().splitlines()[-1] if stderr.strip() else "invalid syntax"
    return [
        make_diagnostic(
            severity=DIAGNOSTIC_SEVERITY_ERROR,
            message=message,
            source="node",
            line=line,
            code="syntax",
        )
    ]


def _tsc_diagnostics(tsc: str, path: Path, cwd: Path) -> list[dict[str, Any]]:
    proc = _run(
        [
            tsc,
            "--noEmit",
            "--pretty",
            "false",
            "--skipLibCheck",
            "--isolatedModules",
            "--strict",
            "false",
            str(path),
        ],
        cwd,
    )
    out: list[dict[str, Any]] = []
    for raw in (proc.stdout or proc.stderr or "").splitlines():
        match = _TSC_RE.match(raw.strip())
        if not match:
            continue
        level = match.group("level")
        out.append(
            make_diagnostic(
                severity=(
                    DIAGNOSTIC_SEVERITY_ERROR
                    if level == "error"
                    else DIAGNOSTIC_SEVERITY_WARNING
                ),
                message=match.group("msg"),
                source="tsc",
                line=int(match.group("line")),
                column=int(match.group("col")),
                code=match.group("code"),
            )
        )
    return out


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        stdin=subprocess.DEVNULL,
    )


def _range(line: int, column: int, end_line: int | None, end_column: int | None) -> dict[str, Any]:
    start_line = max(int(line or 1) - 1, 0)
    start_col = max(int(column or 1) - 1, 0)
    stop_line = max(int(end_line or line or 1) - 1, start_line)
    stop_col = max(int(end_column or column or 1) - 1, 0)
    if stop_line == start_line and stop_col <= start_col:
        stop_col = start_col + 1
    return {
        "start": {"line": start_line, "column": start_col},
        "end": {"line": stop_line, "column": stop_col},
    }


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name
