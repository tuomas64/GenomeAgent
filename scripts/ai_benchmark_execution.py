#!/usr/bin/env python3
"""Plan, authorize, run, observe and score a bounded AI GPU benchmark."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genomeagent.ai_benchmark_execution import (  # noqa: E402
    AIBenchmarkExecutionCore,
    AIBenchmarkExecutionError,
)
from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry  # noqa: E402
from genomeagent.task_scanner import SSHRemotePythonRunner, TaskScanError  # noqa: E402


def _roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend-root", type=Path, default=Path("config/ai/backends"))
    parser.add_argument("--prompt-root", type=Path, default=Path("config/ai/prompts"))
    parser.add_argument("--suite-root", type=Path, default=Path("config/ai/suites"))
    parser.add_argument("--case-root", type=Path, default=Path("benchmarks/ai"))
    parser.add_argument("--policy-root", type=Path, default=Path("config/ai/benchmark_execution"))
    parser.add_argument("--backend-state-root", type=Path, default=Path("workspace/ai_backend_state"))
    parser.add_argument("--run-root", type=Path, default=Path("workspace/ai_runs"))
    parser.add_argument("--plan-root", type=Path, default=Path("workspace/ai_benchmark_plans"))
    parser.add_argument("--authorization-root", type=Path, default=Path("workspace/ai_benchmark_authorizations"))
    parser.add_argument("--execution-root", type=Path, default=Path("workspace/ai_benchmark_executions"))
    parser.add_argument("--evaluation-root", type=Path, default=Path("workspace/ai_evaluations"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Controlled bounded AI benchmark execution.")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("backend")
    prepare.add_argument("--suite", required=True)
    _roots(prepare)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("backend")
    authorize.add_argument("--suite", required=True)
    authorize.add_argument("--plan-id", required=True)
    authorize.add_argument("--reviewer", required=True)
    authorize.add_argument("--confirm-bounded-gpu-benchmark", action="store_true")
    _roots(authorize)
    launch = commands.add_parser("launch")
    launch.add_argument("backend")
    launch.add_argument("--authorization-id", required=True)
    launch.add_argument("--host")
    launch.add_argument("--confirm-submit-bounded-gpu-benchmark", action="store_true")
    _roots(launch)
    status = commands.add_parser("status")
    status.add_argument("backend")
    status.add_argument("--authorization-id", required=True)
    status.add_argument("--host")
    status.add_argument("--stamp")
    _roots(status)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("backend")
    evaluate.add_argument("--authorization-id", required=True)
    _roots(evaluate)
    return parser.parse_args()


def _core(args: argparse.Namespace) -> AIBenchmarkExecutionCore:
    registry = AIRegistry(args.backend_root, args.prompt_root, args.suite_root, args.case_root)
    return AIBenchmarkExecutionCore(
        registry=registry,
        policy_root=args.policy_root,
        backend_state_root=args.backend_state_root,
        run_root=args.run_root,
        plan_root=args.plan_root,
        authorization_root=args.authorization_root,
        execution_root=args.execution_root,
        evaluation_root=args.evaluation_root,
    )


def _runner(args: argparse.Namespace, core: AIBenchmarkExecutionCore):
    _, policy, _ = core.policy(args.backend)
    return SSHRemotePythonRunner(
        args.host or policy["ssh_host"], python_executable=policy["remote_python"]
    )


def _header(title: str) -> None:
    print("=" * 80)
    print(title)
    print("=" * 80)


def main() -> int:
    args = parse_args()
    try:
        core = _core(args)
        if args.command == "prepare":
            result = core.prepare(args.backend, args.suite)
            _header("GenomeAgent Controlled Bounded GPU Benchmark Plan v1")
            print("Source mode       : deterministic local artifact replay")
            print("Remote access     : disabled")
            print("GPU/inference     : disabled during preparation")
            print("Automatic submit  : disabled")
            print("Backend           : {}".format(result.backend_id))
            print("Suite             : {}".format(result.suite_id))
            print("Run ID            : {}".format(result.run_id))
            print("Plan ID           : {}".format(result.plan_id))
            print("Status            : {}".format(result.status))
            print("Blockers          : {}".format(", ".join(result.blockers)))
            print("\nPlan directory: {}".format(result.plan_dir))
            for path in result.artifact_paths:
                print("Wrote: {}".format(path))
            return 0
        if args.command == "authorize":
            result = core.authorize(
                args.backend, args.suite, args.plan_id, args.reviewer,
                args.confirm_bounded_gpu_benchmark,
            )
            _header("GenomeAgent Explicit Bounded GPU Benchmark Authorization v1")
            print("Backend              : {}".format(result.backend_id))
            print("Suite                : {}".format(result.suite_id))
            print("Plan ID              : {}".format(result.plan_id))
            print("Reviewer             : {}".format(args.reviewer))
            print("Authorized execution : one GH200 job, fixture suite only")
            print("Model output execution: disabled")
            print("Training/activation  : disabled")
            print("\nAuthorization complete.")
            print("Authorization ID : {}".format(result.authorization_id))
            print("Status           : {}".format(result.status))
            print("Expires          : {}".format(result.expires_at_utc))
            print("\nWrote: {}".format(result.authorization_path))
            print("Wrote: {}".format(result.authorization_path.with_suffix(".md")))
            return 0
        if args.command == "launch":
            runner = _runner(args, core)
            _header("GenomeAgent Controlled Bounded GPU Benchmark Submission v1")
            print("Backend          : {}".format(args.backend))
            print("Authorization ID : {}".format(args.authorization_id))
            print("SSH host         : {}".format(runner.host))
            print("Slurm request    : gputest, 1 GH200, 16 CPU, 120G, 00:15:00")
            print("Provider network : disabled")
            print("Training/output execution/activation: disabled")
            print("")
            result = core.launch(
                args.backend, args.authorization_id, runner,
                args.confirm_submit_bounded_gpu_benchmark,
            )
            print("Submission complete.")
            print("Execution ID : {}".format(result.execution_id))
            print("Job ID       : {}".format(result.job_id))
            print("Status       : {}".format(result.remote_status))
            print("\nWrote: {}".format(result.launch_path))
            return 0
        if args.command == "status":
            runner = _runner(args, core)
            result = core.status(
                args.backend, args.authorization_id, runner, stamp=args.stamp
            )
            _header("GenomeAgent Read-only Bounded GPU Benchmark Status")
            print("Remote writes/model execution: disabled during observation")
            print("Execution ID : {}".format(result.execution_id))
            print("Job ID       : {}".format(result.job_id))
            print("Status       : {}".format(result.status))
            print("\nWrote: {}".format(result.observation_path))
            if result.responses_path:
                print("Wrote: {}".format(result.responses_path))
                print("Next: rerun with `evaluate` instead of `status`.")
            return 0
        result = core.evaluate(args.backend, args.authorization_id)
        _header("GenomeAgent Deterministic Bounded AI Benchmark Evaluation")
        print("Remote access/model execution: disabled")
        print("Generated output execution    : disabled")
        print("Automatic backend activation  : disabled")
        print("Backend       : {}".format(result.backend_id))
        print("Suite         : {}".format(result.suite_id))
        print("Status        : {}".format(result.status))
        print("Cases passed  : {} / {}".format(result.cases_passed, result.cases_expected))
        print("Mean score    : {:.3f}".format(result.mean_score))
        print("\nEvaluation directory: {}".format(result.evaluation_dir))
        for path in result.artifact_paths:
            print("Wrote: {}".format(path))
        return 0
    except (AIBenchmarkExecutionError, AIEvaluationError, TaskScanError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
