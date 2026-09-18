#!/usr/bin/env python3
"""Refresh tests/stress/catalog.json from GitHub code search (requires `gh`)."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "tests" / "stress" / "catalog.json"
EXCLUDED = {
    "actuallyrizzn/cursor-sdk": "unrelated unofficial package also named cursor_sdk",
}
QUERIES = [
    "cursor-sdk filename:requirements.txt",
    "cursor-sdk filename:pyproject.toml",
    '"from cursor_sdk" extension:py',
    '"import cursor_sdk" extension:py',
]
HTML_RE = re.compile(r"https://github.com/([^/]+/[^/]+)/blob/([0-9a-f]+)/(.*)")


def gh_search(query: str) -> list[dict]:
    hits: list[dict] = []
    page = 1
    while page <= 10:
        raw = subprocess.check_output(
            [
                "gh",
                "api",
                "-X",
                "GET",
                "search/code",
                "-f",
                f"q={query}",
                "-F",
                "per_page=100",
                "-F",
                f"page={page}",
            ],
            text=True,
        )
        body = json.loads(raw)
        items = body.get("items") or []
        for item in items:
            repo = item["repository"]["full_name"]
            hits.append({"repo": repo, "path": item["path"], "html": item["html_url"]})
        if len(items) < 100:
            break
        page += 1
        time.sleep(2)
    return hits


def extract_imports(src: str) -> dict:
    info: dict = {
        "imports": [],
        "uses_await_agent_create": bool(re.search(r"await\s+Agent\.create\b", src)),
        "uses_sync_agent_create": bool(re.search(r"(?<!await )Agent\.create\b", src)),
        "uses_async_agent": "AsyncAgent" in src,
        "uses_client": bool(re.search(r"\b(AsyncClient|CursorClient|Client)\b", src)),
        "uses_bridge": bool(re.search(r"\b(AsyncBridge|BridgeEndpoint|Bridge)\b", src)),
    }
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return info

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "cursor_sdk" or alias.name.startswith("cursor_sdk."):
                    info["imports"].append(
                        {
                            "kind": "import",
                            "module": alias.name,
                            "names": [],
                            "asname": alias.asname,
                            "lineno": node.lineno,
                        }
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "cursor_sdk" or mod.startswith("cursor_sdk."):
                info["imports"].append(
                    {
                        "kind": "from",
                        "module": mod,
                        "names": [a.name for a in node.names],
                        "asname": None,
                        "lineno": node.lineno,
                    }
                )
    return info


def fetch(url: str) -> str | None:
    req = Request(url, headers={"User-Agent": "opencursor-stress-collector"})
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def main() -> int:
    print("searching GitHub…", file=sys.stderr)
    req_repos: set[str] = set()
    pyp_repos: set[str] = set()
    py_hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for query in QUERIES:
        print(f"  {query}", file=sys.stderr)
        hits = gh_search(query)
        if "filename:requirements.txt" in query:
            req_repos.update(h["repo"] for h in hits)
        elif "filename:pyproject.toml" in query:
            pyp_repos.update(h["repo"] for h in hits)
        else:
            for hit in hits:
                key = (hit["repo"], hit["path"])
                if key in seen:
                    continue
                seen.add(key)
                py_hits.append(hit)
        time.sleep(2)

    files: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = {}
        for hit in py_hits:
            match = HTML_RE.match(hit["html"])
            if not match:
                continue
            repo, sha, path = match.group(1), match.group(2), match.group(3)
            url = f"https://raw.githubusercontent.com/{repo}/{sha}/{quote(path, safe='/')}"
            futs[pool.submit(fetch, url)] = {**hit, "sha": sha}
        for i, fut in enumerate(as_completed(futs), start=1):
            if i % 25 == 0:
                print(f"fetched {i}/{len(futs)}", file=sys.stderr)
            meta = futs[fut]
            src = fut.result()
            if src is None:
                continue
            extracted = extract_imports(src)
            if not extracted["imports"]:
                continue
            files.append({**meta, **extracted})

    by_repo: dict[str, list] = defaultdict(list)
    for row in files:
        by_repo[row["repo"]].append(row)

    name_counts: Counter[str] = Counter()
    mod_counts: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    repos_out = []
    rewriteable = 0
    unported = 0
    for name in sorted(by_repo):
        if name in EXCLUDED:
            continue
        entries = []
        for row in sorted(by_repo[name], key=lambda r: r["path"]):
            for flag in (
                "uses_sync_agent_create",
                "uses_await_agent_create",
                "uses_async_agent",
                "uses_client",
                "uses_bridge",
            ):
                if row.get(flag):
                    flags[flag] += 1
            blocked = bool(row.get("uses_client") or row.get("uses_async_agent") or row.get("uses_bridge"))
            for imp in row["imports"]:
                mod_counts[imp["module"]] += 1
                if imp["kind"] == "from":
                    for n in imp["names"]:
                        if n != "*":
                            name_counts[n] += 1
                if imp["module"] not in {"cursor_sdk", "cursor_sdk.types", "cursor_sdk.errors", "cursor_sdk.events"}:
                    blocked = True
            if blocked:
                unported += 1
            else:
                rewriteable += 1
            entries.append(
                {
                    "path": row["path"],
                    "sha": row.get("sha"),
                    "imports": row["imports"],
                    "uses_sync_agent_create": bool(row.get("uses_sync_agent_create")),
                    "uses_await_agent_create": bool(row.get("uses_await_agent_create")),
                    "uses_async_agent": bool(row.get("uses_async_agent")),
                    "uses_bridge": bool(row.get("uses_bridge")),
                    "uses_client": bool(row.get("uses_client")),
                }
            )
        repos_out.append(
            {
                "full_name": name,
                "in_requirements_txt": name in req_repos,
                "in_pyproject_toml": name in pyp_repos,
                "files": entries,
            }
        )

    catalog = {
        "source": "GitHub code search via gh api search/code; blobs from raw.githubusercontent.com",
        "queries": QUERIES,
        "excluded_repos": [{"full_name": k, "reason": v} for k, v in EXCLUDED.items()],
        "stats": {
            "repo_count": len(repos_out),
            "file_count": sum(len(r["files"]) for r in repos_out),
            "requirements_txt_repos": sum(1 for r in repos_out if r["in_requirements_txt"]),
            "pyproject_toml_repos": sum(1 for r in repos_out if r["in_pyproject_toml"]),
            "declared_dep_repos": sum(1 for r in repos_out if r["in_requirements_txt"] or r["in_pyproject_toml"]),
            "files_rewriteable_public": rewriteable,
            "files_using_unported_bridge": unported,
            "files_sync_agent_create": flags["uses_sync_agent_create"],
            "files_await_agent_create": flags["uses_await_agent_create"],
            "files_async_agent": flags["uses_async_agent"],
            "files_client": flags["uses_client"],
            "files_bridge": flags["uses_bridge"],
            "top_imported_names": name_counts.most_common(20),
            "imported_modules": mod_counts.most_common(),
        },
        "repos": repos_out,
    }
    DEST.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {DEST} repos={catalog['stats']['repo_count']} files={catalog['stats']['file_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
