#!/usr/bin/env python3
"""Authorize, launch and observe a confined public model download."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.model_acquisition_download import (  # noqa: E402
    ControlledModelDownloadCore,
    ModelAcquisitionDownloadError,
)
from genomeagent.task_scanner import SSHRemotePythonRunner, TaskScanError  # noqa: E402


def _roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend-root", type=Path, default=Path("config/ai/backends"))
    parser.add_argument("--prompt-root", type=Path, default=Path("config/ai/prompts"))
    parser.add_argument("--suite-root", type=Path, default=Path("config/ai/suites"))
    parser.add_argument("--case-root", type=Path, default=Path("benchmarks/ai"))
    parser.add_argument(
        "--policy-root", type=Path, default=Path("config/ai/acquisition_execution")
    )
    parser.add_argument(
        "--bundle-root", type=Path, default=Path("workspace/model_acquisition_bundles")
    )
    parser.add_argument(
        "--plan-root", type=Path, default=Path("workspace/model_acquisition_plans")
    )
    parser.add_argument(
        "--preflight-evidence-root",
        type=Path,
        default=Path("workspace/model_acquisition_preflight_evidence"),
    )
    parser.add_argument(
        "--preflight-state-root",
        type=Path,
        default=Path("workspace/model_acquisition_preflight_state"),
    )
    parser.add_argument(
        "--authorization-root",
        type=Path,
        default=Path("workspace/model_acquisition_download_authorizations"),
    )
    parser.add_argument(
        "--execution-root", type=Path, default=Path("workspace/model_acquisition_downloads")
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control only the approved staging-download phase of model acquisition."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("backend")
    authorize.add_argument("--bundle-id", required=True)
    authorize.add_argument("--preflight-evidence-id", required=True)
    authorize.add_argument("--reviewer", required=True)
    authorize.add_argument("--confirm-public-model-download", action="store_true")
    _roots(authorize)
    launch = commands.add_parser("launch")
    launch.add_argument("backend")
    launch.add_argument("--bundle-id", required=True)
    launch.add_argument("--authorization-id", required=True)
    launch.add_argument("--host")
    launch.add_argument("--confirm-execute-approved-download", action="store_true")
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


def _core(args: argparse.Namespace) -> ControlledModelDownloadCore:
    return ControlledModelDownloadCore(
        registry=_registry(args),
        policy_root=args.policy_root,
        bundle_root=args.bundle_root,
        plan_root=args.plan_root,
        preflight_evidence_root=args.preflight_evidence_root,
        preflight_state_root=args.preflight_state_root,
        authorization_root=args.authorization_root,
        execution_root=args.execution_root,
    )


def _runner(args: argparse.Namespace, core: ControlledModelDownloadCore):
    _, backend, _ = core.registry.backend(args.backend)
    _, policy, _ = core.policy(args.backend)
    host = args.host or str(backend["ssh_host"])
    return SSHRemotePythonRunner(host, python_executable=str(policy["remote_python"]))


def _header(title: str) -> None:
    print("=" * 80)
    print(title)
    print("=" * 80)


def _authorize(args: argparse.Namespace) -> int:
    core = _core(args)
    _header("GenomeAgent Explicit Public Model Download Authorization v1")
    print("Backend                 : {}".format(args.backend))
    print("Bundle ID               : {}".format(args.bundle_id))
    print("Preflight evidence      : {}".format(args.preflight_evidence_id))
    print("Reviewer                : {}".format(args.reviewer))
    print("Authorized mutation     : exact public revision into staging only")
    print("Credentials/project data: forbidden")
    print("Hashing/publication     : forbidden")
    print("Slurm/GPU/inference     : forbidden")
    print("")
    result = core.authorize(
        args.backend,
        args.bundle_id,
        args.preflight_evidence_id,
        args.reviewer,
        args.confirm_public_model_download,
    )
    print("Authorization complete.")
    print("Authorization ID : {}".format(result.authorization_id))
    print("Status           : {}".format(result.status))
    print("Expires          : {}".format(result.expires_at_utc))
    print("Next safe action : launch_exact_approved_staging_download")
    print("")
    print("Wrote: {}".format(result.authorization_path))
    print("Wrote: {}".format(result.authorization_path.with_suffix(".md")))
    return 0


def _launch(args: argparse.Namespace) -> int:
    core = _core(args)
    runner = _runner(args, core)
    _header("GenomeAgent Controlled Public Model Download Launch v1")
    print("Backend              : {}".format(args.backend))
    print("Bundle ID            : {}".format(args.bundle_id))
    print("Authorization ID     : {}".format(args.authorization_id))
    print("SSH host             : {}".format(runner.host))
    print("Remote write         : explicitly authorized confined paths")
    print("Provider request     : exact public pinned repository")
    print("Model download       : staging only, background, max workers 2")
    print("Credentials          : disabled and removed from worker environment")
    print("Hashing/publication  : disabled")
    print("Slurm/GPU/inference  : disabled")
    print("")
    result = core.launch(
        args.backend,
        args.bundle_id,
        args.authorization_id,
        runner,
        args.confirm_execute_approved_download,
    )
    print("Launch complete.")
    print("Execution ID    : {}".format(result.execution_id))
    print("Remote status   : {}".format(result.remote_status))
    print("Control path    : {}".format(result.control_directory))
    print("")
    print("Wrote: {}".format(result.launch_path))
    print("Next: rerun this command with `status` instead of `launch`.")
    return 0


def _status(args: argparse.Namespace) -> int:
    core = _core(args)
    runner = _runner(args, core)
    _header("GenomeAgent Read-only Public Model Download Status")
    print("Remote writes/provider requests: disabled")
    result = core.status(
        args.backend,
        args.bundle_id,
        args.authorization_id,
        runner,
        stamp=args.stamp,
    )
    print("Execution ID : {}".format(result.execution_id))
    print("Status       : {}".format(result.status))
    print("")
    print("Wrote: {}".format(result.observation_path))
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "authorize":
            return _authorize(args)
        if args.command == "launch":
            return _launch(args)
        return _status(args)
    except (ModelAcquisitionDownloadError, AIEvaluationError, TaskScanError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
