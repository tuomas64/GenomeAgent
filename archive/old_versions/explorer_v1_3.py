#!/usr/bin/env python3
"""
GenomeAgent Explorer v1.3
=========================

A deliberately simple, read-only full-project explorer built around the same
working SSH + GNU find design as GenomeAgent Scanner v2.2.

It performs ONE remote metadata traversal of:

    /scratch/project_2001113

It does not read file contents and does not modify the project.

Progress:
    - Prints every 5,000 discovered entries.
    - On later runs, estimates percentage from the previous completed scan.
    - Writes live status to workspace/explorer/current_progress.json.

Outputs:
    workspace/explorer/<UTC timestamp>/
        inventory.jsonl.gz
        summary.json
        top_level_summary.tsv
        extension_summary.tsv
        largest_files.tsv
        newest_files.tsv
        zero_byte_files.txt
        empty_directories.txt
        permission_errors.txt
        other_find_messages.txt

Usage:
    python3 -u explorer.py

Optional:
    python3 -u explorer.py --host puhti \
        --project /scratch/project_2001113 \
        --progress-every 5000
"""

from __future__ import annotations

import argparse
import gzip
import heapq
import json
import os
import posixpath
import shlex
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


TOP_N = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only inventory of a complete remote project tree."
    )
    parser.add_argument("--host", default="puhti", help="SSH host or alias")
    parser.add_argument(
        "--project",
        default="/scratch/project_2001113",
        help="Remote project root",
    )
    parser.add_argument(
        "--workspace",
        default="workspace/explorer",
        help="Local Explorer output directory",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=5000,
        help="Report progress after this many entries",
    )
    return parser.parse_args()


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024.0 or unit == "PiB":
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{value} B"


def relative_path(absolute_path: str, root: str) -> str:
    clean_root = root.rstrip("/")
    if absolute_path == clean_root:
        return "."
    prefix = clean_root + "/"
    if absolute_path.startswith(prefix):
        return absolute_path[len(prefix):]
    return absolute_path


def top_level(path: str) -> str:
    if path in ("", "."):
        return "<project_root>"
    return path.split("/", 1)[0]


def extension(path: str) -> str:
    name = PurePosixPath(path).name
    if name.startswith(".") and name.count(".") == 1:
        return "<none>"
    suffix = PurePosixPath(name).suffix.lower()
    return suffix or "<none>"


