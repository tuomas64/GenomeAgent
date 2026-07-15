#!/usr/bin/env python3
"""Prepare, validate, or display GenomeAgent proposal-only workflow bundles."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genomeagent.proposal_core import (
    PROPOSAL_CORE_VERSION,
    ProposalCore,
    ProposalCoreError,
)  # noqa: E402


def _roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--policy-root", type=Path, default=Path("config/proposals"))
    parser.add_argument("--task-state-root", type=Path, default=Path("workspace/task_state"))
    parser.add_argument("--task-scan-root", type=Path, default=Path("workspace/task_scans"))
    parser.add_argument("--proposal-root", type=Path, default=Path("workspace/proposals"))


def _core(args: argparse.Namespace) -> ProposalCore:
    return ProposalCore(
        policy_root=args.policy_root,
        task_state_root=args.task_state_root,
        task_scan_root=args.task_scan_root,
        proposal_root=args.proposal_root,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"GenomeAgent deterministic Proposal Core v{PROPOSAL_CORE_VERSION}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Prepare an immutable proposal bundle")
    prepare.add_argument("task")
    prepare.add_argument("--action", required=True, choices=["gather_or_merge"])
    _roots(prepare)

    validate = sub.add_parser("validate", help="Validate a proposal bundle locally")
    validate.add_argument("proposal_dir", type=Path)
    _roots(validate)

    show = sub.add_parser("show", help="Show proposal summary")
    show.add_argument("proposal_dir", type=Path)
    _roots(show)

    args = parser.parse_args()
    core = _core(args)
    try:
        if args.command == "prepare":
            result = core.prepare(args.task, args.action)
            print("=" * 80)
            print(f"GenomeAgent Proposal Core v{PROPOSAL_CORE_VERSION}: {result.task}")
            print("=" * 80)
            print("Mode              : local deterministic proposal only")
            print("Remote access     : none")
            print("Slurm submission  : disabled")
            print("Automatic execute : disabled")
            print(f"Action            : {result.action}")
            print(f"Proposal ID       : {result.proposal_id}")
            print(f"Status            : {result.status}")
            print(f"Intervals         : {result.interval_count}")
            print(f"Chromosomes       : {result.chromosome_count}")
            print(f"Created           : {'yes' if result.created else 'no (identical proposal already exists)'}")
            print(f"Proposal directory: {result.proposal_dir}")
        elif args.command == "validate":
            result = core.validate(args.proposal_dir)
            print("=" * 80)
            print(f"GenomeAgent Proposal Core v{PROPOSAL_CORE_VERSION} validation")
            print("=" * 80)
            print(f"Status            : {result['status']}")
            print(f"Proposal ID       : {result['proposal_id']}")
            print(f"Artifacts verified: {result['artifacts_verified']}")
            print(f"Intervals verified: {result['intervals_verified']}")
            print(f"Chromosomes       : {result['chromosomes_verified']}")
            print("Automatic execute : disabled")
        else:
            print(core.show(args.proposal_dir), end="")
    except ProposalCoreError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
