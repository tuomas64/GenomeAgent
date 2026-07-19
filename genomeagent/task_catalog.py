#!/usr/bin/env python3
"""Dynamic task and dataset discovery shared by GenomeAgent CLI entry points."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _task_id(path: Path, data: dict[str, Any] | None) -> str:
    if data:
        for key in ("task", "task_id", "profile", "name"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return path.stem


def discover_tasks(root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    records: dict[str, dict[str, Any]] = {}

    major_dir = root / "config" / "major_tasks"
    if major_dir.exists():
        for path in sorted(major_dir.glob("*.json")):
            data = _load_json(path)
            task = _task_id(path, data)
            rec = records.setdefault(task, {"task": task})
            rec["major_definition"] = str(path.relative_to(root))
            rec["kind"] = "major_scientific_task"
            if data:
                rec["status"] = data.get("status", "unknown")
                rec["description"] = data.get("description", "")
                rec["run_count"] = len(data.get("runs", [])) if isinstance(data.get("runs"), list) else 0
                rec["task_count"] = len(data.get("tasks", [])) if isinstance(data.get("tasks"), list) else 0
                rec["stage_count"] = len(data.get("stages", [])) if isinstance(data.get("stages"), list) else 0

    config_dir = root / "config" / "tasks"
    if config_dir.exists():
        for path in sorted(config_dir.glob("*.json")):
            data = _load_json(path)
            task = _task_id(path, data)
            rec = records.setdefault(task, {"task": task})
            rec["profile"] = str(path.relative_to(root))
            rec.setdefault("kind", "scanner_task")
            if data:
                rec["profile_version"] = data.get("profile_version") or data.get("version")
                rec.setdefault("description", data.get("description") or data.get("purpose") or "")

    for area, key in (("task_scans", "scan_count"), ("task_state", "state_present")):
        base = root / "workspace" / area
        if not base.exists():
            continue
        for path in sorted(x for x in base.iterdir() if x.is_dir()):
            rec = records.setdefault(path.name, {"task": path.name, "kind": "observed_task"})
            if key == "scan_count":
                rec[key] = sum(1 for x in path.iterdir() if x.is_dir())
            else:
                rec[key] = (path / "current_state.json").exists()
    return [records[key] for key in sorted(records)]


def discover_allas_datasets(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = Path(root) / "data_registry" / "archives"
    if not base.exists():
        return rows
    for path in sorted(base.glob("*.json")):
        data = _load_json(path)
        if not data:
            continue
        rows.append(
            {
                "dataset": data.get("dataset_id", path.stem),
                "status": data.get("archive", {}).get("status", "unknown"),
                "description": data.get("description", ""),
            }
        )
    return rows


def discover_lifecycle_datasets(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    base = Path(root) / "data_registry" / "datasets"
    if not base.exists():
        return rows
    for path in sorted(base.glob("*.json")):
        data = _load_json(path)
        if not data:
            continue
        links = data.get("scientific_links")
        if not isinstance(links, list):
            links = []
            if data.get("created_by"):
                links.append(data["created_by"])
            links.extend(data.get("used_by") or [])
        rows.append(
            {
                "dataset": data.get("dataset_id", path.stem),
                "status": data.get("lifecycle_state", "unknown"),
                "assignment": "assigned" if links else "unassigned",
                "links": len(links),
                "locations": len(data.get("locations", [])) if isinstance(data.get("locations"), list) else 0,
                "description": data.get("description", ""),
            }
        )
    return rows


def print_task_catalog(root: Path, component: str = "all") -> None:
    tasks = discover_tasks(root)
    title = {
        "scanner": "Available Task Scanner and major-task definitions",
        "bridge": "Available Task State Bridge and major-task definitions",
        "all": "Available GenomeAgent tasks",
    }.get(component, "Available GenomeAgent tasks")
    print(title)
    print("=" * 132)
    if not tasks:
        print("No tasks found under config/major_tasks, config/tasks, workspace/task_scans, or workspace/task_state.")
        return
    print(f"{'TASK':<36} {'KIND':<23} {'STATUS':<28} {'RUNS':>4} {'TASKS':>5} {'STAGES':>6} {'SCANS':>6} {'STATE':>6}  DESCRIPTION")
    for rec in tasks:
        task = rec["task"]
        kind = rec.get("kind", "task")
        status = rec.get("status", "-")
        runs = rec.get("run_count", 0)
        task_count = rec.get("task_count", 0)
        stage_count = rec.get("stage_count", 0)
        scans = rec.get("scan_count", 0)
        state = "yes" if rec.get("state_present") else "no"
        description = str(rec.get("description", ""))
        print(f"{task:<36} {kind:<23} {status:<28} {runs:>4} {task_count:>5} {stage_count:>6} {scans:>6} {state:>6}  {description}")
    print()
    print("Major-task definitions may be complete workflows or incremental registries. Only evidenced tasks/stages should be added.")


def print_task_detail(root: Path, task_id: str) -> None:
    path = Path(root) / "config" / "major_tasks" / f"{task_id}.json"
    data = _load_json(path)
    if not data:
        available = ", ".join(rec["task"] for rec in discover_tasks(root) if rec.get("kind") == "major_scientific_task") or "none"
        raise ValueError(f"Unknown major task {task_id!r}. Available: {available}")
    print(f"Major task: {task_id}")
    print("=" * 112)
    print(f"Title       : {data.get('title', '')}")
    print(f"Status      : {data.get('status', 'unknown')}")
    print(f"Mode        : {data.get('definition_mode', 'workflow')}")
    print(f"Description : {data.get('description', '')}")
    print()
    runs = data.get("runs", []) if isinstance(data.get("runs"), list) else []
    tasks = data.get("tasks", []) if isinstance(data.get("tasks"), list) else []
    stages = data.get("stages", []) if isinstance(data.get("stages"), list) else []
    print("Runs")
    if runs:
        for item in runs:
            print(f"  {item.get('run_id', ''):<34} {item.get('status', ''):<24} {item.get('purpose', '')}")
    else:
        print("  none defined yet")
    print("Tasks")
    if tasks:
        for item in tasks:
            print(f"  {item.get('task_id', ''):<34} {item.get('status', ''):<24} {item.get('title', '')}")
    else:
        print("  none defined yet")
    print("Stages")
    if stages:
        for item in stages:
            print(f"  {item.get('stage_id', ''):<34} {item.get('title', '')}")
    else:
        print("  none defined yet")


def _print_dataset_rows(title: str, rows: list[dict[str, Any]]) -> None:
    print(title)
    print("=" * 132)
    if not rows:
        print("None registered.")
        return
    if title == "Dataset lifecycle registry":
        print(f"{'DATASET':<42} {'ASSIGNMENT':<12} {'STATUS':<32} {'LINKS':>5} {'LOCATIONS':>9}  DESCRIPTION")
        for rec in rows:
            print(f"{rec['dataset']:<42} {rec.get('assignment', 'unknown'):<12} {rec['status']:<32} {rec.get('links', 0):>5} {rec.get('locations', 0):>9}  {rec['description']}")
    else:
        for rec in rows:
            print(f"{rec['dataset']:<42} {rec['status']:<34} {rec['description']}")


def print_all_catalogs(root: Path) -> None:
    print_task_catalog(root, "all")
    print()
    _print_dataset_rows("Dataset lifecycle registry", discover_lifecycle_datasets(root))
    print()
    _print_dataset_rows("Managed Allas archives", discover_allas_datasets(root))
