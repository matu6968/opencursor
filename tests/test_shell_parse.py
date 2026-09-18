from __future__ import annotations

from pathlib import Path

from opencursor._local_executor import _exec_shell
from opencursor._protobuf import decode_message, encode_message, oneof_case
from opencursor._shell_parse import (
    denylist_reason,
    parse_shell_command,
    parsing_result_proto,
    unescape_shell_word,
)


def test_parse_pipeline_and_dev_null_redirect() -> None:
    parsed = parse_shell_command("ls -la /tmp > /dev/null && echo hi")
    legacy = parsed["legacy"]
    structured = parsed["structured"]
    assert structured["parsing_failed"] is False
    assert legacy["simple_commands"] == ["ls", "echo"]
    assert legacy["has_output_redirect"] is True
    assert structured["has_redirects"] is True
    assert structured["all_redirects_are_dev_null"] is True
    assert structured["executable_commands"][0]["name"] == "ls"
    assert [arg["value"] for arg in structured["executable_commands"][0]["args"]] == ["-la", "/tmp"]
    assert structured["executable_commands"][1]["full_text"] == "echo hi"
    assert structured["redirects"][0]["operator"] == ">"
    assert structured["redirects"][0]["target_text"] == "/dev/null"


def test_parse_command_substitution() -> None:
    parsed = parse_shell_command("echo $(uname -s)")
    assert parsed["structured"]["has_command_substitution"] is True
    names = parsed["legacy"]["simple_commands"]
    assert "echo" in names
    assert "uname" in names


def test_parse_heredoc_is_input_redirect() -> None:
    parsed = parse_shell_command("cat <<EOF\nhello\nEOF\n")
    assert parsed["legacy"]["has_input_redirect"] is True
    assert parsed["structured"]["has_redirects"] is True
    assert parsed["legacy"]["simple_commands"] == ["cat"]


def test_denylist_fail_closed_on_empty_executables() -> None:
    reason = denylist_reason("!!!", {"parsing_failed": True, "executable_commands": []}, ["rm"])
    assert reason is not None
    assert "fail-closed" in reason


def test_denylist_blocks_matching_command() -> None:
    parsed = parse_shell_command("rm -rf /tmp/x")
    reason = denylist_reason("rm -rf /tmp/x", parsed["structured"], ["rm"])
    assert reason is not None
    assert "denylist rule: rm" in reason
    assert denylist_reason("rm -rf /tmp/x", parsed["structured"], ["aws"]) is None


def test_denylist_colon_rule() -> None:
    parsed = parse_shell_command("aws s3 rm s3://bucket/key")
    reason = denylist_reason("aws s3 rm s3://bucket/key", parsed["structured"], ["aws:*s3 rm*"])
    assert reason is not None


def test_exec_shell_rejected_does_not_run() -> None:
    result = _exec_shell(
        Path("/tmp"),
        {"command": "rm -rf /tmp/x", "admin_command_denylist": ["rm"], "timeout": 1000},
    )
    assert "rejected" in result
    assert "denylist" in result["rejected"]["reason"]


def test_parsing_result_roundtrip() -> None:
    parsed = parse_shell_command("ls > /dev/null")
    msg = {
        "id": 2,
        "exec_id": "e-shell",
        "shell_args": {
            "command": "ls > /dev/null",
            "working_directory": "/tmp",
            "simple_commands": parsed["legacy"]["simple_commands"],
            "parsing_result": parsing_result_proto(parsed["structured"]),
        },
    }
    decoded = decode_message(
        "agent.v1.ExecServerMessage", encode_message("agent.v1.ExecServerMessage", msg)
    )
    assert oneof_case(decoded, "message") == "shell_args"
    assert decoded["shell_args"]["simple_commands"] == ["ls"]
    assert decoded["shell_args"]["parsing_result"]["has_redirects"] is True
    assert decoded["shell_args"]["parsing_result"]["all_redirects_are_dev_null"] is True


def test_unescape_ansi_c_string() -> None:
    assert unescape_shell_word("$'a\\nb'") == "a b"
