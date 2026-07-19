#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from genomeagent.dataset_registry import (  # noqa: E402
    COMPONENT_VERSION,
    DatasetRegistryError,
    add_incremental_task,
    add_location,
    add_scientific_link,
    create_incremental_major_task,
    create_or_update_dataset,
    discover_datasets,
    discover_major_tasks,
    list_unassigned,
    load_dataset,
    major_task_by_id,
    make_link,
    migrate_all,
    register_transfer,
    resolve_dataset_identifier,
    suggest_assignment,
    transfer_by_identifier,
    validate_dataset,
)

CREATE_MAJOR_TASK = "[create new major task]"
CREATE_COMPONENT_TASK = "[create new task]"


def print_major_tasks() -> None:
    tasks = discover_major_tasks(ROOT)
    print("Available major tasks")
    print("=" * 112)
    if not tasks:
        print("No major tasks found under config/major_tasks/.")
        return
    print(f"{'MAJOR TASK':<38} {'STATUS':<28} {'RUNS':>4} {'TASKS':>6} {'STAGES':>7}  DESCRIPTION")
    for task in tasks:
        print(
            f"{task['task_id']:<38} {str(task.get('status', 'unknown')):<28} "
            f"{len(task.get('runs', [])):>4} {len(task.get('tasks', [])):>6} "
            f"{len(task.get('stages', [])):>7}  {task.get('description', '')}"
        )


def print_task_choices(task_id: str) -> None:
    task = major_task_by_id(ROOT, task_id)
    print(f"Major task: {task_id}")
    print("=" * 112)
    runs = task.get("runs", [])
    tasks = task.get("tasks", [])
    stages = task.get("stages", [])
    if runs:
        print("Runs")
        for item in runs:
            print(f"  {item.get('run_id'):<34} {item.get('status', ''):<24} {item.get('purpose', '')}")
    else:
        print("Runs: none defined yet")
    if tasks:
        print("Tasks")
        for item in tasks:
            print(f"  {item.get('task_id'):<34} {item.get('status', ''):<24} {item.get('title', '')}")
    else:
        print("Tasks: none defined yet")
    if stages:
        print("Stages")
        for item in stages:
            print(f"  {item.get('stage_id'):<34} {item.get('title', '')}")
    else:
        print("Stages: none defined yet")


def print_dataset_list() -> None:
    rows = discover_datasets(ROOT)
    print("GenomeAgent dataset registry")
    print("=" * 132)
    if not rows:
        print("No datasets registered.")
        return
    print(f"{'DATASET':<42} {'ASSIGNMENT':<12} {'LIFECYCLE':<34} {'LINKS':>5} {'LOCATIONS':>9}  DESCRIPTION")
    for data in rows:
        print(
            f"{data['dataset_id']:<42} {data.get('assignment_status', 'unknown'):<12} "
            f"{data.get('lifecycle_state', 'unknown'):<34} "
            f"{len(data.get('scientific_links', [])):>5} {len(data.get('locations', [])):>9}  "
            f"{data.get('description', '')}"
        )


def print_unassigned() -> None:
    rows = list_unassigned(ROOT)
    print("Unassigned datasets and transfers")
    print("=" * 120)
    if not rows:
        print("None.")
        return
    print(f"{'KIND':<20} {'IDENTIFIER':<46} {'STATUS':<24} DESCRIPTION")
    for row in rows:
        print(
            f"{row.get('kind', ''):<20} {row.get('identifier', ''):<46} "
            f"{row.get('status', ''):<24} {row.get('description', '')}"
        )


