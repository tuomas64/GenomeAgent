#!/usr/bin/env python3
"""GenomeAgent reusable Task Scanner command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.task_profiles import (  # noqa: E402
    GamDeduplicationProfile,
    ScatteredJointCallingProfile,
)
from genomeagent.task_scanner import (  # noqa: E402
    SSHRemotePythonRunner,
    TaskScanError,
    TaskScannerCore,
    read_json_config,
)


PROFILE_REGISTRY = {
    "gam_deduplication": GamDeduplicationProfile,
    "scattered_joint_calling": ScatteredJointCallingProfile,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a deterministic, read-only GenomeAgent task profile against an HPC host."
        )
    )
    parser.add_argument("task", choices=sorted(PROFILE_REGISTRY))
    parser.add_argument("--host", default="puhti", help="SSH host alias (default: puhti).")
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "JSON profile configuration. Defaults to config/tasks/<task>.json "
            "inside the repository."
        ),
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("workspace/task_scans"),
        help="Local root for timestamped scan results.",
    )
    parser.add_argument(
        "--stamp",
        help="Optional fixed output timestamp for testing or reproducible fixtures.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config or REPOSITORY_ROOT / "config" / "tasks" / f"{args.task}.json"

    try:
        config = read_json_config(config_path)
        profile = PROFILE_REGISTRY[args.task]()
        runner = SSHRemotePythonRunner(
            args.host,
            python_executable=config.get("remote_python", "python3"),
        )
        scanner = TaskScannerCore(runner=runner, output_root=args.out_root)

        print("=" * 80)
        print(f"GenomeAgent Task Scanner: {args.task}")
        print("=" * 80)
        print(f"SSH host      : {args.host}")
        print(f"Remote Python : {runner.python_executable}")
        print(f"Configuration : {config_path}")
        print("Mode          : read-only")
        print("")

        run = scanner.run(profile=profile, config=config, stamp=args.stamp)
    except TaskScanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    status = run.payload["status_summary"]
    print("Scan complete.")
    print(f"Overall status   : {status['overall_status']}")
    print(f"Current stage    : {status['current_stage']}")
    print(f"Next safe action : {status['next_safe_action']}")
    print("")
    print(f"Output directory : {run.scan_dir}")
    for path in run.artifact_paths:
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
