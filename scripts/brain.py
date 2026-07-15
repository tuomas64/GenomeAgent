#!/usr/bin/env python3
"""Promote GenomeAgent knowledge and plan non-executable workflow transfers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.brain_v2 import (  # noqa: E402
    BrainKnowledgeCore,
    BrainV2Error,
    WorkflowTransferCore,
)
from genomeagent.task_state import SUPPORTED_TASKS  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build deterministic Brain v2 knowledge or a read-only workflow "
            "transfer proposal."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser(
        "ingest",
        help="Promote deterministic task artifacts into versioned knowledge.",
    )
    ingest.add_argument("task", choices=sorted(SUPPORTED_TASKS))
    ingest.add_argument("--workflow-id")
    ingest.add_argument("--state-root", type=Path, default=Path("workspace/task_state"))
    ingest.add_argument(
        "--resource-root", type=Path, default=Path("workspace/task_resources")
    )
    ingest.add_argument("--plan-root", type=Path, default=Path("workspace/task_plans"))
    ingest.add_argument("--workflow-root", type=Path, default=Path("config/workflows"))
    ingest.add_argument(
        "--environment-root", type=Path, default=Path("config/environments")
    )
    ingest.add_argument(
        "--knowledge-root", type=Path, default=Path("workspace/brain_knowledge")
    )
    ingest.add_argument(
        "--legacy-knowledge-path",
        type=Path,
        default=Path("workspace/project_knowledge.json"),
    )

    transfer = subparsers.add_parser(
        "plan-workflow",
        help="Evaluate promoted workflow knowledge for a target environment.",
    )
    transfer.add_argument("workflow_id")
    transfer.add_argument("--target-environment", required=True)
    transfer.add_argument("--workflow-root", type=Path, default=Path("config/workflows"))
    transfer.add_argument(
        "--environment-root", type=Path, default=Path("config/environments")
    )
    transfer.add_argument(
        "--knowledge-root", type=Path, default=Path("workspace/brain_knowledge")
    )
    transfer.add_argument("--plan-root", type=Path, default=Path("workspace/task_plans"))
    transfer.add_argument(
        "--transfer-root", type=Path, default=Path("workspace/workflow_transfers")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "ingest":
            result = BrainKnowledgeCore(
                state_root=args.state_root,
                resource_root=args.resource_root,
                plan_root=args.plan_root,
                workflow_root=args.workflow_root,
                environment_root=args.environment_root,
                knowledge_root=args.knowledge_root,
                legacy_knowledge_path=args.legacy_knowledge_path,
            ).ingest(args.task, workflow_id=args.workflow_id)
            print("=" * 80)
            print("GenomeAgent Brain Knowledge Promotion Core v2: {}".format(result.task))
            print("=" * 80)
            print("Source mode       : deterministic local artifact replay")
            print("Remote access     : none")
            print("AI promotion      : disabled")
            print("Execution         : disabled")
            print("Workflow          : {}".format(result.workflow_id))
            print("Knowledge snapshot: {}".format(result.snapshot_id))
            print("Snapshot created  : {}".format("yes" if result.snapshot_created else "no"))
            print("Promoted claims   : {}".format(result.promoted_claims))
            print("Candidate claims  : {}".format(result.candidate_claims))
            print("")
            print("Knowledge directory: {}".format(result.knowledge_dir))
        else:
            result = WorkflowTransferCore(
                workflow_root=args.workflow_root,
                environment_root=args.environment_root,
                knowledge_root=args.knowledge_root,
                plan_root=args.plan_root,
                transfer_root=args.transfer_root,
            ).plan(args.workflow_id, args.target_environment)
            print("=" * 80)
            print("GenomeAgent Workflow Transfer Core v2: {}".format(result.workflow_id))
            print("=" * 80)
            print("Source mode       : promoted local knowledge")
            print("Remote access     : none")
            print("Execution         : disabled")
            print("Scheduler changes : disabled")
            print("Target environment: {}".format(result.target_environment))
            print("Status            : {}".format(result.status))
            print("")
            print("Transfer directory: {}".format(result.transfer_dir))
        for path in result.artifact_paths:
            print("Wrote: {}".format(path))
        return 0
    except BrainV2Error as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
