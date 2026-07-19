#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from genomeagent.task_lifecycle import (  # noqa: E402
    COMPONENT_VERSION,
    LifecycleError,
    discover_tasks,
    export_methods_snapshot,
    methods_evidence_snapshot,
    methods_markdown,
    resolved_stage_states,
    run_by_id,
    task_by_id,
    validate_all,
)


def print_tasks() -> None:
    tasks = discover_tasks(ROOT)
    print("Major scientific tasks")
    print("=" * 112)
    if not tasks:
        print("No definitions found under config/major_tasks/.")
        return
    print(f"{'TASK':<36} {'STATUS':<32} {'RUNS':>4}  BOUNDARY")
    for task in tasks:
        print(
            f"{task['task_id']:<36} {task['status']:<32} "
            f"{len(task['runs']):>4}  {task['methods_boundary']}"
        )


def print_runs(task_id: str) -> None:
    task = task_by_id(ROOT, task_id)
    print(f"Runs: {task_id}")
    print("=" * 112)
    print(f"{'RUN':<34} {'STATUS':<32} {'SAMPLES':>9}  PURPOSE")
    if not task["runs"]:
        print("No runs defined yet.")
        return
    for run in task["runs"]:
        samples = run.get("sample_count")
        sample_text = str(samples) if isinstance(samples, int) else "discover"
        print(
            f"{run['run_id']:<34} {run['status']:<32} "
            f"{sample_text:>9}  {run.get('purpose', '')}"
        )


def print_task(task_id: str) -> None:
    task = task_by_id(ROOT, task_id)
    print(json.dumps(task, indent=2, ensure_ascii=False))


def print_run(task_id: str, run_id: str) -> None:
    task = task_by_id(ROOT, task_id)
    run = run_by_id(task, run_id)
    payload = dict(run)
    payload["resolved_stage_states"] = resolved_stage_states(task, run)
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"GenomeAgent Task Lifecycle Foundation v{COMPONENT_VERSION}",
        epilog=(
            "Examples:\n"
            "  bin/genomeagent-task-lifecycle list\n"
            "  bin/genomeagent-task-lifecycle runs linear_snp_indel_discovery\n"
            "  bin/genomeagent-task-lifecycle methods-status linear_snp_indel_discovery linear_original_455\n"
            "  bin/genomeagent-task-lifecycle export-methods graph_snp_indel_discovery graph_initial_458"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List major scientific tasks")
    runs = sub.add_parser("runs", help="List runs for a major task")
    runs.add_argument("task")
    show = sub.add_parser("show", help="Show a major task definition as JSON")
    show.add_argument("task")
    show_run = sub.add_parser("show-run", help="Show one run and resolved stage states")
    show_run.add_argument("task")
    show_run.add_argument("run")
    status = sub.add_parser("methods-status", help="Show Materials and Methods evidence completeness")
    status.add_argument("task")
    status.add_argument("run")
    export = sub.add_parser("export-methods", help="Write a timestamped methods-evidence snapshot")
    export.add_argument("task")
    export.add_argument("run")
    sub.add_parser("validate", help="Validate all major-task definitions")
    args = parser.parse_args()

    try:
        if args.command == "list":
            print_tasks()
        elif args.command == "runs":
            print_runs(args.task)
        elif args.command == "show":
            print_task(args.task)
        elif args.command == "show-run":
            print_run(args.task, args.run)
        elif args.command == "methods-status":
            task = task_by_id(ROOT, args.task)
            run = run_by_id(task, args.run)
            print(methods_markdown(methods_evidence_snapshot(task, run)))
        elif args.command == "export-methods":
            out = export_methods_snapshot(ROOT, args.task, args.run)
            print(f"Methods evidence snapshot: {out}")
        elif args.command == "validate":
            rows = validate_all(ROOT)
            for task_id, path in rows:
                print(f"VALID {task_id}: {path}")
            print(f"Validated major tasks: {len(rows)}")
        return 0
    except LifecycleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
