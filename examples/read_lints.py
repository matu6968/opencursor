# SPDX-License-Identifier: MIT-0
"""Collect diagnostics for a path (same exec the local agent uses for readLints)."""

import json
import os
import sys
from pathlib import Path

from opencursor._diagnostics import exec_diagnostics


def main() -> None:
    cwd = Path(os.environ.get("OPENCURSOR_LOCAL_CWD") or os.getcwd()).resolve()
    target = " ".join(sys.argv[1:]) or str(cwd)
    print(json.dumps(exec_diagnostics(cwd, {"path": target}), indent=2))


if __name__ == "__main__":
    main()
