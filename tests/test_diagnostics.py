from __future__ import annotations

import json
from pathlib import Path

from opencursor._diagnostics import (
    DIAGNOSTIC_SEVERITY_ERROR,
    collect_diagnostics,
    exec_diagnostics,
    make_diagnostic,
)
from opencursor._local_executor import resolve_allowed_tools
from opencursor._protobuf import decode_message, encode_message, oneof_case
from opencursor.types import AgentOptions


def test_python_syntax_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.py"
    path.write_text("def broken(\n", encoding="utf-8")
    result = exec_diagnostics(tmp_path, {"path": str(path)})
    assert "success" in result
    diags = result["success"]["diagnostics"]
    assert result["success"]["total_diagnostics"] == len(diags) >= 1
    assert diags[0]["severity"] == DIAGNOSTIC_SEVERITY_ERROR
    assert diags[0]["source"] in {"python", "ruff"}
    assert diags[0]["range"]["start"]["line"] >= 0


def test_valid_python_has_no_syntax_diagnostics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("opencursor._diagnostics.shutil.which", lambda name: None)
    path = tmp_path / "ok.py"
    path.write_text("x = 1\n", encoding="utf-8")
    result = exec_diagnostics(tmp_path, {"path": str(path)})
    assert result["success"]["diagnostics"] == []
    assert result["success"]["total_diagnostics"] == 0


def test_missing_file(tmp_path: Path) -> None:
    result = exec_diagnostics(tmp_path, {"path": "nope.py"})
    assert "file_not_found" in result
    assert result["file_not_found"]["path"] == "nope.py"


def test_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    result = exec_diagnostics(tmp_path, {"path": "bad.json"})
    assert result["success"]["total_diagnostics"] >= 1
    assert result["success"]["diagnostics"][0]["source"] == "json"


def test_directory_prefixes_relative_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("opencursor._diagnostics.shutil.which", lambda name: None)
    nested = tmp_path / "pkg"
    nested.mkdir()
    (nested / "bad.py").write_text("def broken(\n", encoding="utf-8")
    result = exec_diagnostics(tmp_path, {"path": str(tmp_path)})
    messages = [item["message"] for item in result["success"]["diagnostics"]]
    assert any("pkg/bad.py:" in msg or "pkg\\bad.py:" in msg for msg in messages)


def test_make_diagnostic_is_zero_based() -> None:
    diag = make_diagnostic(
        severity=DIAGNOSTIC_SEVERITY_ERROR,
        message="x",
        source="python",
        line=2,
        column=5,
        end_line=2,
        end_column=8,
        code="syntax",
    )
    assert diag["range"]["start"] == {"line": 1, "column": 4}
    assert diag["range"]["end"] == {"line": 1, "column": 7}
    assert diag["code"] == "syntax"


def test_ruff_json_is_parsed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "unused.py"
    path.write_text("import os\n", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/ruff" if name == "ruff" else None

    def fake_run(argv, cwd):
        payload = [
            {
                "code": "F401",
                "message": "`os` imported but unused",
                "location": {"row": 1, "column": 8},
                "end_location": {"row": 1, "column": 10},
            }
        ]

        class Proc:
            returncode = 1
            stdout = json.dumps(payload)
            stderr = ""

        return Proc()

    monkeypatch.setattr("opencursor._diagnostics.shutil.which", fake_which)
    monkeypatch.setattr("opencursor._diagnostics._run", fake_run)
    diags = collect_diagnostics(path, tmp_path)
    assert len(diags) == 1
    assert diags[0]["source"] == "ruff"
    assert diags[0]["code"] == "F401"
    assert diags[0]["range"]["start"]["column"] == 7


def test_diagnostics_result_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("opencursor._diagnostics.shutil.which", lambda name: None)
    path = tmp_path / "bad.py"
    path.write_text("def broken(\n", encoding="utf-8")
    payload = exec_diagnostics(tmp_path, {"path": "bad.py"})
    msg = {"id": 3, "exec_id": "e-diag", "diagnostics_result": payload}
    decoded = decode_message(
        "agent.v1.ExecClientMessage", encode_message("agent.v1.ExecClientMessage", msg)
    )
    assert oneof_case(decoded, "message") == "diagnostics_result"
    assert oneof_case(decoded["diagnostics_result"], "result") == "success"
    assert decoded["diagnostics_result"]["success"]["total_diagnostics"] >= 1
    first = decoded["diagnostics_result"]["success"]["diagnostics"][0]
    assert first["severity"] == DIAGNOSTIC_SEVERITY_ERROR
    assert "start" in first["range"]


def test_read_lints_is_in_default_and_public_map() -> None:
    opts = AgentOptions.model_validate({"model": {"id": "x"}})
    assert "read_lints_tool_call" in resolve_allowed_tools(opts)
    named = AgentOptions.model_validate({"model": {"id": "x"}, "tools": ["readLints"]})
    assert resolve_allowed_tools(named) == ["read_lints_tool_call"]
