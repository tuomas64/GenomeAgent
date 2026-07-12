#!/usr/bin/env python3
"""
GenomeAgent Brain Corpus Extractor v1.0
=======================================

Builds a clean script corpus from the latest successful Explorer inventory.

Selected script types:
    .sh
    .slurm
    .sbatch
    .py

The inventory is used locally to avoid another remote project-wide find scan.
Only the selected files are retrieved from Puhti, in one SSH + tar operation.

Default inputs:
    workspace/explorer/latest_scan.json
    <latest_run>/inventory.jsonl.gz

Outputs:
    workspace/brain_inputs/<UTC timestamp>/
        project_snapshot_brain.json
        scripts_brain.tar.gz
        selected_scripts.txt
        excluded_scripts.tsv
        extraction_report.json
        tar_stderr.txt

For compatibility with brain_v1.py, successful outputs are also copied to:
    workspace/project_snapshot_brain.json
    workspace/scripts_brain.tar.gz

Usage:
    python3 extract_brain_scripts.py

Optional:
    python3 extract_brain_scripts.py \
        --host puhti \
        --workspace workspace \
        --max-file-size 1048576
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import subprocess
import sys
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


SCRIPT_SUFFIXES = {".sh", ".slurm", ".sbatch", ".py"}

# These path components identify third-party software, package caches,
# virtual environments, version-control internals, and generated Python caches.
DEFAULT_EXCLUDED_COMPONENTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "virtualenv",
    ".conda",
    "conda",
    "miniconda",
    "miniconda3",
    "anaconda",
    "anaconda3",
    "mambaforge",
    "micromamba",
    "envs",
    "site-packages",
    "dist-packages",
    "__pycache__",
    "node_modules",
    "bioconda3_pkgs",
    "pkgs",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a clean GenomeAgent Brain script corpus."
    )
    parser.add_argument("--host", default="puhti", help="SSH host or alias")
    parser.add_argument(
        "--workspace",
        default="workspace",
        help="GenomeAgent workspace directory",
    )
    parser.add_argument(
        "--latest-scan",
        default=None,
        help=(
            "Path to Explorer latest_scan.json. "
            "Default: <workspace>/explorer/latest_scan.json"
        ),
    )
    parser.add_argument(
        "--inventory",
        default=None,
        help="Explicit inventory.jsonl.gz path; overrides latest_scan.json",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Remote project root; normally read from latest_scan.json",
    )
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=1_048_576,
        help="Maximum selected script size in bytes (default: 1 MiB)",
    )
    parser.add_argument(
        "--include-zero-byte",
        action="store_true",
        help="Include zero-byte script files",
    )
    return parser.parse_args()


def load_scan_context(
    workspace: Path,
    latest_scan_arg: str | None,
    inventory_arg: str | None,
    project_arg: str | None,
) -> tuple[Path, str]:
    if inventory_arg:
        inventory = Path(inventory_arg).expanduser().resolve()
        if not inventory.exists():
            raise FileNotFoundError(f"Inventory not found: {inventory}")
        if not project_arg:
            raise ValueError("--project is required when --inventory is used")
        return inventory, project_arg.rstrip("/")

    latest_scan = (
        Path(latest_scan_arg).expanduser()
        if latest_scan_arg
        else workspace / "explorer" / "latest_scan.json"
    )
    if not latest_scan.exists():
        raise FileNotFoundError(f"Explorer state not found: {latest_scan}")

    state = json.loads(latest_scan.read_text(encoding="utf-8"))
    project = project_arg or state.get("project_root")
    latest_run = state.get("latest_run")

    if not project:
        raise ValueError(f"project_root is missing from {latest_scan}")
    if not latest_run:
        raise ValueError(f"latest_run is missing from {latest_scan}")

    inventory = Path(latest_run) / "inventory.jsonl.gz"
    if not inventory.exists():
        raise FileNotFoundError(f"Inventory not found: {inventory}")

    return inventory.resolve(), str(project).rstrip("/")


def exclusion_reason(
    record: dict[str, Any],
    max_file_size: int,
    include_zero_byte: bool,
) -> str | None:
    if record.get("type") != "file":
        return "not_regular_file"

    path = str(record.get("path", ""))
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in SCRIPT_SUFFIXES:
        return "unsupported_extension"

    components = {part.lower() for part in PurePosixPath(path).parts}
    blocked = sorted(components & DEFAULT_EXCLUDED_COMPONENTS)
    if blocked:
        return "excluded_path_component:" + ",".join(blocked)

    try:
        size = int(record.get("size", 0))
    except (TypeError, ValueError):
        return "invalid_size"

    if size == 0 and not include_zero_byte:
        return "zero_byte"

    if size > max_file_size:
        return f"oversized:{size}"

    if path in ("", ".") or path.startswith("../") or path.startswith("/"):
        return "unsafe_relative_path"

    return None


def read_inventory(
    inventory: Path,
    max_file_size: int,
    include_zero_byte: bool,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, int]], dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    excluded: list[tuple[str, str, int]] = []

    total_records = 0
    candidate_extension_records = 0
    selected_bytes = 0
    selected_by_suffix: Counter[str] = Counter()
    excluded_by_reason: Counter[str] = Counter()

    with gzip.open(inventory, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue

            total_records += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid JSON in {inventory} at line {line_number}"
                ) from exc

            path = str(record.get("path", ""))
            suffix = PurePosixPath(path).suffix.lower()
            if suffix in SCRIPT_SUFFIXES:
                candidate_extension_records += 1

            reason = exclusion_reason(
                record,
                max_file_size=max_file_size,
                include_zero_byte=include_zero_byte,
            )
            if reason is not None:
                # Only report exclusions that had one of the requested suffixes.
                if suffix in SCRIPT_SUFFIXES:
                    try:
                        size = int(record.get("size", 0))
                    except (TypeError, ValueError):
                        size = 0
                    excluded.append((path, reason, size))
                    excluded_by_reason[reason] += 1
                continue

            normalized = {
                "path": path,
                "size": int(record.get("size", 0)),
                "mtime_epoch": float(record.get("mtime_epoch", 0.0)),
                "mode": str(record.get("mode", "")),
            }
            selected.append(normalized)
            selected_bytes += normalized["size"]
            selected_by_suffix[suffix] += 1

    selected.sort(key=lambda item: item["path"])
    excluded.sort(key=lambda item: item[0])

    report = {
        "inventory_records_examined": total_records,
        "requested_extension_candidates": candidate_extension_records,
        "scripts_selected": len(selected),
        "selected_uncompressed_bytes_from_inventory": selected_bytes,
        "selected_by_suffix": dict(sorted(selected_by_suffix.items())),
        "scripts_excluded": len(excluded),
        "excluded_by_reason": dict(sorted(excluded_by_reason.items())),
    }
    return selected, excluded, report


def retrieve_scripts(
    host: str,
    project_root: str,
    selected: list[dict[str, Any]],
    tarball: Path,
    stderr_path: Path,
) -> tuple[int, str]:
    if not selected:
        raise RuntimeError("No scripts were selected from the inventory.")

    # Send NUL-delimited relative paths to GNU tar on Puhti. This avoids command
    # length limits and safely handles spaces and most unusual filenames.
    payload = b"".join(
        item["path"].encode("utf-8", errors="surrogateescape") + b"\0"
        for item in selected
    )

    remote = (
        f"cd {subprocess.list2cmdline([project_root])} && "
        "tar --null --verbatim-files-from --no-recursion "
        "--ignore-failed-read -czf - -T -"
    )

    with tarball.open("wb") as output:
        process = subprocess.Popen(
            ["ssh", host, remote],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.PIPE,
        )
        _, stderr = process.communicate(input=payload)

    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
    stderr_path.write_text(stderr_text, encoding="utf-8")
    return process.returncode, stderr_text


def inspect_tarball(
    tarball: Path,
    metadata_by_path: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    collected: list[dict[str, Any]] = []
    total_raw_bytes = 0
    total_text_characters = 0

    with tarfile.open(tarball, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        members.sort(key=lambda member: member.name)

        for member in members:
            path = member.name.lstrip("./")
            metadata = metadata_by_path.get(path, {})
            extracted = archive.extractfile(member)
            if extracted is None:
                continue

            raw = extracted.read()
            text = raw.decode("utf-8", errors="replace")

            collected.append(
                {
                    "path": path,
                    "size": len(raw),
                    "inventory_size": metadata.get("size"),
                    "mtime_epoch": metadata.get("mtime_epoch"),
                    "mode": metadata.get("mode"),
                }
            )
            total_raw_bytes += len(raw)
            total_text_characters += len(text)

    return collected, total_raw_bytes, total_text_characters


def atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


def main() -> None:
    args = parse_args()

    if args.max_file_size < 1:
        raise SystemExit("--max-file-size must be at least 1 byte")

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        inventory, project_root = load_scan_context(
            workspace=workspace,
            latest_scan_arg=args.latest_scan,
            inventory_arg=args.inventory,
            project_arg=args.project,
        )

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = workspace / "brain_inputs" / timestamp
        run_dir.mkdir(parents=True, exist_ok=False)

        print("=" * 68, flush=True)
        print("GenomeAgent Brain Corpus Extractor v1.0", flush=True)
        print("=" * 68, flush=True)
        print(f"Inventory    : {inventory}", flush=True)
        print(f"Remote root  : {project_root}", flush=True)
        print(
            "Script types : .sh, .slurm, .sbatch, .py",
            flush=True,
        )
        print("Selecting scripts from the existing inventory...", flush=True)

        selected, excluded, report = read_inventory(
            inventory=inventory,
            max_file_size=args.max_file_size,
            include_zero_byte=args.include_zero_byte,
        )

        selected_path = run_dir / "selected_scripts.txt"
        selected_path.write_text(
            "".join(item["path"] + "\n" for item in selected),
            encoding="utf-8",
        )

        excluded_path = run_dir / "excluded_scripts.tsv"
        with excluded_path.open("w", encoding="utf-8") as output:
            output.write("path\treason\tsize_bytes\n")
            for path, reason, size in excluded:
                output.write(f"{path}\t{reason}\t{size}\n")

        print(f"Selected     : {len(selected):,}", flush=True)
        print(f"Excluded     : {len(excluded):,}", flush=True)
        print("Retrieving selected scripts from Puhti...", flush=True)

        tarball = run_dir / "scripts_brain.tar.gz"
        stderr_path = run_dir / "tar_stderr.txt"
        return_code, stderr_text = retrieve_scripts(
            host=args.host,
            project_root=project_root,
            selected=selected,
            tarball=tarball,
            stderr_path=stderr_path,
        )

        if return_code != 0:
            raise RuntimeError(
                f"Remote tar exited with status {return_code}. "
                f"See {stderr_path}"
            )

        metadata_by_path = {item["path"]: item for item in selected}
        collected, raw_bytes, text_chars = inspect_tarball(
            tarball=tarball,
            metadata_by_path=metadata_by_path,
        )

        collected_paths = {item["path"] for item in collected}
        requested_paths = {item["path"] for item in selected}
        missing_paths = sorted(requested_paths - collected_paths)

        snapshot = {
            "scanner": "GenomeAgent Brain Corpus Extractor v1.0",
            "cluster": args.host,
            "project_root": project_root,
            "source_inventory": str(inventory),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "script_extensions": sorted(SCRIPT_SUFFIXES),
            "scripts": collected,
        }

        snapshot_path = run_dir / "project_snapshot_brain.json"
        snapshot_path.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        report.update(
            {
                "extractor": "GenomeAgent Brain Corpus Extractor v1.0",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "host": args.host,
                "project_root": project_root,
                "source_inventory": str(inventory),
                "output_directory": str(run_dir),
                "remote_tar_exit_status": return_code,
                "remote_tar_messages_present": bool(stderr_text.strip()),
                "scripts_collected": len(collected),
                "scripts_missing_after_tar": len(missing_paths),
                "missing_paths": missing_paths,
                "collected_uncompressed_bytes": raw_bytes,
                "collected_text_characters": text_chars,
                "brain_v1_default_character_limit": 900_000,
                "fits_brain_v1_default_limit": text_chars <= 900_000,
            }
        )

        report_path = run_dir / "extraction_report.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Publish successful corpus to the paths expected by brain_v1.py.
        canonical_snapshot = workspace / "project_snapshot_brain.json"
        canonical_tarball = workspace / "scripts_brain.tar.gz"
        atomic_copy(snapshot_path, canonical_snapshot)
        atomic_copy(tarball, canonical_tarball)

        print()
        print("=" * 68)
        print("Brain corpus extraction complete")
        print("=" * 68)
        print(f"Scripts requested  : {len(selected):,}")
        print(f"Scripts collected  : {len(collected):,}")
        print(f"Missing/changed    : {len(missing_paths):,}")
        print(f"Text characters    : {text_chars:,}")
        print(
            "Fits Brain v1 limit: "
            + ("yes" if text_chars <= 900_000 else "no"),
        )
        print(f"Results            : {run_dir}")
        print(f"Brain snapshot     : {canonical_snapshot}")
        print(f"Brain tarball      : {canonical_tarball}")

        if text_chars > 900_000:
            print()
            print(
                "NOTE: The corpus exceeds Brain v1's 900,000-character "
                "default. Do not run Brain v1 unchanged; use chunked "
                "learning or raise the limit only after checking model context."
            )

    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
