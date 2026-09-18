from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


def setting_sources(options: Any) -> list[str]:
    local = getattr(options, "local", None)
    raw = getattr(local, "settingSources", None) if local is not None else None
    if not raw:
        return []
    return [str(item) for item in raw]


def project_settings_enabled(options: Any) -> bool:
    sources = setting_sources(options)
    return "project" in sources or "all" in sources


def load_project_rules(roots: Iterable[Path]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        resolved = Path(root).expanduser().resolve()
        agents = resolved / "AGENTS.md"
        if agents.is_file():
            _append_rule(rules, seen, agents)
        rules_dir = resolved / ".cursor" / "rules"
        if not rules_dir.is_dir():
            continue
        for path in sorted(rules_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".md", ".mdc", ".txt"}:
                _append_rule(rules, seen, path)
    return rules


def _append_rule(rules: list[dict[str, Any]], seen: set[str], path: Path) -> None:
    key = str(path)
    if key in seen:
        return
    seen.add(key)
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    rules.append(
        {
            "full_path": key,
            "content": content,
            "type": {"global": {}},
        }
    )
