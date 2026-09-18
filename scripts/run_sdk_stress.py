#!/usr/bin/env python3
"""Clone GitHub cursor-sdk dependents, rewrite imports to opencursor, compile.

Default mode prints catalog stats. Pass --clone to shallow-clone the next
``--limit`` repos that are not already under tests/stress/.work (complete git
checkouts are skipped, not re-cloned or re-checked). Repeat the command to
walk the candidate list in batches of ``limit``.

  PYTHONPATH=. python scripts/run_sdk_stress.py
  PYTHONPATH=. python scripts/run_sdk_stress.py --clone --limit 5
"""

from __future__ import annotations

import argparse
import compileall
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from stress_rewrite import rewrite_module_name, rewrite_source  # noqa: E402

CATALOG = ROOT / "tests" / "stress" / "catalog.json"
WORK = ROOT / "tests" / "stress" / ".work"


def load_catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def work_dest(full_name: str, work: Path = WORK) -> Path:
    return work / full_name.replace("/", "__")


def already_cloned(dest: Path) -> bool:
    return (dest / ".git").is_dir()


def clone_candidates(catalog: dict) -> list[str]:
    scored: list[tuple[int, str]] = []
    for repo in catalog["repos"]:
        files = repo["files"]
        if any(f.get("uses_client") or f.get("uses_async_agent") or f.get("uses_bridge") for f in files):
            continue
        unported = False
        for file in files:
            for imp in file["imports"]:
                if imp["kind"] == "from" and rewrite_module_name(imp["module"]) is None:
                    unported = True
                    break
            if unported:
                break
        if unported:
            continue
        scored.append((len(files), repo["full_name"]))
    scored.sort()
    return [name for _, name in scored]


def next_clone_batch(candidates: list[str], work: Path, limit: int) -> tuple[list[str], int]:
    """Next ``limit`` candidates whose work dir is not already a git checkout."""
    skipped = 0
    batch: list[str] = []
    for name in candidates:
        if already_cloned(work_dest(name, work)):
            skipped += 1
            continue
        batch.append(name)
        if len(batch) >= limit:
            break
    return batch, skipped


def rewrite_tree(root: Path) -> int:
    changed = 0
    for path in root.rglob("*.py"):
        if ".git" in path.parts:
            continue
        original = path.read_text(encoding="utf-8", errors="replace")
        if "cursor_sdk" not in original:
            continue
        updated = rewrite_source(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    return changed


def clone_rewrite_compile(limit: int, work: Path = WORK) -> list[dict]:
    catalog = load_catalog()
    candidates = clone_candidates(catalog)
    work.mkdir(parents=True, exist_ok=True)
    names, skipped = next_clone_batch(candidates, work, limit)
    remaining = len(candidates) - skipped - len(names)
    results: list[dict] = [
        {
            "status": "batch",
            "repo": "",
            "skipped": skipped,
            "queued": remaining,
            "batch": len(names),
            "candidates": len(candidates),
        }
    ]
    for full_name in names:
        dest = work_dest(full_name, work)
        row: dict = {"repo": full_name, "path": str(dest)}
        if dest.exists() and not already_cloned(dest):
            shutil.rmtree(dest)
        proc = subprocess.run(
            ["gh", "repo", "clone", full_name, str(dest), "--", "--depth", "1"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            row["status"] = "clone_failed"
            row["error"] = (proc.stderr or proc.stdout).strip()[:500]
            results.append(row)
            continue
        row["rewritten"] = rewrite_tree(dest)
        ok = compileall.compile_dir(str(dest), quiet=1, maxlevels=20, workers=0)
        row["status"] = "ok" if ok else "compile_failed"
        results.append(row)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clone", action="store_true", help="shallow-clone the next batch of uncloned dependents and compile")
    parser.add_argument("--limit", type=int, default=5, help="how many new repos to clone this run")
    args = parser.parse_args()
    catalog = load_catalog()
    stats = catalog["stats"]
    print(
        f"catalog: {stats['repo_count']} repos, {stats['file_count']} import files, "
        f"{stats['declared_dep_repos']} declared pip dependents "
        f"({stats['requirements_txt_repos']} requirements.txt, {stats['pyproject_toml_repos']} pyproject.toml)"
    )
    print(
        f"rewriteable public files: {stats['files_rewriteable_public']}; "
        f"unported Client/Bridge/Async*: {stats['files_using_unported_bridge']}"
    )
    print(
        f"sync Agent.create: {stats['files_sync_agent_create']}; "
        f"await Agent.create: {stats['files_await_agent_create']}"
    )
    if not args.clone:
        return 0
    results = clone_rewrite_compile(args.limit)
    failed = 0
    for row in results:
        if row["status"] == "batch":
            print(
                f"already cloned: {row['skipped']}; this run: {row['batch']}; "
                f"still queued: {row['queued']} (of {row['candidates']} candidates)"
            )
            if row["batch"] == 0:
                print("no remaining uncloned candidates")
            continue
        print(f"{row['status']:16s} {row['repo']} rewritten={row.get('rewritten', 0)}")
        if row.get("error"):
            print(f"  {row['error']}")
        if row["status"] != "ok":
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
