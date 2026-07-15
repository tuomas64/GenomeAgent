#!/usr/bin/env python3
"""Validate AI backends and create deterministic benchmark evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.ai_evaluation import (  # noqa: E402
    AIEvaluationCore,
    AIEvaluationError,
    AIRegistry,
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
            "Manage a local-first AI backend registry and deterministic benchmark "
            "evidence. This command never submits jobs or executes model output."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate",
        help="Validate all registered backends, prompts, suites and cases.",
    )
    _add_registry_paths(validate)

    prepare = subparsers.add_parser(
        "prepare",
        help="Create an immutable, non-executable benchmark run package.",
    )
    prepare.add_argument("--backend", required=True, help="Registered backend ID.")
    prepare.add_argument("--suite", required=True, help="Registered benchmark suite ID.")
    prepare.add_argument(
        "--run-root",
        type=Path,
        default=Path("workspace/ai_runs"),
    )
    _add_registry_paths(prepare)

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Score returned JSONL responses without running a model.",
    )
    evaluate.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="Prepared immutable run directory.",
    )
    evaluate.add_argument(
        "--responses",
        type=Path,
        required=True,
        help="JSONL response evidence produced outside this command.",
    )
    evaluate.add_argument(
        "--evaluation-root",
        type=Path,
        default=Path("workspace/ai_evaluations"),
    )
    _add_registry_paths(evaluate)
    return parser.parse_args()


def _registry(args: argparse.Namespace) -> AIRegistry:
    return AIRegistry(
        backend_root=args.backend_root,
        prompt_root=args.prompt_root,
        suite_root=args.suite_root,
        case_root=args.case_root,
    )


def _print_header(title: str) -> None:
    print("=" * 80)
    print(title)
    print("=" * 80)


def _validate(args: argparse.Namespace) -> int:
    result = _registry(args).validate()
    _print_header("GenomeAgent AI Backend Registry: validation")
    print("Source mode       : versioned local configuration")
    print("Remote access     : none")
    print("Model execution   : disabled")
    print("Scheduler changes : disabled")
    print("Backends          : {}".format(result.backends))
    print("Prompts           : {}".format(result.prompts))
    print("Suites            : {}".format(result.suites))
    print("Benchmark cases   : {}".format(result.cases))
    print("")
    print("Backend readiness:")
    for backend_id, status in sorted(result.backend_statuses.items()):
        print("- {}: {}".format(backend_id, status))
    return 0


def _prepare(args: argparse.Namespace) -> int:
    core = AIEvaluationCore(registry=_registry(args), run_root=args.run_root)
    result = core.prepare(args.backend, args.suite)
    _print_header("GenomeAgent AI Benchmark Run Preparation")
    print("Source mode       : deterministic local registry replay")
    print("Remote access     : none")
    print("Model download    : disabled")
    print("Model execution   : disabled")
    print("Job submission    : disabled")
    print("External fallback : disabled")
    print("Backend           : {}".format(result.backend_id))
    print("Suite             : {}".format(result.suite_id))
    print("Status            : {}".format(result.status))
    print("Run ID            : {}".format(result.run_id))
    if result.blockers:
        print("Blockers          : {}".format(", ".join(result.blockers)))
    else:
        print("Blockers          : none; researcher review still required")
    print("")
    print("Run directory: {}".format(result.run_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


def _evaluate(args: argparse.Namespace) -> int:
    core = AIEvaluationCore(
        registry=_registry(args),
        evaluation_root=args.evaluation_root,
    )
    result = core.evaluate(args.run_dir, args.responses)
    _print_header("GenomeAgent AI Benchmark Evaluation")
    print("Source mode       : immutable local response evidence")
    print("Remote access     : none")
    print("Model execution   : disabled")
    print("Output execution  : disabled")
    print("Backend activation: disabled")
    print("Backend           : {}".format(result.backend_id))
    print("Suite             : {}".format(result.suite_id))
    print("Status            : {}".format(result.status))
    print("Cases received    : {} / {}".format(
        result.cases_received, result.cases_expected
    ))
    print("Cases passed      : {}".format(result.cases_passed))
    print("Mean score        : {:.3f}".format(result.mean_score))
    print("")
    print("Evaluation directory: {}".format(result.evaluation_dir))
    for path in result.artifact_paths:
        print("Wrote: {}".format(path))
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "validate":
            return _validate(args)
        if args.command == "prepare":
            return _prepare(args)
        return _evaluate(args)
    except AIEvaluationError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
