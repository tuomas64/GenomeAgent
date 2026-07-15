#!/usr/bin/env python3
"""Build deterministic, non-executable GenomeAgent task plans."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.resource_decision import (  # noqa: E402
    ResourceDecisionCore,
    ResourceDecisionError,
)
from genomeagent.task_state import SUPPORTED_TASKS  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build deterministic resource and execution-readiness plans without "
            "remote access or scheduler changes."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    resources = subparsers.add_parser(
        "resources",
        help="Plan target-environment resources from learned evidence.",
    )
    resources.add_argument("task", choices=sorted(SUPPORTED_TASKS))
    resources.add_argument(
        "--target-environment",
        required=True,
        help="Registered environment ID, for example puhti or roihu.",
    )
    resources.add_argument(
        "--source-environment",
        help=(
            "Select one registered source when several environments contain the "
            "same profile; normally omitted."
        ),
    )
    resources.add_argument(
        "--profile-key",
        help="Resource profile key; required when a task has multiple profiles.",
    )
    resources.add_argument(
        "--resource-root",
        type=Path,
        default=Path("workspace/task_resources"),
    )
    resources.add_argument(
        "--state-root",
        type=Path,
        default=Path("workspace/task_state"),
    )
    resources.add_argument(
        "--environment-root",
        type=Path,
        default=Path("config/environments"),
    )
    resources.add_argument(
        "--plan-root",
        type=Path,
        default=Path("workspace/task_plans"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        core = ResourceDecisionCore(
            resource_root=args.resource_root,
            state_root=args.state_root,
            environment_root=args.environment_root,
            plan_root=args.plan_root,
        )
        result = core.plan_resources(
            args.task,
            target_environment=args.target_environment,
            source_environment=args.source_environment,
            profile_key=args.profile_key,
        )
    except ResourceDecisionError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2

    print("=" * 80)
    print("GenomeAgent Resource Decision and Transfer Core: {}".format(result.task))
    print("=" * 80)
    print("Source mode       : local deterministic knowledge transfer")
    print("Remote access     : none")
    print("Execution         : disabled")
    print("Scheduler changes : disabled")
    print("Profile           : {}".format(result.profile_key))
    print("Source environment: {}".format(result.source_environment))
    print("Target environment: {}".format(result.target_environment))
    print("Decision          : {}".format(result.decision))
    print("Confidence        : {}".format(result.confidence))
    print("")
    print("Plan directory: {}".format(result.plan_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
