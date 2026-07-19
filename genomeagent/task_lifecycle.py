#!/usr/bin/env python3
"""GenomeAgent Task Lifecycle Foundation v0.1.

This module models stable scientific tasks separately from their individual runs.
It is local-only: it reads Git-tracked definitions and writes optional methods-
evidence snapshots under workspace/. It has no SSH, scheduler, or execution
authority.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
COMPONENT_VERSION = "0.2.0"
ALLOWED_TASK_STATUS = {
    "planned",
    "active",
    "completed",
    "completed_with_active_rerun",
    "paused",
    "superseded",
}
ALLOWED_RUN_STATUS = {
    "planned",
    "active",
    "completed",
    "completed_comparison_pending",
    "paused",
    "failed",
    "superseded",
}
ALLOWED_STAGE_STATE = {
    "planned",
    "pending",
    "active",
    "completed",
    "validated",
    "historical_or_archived",
    "reused_from_parent_run",
    "not_applicable",
    "unknown_requires_scan",
}


class LifecycleError(RuntimeError):
    """Raised for invalid task lifecycle definitions or operations."""


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LifecycleError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"Expected JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _required_string(data: dict[str, Any], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LifecycleError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _unique(values: Iterable[str], context: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise LifecycleError(f"Duplicate identifiers in {context}: {sorted(duplicates)}")


def validate_task_definition(data: dict[str, Any], source: str = "task") -> dict[str, Any]:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise LifecycleError(
            f"{source}.schema_version must be {SCHEMA_VERSION!r}; "
            f"found {data.get('schema_version')!r}"
        )
    task_id = _required_string(data, "task_id", source)
    _required_string(data, "title", source)
    _required_string(data, "description", source)
    _required_string(data, "methods_boundary", source)
    status = _required_string(data, "status", source)
    if status not in ALLOWED_TASK_STATUS:
        raise LifecycleError(f"Unsupported task status for {task_id}: {status}")
    definition_mode = str(data.get("definition_mode", "workflow"))
    if definition_mode not in {"workflow", "incremental_registry"}:
        raise LifecycleError(f"Unsupported definition_mode for {task_id}: {definition_mode}")

    component_tasks = data.get("tasks", [])
    if not isinstance(component_tasks, list):
        raise LifecycleError(f"{task_id}.tasks must be a list")
    component_task_ids: list[str] = []
    for index, component in enumerate(component_tasks, start=1):
        if not isinstance(component, dict):
            raise LifecycleError(f"{task_id}.tasks[{index}] must be an object")
        component_id = _required_string(component, "task_id", f"{task_id}.tasks[{index}]")
        _required_string(component, "title", f"{task_id}.{component_id}")
        component_task_ids.append(component_id)
    _unique(component_task_ids, f"{task_id}.tasks")

    stages = data.get("stages", [])
    if not isinstance(stages, list):
        raise LifecycleError(f"{task_id}.stages must be a list")
    if definition_mode == "workflow" and not stages:
        raise LifecycleError(f"{task_id}.stages must be a non-empty list for workflow definitions")
    stage_ids: list[str] = []
    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            raise LifecycleError(f"{task_id}.stages[{index}] must be an object")
        stage_id = _required_string(stage, "stage_id", f"{task_id}.stages[{index}]")
        _required_string(stage, "title", f"{task_id}.{stage_id}")
        order = stage.get("order")
        if order != index:
            raise LifecycleError(
                f"{task_id}.{stage_id}.order must be {index}; found {order!r}"
            )
        fields = stage.get("methods_evidence_fields")
        if not isinstance(fields, list) or not fields or not all(isinstance(x, str) and x for x in fields):
            raise LifecycleError(
                f"{task_id}.{stage_id}.methods_evidence_fields must be non-empty strings"
            )
        stage_ids.append(stage_id)
    _unique(stage_ids, f"{task_id}.stages")

    runs = data.get("runs", [])
    if not isinstance(runs, list):
        raise LifecycleError(f"{task_id}.runs must be a list")
    if definition_mode == "workflow" and not runs:
        raise LifecycleError(f"{task_id}.runs must be a non-empty list for workflow definitions")
    if definition_mode == "incremental_registry" and not (component_tasks or stages or runs):
        raise LifecycleError(
            f"{task_id} incremental registry must contain at least one evidenced task, stage, or run"
        )
    run_ids: list[str] = []
    run_map: dict[str, dict[str, Any]] = {}
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            raise LifecycleError(f"{task_id}.runs[{index}] must be an object")
        run_id = _required_string(run, "run_id", f"{task_id}.runs[{index}]")
        run_status = _required_string(run, "status", f"{task_id}.{run_id}")
        if run_status not in ALLOWED_RUN_STATUS:
            raise LifecycleError(f"Unsupported run status for {task_id}/{run_id}: {run_status}")
        stage_states = run.get("stage_states", {})
        if not isinstance(stage_states, dict):
            raise LifecycleError(f"{task_id}/{run_id}.stage_states must be an object")
        unknown_stages = sorted(set(stage_states) - set(stage_ids))
        if unknown_stages:
            raise LifecycleError(
                f"{task_id}/{run_id} refers to unknown stages: {unknown_stages}"
            )
        for stage_id, state in stage_states.items():
            if state not in ALLOWED_STAGE_STATE:
                raise LifecycleError(
                    f"Unsupported stage state {state!r} for {task_id}/{run_id}/{stage_id}"
                )
        run_ids.append(run_id)
        run_map[run_id] = run
    _unique(run_ids, f"{task_id}.runs")
    for run_id, run in run_map.items():
        parent = run.get("derived_from")
        if parent is not None and parent not in run_map:
            raise LifecycleError(
                f"{task_id}/{run_id}.derived_from refers to missing run {parent!r}"
            )
        if parent == run_id:
            raise LifecycleError(f"{task_id}/{run_id} cannot derive from itself")

    data.setdefault("definition_mode", definition_mode)
    data.setdefault("tasks", component_tasks)
    data.setdefault("stages", stages)
    data.setdefault("runs", runs)
    return data


def discover_task_files(root: Path) -> list[Path]:
    base = Path(root) / "config" / "major_tasks"
    return sorted(base.glob("*.json")) if base.exists() else []


def discover_tasks(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in discover_task_files(root):
        data = validate_task_definition(read_json(path), str(path))
        rows.append(data)
    return rows


def task_by_id(root: Path, task_id: str) -> dict[str, Any]:
    for data in discover_tasks(root):
        if data["task_id"] == task_id:
            return data
    available = ", ".join(x["task_id"] for x in discover_tasks(root)) or "none"
    raise LifecycleError(f"Unknown major task {task_id!r}. Available: {available}")


def run_by_id(task: dict[str, Any], run_id: str) -> dict[str, Any]:
    for run in task["runs"]:
        if run["run_id"] == run_id:
            return run
    available = ", ".join(x["run_id"] for x in task["runs"])
    raise LifecycleError(
        f"Unknown run {run_id!r} for {task['task_id']}. Available: {available}"
    )


def resolved_stage_states(task: dict[str, Any], run: dict[str, Any]) -> dict[str, str]:
    states = {stage["stage_id"]: "unknown_requires_scan" for stage in task["stages"]}
    parent_id = run.get("derived_from")
    if parent_id:
        parent = run_by_id(task, parent_id)
        parent_states = resolved_stage_states(task, parent)
        for stage_id in run.get("reused_stages", []):
            if stage_id not in states:
                raise LifecycleError(
                    f"{task['task_id']}/{run['run_id']} reuses unknown stage {stage_id!r}"
                )
            parent_state = parent_states.get(stage_id, "unknown_requires_scan")
            states[stage_id] = (
                "reused_from_parent_run"
                if parent_state in {"completed", "validated", "historical_or_archived"}
                else "unknown_requires_scan"
            )
    states.update(run.get("stage_states", {}))
    return states


def methods_evidence_snapshot(task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    states = resolved_stage_states(task, run)
    evidence_index = run.get("methods_evidence", {})
    stage_rows: list[dict[str, Any]] = []
    required_total = 0
    resolved_total = 0
    for stage in task["stages"]:
        stage_id = stage["stage_id"]
        required = list(stage["methods_evidence_fields"])
        evidence = evidence_index.get(stage_id, {})
        resolved = [field for field in required if evidence.get(field) not in (None, "", [], {})]
        missing = [field for field in required if field not in resolved]
        required_total += len(required)
        resolved_total += len(resolved)
        stage_rows.append(
            {
                "order": stage["order"],
                "stage_id": stage_id,
                "title": stage["title"],
                "stage_state": states[stage_id],
                "required_evidence_fields": required,
                "resolved_evidence_fields": resolved,
                "missing_evidence_fields": missing,
                "evidence": evidence,
            }
        )
    return {
        "schema_version": "1.0",
        "component_version": COMPONENT_VERSION,
        "generated_at": utc_now(),
        "task_id": task["task_id"],
        "task_title": task["title"],
        "run_id": run["run_id"],
        "run_status": run["status"],
        "methods_boundary": task["methods_boundary"],
        "evidence_fields_required": required_total,
        "evidence_fields_resolved": resolved_total,
        "publication_methods_ready": required_total > 0 and required_total == resolved_total,
        "stages": stage_rows,
        "authority": {
            "task_scanner_is_source_of_current_state": True,
            "researcher_approval_required_for_scientific_interpretation": True,
            "this_snapshot_executes_nothing": True,
        },
    }


def methods_markdown(snapshot: dict[str, Any]) -> str:
    lines = [
        f"# Methods evidence: {snapshot['task_title']}",
        "",
        f"- Task: `{snapshot['task_id']}`",
        f"- Run: `{snapshot['run_id']}`",
        f"- Run status: `{snapshot['run_status']}`",
        f"- Boundary: {snapshot['methods_boundary']}",
        f"- Resolved evidence: {snapshot['evidence_fields_resolved']}/{snapshot['evidence_fields_required']}",
        f"- Publication methods ready: {'yes' if snapshot['publication_methods_ready'] else 'no'}",
        "",
        "| # | Stage | State | Missing methods evidence |",
        "|---:|---|---|---|",
    ]
    for stage in snapshot["stages"]:
        missing = ", ".join(stage["missing_evidence_fields"]) or "—"
        lines.append(
            f"| {stage['order']} | `{stage['stage_id']}` — {stage['title']} | "
            f"`{stage['stage_state']}` | {missing} |"
        )
    lines.extend(
        [
            "",
            "> This is an evidence inventory, not a finished Materials and Methods section. "
            "Task Scanner, Task State Bridge, scripts, logs, software versions and researcher-validated "
            "decisions must resolve the missing fields before publication text is generated.",
            "",
        ]
    )
    return "\n".join(lines)


def export_methods_snapshot(root: Path, task_id: str, run_id: str) -> Path:
    task = task_by_id(root, task_id)
    run = run_by_id(task, run_id)
    snapshot = methods_evidence_snapshot(task, run)
    out = Path(root) / "workspace" / "methods" / task_id / run_id / timestamp()
    write_json(out / "methods_evidence.json", snapshot)
    (out / "methods_evidence.md").write_text(methods_markdown(snapshot), encoding="utf-8")
    return out


def validate_all(root: Path) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    for path in discover_task_files(root):
        data = validate_task_definition(read_json(path), str(path))
        # Resolve every run to validate inheritance and reused stage references.
        for run in data["runs"]:
            resolved_stage_states(data, run)
        results.append((data["task_id"], str(path)))
    return results
