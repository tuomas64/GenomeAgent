#!/usr/bin/env python3
"""Collect and ingest bounded read-only AI backend evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.ai_backend_evidence import (  # noqa: E402
    AIBackendEvidenceCollector,
    AIBackendEvidenceCore,
    AIBackendEvidenceError,
)
from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.task_scanner import (  # noqa: E402
    SSHRemotePythonRunner,
    TaskScanError,
)


def _add_registry_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=Path("config/ai/backends"),
    )
    parser.add_argument(
        "--prompt-root",
        type=Path,
        default=Path("config/ai/prompts"),
    )
    parser.add_argument(
        "--suite-root",
        type=Path,
        default=Path("config/ai/suites"),
    )
    parser.add_argument(
        "--case-root",
        type=Path,
        default=Path("benchmarks/ai"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect one bounded read-only AI backend observation or replay saved "
            "snapshots into deterministic current evidence."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser(
        "collect",
        help="Observe a registered backend without remote writes or job submission.",
    )
    collect.add_argument("backend", help="Registered AI backend ID.")
    collect.add_argument(
        "--host",
        help="Override the registered SSH alias without changing the backend record.",
    )
    collect.add_argument(
        "--timeout",
        type=int,
        help="Override the policy timeout, from 5 to 240 seconds.",
    )
    collect.add_argument(
        "--stamp",
        help="Fixed UTC evidence ID for tests, for example 20260715T120000Z.",
    )
    collect.add_argument(
        "--policy-root",
        type=Path,
        default=Path("config/ai/evidence"),
    )
    collect.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("workspace/ai_backend_evidence"),
    )
    _add_registry_paths(collect)

    ingest = subparsers.add_parser(
        "ingest",
        help="Rebuild current backend evidence from immutable local snapshots.",
    )
    ingest.add_argument("backend", help="Registered AI backend ID.")
    ingest.add_argument(
        "--policy-root",
        type=Path,
        default=Path("config/ai/evidence"),
    )
    ingest.add_argument(
        "--evidence-root",
        type=Path,
        default=Path("workspace/ai_backend_evidence"),
    )
    ingest.add_argument(
        "--state-root",
        type=Path,
        default=Path("workspace/ai_backend_state"),
    )
    _add_registry_paths(ingest)
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
    collector = AIBackendEvidenceCollector(
        registry=registry,
        policy_root=args.policy_root,
        evidence_root=args.evidence_root,
    )
    _, backend, _ = registry.backend(args.backend)
    _, policy, _ = collector.policy(args.backend)
    host = args.host or str(backend["ssh_host"])
    runner = SSHRemotePythonRunner(
        host,
        python_executable=str(policy["remote_python"]),
    )
    _header("GenomeAgent Read-only AI Backend Evidence Collector")
    print("Backend              : {}".format(args.backend))
    print("SSH host             : {}".format(host))
    print("Remote Python        : {}".format(runner.python_executable))
    print("Remote writes        : disabled")
    print("Slurm submission     : disabled")
    print("GPU allocation       : disabled")
    print("Model download/import: disabled")
    print("Recursive model scan : disabled")
    print("Large-file hashing   : disabled")
    print("")
    result = collector.collect(
        args.backend,
        runner,
        stamp=args.stamp,
        timeout_seconds=args.timeout,
    )
    print("Collection complete.")
    print("Evidence ID        : {}".format(result.evidence_id))
    print("Environment status : {}".format(result.environment_status))
    print("")
    print("Wrote: {}".format(result.evidence_path))
    print("Wrote: {}".format(result.evidence_path.with_suffix(".md")))
    return 0


def _ingest(args: argparse.Namespace) -> int:
    core = AIBackendEvidenceCore(
        registry=_registry(args),
        policy_root=args.policy_root,
        evidence_root=args.evidence_root,
        state_root=args.state_root,
    )
    result = core.ingest(args.backend)
    _header("GenomeAgent AI Backend Evidence State")
    print("Source mode       : immutable local evidence replay")
    print("Remote access     : none during ingest")
    print("Execution         : disabled")
    print("Registry updates  : disabled")
    print("Backend           : {}".format(result.backend_id))
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
        if args.command == "collect":
            return _collect(args)
        return _ingest(args)
    except (AIBackendEvidenceError, AIEvaluationError, TaskScanError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
