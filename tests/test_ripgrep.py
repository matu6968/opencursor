from __future__ import annotations

from pathlib import Path

from opencursor._ripgrep import (
    MISSING_RG,
    build_rg_args,
    exec_grep,
    parse_content_output,
    parse_count_output,
    parse_files_output,
)
from opencursor._sandbox import locate_ripgrep

VENDORED_RG = (
    Path(__file__).resolve().parents[1]
    / "official"
    / "cursor_sdk"
    / "_vendor"
    / "bridge"
    / "node_modules"
    / "@cursor"
    / "sdk-linux-x64"
    / "bin"
    / "rg"
)


def _rg_path(monkeypatch) -> Path:
    if VENDORED_RG.is_file():
        monkeypatch.setenv("CURSOR_RIPGREP_PATH", str(VENDORED_RG))
        return VENDORED_RG
    found = locate_ripgrep()
    assert found is not None, "need CURSOR_RIPGREP_PATH or a vendored @cursor/sdk-*/bin/rg"
    monkeypatch.setenv("CURSOR_RIPGREP_PATH", str(found))
    return found


def test_parse_content_nul_and_context() -> None:
    raw = (
        "skip.txt\x001:ignored hello\n"
        "--\n"
        "a.py\x001-alpha\n"
        "a.py\x002:hello world\n"
        "a.py\x003-omega\n"
        "--\n"
        "b.rs\x001:hello rust\n"
    )
    parsed = parse_content_output(raw, client_limit=2000, hard_max=10_000)
    files = {item["file"]: item["matches"] for item in parsed["matches"]}
    assert parsed["total_matched_lines"] == 3
    assert files["a.py"][0]["is_context_line"] is True
    assert files["a.py"][1]["content"] == "hello world"
    assert files["a.py"][1]["line_number"] == 2
    assert parsed["client_truncated"] is False
    assert parsed["ripgrep_truncated"] is False


def test_parse_files_and_count() -> None:
    files = parse_files_output("a.py\nb.rs\n", 2000, 10_000)
    assert files["files"] == ["a.py", "b.rs"]
    assert files["total_files"] == 2
    counts = parse_count_output("a.py:1\nb.rs:2\n", 2000, 10_000)
    assert counts["total_matches"] == 3
    assert counts["counts"][1] == {"file": "b.rs", "count": 2}


def test_exec_grep_content_and_ignore(tmp_path, monkeypatch) -> None:
    _rg_path(monkeypatch)
    (tmp_path / "a.py").write_text("alpha\nhello world\nomega\nHello again\n", encoding="utf-8")
    (tmp_path / "b.rs").write_text("hello rust\n", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("ignored hello\n", encoding="utf-8")
    (tmp_path / ".cursorignore").write_text("skip.txt\n", encoding="utf-8")
    result = exec_grep(tmp_path, {"pattern": "hello", "path": str(tmp_path)})
    assert "success" in result
    content = result["success"]["workspace_results"][str(tmp_path)]["content"]
    files = {item["file"] for item in content["matches"]}
    assert any(name.endswith("a.py") for name in files)
    assert any(name.endswith("b.rs") for name in files)
    assert not any(name.endswith("skip.txt") for name in files)
    assert content["total_matched_lines"] == 2


def test_exec_grep_files_count_and_case(tmp_path, monkeypatch) -> None:
    _rg_path(monkeypatch)
    (tmp_path / "a.py").write_text("Hello\nhello\n", encoding="utf-8")
    (tmp_path / "b.rs").write_text("hello\n", encoding="utf-8")
    files = exec_grep(
        tmp_path,
        {"pattern": "hello", "path": str(tmp_path), "output_mode": "files_with_matches"},
    )
    listed = files["success"]["workspace_results"][str(tmp_path)]["files"]["files"]
    assert len(listed) == 2
    counts = exec_grep(
        tmp_path,
        {
            "pattern": "hello",
            "path": str(tmp_path),
            "output_mode": "count",
            "case_insensitive": True,
        },
    )
    total = counts["success"]["workspace_results"][str(tmp_path)]["count"]["total_matches"]
    assert total == 3


def test_exec_grep_missing_binary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_RIPGREP_PATH", str(tmp_path / "no-such-rg"))
    monkeypatch.setattr("opencursor._ripgrep.locate_ripgrep", lambda **kwargs: None)
    result = exec_grep(tmp_path, {"pattern": "hello"})
    assert result["error"]["error"] == MISSING_RG


def test_exec_grep_missing_path(tmp_path, monkeypatch) -> None:
    _rg_path(monkeypatch)
    result = exec_grep(tmp_path, {"pattern": "hello", "path": str(tmp_path / "missing")})
    assert "does not exist" in result["error"]["error"]


def test_build_rg_args_flags(tmp_path, monkeypatch) -> None:
    rg = _rg_path(monkeypatch)
    argv = build_rg_args(
        tmp_path,
        {
            "pattern": "foo",
            "glob": "*.py",
            "multiline": True,
            "case_insensitive": True,
            "context": 2,
        },
        "content",
        ".",
        rg,
    )
    assert argv[:4] == ["--line-number", "--with-filename", "--no-heading", "-0"] or argv[0] == "--cursor-ignore"
    assert "--ignore-case" in argv
    assert "--iglob" in argv and "*.py" in argv
    assert "--multiline" in argv and "--multiline-dotall" in argv
    assert "--sortr" in argv and "modified" in argv
    assert argv[-2:] == ["--", "."]
