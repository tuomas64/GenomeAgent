#!/usr/bin/env python3
"""Collect, ingest and explicitly approve bounded AI model source metadata."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.model_source_evidence import (  # noqa: E402
    ModelSourceApprovalCore,
    ModelSourceEvidenceCollector,
    ModelSourceEvidenceCore,
    ModelSourceEvidenceError,
)


def _registry_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend-root", type=Path, default=Path("config/ai/backends"))
    parser.add_argument("--prompt-root", type=Path, default=Path("config/ai/prompts"))
    parser.add_argument("--suite-root", type=Path, default=Path("config/ai/suites"))
    parser.add_argument("--case-root", type=Path, default=Path("benchmarks/ai"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect bounded public model-info metadata or replay immutable snapshots "
            "without downloading repository files or updating configuration."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser(
        "collect", help="Resolve and confirm public source metadata read-only."
    )
    collect.add_argument("backend", help="Registered AI backend ID.")
    collect.add_argument("--stamp", help="Fixed UTC evidence ID for tests.")
    collect.add_argument(
        "--policy-root", type=Path, default=Path("config/ai/source_evidence")
    )
    collect.add_argument(
        "--evidence-root", type=Path, default=Path("workspace/model_source_evidence")
    )
    _registry_paths(collect)

    ingest = subparsers.add_parser(
        "ingest", help="Rebuild current source state and a review-only proposal."
    )
    ingest.add_argument("backend", help="Registered AI backend ID.")
    ingest.add_argument(
        "--policy-root", type=Path, default=Path("config/ai/source_evidence")
    )
    ingest.add_argument(
        "--specification-root", type=Path, default=Path("config/ai/acquisition")
    )
    ingest.add_argument(
        "--evidence-root", type=Path, default=Path("workspace/model_source_evidence")
    )
    ingest.add_argument(
        "--state-root", type=Path, default=Path("workspace/model_source_state")
    )
    _registry_paths(ingest)

    approve = subparsers.add_parser(
        "approve",
        help=(
            "Record an explicit license review and apply only verified source values."
        ),
    )
    approve.add_argument("backend", help="Registered AI backend ID.")
    approve.add_argument("--evidence-id", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--accept-license", required=True)
    approve.add_argument(
        "--confirm-license-review",
        action="store_true",
        help="Confirm that the named reviewer inspected and accepts the exact license.",
    )
    approve.add_argument(
        "--policy-root", type=Path, default=Path("config/ai/source_evidence")
    )
    approve.add_argument(
        "--specification-root", type=Path, default=Path("config/ai/acquisition")
    )
    approve.add_argument(
        "--evidence-root", type=Path, default=Path("workspace/model_source_evidence")
    )
    approve.add_argument(
        "--state-root", type=Path, default=Path("workspace/model_source_state")
    )
    approve.add_argument(
        "--approval-root", type=Path, default=Path("workspace/model_source_approvals")
    )
    _registry_paths(approve)
    return parser.parse_args()


def _registry(args: argparse.Namespace) -> AIRegistry:
    return AIRegistry(
        backend_root=args.backend_root,
        prompt_root=args.prompt_root,
        suite_root=args.suite_root,
        case_root=args.case_root,
    )


def _header(title: str) -> None:
    print("=" * 80)
    print(title)
    print("=" * 80)


def _collect(args: argparse.Namespace) -> int:
    collector = ModelSourceEvidenceCollector(
        registry=_registry(args),
        policy_root=args.policy_root,
        evidence_root=args.evidence_root,
    )
    _header("GenomeAgent Read-only Model Source Metadata Collector")
    print("Backend               : {}".format(args.backend))
    print("Provider              : public Hugging Face model-info API")
    print("Public metadata calls : bounded to 2")
    print("Authentication        : disabled")
    print("Repository downloads  : disabled")
    print("Model weight downloads: disabled")
    print("Roihu access          : disabled")
    print("Slurm/GPU execution   : disabled")
    print("Configuration updates : disabled")
    print("")
    result = collector.collect(args.backend, stamp=args.stamp)
    print("Collection complete.")
    print("Evidence ID         : {}".format(result.evidence_id))
    print("Status              : {}".format(result.status))
    print("Resolved revision   : {}".format(result.resolved_revision))
    print("Source bytes        : {}".format(result.source_total_bytes))
    print("Inventory SHA-256   : {}".format(result.source_inventory_sha256))
    print("")
    print("Wrote: {}".format(result.evidence_path))
    print("Wrote: {}".format(result.evidence_path.with_suffix(".md")))
    return 0


def _ingest(args: argparse.Namespace) -> int:
    core = ModelSourceEvidenceCore(
        registry=_registry(args),
        policy_root=args.policy_root,
        specification_root=args.specification_root,
        evidence_root=args.evidence_root,
        state_root=args.state_root,
    )
    result = core.ingest(args.backend)
    _header("GenomeAgent Model Source Metadata Evidence State")
    print("Source mode          : immutable local evidence replay")
    print("Remote access        : none during ingest")
    print("Model download       : disabled")
    print("Configuration updates: disabled")
    print("Backend              : {}".format(result.backend_id))
    print("Source snapshots     : {}".format(result.snapshots))
    print("Status               : {}".format(result.status))
    print("Next safe action     : {}".format(result.next_safe_action))
    print("Proposal status      : {}".format(result.proposal_status))
    if result.blockers:
        print("Blockers             : {}".format(", ".join(result.blockers)))
    print("")
    print("State directory: {}".format(result.state_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


def _approve(args: argparse.Namespace) -> int:
    core = ModelSourceApprovalCore(
        registry=_registry(args),
        policy_root=args.policy_root,
        specification_root=args.specification_root,
        evidence_root=args.evidence_root,
        state_root=args.state_root,
        approval_root=args.approval_root,
    )
    _header("GenomeAgent Explicit Model Source and License Approval")
    print("Backend                 : {}".format(args.backend))
    print("Evidence ID             : {}".format(args.evidence_id))
    print("Reviewer                : {}".format(args.reviewer))
    print("Accepted license        : {}".format(args.accept_license))
    print("Authorized config scope : acquisition source identity and license only")
    print("Remote access           : disabled")
    print("Model download          : disabled")
    print("Slurm/GPU execution     : disabled")
    print("Registry update         : disabled")
    print("Backend activation      : disabled")
    print("")
    result = core.approve(
        backend_id=args.backend,
        evidence_id=args.evidence_id,
        reviewer=args.reviewer,
        accepted_license=args.accept_license,
        confirmation=args.confirm_license_review,
    )
    print("Approval complete.")
    print("Approval ID       : {}".format(result.approval_id))
    print("Status            : {}".format(result.status))
    print("Specification edit: {}".format("applied" if result.applied else "already applied"))
    print("Next safe action  : {}".format(result.next_safe_action))
    print("")
    print("Wrote: {}".format(result.approval_path))
    print("Wrote: {}".format(result.approval_path.with_suffix(".md")))
    print("Updated: {}".format(result.specification_path))
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "collect":
            return _collect(args)
        if args.command == "ingest":
            return _ingest(args)
        return _approve(args)
    except (ModelSourceEvidenceError, AIEvaluationError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
