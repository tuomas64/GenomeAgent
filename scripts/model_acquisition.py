#!/usr/bin/env python3
"""Build deterministic, non-executable AI model acquisition plans."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.model_acquisition import (  # noqa: E402
    ModelAcquisitionError,
    ModelAcquisitionPlanner,
)


def _add_registry_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend-root", type=Path, default=Path("config/ai/backends"))
    parser.add_argument("--prompt-root", type=Path, default=Path("config/ai/prompts"))
    parser.add_argument("--suite-root", type=Path, default=Path("config/ai/suites"))
    parser.add_argument("--case-root", type=Path, default=Path("benchmarks/ai"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an immutable model acquisition plan from local registry and saved "
            "environment evidence without downloading or executing anything."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser(
        "plan",
        help="Create a deterministic review-only acquisition plan.",
    )
    plan.add_argument("backend", help="Registered AI backend ID.")
    plan.add_argument(
        "--specification-root",
        type=Path,
        default=Path("config/ai/acquisition"),
    )
    plan.add_argument(
        "--backend-state-root",
        type=Path,
        default=Path("workspace/ai_backend_state"),
    )
    plan.add_argument(
        "--plan-root",
        type=Path,
        default=Path("workspace/model_acquisition_plans"),
    )
    _add_registry_paths(plan)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry = AIRegistry(
        backend_root=args.backend_root,
        prompt_root=args.prompt_root,
        suite_root=args.suite_root,
        case_root=args.case_root,
    )
    planner = ModelAcquisitionPlanner(
        registry=registry,
        specification_root=args.specification_root,
        backend_state_root=args.backend_state_root,
        plan_root=args.plan_root,
    )
    try:
        result = planner.plan(args.backend)
    except (ModelAcquisitionError, AIEvaluationError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2

    print("=" * 80)
    print("GenomeAgent Pinned Model Acquisition Planning Core v1")
    print("=" * 80)
    print("Source mode       : deterministic local artifact replay")
    print("Remote access     : none")
    print("Model download    : disabled")
    print("Remote writes     : disabled")
    print("Slurm submission  : disabled")
    print("GPU allocation    : disabled")
    print("Registry updates  : disabled")
    print("Backend activation: disabled")
    print("Backend           : {}".format(result.backend_id))
    print("Plan ID           : {}".format(result.plan_id))
    print("Status            : {}".format(result.status))
    print("Next safe action  : {}".format(result.next_safe_action))
    if result.blockers:
        print("Blockers          : {}".format(", ".join(result.blockers)))
    print("")
    print("Plan directory: {}".format(result.plan_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