def print_dataset(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def choose(
    prompt: str,
    values: list[str],
    *,
    labels: dict[str, str] | None = None,
    allow_blank: bool = True,
) -> str | None:
    """Choose one identifier interactively while showing descriptive labels."""
    if not sys.stdin.isatty():
        return None
    if not values:
        return None
    labels = labels or {}
    for index, value in enumerate(values, start=1):
        detail = labels.get(value, "")
        print(f"  {index}. {value}" + (f" — {detail}" if detail else ""))
    suffix = " [blank to skip]" if allow_blank else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and allow_blank:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(values):
            return values[int(raw) - 1]
        if raw in values:
            return raw
        print("Select a listed number or identifier.")


def yes_no(prompt: str, *, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        raw = input(f"{prompt}{suffix}: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Answer y or n.")


def prompt_required(prompt: str, *, default: str | None = None) -> str:
    if not sys.stdin.isatty():
        raise DatasetRegistryError(f"Interactive value required: {prompt}")
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        print("A non-empty value is required.")


def create_major_task_interactively() -> tuple[str, str]:
    print()
    print("Create a new incremental major task")
    print("=" * 112)
    print("A first evidenced task is required so the catalog never contains an empty speculative major task.")
    major_task = prompt_required("New major task ID")
    title = prompt_required("Major task title")
    description = prompt_required("Major task description")
    first_task = prompt_required("First task ID")
    first_title = prompt_required("First task title")
    status = prompt_required("Major task status", default="active")
    first_status = prompt_required("First task status", default="active")
    out = create_incremental_major_task(
        ROOT,
        task_id=major_task,
        title=title,
        description=description,
        first_task_id=first_task,
        first_task_title=first_title,
        status=status,
        first_task_status=first_status,
        evidence=["created during interactive dataset assignment"],
    )
    print(f"Created major task: {out}")
    return major_task, first_task


def create_component_task_interactively(major_task: str) -> str:
    print()
    print(f"Add an evidenced task under {major_task}")
    print("=" * 112)
    task_id = prompt_required("New task ID")
    title = prompt_required("Task title")
    status = prompt_required("Task status", default="active")
    out = add_incremental_task(
        ROOT,
        major_task=major_task,
        task_id=task_id,
        title=title,
        status=status,
        evidence=["created during interactive dataset assignment"],
    )
    print(f"Added task: {out}")
    return task_id


def _choice_labels(items: list[dict[str, Any]], id_key: str, title_key: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in items:
        identifier = item.get(id_key)
        if not identifier:
            continue
        parts = []
        title = item.get(title_key)
        status = item.get("status")
        purpose = item.get("purpose")
        if title:
            parts.append(str(title))
        elif purpose:
            parts.append(str(purpose))
        if status:
            parts.append(f"status={status}")
        labels[str(identifier)] = "; ".join(parts)
    return labels


def _guided_link_from_values(
    *,
    major_task: str | None = None,
    run_id: str | None = None,
    task: str | None = None,
    stage_id: str | None = None,
    relationship: str | None = None,
    role: str | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any] | None:
    """Complete a scientific link from explicit values and interactive choices."""
    definitions = discover_major_tasks(ROOT)
    if not definitions:
        return None

    created_task: str | None = None
    if not major_task:
        print_major_tasks()
        task_ids = [str(x["task_id"]) for x in definitions] + [CREATE_MAJOR_TASK]
        labels = {
            str(x["task_id"]): "; ".join(
                part for part in [str(x.get("title", "")), f"status={x.get('status', 'unknown')}" if x.get("status") else ""] if part
            )
            for x in definitions
        }
        labels[CREATE_MAJOR_TASK] = "Create a new incremental major task and its first evidenced task"
        selected = choose("Select major task", task_ids, labels=labels, allow_blank=False)
        if not selected:
            return None
        if selected == CREATE_MAJOR_TASK:
            major_task, created_task = create_major_task_interactively()
            task = task or created_task
        else:
            major_task = selected

    definition = major_task_by_id(ROOT, major_task)
    print_task_choices(major_task)

    runs = [x for x in definition.get("runs", []) if x.get("run_id")]
    tasks = [x for x in definition.get("tasks", []) if x.get("task_id")]
    stages = [x for x in definition.get("stages", []) if x.get("stage_id")]

    if run_id is None and runs:
        run_id = choose(
            "Select run",
            [str(x["run_id"]) for x in runs],
            labels=_choice_labels(runs, "run_id", "purpose"),
            allow_blank=True,
        )
    if task is None:
        task_values = [str(x["task_id"]) for x in tasks]
        task_labels = _choice_labels(tasks, "task_id", "title")
        if definition.get("definition_mode", "workflow") == "incremental_registry":
            task_values.append(CREATE_COMPONENT_TASK)
            task_labels[CREATE_COMPONENT_TASK] = "Create a new evidenced task under this major task"
        if task_values:
            selected_task = choose(
                "Select task",
                task_values,
                labels=task_labels,
                allow_blank=True,
            )
            if selected_task == CREATE_COMPONENT_TASK:
                task = create_component_task_interactively(major_task)
                definition = major_task_by_id(ROOT, major_task)
            else:
                task = selected_task
    if stage_id is None and stages:
        stage_id = choose(
            "Select stage",
            [str(x["stage_id"]) for x in stages],
            labels=_choice_labels(stages, "stage_id", "title"),
            allow_blank=True,
        )

    if relationship is None:
        relationship = choose(
            "Select relationship",
            ["consumed_by", "produced_by", "associated_with", "derived_from"],
            labels={
                "consumed_by": "the selected task/run/stage uses this dataset",
                "produced_by": "the selected task/run/stage created this dataset",
                "associated_with": "related without a producer/consumer direction",
                "derived_from": "this dataset is derived from the selected dataset context",
            },
            allow_blank=False,
        )
    if role is None and sys.stdin.isatty():
        role = input("Scientific role [input/output/reference/intermediate; blank allowed]: ").strip() or None

    return make_link(
        ROOT,
        major_task=major_task,
        run_id=run_id,
        task=task,
        stage_id=stage_id,
        relationship=relationship or "associated_with",
        role=role,
        evidence=evidence or ["researcher assignment through genomeagent-datasets"],
    )


def interactive_link() -> dict[str, Any] | None:
    return _guided_link_from_values()


def explicit_or_guided_link(args: argparse.Namespace) -> dict[str, Any] | None:
    major_task = getattr(args, "major_task", None)
    if not major_task:
        return None
    # In an interactive terminal, fill any omitted run/task/stage/relationship
    # from the live task definition. In non-interactive use, preserve concise
    # explicit assignment and validate all supplied identifiers.
    if sys.stdin.isatty():
        return _guided_link_from_values(
            major_task=major_task,
            run_id=getattr(args, "run", None),
            task=getattr(args, "task", None),
            stage_id=getattr(args, "stage", None),
            relationship=getattr(args, "relationship", None),
            role=getattr(args, "role", None),
            evidence=["explicit/guided CLI assignment"],
        )
    return make_link(
        ROOT,
        major_task=major_task,
        run_id=getattr(args, "run", None),
        task=getattr(args, "task", None),
        stage_id=getattr(args, "stage", None),
        relationship=getattr(args, "relationship", None) or "associated_with",
        role=getattr(args, "role", None),
        evidence=["explicit CLI assignment"],
    )


def add_link_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--major-task", help="Major task ID; omit for guided interactive selection")
    parser.add_argument("--run", help="Run ID under the selected major task")
    parser.add_argument("--task", help="Task ID under an incremental major task")
    parser.add_argument("--stage", help="Stage ID under a workflow major task")
    parser.add_argument(
        "--relationship",
        choices=["produced_by", "consumed_by", "associated_with", "derived_from"],
        default=None,
        help="Scientific direction; prompted interactively when omitted",
    )
    parser.add_argument("--role", help="Scientific role, such as input, output, reference, or intermediate")
    parser.add_argument("--no-task-link", action="store_true", help="Register location/transfer without a scientific link")


def resolve_initial_links(args: argparse.Namespace) -> list[dict[str, Any]]:
    if getattr(args, "no_task_link", False):
        return []

    explicit = explicit_or_guided_link(args)
    if explicit:
        return [explicit]

    if not sys.stdin.isatty():
        print_major_tasks()
        raise DatasetRegistryError(
            "No scientific assignment supplied. Run in an interactive terminal or use --major-task; "
            "use --no-task-link only for an intentionally unassigned registration."
        )

    links: list[dict[str, Any]] = []
    while True:
        link = interactive_link()
        if not link:
            break
        links.append(link)
        if not yes_no("Add another scientific link for this same dataset?", default=False):
            break
    if not links:
        raise DatasetRegistryError("No scientific assignment selected")
    return links

def command_create_major_task(args: argparse.Namespace) -> None:
    out = create_incremental_major_task(
        ROOT,
        task_id=args.major_task,
        title=args.title,
        description=args.description,
        first_task_id=args.first_task,
        first_task_title=args.first_task_title,
        status=args.status,
        first_task_status=args.first_task_status,
        methods_boundary=args.methods_boundary,
        evidence=["created through genomeagent-datasets create-major-task"],
    )
    print(f"Created major task: {out}")


def command_add_catalog_task(args: argparse.Namespace) -> None:
    out = add_incremental_task(
        ROOT,
        major_task=args.major_task,
        task_id=args.task,
        title=args.title,
        status=args.status,
        evidence=["created through genomeagent-datasets add-task"],
    )
    print(f"Added task: {out}")


def command_assign_path(args: argparse.Namespace) -> None:
    path = args.path.rstrip("/")
    if not path.startswith("/"):
        raise DatasetRegistryError("Assigned Puhti/Roihu path must be absolute")
    links = resolve_initial_links(args)
    location = {
        "provider": "filesystem",
        "host": args.host,
        "path": path,
        "state": args.state,
        "materialization": args.materialization,
    }
    if args.sample_set:
        location["sample_set_id"] = args.sample_set
    if args.observed_samples is not None:
        location["observed_samples"] = args.observed_samples
    if args.expected_samples is not None:
        location["expected_samples"] = args.expected_samples
    out = create_or_update_dataset(
        ROOT,
        dataset_id=args.dataset_id,
        title=args.title,
        description=args.description,
        locations=[location],
        scientific_links=links,
        lifecycle_state=args.lifecycle_state,
    )
    print(f"Registered dataset path: {out}")


def command_assign_transfer(args: argparse.Namespace) -> None:
    links = resolve_initial_links(args)
    out = register_transfer(
        ROOT,
        identifier=args.identifier,
        dataset_id=args.dataset_id,
        title=args.title,
        description=args.description,
        scientific_links=links,
    )
    print(f"Assigned transfer to dataset: {out}")


def command_add_link(args: argparse.Namespace) -> None:
    link = explicit_or_guided_link(args)
    if not link:
        link = interactive_link()
    if not link:
        raise DatasetRegistryError("No task link selected")
    out = add_scientific_link(ROOT, args.dataset, link)
    print(f"Added scientific link: {out}")


def command_add_location(args: argparse.Namespace) -> None:
    if args.provider == "filesystem":
        if not args.host or not args.path:
            raise DatasetRegistryError("Filesystem location requires --host and --path")
        location: dict[str, Any] = {
            "provider": "filesystem",
            "host": args.host,
            "path": args.path,
            "state": args.state,
            "materialization": args.materialization,
        }
    else:
        if not args.bucket or (not args.object and not args.prefix):
            raise DatasetRegistryError("Allas location requires --bucket and --object or --prefix")
        location = {
            "provider": "allas",
            "bucket": args.bucket,
            "state": args.state,
            "materialization": args.materialization,
        }
        if args.object:
            location["object"] = args.object
        if args.prefix:
            location["prefix"] = args.prefix
    if args.sample_set:
        location["sample_set_id"] = args.sample_set
    if args.observed_samples is not None:
        location["observed_samples"] = args.observed_samples
    if args.expected_samples is not None:
        location["expected_samples"] = args.expected_samples
    out = add_location(ROOT, args.dataset, location)
    print(f"Added location: {out}")


def command_suggest(args: argparse.Namespace) -> None:
    rows = suggest_assignment(ROOT, args.identifier)
    print(f"Assignment suggestions: {args.identifier}")
    print("=" * 112)
    if not rows:
        print("No deterministic suggestion found. Existing major tasks are:")
        print_major_tasks()
        return
    for index, row in enumerate(rows, start=1):
        print(f"Suggestion {index}: confidence={row.get('confidence')}")
        print(json.dumps(row, indent=2, ensure_ascii=False))


def command_validate() -> None:
    rows = discover_datasets(ROOT)
    for data in rows:
        validate_dataset(data)
        print(f"VALID {data['dataset_id']}: {ROOT / 'data_registry' / 'datasets' / (data['dataset_id'] + '.json')}")
    print(f"Validated datasets: {len(rows)}")


def assignment_catalog_text() -> str:
    """Render the current major-task/run/task/stage IDs for command help."""
    definitions = discover_major_tasks(ROOT)
    lines = [
        "Current assignment targets (loaded from config/major_tasks):",
        "  Interactive assignment also offers: Create a new major task / Create a new task",
    ]
    if not definitions:
        lines.append("  none")
        return "\n".join(lines)
    for definition in definitions:
        task_id = str(definition.get("task_id", ""))
        title = str(definition.get("title", ""))
        status = str(definition.get("status", "unknown"))
        lines.append(f"  {task_id} — {title} [status={status}]")
        runs = [x for x in definition.get("runs", []) if x.get("run_id")]
        tasks = [x for x in definition.get("tasks", []) if x.get("task_id")]
        stages = [x for x in definition.get("stages", []) if x.get("stage_id")]
        if runs:
            lines.append("    runs:")
            for item in runs:
                lines.append(f"      {item['run_id']} — {item.get('purpose', '')}")
        if tasks:
            lines.append("    tasks:")
            for item in tasks:
                lines.append(f"      {item['task_id']} — {item.get('title', '')}")
        if stages:
            lines.append("    stages:")
            for item in stages:
                lines.append(f"      {item['stage_id']} — {item.get('title', '')}")
        if not runs and not tasks and not stages:
            lines.append("    no runs, tasks, or stages defined yet")
    return "\n".join(lines)


def assignment_epilog(example: str) -> str:
    return f"{example.rstrip()}\n\n{assignment_catalog_text()}"

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="genomeagent-datasets",
        description=f"GenomeAgent Dataset Registry v{COMPONENT_VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  bin/genomeagent-datasets list
  bin/genomeagent-datasets list-unassigned
  bin/genomeagent-datasets tasks
  bin/genomeagent-datasets task-options pangenome_construction

  bin/genomeagent-datasets assign-path --host puhti \\
    --dataset-id pangenome_final_assemblies_29 \\
    /scratch/project_2001113/pangenome/.../Final_assemblies

  bin/genomeagent-datasets assign-transfer \\
    folder_final_assemblies_ca9a84679e \\
    --dataset-id pangenome_final_assemblies_29

  bin/genomeagent-datasets add-task-link pangenome_final_assemblies_29 \\
    --major-task pangenome_construction --task pggb \\
    --relationship consumed_by --role input
""",
    )
    parser.add_argument("--version", action="version", version=COMPONENT_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List registered datasets")
    sub.add_parser("list-unassigned", help="List unassigned datasets and Allas transfers")
    sub.add_parser("tasks", help="List major tasks available for assignment")
    task_options = sub.add_parser("task-options", help="List runs, tasks, and stages under a major task")
    task_options.add_argument("major_task")
    show = sub.add_parser("show", help="Show a dataset by dataset ID, path, Allas location, or transfer ID")
    show.add_argument("identifier")
    suggest = sub.add_parser("suggest-assignment", help="Suggest a dataset and scientific assignment")
    suggest.add_argument("identifier")

    create_major = sub.add_parser(
        "create-major-task",
        help="Create an incremental major task with its first evidenced task",
    )
    create_major.add_argument("--major-task", required=True)
    create_major.add_argument("--title", required=True)
    create_major.add_argument("--description", required=True)
    create_major.add_argument("--first-task", required=True)
    create_major.add_argument("--first-task-title", required=True)
    create_major.add_argument("--status", default="active")
    create_major.add_argument("--first-task-status", default="active")
    create_major.add_argument("--methods-boundary")

    add_catalog_task = sub.add_parser(
        "add-task",
        help="Add an evidenced task under an incremental major task",
    )
    add_catalog_task.add_argument("major_task")
    add_catalog_task.add_argument("--task", required=True)
    add_catalog_task.add_argument("--title", required=True)
    add_catalog_task.add_argument("--status", default="active")

    assign_path = sub.add_parser(
        "assign-path",
        help="Register an existing Puhti or Roihu path",
        description="Register a local dataset path and assign one or more scientific links.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=assignment_epilog("""Example:
  bin/genomeagent-datasets assign-path --host puhti --dataset-id DATASET_ID /absolute/path

Omit --major-task for guided selection. Shared datasets can receive several links in one interactive session."""),
    )
    assign_path.add_argument("path")
    assign_path.add_argument("--host", required=True, choices=["puhti", "roihu-cpu", "roihu-gpu"])
    assign_path.add_argument("--dataset-id", required=True)
    assign_path.add_argument("--title")
    assign_path.add_argument("--description")
    assign_path.add_argument("--state", default="available_local")
    assign_path.add_argument("--materialization", choices=["complete", "partial", "virtual", "unknown"], default="complete")
    assign_path.add_argument("--sample-set")
    assign_path.add_argument("--observed-samples", type=int)
    assign_path.add_argument("--expected-samples", type=int)
    assign_path.add_argument("--lifecycle-state", default="available_local")
    add_link_arguments(assign_path)

    assign_transfer = sub.add_parser(
        "assign-transfer",
        help="Assign an existing Allas folder-transfer task",
        description="Attach an existing transfer to a dataset and assign one or more scientific links.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=assignment_epilog("""Example:
  bin/genomeagent-datasets assign-transfer TRANSFER_ID --dataset-id DATASET_ID

Omit --major-task for guided selection."""),
    )
    assign_transfer.add_argument("identifier", help="Transfer task ID or original source path")
    assign_transfer.add_argument("--dataset-id", required=True)
    assign_transfer.add_argument("--title")
    assign_transfer.add_argument("--description")
    add_link_arguments(assign_transfer)

    add_link = sub.add_parser(
        "add-task-link",
        help="Add another major-task/task/run/stage relationship",
        description="Add another scientific relationship to an existing dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=assignment_epilog("""Example:
  bin/genomeagent-datasets add-task-link DATASET_ID

Omit --major-task for guided selection."""),
    )
    add_link.add_argument("dataset")
    add_link_arguments(add_link)

    add_loc = sub.add_parser("add-location", help="Add another Puhti, Roihu, or Allas location")
    add_loc.add_argument("dataset")
    add_loc.add_argument("--provider", choices=["filesystem", "allas"], required=True)
    add_loc.add_argument("--host", choices=["puhti", "roihu-cpu", "roihu-gpu"])
    add_loc.add_argument("--path")
    add_loc.add_argument("--bucket")
    add_loc.add_argument("--object")
    add_loc.add_argument("--prefix")
    add_loc.add_argument("--state", required=True)
    add_loc.add_argument("--materialization", choices=["complete", "partial", "virtual", "unknown"], default="complete")
    add_loc.add_argument("--sample-set")
    add_loc.add_argument("--observed-samples", type=int)
    add_loc.add_argument("--expected-samples", type=int)

    sub.add_parser("validate", help="Validate all dataset records")
    sub.add_parser("migrate", help="Migrate existing dataset records to schema 1.1")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            print_dataset_list()
        elif args.command == "list-unassigned":
            print_unassigned()
        elif args.command == "tasks":
            print_major_tasks()
        elif args.command == "task-options":
            print_task_choices(args.major_task)
        elif args.command == "show":
            data = resolve_dataset_identifier(ROOT, args.identifier)
            if data is None:
                try:
                    path, transfer = transfer_by_identifier(ROOT, args.identifier)
                except DatasetRegistryError:
                    raise DatasetRegistryError(f"Unknown dataset, path, Allas location, or transfer: {args.identifier!r}")
                print(json.dumps({"unassigned_transfer": transfer, "manifest": str(path)}, indent=2, ensure_ascii=False))
            else:
                print_dataset(data)
        elif args.command == "suggest-assignment":
            command_suggest(args)
        elif args.command == "create-major-task":
            command_create_major_task(args)
        elif args.command == "add-task":
            command_add_catalog_task(args)
        elif args.command == "assign-path":
            command_assign_path(args)
        elif args.command == "assign-transfer":
            command_assign_transfer(args)
        elif args.command == "add-task-link":
            command_add_link(args)
        elif args.command == "add-location":
            command_add_location(args)
        elif args.command == "validate":
            command_validate()
        elif args.command == "migrate":
            paths = migrate_all(ROOT)
            for path in paths:
                print(f"MIGRATED {path}")
            print(f"Migrated datasets: {len(paths)}")
        return 0
    except DatasetRegistryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
