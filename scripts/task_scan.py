#!/usr/bin/env python3
"""GenomeAgent task discovery shim: Task Scanner stable CLI."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IMPL = Path(__file__).with_name("task_scan_impl.py")
sys.path.insert(0, str(ROOT))
from genomeagent.task_catalog import print_task_catalog  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    if args in (["list"], ["--list-tasks"]):
        print_task_catalog(ROOT, "scanner")
        return 0
    if "--help" in args or "-h" in args:
        proc = subprocess.run([sys.executable, str(IMPL), *args])
        print()
        print_task_catalog(ROOT, "scanner")
        print("\nConvenience: python3 scripts/task_scan.py list")
        return proc.returncode
    os.execv(sys.executable, [sys.executable, str(IMPL), *args])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
