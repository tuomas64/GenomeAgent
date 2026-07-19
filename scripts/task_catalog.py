#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from genomeagent.task_catalog import print_all_catalogs, print_task_catalog, print_task_detail  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List and inspect discoverable GenomeAgent tasks and datasets",
        epilog=(
            "Examples:\n"
            "  bin/genomeagent-tasks\n"
            "  bin/genomeagent-tasks list\n"
            "  bin/genomeagent-tasks show pangenome_construction\n"
            "  bin/genomeagent-tasks scanner\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("args", nargs="*")
    ns = parser.parse_args()
    args = ns.args
    if not args or args == ["list"] or args == ["all"]:
        print_all_catalogs(ROOT)
        return 0
    if len(args) == 1 and args[0] in {"scanner", "bridge"}:
        print_task_catalog(ROOT, args[0])
        return 0
    if len(args) == 2 and args[0] == "show":
        try:
            print_task_detail(ROOT, args[1])
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0
    parser.error("Use no arguments, list, scanner, bridge, or show <major-task>")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
