#!/usr/bin/env python3
"""Collect installed-model evidence and register a verified AI backend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.model_registration import (  # noqa: E402
    InstalledModelEvidenceCollector,
    InstalledModelEvidenceCore,
    ModelRegistrationApprovalCore,
    ModelRegistrationError,
)
from genomeagent.task_scanner import (  # noqa: E402
    SSHRemotePythonRunner,
    TaskScanError,
)


def _registry_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend-root", type=Path, default=Path("config/ai/backends"))
    parser.add_argument("--prompt-root", type=Path, default=Path("config/ai/prompts"))
    parser.add_argument("--suite-root", type=Path, default=Path("config/ai/suites"))
    parser.add_argument("--case-root", type=Path, default=Path("benchmarks/ai"))


def _common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--policy-root", type=Path, default=Path("config/ai/model_registration")
    )
    parser.add_argument(
        "--specification-root", type=Path, default=Path("config/ai/acquisition")
    )
    parser.add_argument(
        "--bundle-root", type=Path, default=Path("workspace/model_acquisition_bundles")
    )
    parser.add_argument(
        "--publication-root", type=Path, default=Path("workspace/model_publications")
    )
    parser.add_argument(
        "--verification-root",
        type=Path,
        default=Path("workspace/model_integrity_verifications"),
    )
    parser.add_argument(
        "--evidence-root", type=Path, default=Path("workspace/installed_model_evidence")
    )
    _registry_arguments(parser)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a published model identity without reading weights, then apply only "
            "an explicitly approved backend-registry identity update."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect", help="Collect bounded read-only installed evidence.")
    collect.add_argument("backend")
    collect.add_argument("--bundle-id", required=True)
    collect.add_argument("--publication-id", required=True)
    collect.add_argument("--host")
    collect.add_argument("--timeout", type=int)
    collect.add_argument("--stamp")
    _common_paths(collect)
    ingest = commands.add_parser("ingest", help="Rebuild deterministic registration state.")
    ingest.add_argument("backend")
    ingest.add_argument(
        "--state-root", type=Path, default=Path("workspace/installed_model_state")
    )
    _common_paths(ingest)
    approve = commands.add_parser(
        "approve", help="Explicitly approve and apply the scoped registry identity update."
    )
    approve.add_argument("backend")
    approve.add_argument("--evidence-id", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument(
        "--confirm-register-verified-installation", action="store_true"
    )
    approve.add_argument(
        "--state-root", type=Path, default=Path("workspace/installed_model_state")
    )
    approve.add_argument(
        "--approval-root", type=Path, default=Path("workspace/model_registration_approvals")
    )
    _common_paths(approve)
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


def _collector(args: argparse.Namespace) -> InstalledModelEvidenceCollector:
    return InstalledModelEvidenceCollector(
        _registry(args),
        args.policy_root,
        args.specification_root,
        args.bundle_root,
        args.publication_root,
        args.verification_root,
        args.evidence_root,
    )


def _core(args: argparse.Namespace) -> InstalledModelEvidenceCore:
    return InstalledModelEvidenceCore(
        _registry(args),
        args.policy_root,
        args.specification_root,
        args.bundle_root,
        args.publication_root,
        args.verification_root,
        args.evidence_root,
        args.state_root,
    )


def _collect(args: argparse.Namespace) -> int:
    collector = _collector(args)
    _, policy_value, _ = collector.policy(args.backend)
    host = args.host or policy_value["ssh_host"]
    runner = SSHRemotePythonRunner(
        host, python_executable=str(policy_value["remote_python"])
    )
    _header("GenomeAgent Read-only Installed Model Evidence Collector v1")
    print("Backend              : {}".format(args.backend))
    print("Bundle ID            : {}".format(args.bundle_id))
    print("Publication ID       : {}".format(args.publication_id))
    print("SSH host             : {}".format(host))
    print("Remote writes        : disabled")
    print("Model weight reads   : disabled")
    print("Large-file hashing   : disabled")
    print("Slurm/GPU/inference  : disabled")
    print("Registry updates     : disabled")
    print("")
    result = collector.collect(
        args.backend,
        args.bundle_id,
        args.publication_id,
        runner,
        stamp=args.stamp,
        timeout_seconds=args.timeout,
    )
    print("Collection complete.")
    print("Evidence ID : {}".format(result.evidence_id))
    print("Status      : {}".format(result.status))
    if result.blockers:
        print("Blockers    : {}".format(", ".join(result.blockers)))
    print("")
    print("Wrote: {}".format(result.evidence_path))
    print("Wrote: {}".format(result.evidence_path.with_suffix(".md")))
    return 0


def _ingest(args: argparse.Namespace) -> int:
    result = _core(args).ingest(args.backend)
    _header("GenomeAgent Installed Model Registration State v1")
    print("Source mode       : immutable local evidence replay")
    print("Remote access     : none during ingest")
    print("Registry updates  : disabled")
    print("GPU/inference     : disabled")
    print("Backend           : {}".format(result.backend_id))
    print("Source snapshots  : {}".format(result.snapshots))
    print("Status            : {}".format(result.status))
    print("Next safe action  : {}".format(result.next_safe_action))
    print("Proposal status   : {}".format(result.proposal_status))
    if result.blockers:
        print("Blockers          : {}".format(", ".join(result.blockers)))
    print("")
    print("State directory: {}".format(result.state_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


def _approve(args: argparse.Namespace) -> int:
    core = ModelRegistrationApprovalCore(
        _registry(args),
        args.policy_root,
        args.specification_root,
        args.bundle_root,
        args.publication_root,
        args.verification_root,
        args.evidence_root,
        args.state_root,
        args.approval_root,
    )
    _header("GenomeAgent Explicit Verified Model Registration Approval v1")
    print("Backend              : {}".format(args.backend))
    print("Evidence ID          : {}".format(args.evidence_id))
    print("Reviewer             : {}".format(args.reviewer))
    print("Authorized mutation  : verified backend identity fields only")
    print("Remote access/writes : disabled")
    print("Slurm/GPU/inference  : disabled")
    print("Backend activation   : disabled")
    print("")
    result = core.approve(
        args.backend,
        args.evidence_id,
        args.reviewer,
        args.confirm_register_verified_installation,
    )
    print("Approval complete.")
    print("Approval ID     : {}".format(result.approval_id))
    print("Status          : {}".format(result.status))
    print("Registry update : {}".format("applied" if result.applied else "already applied"))
    print("Next safe action: {}".format(result.next_safe_action))
    print("")
    print("Wrote: {}".format(result.approval_path))
    print("Wrote: {}".format(result.approval_path.with_suffix(".md")))
    print("Updated: {}".format(result.backend_path))
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "collect":
            return _collect(args)
        if args.command == "ingest":
            return _ingest(args)
        return _approve(args)
    except (ModelRegistrationError, AIEvaluationError, TaskScanError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
