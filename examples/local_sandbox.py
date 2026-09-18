# SPDX-License-Identifier: MIT-0
"""Locate cursorsandbox and run a command through the vendor helper CLI."""

import json
import os
import sys
from pathlib import Path

from opencursor._sandbox import (
    helper_cli_document,
    locate_cursorsandbox,
    run_sandboxed,
    sandbox_supported,
)


def main() -> None:
    cwd = Path(os.environ.get("OPENCURSOR_LOCAL_CWD") or os.getcwd()).resolve()
    binary = locate_cursorsandbox(excluded_workspace=cwd)
    if binary is None:
        print("No cursorsandbox helper found.")
        print("Set CURSOR_SANDBOX_BIN to the vendor binary, or install @cursor/sdk-<platform> with a bin/ directory.")
        print("This example does not reverse-engineer the helper; it only wraps the published CLI.")
        raise SystemExit(0)
    print(f"binary: {binary}")
    ok, reason = sandbox_supported(binary, cwd)
    print(f"supported: {ok}")
    if not ok:
        print(f"reason: {reason}")
        raise SystemExit(1)
    policy = {"type": "workspace_readwrite"}
    document = helper_cli_document(policy, cwd)
    print("policy:", json.dumps(document["sandbox"], indent=2)[:800])
    proc = run_sandboxed(
        command=" ".join(sys.argv[1:]) or "echo sandboxed",
        working_directory=cwd,
        workspace=cwd,
        policy=policy,
        binary=binary,
        timeout_s=15.0,
    )
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
