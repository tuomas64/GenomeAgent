#!/usr/bin/env python3
"""
GenomeAgent Explorer v1.0

Read-only full exploration of /scratch/project_2001113 through SSH alias `puhti`.
Runs from the Mac and inventories the entire accessible project tree.

Usage:
    python3 explorer.py

Outputs:
    workspace/explorer/<timestamp>/inventory.jsonl.gz
    workspace/explorer/<timestamp>/summary.json
    workspace/explorer/<timestamp>/top_level_summary.tsv
    workspace/explorer/<timestamp>/extension_summary.tsv
    workspace/explorer/<timestamp>/largest_files.tsv
    workspace/explorer/<timestamp>/newest_files.tsv
    workspace/explorer/<timestamp>/zero_byte_files.txt
    workspace/explorer/<timestamp>/empty_directories.txt
    workspace/explorer/<timestamp>/permission_errors.txt
"""

from __future__ import annotations

import argparse
import gzip
import heapq
import json
import shlex
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

CHUNK = 1024 * 1024
TOP_N = 100
TYPE_NAMES = {
    "f": "file", "d": "directory", "l": "symlink",
    "b": "block_device", "c": "character_device",
    "p": "fifo", "s": "socket", "?": "unknown",
}


def args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Full read-only HPC project explorer")
    p.add_argument("--host", default="puhti")
    p.add_argument("--project", default="/scratch/project_2001113")
    p.add_argument("--workspace", default="workspace/explorer")
    return p.parse_args()


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{n} B"


def nul_tokens(stream):
    buf = b""
    while True:
        block = stream.read(CHUNK)
        if not block:
            break
        buf += block
        fields = buf.split(b"\0")
        buf = fields.pop()
        yield from fields
    if buf:
        raise RuntimeError("Incomplete NUL-delimited record from remote find")


def relpath(path: str, root: str) -> str:
    root = root.rstrip("/")
    if path == root:
        return "."
    prefix = root + "/"
    return path[len(prefix):] if path.startswith(prefix) else path


def top_key(path: str) -> str:
    return "<project_root>" if path in ("", ".") else path.split("/", 1)[0]


def ext_key(path: str) -> str:
    name = PurePosixPath(path).name
    suffix = PurePosixPath(name).suffix.lower()
    return suffix if suffix else "<none>"


def read_stderr(stream, messages: list[str], denied: list[str]) -> None:
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue
        messages.append(line)
        if "Permission denied" in line:
            denied.append(line)


