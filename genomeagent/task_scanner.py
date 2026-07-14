#!/usr/bin/env python3
"""Reusable, read-only task scanning infrastructure for GenomeAgent."""

from __future__ import annotations

import csv
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class TaskScanError(RuntimeError):
    """Raised when a task scan cannot be collected or written safely."""


class RemoteRunner(Protocol):
    """Interface used by profiles to collect a single remote observation."""

    host: str

    def run_python(self, program: str, timeout_seconds: int = 240) -> dict[str, Any]:
        """Run a read-only Python observation program and return its JSON result."""


class TaskProfile(Protocol):
    """Contract implemented by each scientific task profile."""

    name: str
    title: str

    def collect(
        self,
        runner: RemoteRunner,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Collect raw observations without modifying the remote project."""

    def interpret(
        self,
        data: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Infer deterministic status and the next safe action."""

    def render_report(
        self,
        payload: Mapping[str, Any],
        scan_dir: Path,
    ) -> str:
        """Render the human-readable Markdown report."""

    def write_artifacts(
        self,
        payload: Mapping[str, Any],
        scan_dir: Path,
    ) -> Sequence[Path]:
        """Write task-specific TSV and text artifacts."""


@dataclass(frozen=True)
class TaskScanRun:
    task: str
    scan_dir: Path
    payload: dict[str, Any]
    artifact_paths: tuple[Path, ...]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_json_config(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaskScanError(f"Task configuration does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TaskScanError(f"Invalid JSON task configuration {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise TaskScanError(f"Task configuration must be a JSON object: {path}")
    return loaded


def write_tsv(path: Path, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    """Write stable, spreadsheet-friendly TSV without allowing embedded newlines."""

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column, "")
                if value is None:
                    value = ""
                if isinstance(value, (list, tuple, set)):
                    value = ";".join(str(item) for item in value)
                values.append(str(value).replace("\r", " ").replace("\n", " "))
            writer.writerow(values)


class SSHRemotePythonRunner:
    """Execute one Python observation program through an SSH host alias."""

    def __init__(self, host: str):
        self.host = host

    def run_python(self, program: str, timeout_seconds: int = 240) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", self.host, "python3", "-"],
                input=program,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            progress = exc.stderr or ""
            if isinstance(progress, bytes):
                progress = progress.decode("utf-8", errors="replace")
            progress = str(progress).strip()[-2000:]
            detail = f" Last remote progress:\n{progress}" if progress else ""
            raise TaskScanError(
                f"Remote task scan timed out after {timeout_seconds}s on {self.host}."
                f"{detail}"
            ) from exc
        except OSError as exc:
            raise TaskScanError(f"Could not start SSH for host {self.host}: {exc}") from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()[-4000:]
            raise TaskScanError(
                f"Remote task scan failed on {self.host} with exit code "
                f"{completed.returncode}: {stderr or 'no stderr'}"
            )

        # Login banners can occasionally appear before the JSON observation.
        for line in reversed(completed.stdout.splitlines()):
            candidate = line.strip()
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

        raise TaskScanError(
            "Remote task scanner returned no JSON object. "
            f"Last stdout bytes: {completed.stdout[-1000:]!r}"
        )


class TaskScannerCore:
    """Run a task profile and persist a standard, auditable result bundle."""

    schema_version = "1.0"

    def __init__(
        self,
        runner: RemoteRunner,
        output_root: Path = Path("workspace/task_scans"),
    ):
        self.runner = runner
        self.output_root = output_root

    def run(
        self,
        profile: TaskProfile,
        config: Mapping[str, Any],
        stamp: str | None = None,
    ) -> TaskScanRun:
        scan_stamp = stamp or utc_stamp()
        scan_dir = self.output_root / profile.name / scan_stamp

        if scan_dir.exists():
            raise TaskScanError(f"Refusing to overwrite existing scan directory: {scan_dir}")

        observation = profile.collect(self.runner, config)
        status = profile.interpret(observation, config)

        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "task": profile.name,
            "title": profile.title,
            "mode": "read_only",
            "host": self.runner.host,
            "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "configuration": dict(config),
            "observation": observation,
            "status_summary": status,
        }

        scan_dir.mkdir(parents=True, exist_ok=False)
        task_scan_path = scan_dir / "task_scan.json"
        task_scan_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        task_paths = list(profile.write_artifacts(payload, scan_dir))
        report_path = scan_dir / "report.md"
        report_path.write_text(profile.render_report(payload, scan_dir), encoding="utf-8")

        artifact_paths = tuple([report_path, task_scan_path, *task_paths])
        return TaskScanRun(
            task=profile.name,
            scan_dir=scan_dir,
            payload=payload,
            artifact_paths=artifact_paths,
        )
