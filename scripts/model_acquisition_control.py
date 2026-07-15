#!/usr/bin/env python3
"""Approve and prepare controlled, non-executing model acquisition bundles."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.model_acquisition_execution import (  # noqa: E402
    ControlledModelAcquisitionCore,
    ModelAcquisitionExecutionError,
)


def _shared(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("backend", help="Registered AI backend ID.")
    parser.add_argument("--plan-id", required=True, help="Exact reviewed plan ID.")
    parser.add_argument("--backend-root", type=Path, default=Path("config/ai/backends"))
    parser.add_argument("--prompt-root", type=Path, default=Path("config/ai/prompts"))
    parser.add_argument("--suite-root", type=Path, default=Path("config/ai/suites"))
    parser.add_argument("--case-root", type=Path, default=Path("benchmarks/ai"))
    parser.add_argument(
        "--specification-root", type=Path, default=Path("config/ai/acquisition")
    )
    parser.add_argument(
        "--backend-state-root", type=Path, default=Path("workspace/ai_backend_state")
    )
    parser.add_argument(
        "--plan-root", type=Path, default=Path("workspace/model_acquisition_plans")
    )
    parser.add_argument(
        "--source-evidence-root",
        type=Path,
        default=Path("workspace/model_source_evidence"),
    )
    parser.add_argument(
        "--approval-root",
        type=Path,
        default=Path("workspace/model_acquisition_approvals"),
    )
    parser.add_argument(
        "--bundle-root",
        type=Path,
        default=Path("workspace/model_acquisition_bundles"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Approve an exact current acquisition plan and prepare a data-only "
            "execution bundle without remote access, downloads or submission."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    approve = subparsers.add_parser(
        "approve", help="Record explicit approval for execution preparation only."
    )
    _shared(approve)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument(
        "--confirm-execution-preparation",
        action="store_true",
        help=(
            "Confirm review of the exact plan while retaining a separate execution gate."
        ),
    )

    prepare = subparsers.add_parser(
        "prepare", help="Build a non-executable acquisition contract bundle."
    )
    _shared(prepare)
    prepare.add_argument("--approval-id", required=True)
    return parser.parse_args()


def _core(args: argparse.Namespace) -> ControlledModelAcquisitionCore:
    registry = AIRegistry(
        backend_root=args.backend_root,
        prompt_root=args.prompt_root,
        suite_root=args.suite_root,
        case_root=args.case_root,
    )
    return ControlledModelAcquisitionCore(
        registry=registry,
        specification_root=args.specification_root,
        backend_state_root=args.backend_state_root,
        plan_root=args.plan_root,
        source_evidence_root=args.source_evidence_root,
        approval_root=args.approval_root,
        bundle_root=args.bundle_root,
    )


def _header(title: str) -> None:
    print("=" * 80)
    print(title)
    print("=" * 80)


def _approve(args: argparse.Namespace) -> int:
    _header("GenomeAgent Controlled Model Acquisition Approval v1")
    print("Backend              : {}".format(args.backend))
    print("Plan ID              : {}".format(args.plan_id))
    print("Reviewer             : {}".format(args.reviewer))
    print("Authorized scope     : execution-bundle preparation only")
    print("Remote access/writes : disabled")
    print("Model download       : disabled")
    print("Slurm/GPU execution  : disabled")
    print("Publication          : disabled")
    print("Backend activation   : disabled")
    print("")
    result = _core(args).approve(
        backend_id=args.backend,
        plan_id=args.plan_id,
        reviewer=args.reviewer,
        confirmation=args.confirm_execution_preparation,
    )
    print("Approval complete.")
    print("Approval ID      : {}".format(result.approval_id))
    print("Status           : {}".format(result.status))
    print("Next safe action : {}".format(result.next_safe_action))
    print("")
    print("Wrote: {}".format(result.approval_path))
    print("Wrote: {}".format(result.approval_path.with_suffix(".md")))
    return 0


def _prepare(args: argparse.Namespace) -> int:
    result = _core(args).prepare(
        backend_id=args.backend,
        plan_id=args.plan_id,
        approval_id=args.approval_id,
    )
    _header("GenomeAgent Controlled Model Acquisition Bundle v1")
    print("Source mode          : deterministic local artifact replay")
    print("Remote access/writes : disabled")
    print("Model download       : disabled")
    print("Slurm/GPU execution  : disabled")
    print("Publication          : disabled")
    print("Backend activation   : disabled")
    print("Backend              : {}".format(result.backend_id))
    print("Plan ID              : {}".format(result.plan_id))
    print("Approval ID          : {}".format(result.approval_id))
    print("Bundle ID            : {}".format(result.bundle_id))
    print("Status               : {}".format(result.status))
    print("Blockers             : {}".format(", ".join(result.blockers)))
    print("")
    print("Bundle directory: {}".format(result.bundle_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "approve":
            return _approve(args)
        return _prepare(args)
    except (ModelAcquisitionExecutionError, AIEvaluationError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
