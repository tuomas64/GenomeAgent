#!/usr/bin/env python3
"""Collect, authorize, submit and observe staged-model integrity verification."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.model_integrity_verification import (  # noqa: E402
    ModelIntegrityInventoryCollector,
    ModelIntegrityVerificationCore,
    ModelIntegrityVerificationError,
)
from genomeagent.task_scanner import SSHRemotePythonRunner, TaskScanError  # noqa: E402


def _roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend-root", type=Path, default=Path("config/ai/backends"))
    parser.add_argument("--prompt-root", type=Path, default=Path("config/ai/prompts"))
    parser.add_argument("--suite-root", type=Path, default=Path("config/ai/suites"))
    parser.add_argument("--case-root", type=Path, default=Path("benchmarks/ai"))
    parser.add_argument(
        "--policy-root", type=Path, default=Path("config/ai/integrity_verification")
    )
    parser.add_argument(
        "--bundle-root", type=Path, default=Path("workspace/model_acquisition_bundles")
    )
    parser.add_argument(
        "--plan-root", type=Path, default=Path("workspace/model_acquisition_plans")
    )
    parser.add_argument(
        "--download-execution-root",
        type=Path,
        default=Path("workspace/model_acquisition_downloads"),
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("workspace/model_integrity_inventory_evidence"),
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path("workspace/model_integrity_inventory_state"),
    )
    parser.add_argument(
        "--authorization-root",
        type=Path,
        default=Path("workspace/model_integrity_authorizations"),
    )
    parser.add_argument(
        "--verification-root",
        type=Path,
        default=Path("workspace/model_integrity_verifications"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a downloaded model without publishing or executing it."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("backend")
    collect.add_argument("--bundle-id", required=True)
    collect.add_argument("--download-execution-id", required=True)
    collect.add_argument("--host")
    collect.add_argument("--stamp")
    _roots(collect)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("backend")
    ingest.add_argument("--bundle-id", required=True)
    _roots(ingest)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("backend")
    authorize.add_argument("--bundle-id", required=True)
    authorize.add_argument("--download-execution-id", required=True)
    authorize.add_argument("--inventory-evidence-id", required=True)
    authorize.add_argument("--reviewer", required=True)
    authorize.add_argument("--confirm-staging-hash-verification", action="store_true")
    _roots(authorize)
    launch = commands.add_parser("launch")
    launch.add_argument("backend")
    launch.add_argument("--bundle-id", required=True)
    launch.add_argument("--authorization-id", required=True)
    launch.add_argument("--host")
    launch.add_argument("--confirm-submit-hash-verification", action="store_true")
    _roots(launch)
    status = commands.add_parser("status")
    status.add_argument("backend")
    status.add_argument("--bundle-id", required=True)
    status.add_argument("--authorization-id", required=True)
    status.add_argument("--host")
    status.add_argument("--stamp")
    _roots(status)
    return parser.parse_args()


def _registry(args: argparse.Namespace) -> AIRegistry:
    return AIRegistry(
        backend_root=args.backend_root,
        prompt_root=args.prompt_root,
        suite_root=args.suite_root,
        case_root=args.case_root,
    )


def _collector(args: argparse.Namespace) -> ModelIntegrityInventoryCollector:
    return ModelIntegrityInventoryCollector(
        registry=_registry(args),
        policy_root=args.policy_root,
        bundle_root=args.bundle_root,
        plan_root=args.plan_root,
        download_execution_root=args.download_execution_root,
        evidence_root=args.evidence_root,
    )


def _core(args: argparse.Namespace) -> ModelIntegrityVerificationCore:
    return ModelIntegrityVerificationCore(
        registry=_registry(args),
        policy_root=args.policy_root,
        bundle_root=args.bundle_root,
        plan_root=args.plan_root,
        download_execution_root=args.download_execution_root,
        evidence_root=args.evidence_root,
        state_root=args.state_root,
        authorization_root=args.authorization_root,
        verification_root=args.verification_root,
    )


def _runner(args: argparse.Namespace, core: ModelIntegrityVerificationCore):
    _, policy, _ = core.policy(args.backend)
    return SSHRemotePythonRunner(
        args.host or policy["ssh_host"],
        python_executable=policy["remote_python"],
    )


def _header(title: str) -> None:
    print("=" * 80)
    print(title)
    print("=" * 80)


def _collect(args: argparse.Namespace) -> int:
    core = _core(args)
    runner = _runner(args, core)
    _header("GenomeAgent Read-only Staged Model Inventory Collector")
    print("Backend            : {}".format(args.backend))
    print("Bundle ID          : {}".format(args.bundle_id))
    print("Download execution : {}".format(args.download_execution_id))
    print("SSH host           : {}".format(runner.host))
    print("Model contents read: no")
    print("Hashing/submission : no")
    print("Remote writes      : no")
    print("Publication/GPU    : no")
    print("")
    result = _collector(args).collect(
        args.backend,
        args.bundle_id,
        args.download_execution_id,
        runner,
        stamp=args.stamp,
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
    _header("GenomeAgent Staged Model Inventory Evidence State")
    print("Source mode      : immutable local evidence replay")
    print("Remote access    : none during ingest")
    print("Execution        : disabled")
    result = _core(args).ingest(args.backend, args.bundle_id)
    print("Backend          : {}".format(result.backend_id))
    print("Bundle ID        : {}".format(result.bundle_id))
    print("Source snapshots : {}".format(result.source_snapshots))
    print("Status           : {}".format(result.status))
    print("Next safe action : {}".format(result.next_safe_action))
    if result.blockers:
        print("Blockers         : {}".format(", ".join(result.blockers)))
    print("")
    for name in ("current_evidence.json", "readiness.json", "provenance.json", "report.md"):
        print("Wrote: {}".format(result.state_dir / name))
    return 0


def _authorize(args: argparse.Namespace) -> int:
    _header("GenomeAgent Explicit Model Hash Verification Authorization v1")
    print("Backend              : {}".format(args.backend))
    print("Bundle ID            : {}".format(args.bundle_id))
    print("Inventory evidence   : {}".format(args.inventory_evidence_id))
    print("Reviewer             : {}".format(args.reviewer))
    print("Authorized read      : exact staged model files")
    print("Authorized mutation  : verification control files and one Slurm submission")
    print("Staging modification : forbidden")
    print("Publication/GPU      : forbidden")
    print("")
    result = _core(args).authorize(
        args.backend,
        args.bundle_id,
        args.download_execution_id,
        args.inventory_evidence_id,
        args.reviewer,
        args.confirm_staging_hash_verification,
    )
    print("Authorization complete.")
    print("Authorization ID : {}".format(result.authorization_id))
    print("Status           : {}".format(result.status))
    print("Expires          : {}".format(result.expires_at_utc))
    print("")
    print("Wrote: {}".format(result.authorization_path))
    print("Wrote: {}".format(result.authorization_path.with_suffix(".md")))
    return 0


def _launch(args: argparse.Namespace) -> int:
    core = _core(args)
    runner = _runner(args, core)
    _header("GenomeAgent Controlled Model SHA-256 Verification Submission v1")
    print("Backend          : {}".format(args.backend))
    print("Bundle ID        : {}".format(args.bundle_id))
    print("Authorization ID : {}".format(args.authorization_id))
    print("SSH host         : {}".format(runner.host))
    print("Slurm request    : small, 1 CPU, 4G, 02:00:00")
    print("Model read       : exact staged inventory only")
    print("Staging writes   : disabled")
    print("Publication/GPU  : disabled")
    print("")
    result = core.launch(
        args.backend,
        args.bundle_id,
        args.authorization_id,
        runner,
        args.confirm_submit_hash_verification,
    )
    print("Submission complete.")
    print("Verification ID : {}".format(result.verification_id))
    print("Job ID          : {}".format(result.job_id))
    print("Remote status   : {}".format(result.remote_status))
    print("")
    print("Wrote: {}".format(result.launch_path))
    return 0


def _status(args: argparse.Namespace) -> int:
    core = _core(args)
    runner = _runner(args, core)
    _header("GenomeAgent Read-only Model Integrity Verification Status")
    print("Remote writes/model reads: disabled")
    result = core.status(
        args.backend,
        args.bundle_id,
        args.authorization_id,
        runner,
        stamp=args.stamp,
    )
    print("Verification ID : {}".format(result.verification_id))
    print("Job ID          : {}".format(result.job_id or "unknown"))
    print("Status          : {}".format(result.status))
    print("")
    print("Wrote: {}".format(result.observation_path))
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "collect":
            return _collect(args)
        if args.command == "ingest":
            return _ingest(args)
        if args.command == "authorize":
            return _authorize(args)
        if args.command == "launch":
            return _launch(args)
        return _status(args)
    except (ModelIntegrityVerificationError, AIEvaluationError, TaskScanError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