def load_previous_total(workspace: Path, project: str) -> int | None:
    latest = workspace / "latest_scan.json"
    if not latest.exists():
        return None
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
        if data.get("project_root") != project:
            return None
        value = int(data.get("total_entries", 0))
        return value if value > 0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_progress(
    path: Path,
    *,
    status: str,
    entries: int,
    previous_total: int | None,
    current_path: str = "",
    started_at: str,
) -> None:
    percentage = None
    if previous_total:
        percentage = round(entries * 100.0 / previous_total, 2)

    payload = {
        "status": status,
        "entries_processed": entries,
        "estimated_percentage": percentage,
        "estimate_based_on_previous_total": previous_total,
        "current_path": current_path,
        "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def build_remote_command(project: str, progress_every: int) -> str:
    root_q = shlex.quote(project)

    # GNU find prints five tab-separated metadata fields. This is intentionally
    # simpler than the earlier NUL-token streaming implementation.
    #
    # The awk stage flushes stdout and reports progress to stderr every N rows,
    # ensuring the Mac sees activity during a large traversal.
    script = f"""
set -o pipefail
export LC_ALL=C
ROOT={root_q}
ERROR_FILE=$(mktemp)
trap 'rm -f "$ERROR_FILE"' EXIT

if [[ ! -d "$ROOT" ]]; then
    echo "ERROR: directory does not exist: $ROOT" >&2
    exit 2
fi

find "$ROOT" -xdev -printf '%y\\t%s\\t%T@\\t%m\\t%p\\n' 2>"$ERROR_FILE" |
awk -F '\\t' -v step={int(progress_every)} '
{{
    print
    if (NR % step == 0) {{
        printf "__EXPLORER_PROGRESS__\\t%d\\t%s\\n", NR, $5 > "/dev/stderr"
        fflush("/dev/stderr")
        fflush()
    }}
}}
END {{
    printf "__EXPLORER_FINAL__\\t%d\\n", NR > "/dev/stderr"
    fflush("/dev/stderr")
    fflush()
}}
'

FIND_STATUS=${{PIPESTATUS[0]}}
cat "$ERROR_FILE" >&2
exit "$FIND_STATUS"
""".strip()

    return "bash -lc " + shlex.quote(script)


def stderr_worker(
    stream: Any,
    *,
    progress_file: Path,
    previous_total: int | None,
    started_at: str,
    progress_state: dict[str, Any],
    permission_errors: list[str],
    other_messages: list[str],
) -> None:
    for raw in iter(stream.readline, ""):
        line = raw.rstrip("\n")
        if not line:
            continue

        if line.startswith("__EXPLORER_PROGRESS__\t"):
            parts = line.split("\t", 2)
            try:
                count = int(parts[1])
            except (IndexError, ValueError):
                other_messages.append(line)
                continue

            current = parts[2] if len(parts) > 2 else ""
            progress_state["remote_count"] = count
            progress_state["current_path"] = current

            if previous_total:
                pct = count * 100.0 / previous_total
                print(
                    f"{count:,} entries found "
                    f"(approximately {pct:.1f}% of previous scan); "
                    f"current: {top_level(relative_path(current, progress_state['project']))}",
                    flush=True,
                )
            else:
                print(
                    f"{count:,} entries found; "
                    f"current: {top_level(relative_path(current, progress_state['project']))}",
                    flush=True,
                )

            write_progress(
                progress_file,
                status="running",
                entries=count,
                previous_total=previous_total,
                current_path=current,
                started_at=started_at,
            )
            continue

        if line.startswith("__EXPLORER_FINAL__\t"):
            parts = line.split("\t", 1)
            try:
                progress_state["remote_final"] = int(parts[1])
            except (IndexError, ValueError):
                other_messages.append(line)
            continue

        if "Permission denied" in line:
            permission_errors.append(line)
        else:
            other_messages.append(line)


def write_table_summaries(run_dir: Path, result: dict[str, Any]) -> None:
    with (run_dir / "top_level_summary.tsv").open(
        "w", encoding="utf-8"
    ) as out:
        out.write(
            "top_level\tfiles\tdirectories\tsymlinks\tother\t"
            "logical_bytes\tlogical_size\n"
        )
        rows = sorted(
            result["top_level"].items(),
            key=lambda item: item[1]["logical_bytes"],
            reverse=True,
        )
        for name, stats in rows:
            out.write(
                f"{name}\t{stats['files']}\t{stats['directories']}\t"
                f"{stats['symlinks']}\t{stats['other']}\t"
                f"{stats['logical_bytes']}\t"
                f"{human_size(stats['logical_bytes'])}\n"
            )

    with (run_dir / "extension_summary.tsv").open(
        "w", encoding="utf-8"
    ) as out:
        out.write("extension\tfiles\tlogical_bytes\tlogical_size\n")
        exts = sorted(
            result["extension_counts"],
            key=lambda item: result["extension_bytes"].get(item, 0),
            reverse=True,
        )
        for ext in exts:
            size = result["extension_bytes"].get(ext, 0)
            out.write(
                f"{ext}\t{result['extension_counts'][ext]}\t"
                f"{size}\t{human_size(size)}\n"
            )

    with (run_dir / "largest_files.tsv").open(
        "w", encoding="utf-8"
    ) as out:
        out.write("rank\tsize_bytes\tsize\tmodified_utc\tpath\n")
        for rank, item in enumerate(result["largest_files"], 1):
            modified = datetime.fromtimestamp(
                item["mtime_epoch"], timezone.utc
            ).isoformat()
            out.write(
                f"{rank}\t{item['size']}\t{human_size(item['size'])}\t"
                f"{modified}\t{item['path']}\n"
            )

    with (run_dir / "newest_files.tsv").open(
        "w", encoding="utf-8"
    ) as out:
        out.write("rank\tmodified_utc\tsize_bytes\tsize\tpath\n")
        for rank, item in enumerate(result["newest_files"], 1):
            modified = datetime.fromtimestamp(
                item["mtime_epoch"], timezone.utc
            ).isoformat()
            out.write(
                f"{rank}\t{modified}\t{item['size']}\t"
                f"{human_size(item['size'])}\t{item['path']}\n"
            )


def main() -> None:
    args = parse_args()
    if args.progress_every < 1:
        raise SystemExit("--progress-every must be at least 1")

    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = workspace / timestamp
    run_dir.mkdir(parents=True, exist_ok=False)

    progress_file = workspace / "current_progress.json"
    previous_total = load_previous_total(workspace, args.project)
    started_at = datetime.now(timezone.utc).isoformat()
    started_clock = time.time()

    print("Connecting to Puhti...", flush=True)
    print(f"Exploring the complete tree: {args.project}", flush=True)
    print("Method: one read-only remote GNU find traversal", flush=True)
    if previous_total:
        print(
            f"Previous completed scan: {previous_total:,} entries "
            "(used only for estimated percentage)",
            flush=True,
        )
    else:
        print(
            "First full scan: progress will be shown as entries found.",
            flush=True,
        )
    print(
        f"Progress interval: every {args.progress_every:,} entries",
        flush=True,
    )

    write_progress(
        progress_file,
        status="connecting",
        entries=0,
        previous_total=previous_total,
        started_at=started_at,
    )

    remote = build_remote_command(args.project, args.progress_every)

    partial_inventory = run_dir / "inventory.jsonl.gz.partial"
    final_inventory = run_dir / "inventory.jsonl.gz"
    zero_path = run_dir / "zero_byte_files.txt"

    counts: Counter[str] = Counter()
    ext_counts: Counter[str] = Counter()
    ext_bytes: Counter[str] = Counter()
    top_stats = defaultdict(
        lambda: {
            "files": 0,
            "directories": 0,
            "symlinks": 0,
            "other": 0,
            "logical_bytes": 0,
        }
    )

    all_directories: set[str] = set()
    directories_with_children: set[str] = set()
    largest_heap: list[tuple[int, str, float]] = []
    newest_heap: list[tuple[float, str, int]] = []

    total_entries = 0
    logical_bytes = 0
    zero_count = 0
    malformed_lines = 0

    permission_errors: list[str] = []
    other_messages: list[str] = []
    progress_state: dict[str, Any] = {
        "remote_count": 0,
        "remote_final": None,
        "current_path": "",
        "project": args.project,
    }

    try:
        process = subprocess.Popen(
            ["ssh", args.host, remote],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        if process.stdout is None or process.stderr is None:
            raise RuntimeError("Could not open SSH output streams.")

        thread = threading.Thread(
            target=stderr_worker,
            args=(process.stderr,),
            kwargs={
                "progress_file": progress_file,
                "previous_total": previous_total,
                "started_at": started_at,
                "progress_state": progress_state,
                "permission_errors": permission_errors,
                "other_messages": other_messages,
            },
            daemon=True,
        )
        thread.start()

        with (
            gzip.open(partial_inventory, "wt", encoding="utf-8") as inventory,
            zero_path.open("w", encoding="utf-8") as zero_out,
        ):
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                fields = line.split("\t", 4)
                if len(fields) != 5:
                    malformed_lines += 1
                    continue

                kind_code, raw_size, raw_mtime, mode, absolute = fields
                try:
                    size = int(raw_size)
                    mtime = float(raw_mtime)
                except ValueError:
                    malformed_lines += 1
                    continue

                rel = relative_path(absolute, args.project)
                kind = {
                    "f": "file",
                    "d": "directory",
                    "l": "symlink",
                    "b": "block_device",
                    "c": "character_device",
                    "p": "fifo",
                    "s": "socket",
                }.get(kind_code, "other")

                record = {
                    "path": rel,
                    "type": kind,
                    "size": size,
                    "mtime_epoch": mtime,
                    "mode": mode,
                }
                inventory.write(json.dumps(record, ensure_ascii=False))
                inventory.write("\n")

                total_entries += 1
                counts[kind] += 1
                top = top_level(rel)

                if rel not in ("", "."):
                    parent = posixpath.dirname(rel) or "."
                    directories_with_children.add(parent)

                if kind == "directory":
                    all_directories.add(rel)
                    top_stats[top]["directories"] += 1

                elif kind == "file":
                    logical_bytes += size
                    top_stats[top]["files"] += 1
                    top_stats[top]["logical_bytes"] += size

                    ext = extension(rel)
                    ext_counts[ext] += 1
                    ext_bytes[ext] += size

                    if size == 0:
                        zero_out.write(rel + "\n")
                        zero_count += 1

                    largest_item = (size, rel, mtime)
                    if len(largest_heap) < TOP_N:
                        heapq.heappush(largest_heap, largest_item)
                    elif size > largest_heap[0][0]:
                        heapq.heapreplace(largest_heap, largest_item)

                    newest_item = (mtime, rel, size)
                    if len(newest_heap) < TOP_N:
                        heapq.heappush(newest_heap, newest_item)
                    elif mtime > newest_heap[0][0]:
                        heapq.heapreplace(newest_heap, newest_item)

                elif kind == "symlink":
                    top_stats[top]["symlinks"] += 1
                else:
                    top_stats[top]["other"] += 1

        return_code = process.wait()
        thread.join()

        if return_code != 0:
            raise RuntimeError(
                f"Remote find exited with status {return_code}. "
                f"See {run_dir / 'other_find_messages.txt'}."
            )

        partial_inventory.replace(final_inventory)

    except KeyboardInterrupt:
        print("\nInterrupted by user.", flush=True)
        write_progress(
            progress_file,
            status="interrupted",
            entries=total_entries,
            previous_total=previous_total,
            current_path=progress_state.get("current_path", ""),
            started_at=started_at,
        )
        raise SystemExit(130)

    except Exception as exc:
        write_progress(
            progress_file,
            status="failed",
            entries=total_entries,
            previous_total=previous_total,
            current_path=progress_state.get("current_path", ""),
            started_at=started_at,
        )
        print(f"\nERROR: {exc}", file=sys.stderr, flush=True)
        print(f"Partial results: {run_dir}", file=sys.stderr, flush=True)
        raise SystemExit(1)

    empty_directories = sorted(
        path
        for path in all_directories
        if path not in directories_with_children
    )
    (run_dir / "empty_directories.txt").write_text(
        "".join(path + "\n" for path in empty_directories),
        encoding="utf-8",
    )
    (run_dir / "permission_errors.txt").write_text(
        "".join(line + "\n" for line in permission_errors),
        encoding="utf-8",
    )
    (run_dir / "other_find_messages.txt").write_text(
        "".join(line + "\n" for line in other_messages),
        encoding="utf-8",
    )

    largest = [
        {"path": path, "size": size, "mtime_epoch": mtime}
        for size, path, mtime in sorted(largest_heap, reverse=True)
    ]
    newest = [
        {"path": path, "size": size, "mtime_epoch": mtime}
        for mtime, path, size in sorted(newest_heap, reverse=True)
    ]

    elapsed = time.time() - started_clock
    result = {
        "explorer": "GenomeAgent Explorer v1.3",
        "mode": "full_project_read_only",
        "host": args.host,
        "project_root": args.project,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed, 3),
        "total_entries": total_entries,
        "counts": dict(counts),
        "logical_file_bytes": logical_bytes,
        "logical_file_size": human_size(logical_bytes),
        "zero_byte_files": zero_count,
        "empty_directories": len(empty_directories),
        "permission_errors": len(permission_errors),
        "malformed_inventory_lines": malformed_lines,
        "top_level": dict(top_stats),
        "extension_counts": dict(ext_counts),
        "extension_bytes": dict(ext_bytes),
        "largest_files": largest,
        "newest_files": newest,
    }

    (run_dir / "summary.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    write_table_summaries(run_dir, result)

    latest = {
        "explorer": "GenomeAgent Explorer v1.3",
        "project_root": args.project,
        "latest_run": str(run_dir.resolve()),
        "total_entries": total_entries,
        "finished_at": result["finished_at"],
    }
    (workspace / "latest_scan.json").write_text(
        json.dumps(latest, indent=2),
        encoding="utf-8",
    )

    write_progress(
        progress_file,
        status="complete",
        entries=total_entries,
        previous_total=previous_total,
        current_path=progress_state.get("current_path", ""),
        started_at=started_at,
    )

    print()
    print("=" * 68)
    print("GenomeAgent Explorer v1.3 complete")
    print("=" * 68)
    print(f"Entries             : {total_entries:,}")
    print(f"Files               : {counts.get('file', 0):,}")
    print(f"Directories         : {counts.get('directory', 0):,}")
    print(f"Symlinks            : {counts.get('symlink', 0):,}")
    print(f"Logical file size   : {human_size(logical_bytes)}")
    print(f"Empty directories   : {len(empty_directories):,}")
    print(f"Zero-byte files     : {zero_count:,}")
    print(f"Permission errors   : {len(permission_errors):,}")
    print(f"Elapsed             : {elapsed / 60.0:.2f} minutes")
    print(f"Results             : {run_dir.resolve()}")


if __name__ == "__main__":
    main()
