#!/usr/bin/env python3
"""Collect and ingest deterministic HPC resource evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.resource_evidence import (  # noqa: E402
    ResourceCollector,
    ResourceEvidenceCore,
    ResourceEvidenceError,
)
from genomeagent.task_state import SUPPORTED_TASKS  # noqa: E402


def _unit_overrides(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError(
                "Unit mappings must use ATTEMPT_ID=UNIT_ID."
            )
        attempt_id, unit_id = value.split("=", 1)
        if not attempt_id.strip() or not unit_id.strip():
            raise argparse.ArgumentTypeError(
                "Unit mappings require non-empty attempt and unit IDs."
            )
        if attempt_id in result and result[attempt_id] != unit_id:
            raise argparse.ArgumentTypeError(
                "Conflicting mappings for attempt {}.".format(attempt_id)
            )
        result[attempt_id.strip()] = unit_id.strip()
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect bounded read-only Slurm accounting and deterministically "
            "derive resource profiles, anomalies and proposal-only recommendations."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser(
        "collect",
        help="Read sacct for explicit job IDs and write one immutable local snapshot.",
    )
    collect.add_argument("task", choices=sorted(SUPPORTED_TASKS))
    collect.add_argument(
        "--job-id",
        action="append",
        required=True,
        help="Slurm parent job or array-task ID; repeat for at most 20 IDs.",
    )
    collect.add_argument(
        "--profile-key",
        required=True,
        help="Reusable comparison group, for example scattered_genotypegvcfs_250kb.",
    )
    collect.add_argument("--host", default="puhti")
    collect.add_argument("--timeout", type=int, default=60)
    collect.add_argument(
        "--unit",
        action="append",
        default=[],
        metavar="ATTEMPT_ID=UNIT_ID",
        help="Override or add attempt-to-unit identity; may be repeated.",
    )
    collect.add_argument(
        "--scan-root",
        type=Path,
        default=Path("workspace/task_scans"),
        help="Task Scanner root used only to link known job attempts to units.",
    )
    collect.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("workspace/task_resource_evidence"),
        help="Root for immutable local resource-evidence snapshots.",
    )

    collect_new = subparsers.add_parser(
        "collect-new",
        help=(
            "Discover newly terminal array attempts in Task Scanner history and "
            "collect bounded read-only accounting."
        ),
    )
    collect_new.add_argument("task", choices=sorted(SUPPORTED_TASKS))
    collect_new.add_argument("--host", default="puhti")
    collect_new.add_argument("--timeout", type=int, default=60)
    collect_new.add_argument(
        "--max-attempts",
        type=int,
        default=20,
        help="Maximum newly terminal array elements to query in this run (1-20).",
    )
    collect_new.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the local discovery plan without SSH access or local writes.",
    )
    collect_new.add_argument(
        "--scan-root",
        type=Path,
        default=Path("workspace/task_scans"),
    )
    collect_new.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("workspace/task_resource_evidence"),
    )

    ingest = subparsers.add_parser(
        "ingest",
        help="Replay all local evidence into profiles and recommendations.",
    )
    ingest.add_argument("task", choices=sorted(SUPPORTED_TASKS))
    ingest.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("workspace/task_resource_evidence"),
    )
    ingest.add_argument(
        "--resource-root",
        type=Path,
        default=Path("workspace/task_resources"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "collect":
            overrides = _unit_overrides(args.unit)
            collector = ResourceCollector(
                evidence_root=args.evidence_root,
                scan_root=args.scan_root,
            )
            result = collector.collect(
                args.task,
                args.job_id,
                args.profile_key,
                host=args.host,
                timeout=args.timeout,
                unit_overrides=overrides,
            )
            print("=" * 80)
            print("GenomeAgent Resource Evidence Collector: {}".format(result.task))
            print("=" * 80)
            print("Remote mode          : bounded read-only sacct query")
            print("Remote writes        : none")
            print("Job execution        : disabled")
            print("Resource changes     : disabled")
            print("Observations         : {}".format(result.observations))
            print("Terminal observations: {}".format(result.terminal_observations))
            print("")
            print("Wrote: {}".format(result.evidence_path))
            return 0

        if args.command == "collect-new":
            collector = ResourceCollector(
                evidence_root=args.evidence_root,
                scan_root=args.scan_root,
            )
            if args.dry_run:
                plan = collector.discover_new(
                    args.task,
                    maximum_attempts=args.max_attempts,
                )
                auto_result = None
            else:
                auto_result = collector.collect_new(
                    args.task,
                    host=args.host,
                    timeout=args.timeout,
                    maximum_attempts=args.max_attempts,
                )
                plan = auto_result.plan
            print("=" * 80)
            print("GenomeAgent Automatic Resource Discovery: {}".format(plan.task))
            print("=" * 80)
            print("Source              : saved read-only Task Scanner history")
            print("Source scans        : {}".format(plan.source_scans))
            print("Terminal attempts   : {}".format(plan.terminal_attempts_seen))
            print("Already evidenced   : {}".format(plan.already_evidenced))
            print("Selected this run   : {}".format(len(plan.selected)))
            print("Remaining candidates: {}".format(plan.remaining))
            print("Ignored parent rows : {}".format(plan.ignored_non_array_records))
            print("Dry run             : {}".format("yes" if args.dry_run else "no"))
            print("")
            if plan.selected:
                print("Selected attempts:")
                for item in plan.selected:
                    unit = (
                        " unit={}".format(item["unit_id"])
                        if item.get("unit_id") else ""
                    )
                    print(
                        "- {} state={} profile={}{}".format(
                            item["attempt_id"],
                            item["state"],
                            item["profile_key"],
                            unit,
                        )
                    )
            else:
                print("No newly terminal attempts require collection.")
            if args.dry_run:
                print("")
                print("Remote access: none")
                print("Local writes : none")
                return 0
            print("")
            print("Remote mode     : bounded read-only sacct query")
            print("Remote writes   : none")
            print("Job execution   : disabled")
            print("Resource changes: disabled")
            if auto_result is not None:
                for collection in auto_result.collections:
                    print("Wrote: {}".format(collection.evidence_path))
            if plan.selected:
                print("")
                print(
                    "Next: python3 scripts/task_resources.py ingest {}".format(
                        plan.task
                    )
                )
            return 0

        core = ResourceEvidenceCore(
            evidence_root=args.evidence_root,
            resource_root=args.resource_root,
        )
        result = core.ingest(args.task)
    except (ResourceEvidenceError, argparse.ArgumentTypeError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2

    print("=" * 80)
    print("GenomeAgent Resource Evidence and Learning Core: {}".format(result.task))
    print("=" * 80)
    print("Source mode     : immutable local evidence replay")
    print("Remote access   : none during ingest")
    print("Execution       : disabled")
    print("Source snapshots: {}".format(result.source_snapshots))
    print("Observations    : {}".format(result.observations))
    print("Current attempts: {}".format(result.current_attempts))
    print("Profiles        : {}".format(result.profiles))
    print("Anomalies       : {}".format(result.anomalies))
    print("Recommendations : {}".format(result.recommendations))
    print("")
    print("Resource directory: {}".format(result.resource_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
