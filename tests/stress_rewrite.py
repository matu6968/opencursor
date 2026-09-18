"""Rewrite official `cursor_sdk` import lines to `opencursor`.

Public submodules that exist on both packages are remapped. Bridge internals
(`_bridge`, `asyncio`, …) are left unchanged so stress tests can classify them.
"""

from __future__ import annotations

import re

PUBLIC_SUBMODULES = frozenset({"types", "errors", "events"})

_FROM = re.compile(
    r"^([ \t]*)from[ \t]+cursor_sdk((?:\.[A-Za-z_][\w]*)*)[ \t]+import[ \t]+",
    re.M,
)
_IMPORT = re.compile(
    r"^([ \t]*)import[ \t]+cursor_sdk(\.[A-Za-z_][\w]*)?(\s+as\s+[A-Za-z_][\w]*)?",
    re.M,
)


def rewrite_module_name(module: str) -> str | None:
    if module == "cursor_sdk":
        return "opencursor"
    if not module.startswith("cursor_sdk."):
        return None
    rest = module[len("cursor_sdk.") :]
    first = rest.split(".", 1)[0]
    if first in PUBLIC_SUBMODULES:
        return "opencursor." + rest
    return None


def rewrite_source(source: str) -> str:
    def from_sub(match: re.Match[str]) -> str:
        indent, suffix = match.group(1), match.group(2) or ""
        dest = rewrite_module_name("cursor_sdk" + suffix)
        if dest is None:
            return match.group(0)
        return f"{indent}from {dest} import "

    def import_sub(match: re.Match[str]) -> str:
        indent = match.group(1)
        suffix = match.group(2) or ""
        as_clause = match.group(3)
        dest = rewrite_module_name("cursor_sdk" + suffix)
        if dest is None:
            return match.group(0)
        if as_clause:
            return f"{indent}import {dest}{as_clause}"
        if dest == "opencursor":
            return f"{indent}import opencursor as cursor_sdk"
        return f"{indent}import {dest}"

    return _IMPORT.sub(import_sub, _FROM.sub(from_sub, source))