def scan_inventory(host: str, project: str, outdir: Path) -> dict:
    qroot = shlex.quote(project)
    remote = (
        "set -o pipefail; "
        f"test -d {qroot} || {{ echo 'Missing project: {qroot}' >&2; exit 2; }}; "
        f"nice -n 10 find {qroot} -xdev "
        r"-printf '%y\0%s\0%T@\0%m\0%p\0'"
    )

    inventory_tmp = outdir / "inventory.jsonl.gz.partial"
    inventory_final = outdir / "inventory.jsonl.gz"
    zero_path = outdir / "zero_byte_files.txt"

    counts = Counter()
    ext_counts = Counter()
    ext_bytes = Counter()
    top = defaultdict(lambda: {
        "files": 0, "directories": 0, "symlinks": 0,
        "other": 0, "logical_bytes": 0,
    })
    largest = []
    newest = []
    messages: list[str] = []
    denied: list[str] = []
    total_bytes = 0
    zero_count = 0
    entries = 0
    started = time.time()

    print("Connecting to Puhti...")
    print(f"Exploring the complete tree: {project}")

    with subprocess.Popen(
        ["ssh", host, remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as proc, gzip.open(inventory_tmp, "wt", encoding="utf-8") as inv, \
            zero_path.open("w", encoding="utf-8") as zero:

        if proc.stdout is None or proc.stderr is None:
            raise RuntimeError("Could not open SSH streams")

        thread = threading.Thread(
            target=read_stderr, args=(proc.stderr, messages, denied), daemon=True
        )
        thread.start()
        tokens = nul_tokens(proc.stdout)

        while True:
            try:
                raw_type = next(tokens)
            except StopIteration:
                break
            try:
                raw_size = next(tokens)
                raw_mtime = next(tokens)
                raw_mode = next(tokens)
                raw_path = next(tokens)
            except StopIteration as exc:
                proc.kill()
                raise RuntimeError("Partial record from remote find") from exc

            code = raw_type.decode("ascii", errors="replace") or "?"
            kind = TYPE_NAMES.get(code, "unknown")
            try:
                size = int(raw_size)
            except ValueError:
                size = 0
            try:
                mtime = float(raw_mtime)
            except ValueError:
                mtime = 0.0

            path = relpath(raw_path.decode("utf-8", errors="replace"), project)
            record = {
                "path": path,
                "type": kind,
                "size": size,
                "mtime_epoch": mtime,
                "mode": raw_mode.decode("ascii", errors="replace"),
            }
            inv.write(json.dumps(record, ensure_ascii=False) + "\n")

            entries += 1
            counts[kind] += 1
            group = top[top_key(path)]

            if kind == "file":
                total_bytes += size
                group["files"] += 1
                group["logical_bytes"] += size
                extension = ext_key(path)
                ext_counts[extension] += 1
                ext_bytes[extension] += size

                if size == 0:
                    zero.write(path + "\n")
                    zero_count += 1

                item_size = (size, path, mtime)
                if len(largest) < TOP_N:
                    heapq.heappush(largest, item_size)
                elif size > largest[0][0]:
                    heapq.heapreplace(largest, item_size)

                item_time = (mtime, path, size)
                if len(newest) < TOP_N:
                    heapq.heappush(newest, item_time)
                elif mtime > newest[0][0]:
                    heapq.heapreplace(newest, item_time)

            elif kind == "directory":
                group["directories"] += 1
            elif kind == "symlink":
                group["symlinks"] += 1
            else:
                group["other"] += 1

            if entries % 250000 == 0:
                print(f"  {entries:,} entries inventoried...")

        rc = proc.wait()
        thread.join()

    if rc != 0:
        inventory_tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Remote find failed with exit code {rc}")

    inventory_tmp.replace(inventory_final)

    return {
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - started, 3),
        "entries": entries,
        "counts": dict(counts),
        "logical_file_bytes": total_bytes,
        "zero_byte_files": zero_count,
        "top_level": dict(top),
        "extension_counts": dict(ext_counts),
        "extension_bytes": dict(ext_bytes),
        "largest_files": [
            {"path": p, "size": s, "mtime_epoch": t}
            for s, p, t in sorted(largest, reverse=True)
        ],
        "newest_files": [
            {"path": p, "size": s, "mtime_epoch": t}
            for t, p, s in sorted(newest, reverse=True)
        ],
        "messages": messages,
        "permission_errors": denied,
    }


def scan_empty_dirs(host: str, project: str, outdir: Path):
    remote = f"nice -n 10 find {shlex.quote(project)} -xdev -type d -empty -print0"
    messages: list[str] = []
    denied: list[str] = []
    count = 0

    print("Finding empty directories...")
    with subprocess.Popen(
        ["ssh", host, remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as proc, (outdir / "empty_directories.txt").open("w", encoding="utf-8") as out:
        if proc.stdout is None or proc.stderr is None:
            raise RuntimeError("Could not open SSH streams")
        thread = threading.Thread(
            target=read_stderr, args=(proc.stderr, messages, denied), daemon=True
        )
        thread.start()
        for raw in nul_tokens(proc.stdout):
            out.write(relpath(raw.decode("utf-8", errors="replace"), project) + "\n")
            count += 1
        rc = proc.wait()
        thread.join()

    if rc != 0 and not denied:
        raise RuntimeError(f"Empty-directory scan failed with exit code {rc}")
    return count, messages, denied


def write_tables(outdir: Path, result: dict) -> None:
    with (outdir / "top_level_summary.tsv").open("w", encoding="utf-8") as out:
        out.write("top_level\tfiles\tdirectories\tsymlinks\tother\tlogical_bytes\tlogical_size\n")
        rows = sorted(
            result["top_level"].items(),
            key=lambda x: x[1]["logical_bytes"], reverse=True,
        )
        for name, s in rows:
            out.write(
                f"{name}\t{s['files']}\t{s['directories']}\t{s['symlinks']}\t"
                f"{s['other']}\t{s['logical_bytes']}\t{human_size(s['logical_bytes'])}\n"
            )

    with (outdir / "extension_summary.tsv").open("w", encoding="utf-8") as out:
        out.write("extension\tfiles\tlogical_bytes\tlogical_size\n")
        exts = sorted(
            result["extension_counts"],
            key=lambda e: result["extension_bytes"].get(e, 0), reverse=True,
        )
        for ext in exts:
            size = result["extension_bytes"].get(ext, 0)
            out.write(f"{ext}\t{result['extension_counts'][ext]}\t{size}\t{human_size(size)}\n")

    with (outdir / "largest_files.tsv").open("w", encoding="utf-8") as out:
        out.write("rank\tsize_bytes\tsize\tmodified_utc\tpath\n")
        for i, item in enumerate(result["largest_files"], 1):
            stamp = datetime.fromtimestamp(item["mtime_epoch"], timezone.utc).isoformat()
            out.write(f"{i}\t{item['size']}\t{human_size(item['size'])}\t{stamp}\t{item['path']}\n")

    with (outdir / "newest_files.tsv").open("w", encoding="utf-8") as out:
        out.write("rank\tmodified_utc\tsize_bytes\tsize\tpath\n")
        for i, item in enumerate(result["newest_files"], 1):
            stamp = datetime.fromtimestamp(item["mtime_epoch"], timezone.utc).isoformat()
            out.write(f"{i}\t{stamp}\t{item['size']}\t{human_size(item['size'])}\t{item['path']}\n")


def write_messages(outdir: Path, messages: list[str], denied: list[str]) -> None:
    denied_set = set(denied)
    (outdir / "permission_errors.txt").write_text(
        "\n".join(denied) + ("\n" if denied else ""), encoding="utf-8"
    )
    other = [m for m in messages if m not in denied_set]
    (outdir / "ssh_messages.txt").write_text(
        "\n".join(other) + ("\n" if other else ""), encoding="utf-8"
    )


def main() -> None:
    cfg = args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(cfg.workspace)
    outdir = root / stamp
    outdir.mkdir(parents=True, exist_ok=False)

    try:
        result = scan_inventory(cfg.host, cfg.project, outdir)
        empty_count, empty_messages, empty_denied = scan_empty_dirs(
            cfg.host, cfg.project, outdir
        )
        result["empty_directories"] = empty_count
        result["messages"].extend(empty_messages)
        result["permission_errors"].extend(empty_denied)

        summary = {
            "explorer": "GenomeAgent Explorer v1.0",
            "mode": "full_project_read_only",
            "host": cfg.host,
            "project_root": cfg.project,
            "output_directory": str(outdir.resolve()),
            "started_at": result["started_at"],
            "finished_at": result["finished_at"],
            "elapsed_seconds": result["elapsed_seconds"],
            "entries": result["entries"],
            "counts_by_type": result["counts"],
            "logical_file_bytes": result["logical_file_bytes"],
            "logical_file_size": human_size(result["logical_file_bytes"]),
            "zero_byte_files": result["zero_byte_files"],
            "empty_directories": result["empty_directories"],
            "permission_errors": len(result["permission_errors"]),
            "top_level": result["top_level"],
            "extension_counts": result["extension_counts"],
            "extension_bytes": result["extension_bytes"],
            "largest_files": result["largest_files"],
            "newest_files": result["newest_files"],
        }
        (outdir / "summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        write_tables(outdir, result)
        write_messages(outdir, result["messages"], result["permission_errors"])

        root.mkdir(parents=True, exist_ok=True)
        (root / "latest_scan.json").write_text(
            json.dumps({
                "latest_run": str(outdir.resolve()),
                "project_root": cfg.project,
                "finished_at": result["finished_at"],
            }, indent=2),
            encoding="utf-8",
        )

    except KeyboardInterrupt:
        print(f"\nInterrupted. Partial output remains in {outdir}", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print(f"Partial output remains in {outdir}", file=sys.stderr)
        raise SystemExit(1)

    print("\n" + "=" * 66)
    print("GenomeAgent Explorer v1.0 complete")
    print("=" * 66)
    print(f"Entries inventoried : {result['entries']:,}")
    print(f"Files               : {result['counts'].get('file', 0):,}")
    print(f"Directories         : {result['counts'].get('directory', 0):,}")
    print(f"Logical file size   : {human_size(result['logical_file_bytes'])}")
    print(f"Empty directories   : {result['empty_directories']:,}")
    print(f"Zero-byte files     : {result['zero_byte_files']:,}")
    print(f"Permission errors   : {len(result['permission_errors']):,}")
    print(f"Output directory    : {outdir.resolve()}")


if __name__ == "__main__":
    main()
