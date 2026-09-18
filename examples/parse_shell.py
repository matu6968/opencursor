# SPDX-License-Identifier: MIT-0
"""Parse a shell command with tree-sitter-bash (same analysis the local executor uses)."""

import json
import sys

from opencursor._shell_parse import parse_shell_command, parsing_result_proto


def main() -> None:
    command = " ".join(sys.argv[1:]) or "ls -la /tmp > /dev/null && echo hi"
    parsed = parse_shell_command(command)
    print(json.dumps({"command": command, **parsed, "proto": parsing_result_proto(parsed["structured"])}, indent=2))


if __name__ == "__main__":
    main()
