#!/usr/bin/env python3
"""Collect and ingest read-only model-acquisition runtime preflight evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.model_acquisition_preflight import (  # noqa: E402
    ModelAcquisitionPreflightCollector,
    ModelAcquisitionPreflightCore,
    ModelAcquisitionPreflightError,
)
from genomeagent.task_scanner import (  # noqa: E402
    SSHRemotePythonRunner,
    TaskScanError,
)


def _roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend-root", type=Path, default=Path("config/ai/backends"))
    parser.add_argument("--prompt-root", type=Path, default=Path("config/ai/prompts"))
    parser.add_argument("--suite-root", type=Path, default=Path("config/ai/suites"))
    parser.add_argument("--case-root", type=Path, default=Path("benchmarks/ai"))
    parser.add_argument(
        "--policy-root", type=Path, default=Path("config/ai/acquisition_runtime")
    )
    parser.add_argument(
        "--bundle-root", type=Path, default=Path("workspace/model_acquisition_bundles")
    )
    parser.add_argument(
        "--plan-root", type=Path, default=Path("workspace/model_acquisition_plans")
    )
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("workspace/model_acquisition_preflight_evidence"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Observe model-acquisition runtime readiness without remote writes."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("backend")
    collect.add_argument("--bundle-id", required=True)
    collect.add_argument("--host")
    collect.add_argument("--timeout", type=int)
    collect.add_argument("--stamp")
    _roots(collect)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("backend")
    ingest.add_argument("--bundle-id", required=True)
    ingest.add_argument(
        "--state-root",
        type=Path,
        default=Path("workspace/model_acquisition_preflight_state"),
    )
    _roots(ingest)
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
    registry = _registry(args)
    collector = ModelAcquisitionPreflightCollector(
        registry=registry,
        policy_root=args.policy_root,
        bundle_root=args.bundle_root,
        plan_root=args.plan_root,
        evidence_root=args.evidence_root,
    )
    _, backend, _ = registry.backend(args.backend)
    _, policy, _ = collector.policy(args.backend)
    host = args.host or str(backend["ssh_host"])
    runner = SSHRemotePythonRunner(host, python_executable=str(policy["remote_python"]))
    _header("GenomeAgent Read-only Model Acquisition Runtime Preflight")
    print("Backend              : {}".format(args.backend))
    print("Bundle ID            : {}".format(args.bundle_id))
    print("SSH host             : {}".format(host))
    print("Remote Python        : {}".format(runner.python_executable))
    print("Remote writes        : disabled")
    print("Provider requests    : disabled")
    print("Model download       : disabled")
    print("Slurm submission     : disabled")
    print("GPU allocation       : disabled")
    print("Model file reads     : disabled")
    print("Large-file hashing   : disabled")
    print("")
    result = collector.collect(
        args.backend,
        args.bundle_id,
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
    core = ModelAcquisitionPreflightCore(
        registry=_registry(args),
        policy_root=args.policy_root,
        bundle_root=args.bundle_root,
        plan_root=args.plan_root,
        evidence_root=args.evidence_root,
        state_root=args.state_root,
    )
    result = core.ingest(args.backend, args.bundle_id)
    _header("GenomeAgent Model Acquisition Runtime Preflight State")
    print("Source mode       : immutable local evidence replay")
    print("Remote access     : none during ingest")
    print("Execution         : disabled")
    print("Backend           : {}".format(result.backend_id))
    print("Bundle ID         : {}".format(result.bundle_id))
    print("Source snapshots  : {}".format(result.snapshots))
    print("Status            : {}".format(result.status))
    print("Next safe action  : {}".format(result.next_safe_action))
    if result.blockers:
        print("Blockers          : {}".format(", ".join(result.blockers)))
    print("")
    print("State directory: {}".format(result.state_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


def main() -> int:
    args = parse_args()
    try:
        return _collect(args) if args.command == "collect" else _ingest(args)
    except (ModelAcquisitionPreflightError, AIEvaluationError, TaskScanError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
