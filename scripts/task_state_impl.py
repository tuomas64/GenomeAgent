#!/usr/bin/env python3
"""Ingest Task Scanner bundles into GenomeAgent's operational knowledge bridge."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.task_state import (  # noqa: E402
    SUPPORTED_TASKS,
    TaskStateBridge,
    TaskStateError,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay read-only Task Scanner bundles into deterministic current state, "
            "history, provenance and recommendations."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="Ingest all scan bundles for one task.")
    ingest.add_argument("task", choices=sorted(SUPPORTED_TASKS))
    ingest.add_argument(
        "--scan-root",
        type=Path,
        default=Path("workspace/task_scans"),
        help="Root containing <task>/<timestamp>/task_scan.json bundles.",
    )
    ingest.add_argument(
        "--state-root",
        type=Path,
        default=Path("workspace/task_state"),
        help="Root for canonical task-state artifacts.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        bridge = TaskStateBridge(
            scan_root=args.scan_root,
            state_root=args.state_root,
        )
        result = bridge.ingest(args.task)
    except TaskStateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print("=" * 80)
    print(f"GenomeAgent Task State Bridge: {result.task}")
    print("=" * 80)
    print("Source mode     : read-only scan replay")
    print("Local writes    : canonical state artifacts only")
    print(f"Source scans    : {result.source_scans}")
    print(f"Latest scan     : {result.latest_scan_id}")
    print(f"Overall status  : {result.overall_status}")
    print(f"History events  : {result.events}")
    print(f"Recommendations : {result.recommendations}")
    print("")
    print(f"State directory : {result.state_dir}")
    for path in result.artifact_paths:
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
