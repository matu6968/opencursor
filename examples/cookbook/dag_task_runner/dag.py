# SPDX-License-Identifier: MIT-0
"""DAG schema parsing, validation, and topological ranking.

Mirrors cookbook/sdk/dag-task-runner/src/dag.ts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Complexity = Literal["HIGH", "MED", "LOW"]
COMPLEXITY_VALUES: frozenset[str] = frozenset({"HIGH", "MED", "LOW"})
COMPLEXITY_KEYS: tuple[Complexity, ...] = ("HIGH", "MED", "LOW")

DEFAULT_MODEL_MAP: dict[Complexity, str] = {
    "HIGH": "gpt-5.3-codex",
    "MED": "composer-2",
    "LOW": "auto-low",
}


@dataclass(frozen=True)
class RawTask:
    id: str
    depends_on: list[str]
    complexity: Complexity
    subtask_prompt: str


@dataclass(frozen=True)
class DAG:
    title: str
    tasks: list[RawTask]
    models: dict[str, str] | None = None


def parse_dag(raw: Any) -> DAG:
    if not isinstance(raw, Mapping):
        raise ValueError("DAG file must be a JSON object.")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("DAG.title must be a non-empty string.")
    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, Sequence) or isinstance(tasks_raw, (str, bytes)) or not tasks_raw:
        raise ValueError("DAG.tasks must be a non-empty array.")
    tasks = [_validate_task(item, index) for index, item in enumerate(tasks_raw)]
    ids: set[str] = set()
    for task in tasks:
        if task.id in ids:
            raise ValueError(f"Duplicate task id: {task.id}")
        ids.add(task.id)
    for task in tasks:
        for dep in task.depends_on:
            if dep not in ids:
                raise ValueError(f"Task {task.id} depends_on unknown id: {dep}")
            if dep == task.id:
                raise ValueError(f"Task {task.id} depends on itself.")
    _detect_cycle(tasks)
    models = None if raw.get("models") is None else validate_model_map(raw.get("models"), "DAG.models")
    return DAG(title=title, tasks=tasks, models=models)


def _validate_task(raw: Any, index: int) -> RawTask:
    if not isinstance(raw, Mapping):
        raise ValueError(f"tasks[{index}] must be an object.")
    ident = raw.get("id")
    if not isinstance(ident, str) or not ident.strip():
        raise ValueError(f"tasks[{index}].id must be a non-empty string.")
    depends_on = raw.get("depends_on") or []
    if not isinstance(depends_on, Sequence) or isinstance(depends_on, (str, bytes)) or any(
        not isinstance(item, str) for item in depends_on
    ):
        raise ValueError(f"tasks[{index}].depends_on must be an array of strings.")
    complexity = raw.get("complexity")
    if not isinstance(complexity, str) or complexity not in COMPLEXITY_VALUES:
        raise ValueError(f"tasks[{index}].complexity must be one of HIGH | MED | LOW.")
    prompt = raw.get("subtask_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"tasks[{index}].subtask_prompt must be a non-empty string.")
    unique_deps = list(dict.fromkeys(depends_on))
    return RawTask(id=ident, depends_on=unique_deps, complexity=complexity, subtask_prompt=prompt)  # type: ignore[arg-type]


def _detect_cycle(tasks: list[RawTask]) -> None:
    adj: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for dep in task.depends_on:
            adj[dep].append(task.id)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {task.id: WHITE for task in tasks}
    for start in tasks:
        if color[start.id] != WHITE:
            continue
        stack = [{"id": start.id, "child_idx": 0}]
        path = [start.id]
        color[start.id] = GRAY
        while stack:
            top = stack[-1]
            children = adj[top["id"]]
            if top["child_idx"] >= len(children):
                color[top["id"]] = BLACK
                path.pop()
                stack.pop()
                continue
            child = children[top["child_idx"]]
            top["child_idx"] += 1
            child_color = color.get(child, WHITE)
            if child_color == GRAY:
                cycle_start = path.index(child)
                cycle = " -> ".join([*path[cycle_start:], child])
                raise ValueError(f"Cycle detected: {cycle}")
            if child_color == WHITE:
                color[child] = GRAY
                path.append(child)
                stack.append({"id": child, "child_idx": 0})


def compute_ranks(dag: DAG) -> list[list[RawTask]]:
    remaining = {task.id: len(task.depends_on) for task in dag.tasks}
    by_id = {task.id: task for task in dag.tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in dag.tasks}
    for task in dag.tasks:
        for dep in task.depends_on:
            dependents[dep].append(task.id)
    ranks: list[list[RawTask]] = []
    frontier = [task for task in dag.tasks if remaining[task.id] == 0]
    while frontier:
        ranks.append(frontier)
        nxt: list[RawTask] = []
        for task in frontier:
            for child in dependents[task.id]:
                remaining[child] -= 1
                if remaining[child] == 0:
                    nxt.append(by_id[child])
        frontier = nxt
    placed = sum(len(rank) for rank in ranks)
    if placed != len(dag.tasks):
        raise ValueError("Topological sort failed — DAG contains a cycle.")
    return ranks


def validate_model_map(raw: Any, label: str = "model map") -> dict[str, str]:
    if not isinstance(raw, Mapping) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{label} must be a JSON object.")
    models: dict[str, str] = {}
    for key, value in raw.items():
        if key not in COMPLEXITY_VALUES:
            raise ValueError(f"{label} contains unknown complexity key: {key}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}.{key} must be a non-empty string.")
        models[key] = value.strip()
    return models


def create_model_resolver(overrides: Mapping[str, str] | None = None) -> Callable[[Complexity], str]:
    models: dict[str, str] = {**DEFAULT_MODEL_MAP, **dict(overrides or {})}

    def resolve(complexity: Complexity) -> str:
        if complexity not in COMPLEXITY_KEYS:
            raise ValueError(f"Unknown complexity: {complexity}")
        return models[complexity]

    return resolve
