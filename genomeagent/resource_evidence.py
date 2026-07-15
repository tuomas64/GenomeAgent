#!/usr/bin/env python3
"""Deterministic Slurm resource evidence and learning core.

The collector is deliberately read-only on the remote system.  It records bounded
``sacct`` observations locally; the reducer then rebuilds profiles, anomaly flags
and proposal-only recommendations from those immutable evidence snapshots.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from genomeagent.task_state import SUPPORTED_TASKS, TaskStateError, discover_scan_bundles


TERMINAL_STATES = {
    "BOOT_FAIL",
    "CANCELLED",
    "COMPLETED",
    "DEADLINE",
    "FAILED",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "REVOKED",
    "TIMEOUT",
}
SUCCESS_STATES = {"COMPLETED"}
FAILED_STATES = {"BOOT_FAIL", "DEADLINE", "FAILED", "NODE_FAIL", "REVOKED"}
SACCT_FIELDS = (
    "JobID",
    "JobName",
    "Partition",
    "State",
    "ElapsedRaw",
    "Timelimit",
    "AllocCPUS",
    "TotalCPU",
    "CPUTimeRAW",
    "ReqMem",
    "MaxRSS",
    "NodeList",
    "ExitCode",
)
RESOURCE_POLICY_VERSION = "1.2"


class ResourceEvidenceError(RuntimeError):
    """Raised when resource evidence is incomplete, unsafe or inconsistent."""


@dataclass(frozen=True)
class ResourceCollectionResult:
    task: str
    evidence_path: Path
    observations: int
    terminal_observations: int


@dataclass(frozen=True)
class ResourceDiscoveryPlan:
    task: str
    source_scans: int
    terminal_attempts_seen: int
    already_evidenced: int
    ignored_non_array_records: int
    selected: tuple[dict[str, Any], ...]
    remaining: int


@dataclass(frozen=True)
class ResourceAutoCollectionResult:
    plan: ResourceDiscoveryPlan
    collections: tuple[ResourceCollectionResult, ...]


@dataclass(frozen=True)
class ResourceIngestResult:
    task: str
    resource_dir: Path
    source_snapshots: int
    observations: int
    current_attempts: int
    profiles: int
    anomalies: int
    recommendations: int
    artifact_paths: tuple[Path, ...]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _tsv_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _tsv(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> str:
    lines = ["\t".join(fields)]
    for row in rows:
        lines.append("\t".join(_tsv_value(row.get(field)) for field in fields))
    return "\n".join(lines) + "\n"


def _validate_task(task: str) -> str:
    value = str(task).strip()
    if value not in SUPPORTED_TASKS or not re.fullmatch(r"[a-z0-9_]+", value):
        raise ResourceEvidenceError(
            "Unsupported task {!r}; expected one of {}".format(
                task, sorted(SUPPORTED_TASKS)
            )
        )
    return value


def _normalise_state(value: Any) -> str:
    state = str(value or "").strip().upper().rstrip("+")
    if state.startswith("OUT_OF_ME"):
        return "OUT_OF_MEMORY"
    for terminal in sorted(TERMINAL_STATES, key=len, reverse=True):
        if state.startswith(terminal):
            return terminal
    return state


def _parse_int(value: Any, field: str, allow_empty: bool = True) -> Optional[int]:
    text = str(value or "").strip()
    if not text and allow_empty:
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ResourceEvidenceError("Invalid integer for {}: {!r}".format(field, value)) from exc
    if parsed < 0:
        raise ResourceEvidenceError("Negative value for {}: {!r}".format(field, value))
    return parsed


def parse_slurm_duration(value: Any) -> Optional[float]:
    """Parse Slurm durations such as ``1-12:00:18`` or ``09:12.435``."""

    text = str(value or "").strip()
    if not text or text in {"Unknown", "UNLIMITED", "Partition_Limit"}:
        return None
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError as exc:
            raise ResourceEvidenceError("Invalid Slurm duration: {!r}".format(value)) from exc
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
        elif len(parts) == 2:
            hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
        elif len(parts) == 1:
            hours, minutes, seconds = 0, 0, float(parts[0])
        else:
            raise ValueError
    except ValueError as exc:
        raise ResourceEvidenceError("Invalid Slurm duration: {!r}".format(value)) from exc
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total < 0:
        raise ResourceEvidenceError("Negative Slurm duration: {!r}".format(value))
    return total


_MEMORY_UNITS = {
    "": 1,
    "K": 1024,
    "M": 1024 ** 2,
    "G": 1024 ** 3,
    "T": 1024 ** 4,
    "P": 1024 ** 5,
}


def parse_slurm_memory(value: Any) -> Optional[int]:
    """Parse Slurm memory values, including per-node/per-CPU suffixes."""

    text = str(value or "").strip()
    if not text or text in {"Unknown", "N/A"}:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTP]?)([cn]?)", text, re.I)
    if not match:
        raise ResourceEvidenceError("Invalid Slurm memory value: {!r}".format(value))
    amount = float(match.group(1))
    unit = match.group(2).upper()
    return int(amount * _MEMORY_UNITS[unit])


def _requested_memory_bytes(value: Any, allocated_cpus: Optional[int]) -> Optional[int]:
    """Convert ReqMem to total requested bytes when Slurm marks it per CPU."""

    parsed = parse_slurm_memory(value)
    text = str(value or "").strip().lower()
    if parsed is not None and text.endswith("c") and allocated_cpus:
        return parsed * allocated_cpus
    return parsed


def _parse_sacct_rows(stdout: str) -> list[dict[str, str]]:
    rows = []
    for line_number, raw in enumerate(stdout.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        values = line.split("|")
        if values and values[-1] == "":
            values.pop()
        if len(values) != len(SACCT_FIELDS):
            raise ResourceEvidenceError(
                "Unexpected sacct field count on line {}: expected {}, found {}".format(
                    line_number, len(SACCT_FIELDS), len(values)
                )
            )
        rows.append(dict(zip(SACCT_FIELDS, values)))
    return rows


def _base_job_id(job_id: str) -> str:
    return str(job_id).split(".", 1)[0]


def _attempt_map_from_scans(scan_root: Path, task: str) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    try:
        bundles = discover_scan_bundles(scan_root, task)
    except TaskStateError:
        return mapping
    for bundle in bundles:
        status = bundle.payload.get("status_summary", {})
        if not isinstance(status, Mapping):
            continue
        for row in status.get("intervals", []) or []:
            if not isinstance(row, Mapping):
                continue
            attempt = str(row.get("scheduler_job_id") or "").strip()
            if not attempt:
                continue
            mapping[attempt] = {
                "unit_id": str(row.get("task") or ""),
                "workload": {
                    "batch": row.get("batch", ""),
                    "chromosome": row.get("chromosome", ""),
                    "interval": row.get("interval", ""),
                },
                "source_scan_id": bundle.scan_id,
                "source_task_scan_sha256": bundle.digest,
            }
    return mapping


def _attempt_sort_key(attempt_id: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"([0-9]+)(?:_([0-9]+))?", str(attempt_id))
    if not match:
        return (math.inf, math.inf, str(attempt_id))
    return (
        int(match.group(1)),
        int(match.group(2)) if match.group(2) is not None else -1,
        str(attempt_id),
    )


def _resource_profile_key(task: str, job_name: str) -> str:
    lowered = str(job_name or "").lower()
    if task == "scattered_joint_calling":
        return "scattered_genotypegvcfs_250kb"
    if "dedup_residual" in lowered:
        return "gam_dedup_residual_qc"
    if "dedup_fast" in lowered:
        return "gam_dedup_fast"
    return "gam_deduplication_other"


def _terminal_attempts_in_evidence(evidence_root: Path, task: str) -> set[str]:
    terminal = set()
    for path in sorted((Path(evidence_root) / task).glob("*.json")):
        payload, _ = _read_snapshot(path, task)
        for raw in payload["observations"]:
            if not isinstance(raw, Mapping):
                raise ResourceEvidenceError(
                    "Non-object observation in {}".format(path)
                )
            state = _normalise_state(raw.get("state"))
            if state in TERMINAL_STATES:
                attempt_id = str(raw.get("attempt_id") or "").strip()
                if attempt_id:
                    terminal.add(attempt_id)
    return terminal


def _scatter_batch_map(configuration: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result = {}
    for raw in configuration.get("scatter_batches", []) or []:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("job_name") or "").strip().lower()
        if name:
            result[name] = raw
    return result


def _scatter_workload(
    status: Mapping[str, Any],
    interval_task: int,
    batch: Mapping[str, Any],
) -> dict[str, Any]:
    for row in status.get("intervals", []) or []:
        if not isinstance(row, Mapping):
            continue
        try:
            task_number = int(row.get("task"))
        except (TypeError, ValueError):
            continue
        if task_number == interval_task:
            return {
                "batch": row.get("batch") or batch.get("name", ""),
                "chromosome": row.get("chromosome", ""),
                "interval": row.get("interval", ""),
            }
    return {
        "batch": batch.get("name", ""),
        "chromosome": "",
        "interval": "",
    }


def discover_new_terminal_attempts(
    scan_root: Path,
    evidence_root: Path,
    task: str,
    maximum_attempts: int = 20,
) -> ResourceDiscoveryPlan:
    """Find terminal array attempts in scan history that lack terminal evidence."""

    task = _validate_task(task)
    if maximum_attempts < 1 or maximum_attempts > 20:
        raise ResourceEvidenceError(
            "Automatic discovery limit must be between 1 and 20 attempts."
        )
    try:
        bundles = discover_scan_bundles(Path(scan_root), task)
    except TaskStateError as exc:
        raise ResourceEvidenceError(str(exc)) from exc
    evidenced = _terminal_attempts_in_evidence(Path(evidence_root), task)
    terminal_candidates: dict[str, dict[str, Any]] = {}
    ignored_non_array = set()

    for bundle in bundles:
        payload = bundle.payload
        observation = payload.get("observation", {})
        status = payload.get("status_summary", {})
        configuration = payload.get("configuration", {})
        if not isinstance(observation, Mapping) or not isinstance(status, Mapping):
            continue
        if not isinstance(configuration, Mapping):
            configuration = {}
        jobs = observation.get("jobs", {})
        if not isinstance(jobs, Mapping):
            continue
        batch_by_name = _scatter_batch_map(configuration)
        for row in jobs.get("recent", []) or []:
            if not isinstance(row, Mapping):
                continue
            state = _normalise_state(row.get("state"))
            if state not in TERMINAL_STATES:
                continue
            attempt_id = str(row.get("job_id") or row.get("job_id_raw") or "").strip()
            if not re.fullmatch(r"[0-9]+_[0-9]+", attempt_id):
                if re.fullmatch(r"[0-9]+", attempt_id):
                    ignored_non_array.add(attempt_id)
                continue
            job_name = str(row.get("name") or "").strip()
            unit_id = ""
            workload: dict[str, Any] = {}
            if task == "scattered_joint_calling":
                batch = batch_by_name.get(job_name.lower())
                if batch is None:
                    continue
                try:
                    array_task = int(
                        row.get("array_task_id")
                        if row.get("array_task_id") is not None
                        else attempt_id.rsplit("_", 1)[1]
                    )
                    start = int(batch.get("array_start"))
                    end = int(batch.get("array_end"))
                    offset = int(batch.get("offset"))
                except (TypeError, ValueError):
                    continue
                if not start <= array_task <= end:
                    continue
                interval_task = array_task + offset
                unit_id = str(interval_task)
                workload = _scatter_workload(status, interval_task, batch)
            terminal_candidates[attempt_id] = {
                "attempt_id": attempt_id,
                "state": state,
                "job_name": job_name,
                "profile_key": _resource_profile_key(task, job_name),
                "unit_id": unit_id,
                "workload": workload,
                "source_scan_id": bundle.scan_id,
                "source_task_scan_sha256": bundle.digest,
                "observed_at_utc": bundle.created_at_utc,
            }

    pending = [
        item for attempt_id, item in terminal_candidates.items()
        if attempt_id not in evidenced
    ]
    pending.sort(key=lambda item: (
        str(item.get("observed_at_utc") or ""),
        _attempt_sort_key(str(item["attempt_id"])),
    ))
    selected = tuple(pending[:maximum_attempts])
    return ResourceDiscoveryPlan(
        task=task,
        source_scans=len(bundles),
        terminal_attempts_seen=len(terminal_candidates),
        already_evidenced=len(set(terminal_candidates) & evidenced),
        ignored_non_array_records=len(ignored_non_array),
        selected=selected,
        remaining=max(0, len(pending) - len(selected)),
    )


def _combine_sacct_rows(
    rows: Sequence[Mapping[str, str]],
    task: str,
    profile_key: Optional[str],
    attempt_map: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    roots: dict[str, Mapping[str, str]] = {}
    steps: dict[str, dict[str, Mapping[str, str]]] = {}
    for row in rows:
        job_id = str(row.get("JobID") or "").strip()
        if not job_id:
            continue
        base = _base_job_id(job_id)
        if "." in job_id:
            step = job_id.split(".", 1)[1]
            steps.setdefault(base, {})[step] = row
        else:
            roots[base] = row

    observations = []
    for attempt_id in sorted(roots):
        root = roots[attempt_id]
        batch = steps.get(attempt_id, {}).get("batch", {})
        mapped = dict(attempt_map.get(attempt_id, {}))
        elapsed = _parse_int(root.get("ElapsedRaw"), "ElapsedRaw")
        if elapsed is None:
            elapsed_value = parse_slurm_duration(root.get("Elapsed"))
            elapsed = int(elapsed_value) if elapsed_value is not None else None
        total_cpu = parse_slurm_duration(
            root.get("TotalCPU") or batch.get("TotalCPU")
        )
        allocated_cpus = _parse_int(root.get("AllocCPUS"), "AllocCPUS")
        max_rss = parse_slurm_memory(batch.get("MaxRSS") or root.get("MaxRSS"))
        requested_memory = _requested_memory_bytes(root.get("ReqMem"), allocated_cpus)
        cpu_efficiency = None
        if total_cpu is not None and elapsed and allocated_cpus:
            cpu_efficiency = total_cpu / (elapsed * allocated_cpus)
        memory_efficiency = None
        if max_rss is not None and requested_memory:
            memory_efficiency = max_rss / requested_memory
        state = _normalise_state(root.get("State"))
        attempt_profile_key = str(
            mapped.get("profile_key") or profile_key or ""
        ).strip()
        if not attempt_profile_key:
            raise ResourceEvidenceError(
                "No resource profile key is available for attempt {}.".format(
                    attempt_id
                )
            )
        observation = {
            "schema_version": "1.0",
            "task": task,
            "profile_key": attempt_profile_key,
            "unit_id": str(mapped.get("unit_id") or ""),
            "attempt_id": attempt_id,
            "job_id": attempt_id,
            "job_name": str(root.get("JobName") or ""),
            "partition": str(root.get("Partition") or ""),
            "state": state,
            "terminal": state in TERMINAL_STATES,
            "successful": state in SUCCESS_STATES,
            "elapsed_seconds": elapsed,
            "time_limit_seconds": parse_slurm_duration(root.get("Timelimit")),
            "allocated_cpus": allocated_cpus,
            "total_cpu_seconds": total_cpu,
            "cpu_time_raw_seconds": _parse_int(root.get("CPUTimeRAW"), "CPUTimeRAW"),
            "cpu_efficiency": cpu_efficiency,
            "requested_memory_bytes": requested_memory,
            "max_rss_bytes": max_rss,
            "memory_efficiency": memory_efficiency,
            "node": str(root.get("NodeList") or batch.get("NodeList") or ""),
            "exit_code": str(root.get("ExitCode") or batch.get("ExitCode") or ""),
            "workload": dict(mapped.get("workload") or {}),
            "task_scan_link": {
                "source_scan_id": mapped.get("source_scan_id", ""),
                "source_task_scan_sha256": mapped.get(
                    "source_task_scan_sha256", ""
                ),
            },
            "evidence_quality": {
                "has_parent_record": True,
                "has_batch_step": bool(batch),
                "has_elapsed": elapsed is not None,
                "has_peak_memory": max_rss is not None,
                "has_total_cpu": total_cpu is not None,
            },
        }
        observations.append(observation)
    return observations


def _default_runner(command: Sequence[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class ResourceCollector:
    """Collect a bounded read-only scheduler-accounting snapshot over SSH."""

    schema_version = "1.0"

    def __init__(
        self,
        evidence_root: Path = Path("workspace/task_resource_evidence"),
        scan_root: Path = Path("workspace/task_scans"),
        runner: Callable[[Sequence[str], int], subprocess.CompletedProcess] = _default_runner,
    ):
        self.evidence_root = Path(evidence_root)
        self.scan_root = Path(scan_root)
        self.runner = runner

    def collect(
        self,
        task: str,
        job_ids: Sequence[str],
        profile_key: str,
        host: str = "puhti",
        timeout: int = 60,
        unit_overrides: Optional[Mapping[str, str]] = None,
        attempt_overrides: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> ResourceCollectionResult:
        task = _validate_task(task)
        clean_job_ids = [str(value).strip() for value in job_ids]
        if not clean_job_ids or len(clean_job_ids) > 20:
            raise ResourceEvidenceError("Provide between 1 and 20 Slurm job IDs.")
        if any(not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", value) for value in clean_job_ids):
            raise ResourceEvidenceError("Slurm job IDs may contain only digits and one array suffix.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
            raise ResourceEvidenceError("Unsafe SSH host value: {!r}".format(host))
        profile_key = str(profile_key).strip()
        if not profile_key or not re.fullmatch(r"[A-Za-z0-9._:/-]+", profile_key):
            raise ResourceEvidenceError("Unsafe or empty profile key: {!r}".format(profile_key))
        if timeout < 5 or timeout > 240:
            raise ResourceEvidenceError("Collection timeout must be between 5 and 240 seconds.")

        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            host,
            "sacct",
            "-j",
            ",".join(clean_job_ids),
            "-n",
            "-P",
            "--units=K",
            "--format=" + ",".join(SACCT_FIELDS),
        ]
        try:
            completed = self.runner(command, timeout)
        except subprocess.TimeoutExpired as exc:
            raise ResourceEvidenceError(
                "Read-only sacct collection timed out after {} seconds on {}.".format(
                    timeout, host
                )
            ) from exc
        if completed.returncode != 0:
            stderr = str(completed.stderr or "").strip()[-1000:]
            raise ResourceEvidenceError(
                "Read-only sacct collection failed on {}: {}".format(
                    host, stderr or "unknown error"
                )
            )
        rows = _parse_sacct_rows(str(completed.stdout or ""))
        if not rows:
            raise ResourceEvidenceError("sacct returned no records for the requested jobs.")
        attempt_map = _attempt_map_from_scans(self.scan_root, task)
        for attempt_id, metadata in dict(attempt_overrides or {}).items():
            if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", str(attempt_id)):
                raise ResourceEvidenceError(
                    "Unsafe attempt override ID: {!r}".format(attempt_id)
                )
            if not isinstance(metadata, Mapping):
                raise ResourceEvidenceError(
                    "Attempt override {} must be a mapping.".format(attempt_id)
                )
            existing = dict(attempt_map.get(str(attempt_id), {}))
            existing.update(dict(metadata))
            attempt_map[str(attempt_id)] = existing
        for attempt_id, unit_id in dict(unit_overrides or {}).items():
            if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", str(attempt_id)):
                raise ResourceEvidenceError(
                    "Unsafe unit override attempt ID: {!r}".format(attempt_id)
                )
            existing = dict(attempt_map.get(str(attempt_id), {}))
            existing["unit_id"] = str(unit_id)
            attempt_map[str(attempt_id)] = existing
        observations = _combine_sacct_rows(rows, task, profile_key, attempt_map)
        if not observations:
            raise ResourceEvidenceError("sacct returned no parent job or array-task records.")

        now = datetime.now(timezone.utc)
        observed_at = now.isoformat().replace("+00:00", "Z")
        snapshot = {
            "schema_version": self.schema_version,
            "task": task,
            "source_mode": "read_only_scheduler_accounting",
            "observed_at_utc": observed_at,
            "host": host,
            "query": {
                "job_ids": clean_job_ids,
                "maximum_job_ids": 20,
                "timeout_seconds": timeout,
                "sacct_fields": list(SACCT_FIELDS),
                "units": "K",
            },
            "safety": {
                "remote_writes": False,
                "job_submission": False,
                "job_cancellation": False,
                "resource_changes": False,
            },
            "observations": observations,
        }
        evidence_id = now.strftime("%Y%m%dT%H%M%S%fZ")
        evidence_path = self.evidence_root / task / (evidence_id + ".json")
        if evidence_path.exists():
            raise ResourceEvidenceError("Evidence snapshot already exists: {}".format(evidence_path))
        _atomic_write(evidence_path, _pretty_json(snapshot))
        return ResourceCollectionResult(
            task=task,
            evidence_path=evidence_path,
            observations=len(observations),
            terminal_observations=sum(
                1 for item in observations if item.get("terminal")
            ),
        )

    def discover_new(
        self,
        task: str,
        maximum_attempts: int = 20,
    ) -> ResourceDiscoveryPlan:
        """Build a local-only plan for newly terminal scanner-observed attempts."""

        return discover_new_terminal_attempts(
            self.scan_root,
            self.evidence_root,
            task,
            maximum_attempts=maximum_attempts,
        )

    def collect_new(
        self,
        task: str,
        host: str = "puhti",
        timeout: int = 60,
        maximum_attempts: int = 20,
    ) -> ResourceAutoCollectionResult:
        """Collect only newly terminal attempts discovered in Task Scanner history."""

        plan = self.discover_new(task, maximum_attempts=maximum_attempts)
        if not plan.selected:
            return ResourceAutoCollectionResult(plan=plan, collections=())

        by_profile: dict[str, list[dict[str, Any]]] = {}
        for item in plan.selected:
            by_profile.setdefault(str(item["profile_key"]), []).append(item)
        collections = []
        for profile_key in sorted(by_profile):
            candidates = by_profile[profile_key]
            unit_overrides = {
                str(item["attempt_id"]): str(item["unit_id"])
                for item in candidates
                if item.get("unit_id")
            }
            collections.append(self.collect(
                task,
                [str(item["attempt_id"]) for item in candidates],
                profile_key,
                host=host,
                timeout=timeout,
                unit_overrides=unit_overrides,
                attempt_overrides={
                    str(item["attempt_id"]): item for item in candidates
                },
            ))
        return ResourceAutoCollectionResult(
            plan=plan,
            collections=tuple(collections),
        )


def _read_snapshot(path: Path, task: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ResourceEvidenceError("Evidence snapshot does not exist: {}".format(path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResourceEvidenceError("Invalid evidence JSON {}: {}".format(path, exc)) from exc
    if not isinstance(payload, dict):
        raise ResourceEvidenceError("Evidence snapshot must be a JSON object: {}".format(path))
    if payload.get("task") != task:
        raise ResourceEvidenceError(
            "Task mismatch in {}: expected {}, found {}".format(
                path, task, payload.get("task")
            )
        )
    if payload.get("source_mode") != "read_only_scheduler_accounting":
        raise ResourceEvidenceError(
            "Refusing evidence without read-only source mode: {}".format(path)
        )
    observations = payload.get("observations")
    if not isinstance(observations, list):
        raise ResourceEvidenceError("Evidence has no observations list: {}".format(path))
    return payload, _sha256(raw)


def _number(value: Any, field: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResourceEvidenceError("{} must be numeric or null".format(field))
    if value < 0 or not math.isfinite(float(value)):
        raise ResourceEvidenceError("{} must be finite and non-negative".format(field))
    return float(value)


def _normalise_observation(
    raw: Mapping[str, Any],
    task: str,
    source_id: str,
    source_digest: str,
    observed_at: str,
    source_host: str,
) -> dict[str, Any]:
    if raw.get("task") != task:
        raise ResourceEvidenceError("Observation task does not match snapshot task.")
    attempt_id = str(raw.get("attempt_id") or "").strip()
    profile_key = str(raw.get("profile_key") or "").strip()
    if not attempt_id or not profile_key:
        raise ResourceEvidenceError("Every observation requires attempt_id and profile_key.")
    state = _normalise_state(raw.get("state"))
    elapsed = _number(raw.get("elapsed_seconds"), "elapsed_seconds")
    cpus = _number(raw.get("allocated_cpus"), "allocated_cpus")
    total_cpu = _number(raw.get("total_cpu_seconds"), "total_cpu_seconds")
    requested = _number(raw.get("requested_memory_bytes"), "requested_memory_bytes")
    max_rss = _number(raw.get("max_rss_bytes"), "max_rss_bytes")
    cpu_efficiency = None
    if elapsed and cpus and total_cpu is not None:
        cpu_efficiency = total_cpu / (elapsed * cpus)
    memory_efficiency = None
    if requested and max_rss is not None:
        memory_efficiency = max_rss / requested
    normalized = dict(raw)
    normalized.update({
        "schema_version": "1.0",
        "task": task,
        "source_evidence_id": source_id,
        "source_evidence_sha256": source_digest,
        "source_host": str(source_host or "unknown"),
        "observed_at_utc": observed_at,
        "attempt_id": attempt_id,
        "profile_key": profile_key,
        "unit_id": str(raw.get("unit_id") or ""),
        "state": state,
        "terminal": state in TERMINAL_STATES,
        "successful": state in SUCCESS_STATES,
        "elapsed_seconds": elapsed,
        "allocated_cpus": cpus,
        "total_cpu_seconds": total_cpu,
        "requested_memory_bytes": requested,
        "max_rss_bytes": max_rss,
        "cpu_efficiency": cpu_efficiency,
        "memory_efficiency": memory_efficiency,
    })
    normalized["observation_id"] = _sha256(_canonical_bytes(normalized))
    return normalized


def _nearest_rank(values: Iterable[float], percentile: float) -> Optional[float]:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _summary(values: Iterable[float]) -> dict[str, Optional[float]]:
    present = [float(value) for value in values if value is not None]
    return {
        "n": len(present),
        "min": min(present) if present else None,
        "p50": _nearest_rank(present, 0.50),
        "p90": _nearest_rank(present, 0.90),
        "p95": _nearest_rank(present, 0.95),
        "max": max(present) if present else None,
    }


def _confidence(successes: int) -> str:
    if successes < 3:
        return "insufficient"
    if successes < 5:
        return "low"
    if successes < 20:
        return "medium"
    return "high"


def _workload_coverage(
    profile_key: str,
    successes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    batches = set()
    chromosomes = set()
    described = 0
    for item in successes:
        workload = item.get("workload", {})
        if not isinstance(workload, Mapping):
            continue
        batch = str(workload.get("batch") or "").strip()
        chromosome = str(workload.get("chromosome") or "").strip()
        interval = str(workload.get("interval") or "").strip()
        if batch or chromosome or interval:
            described += 1
        if batch:
            batches.add(batch)
        if chromosome:
            chromosomes.add(chromosome)

    diversity_required = profile_key == "scattered_genotypegvcfs_250kb"
    limitations = []
    if diversity_required:
        if len(batches) < 3:
            limitations.append("fewer_than_3_scatter_batches")
        if len(chromosomes) < 3:
            limitations.append("fewer_than_3_chromosomes")
        if described < len(successes):
            limitations.append("missing_workload_descriptors")
    return {
        "diversity_required": diversity_required,
        "successful_attempts_with_workload": described,
        "unique_batches": len(batches),
        "batches": sorted(batches),
        "unique_chromosomes": len(chromosomes),
        "chromosomes": sorted(chromosomes),
        "sufficient_for_resource_proposal": not limitations,
        "limitations": limitations,
    }


def _diversity_aware_confidence(
    sample_size_confidence: str,
    coverage: Mapping[str, Any],
) -> tuple[str, list[str]]:
    limitations = list(coverage.get("limitations", []))
    if not limitations:
        return sample_size_confidence, []
    if sample_size_confidence == "insufficient":
        return "insufficient", limitations
    if int(coverage.get("unique_batches", 0)) < 2:
        return "low", limitations
    if int(coverage.get("unique_chromosomes", 0)) < 2:
        return "low", limitations
    if sample_size_confidence == "high":
        return "medium", limitations
    return sample_size_confidence, limitations


def _profile_recommendation(
    successes: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    if len(successes) < 3:
        return {
            "status": "insufficient_evidence",
            "minimum_successes_required": 3,
            "successful_observations_available": len(successes),
            "memory_bytes": None,
            "time_limit_seconds": None,
        }
    if not coverage.get("sufficient_for_resource_proposal", True):
        return {
            "status": "insufficient_workload_diversity",
            "successful_observations_available": len(successes),
            "coverage_limitations": list(coverage.get("limitations", [])),
            "memory_bytes": None,
            "time_limit_seconds": None,
            "automatic_application_allowed": False,
        }
    memory_p95 = _nearest_rank(
        [item.get("max_rss_bytes") for item in successes if item.get("max_rss_bytes") is not None],
        0.95,
    )
    elapsed_p95 = _nearest_rank(
        [item.get("elapsed_seconds") for item in successes if item.get("elapsed_seconds") is not None],
        0.95,
    )
    gib = 1024 ** 3
    memory = None
    if memory_p95 is not None:
        memory = int(math.ceil((memory_p95 * 1.25) / gib) * gib)
    time_limit = None
    if elapsed_p95 is not None:
        time_limit = int(math.ceil((elapsed_p95 * 1.30) / 3600) * 3600)
    return {
        "status": "proposal_available_for_researcher_review",
        "basis": "nearest-rank successful-attempt p95 with fixed safety margins",
        "memory_margin": 1.25,
        "runtime_margin": 1.30,
        "memory_bytes": memory,
        "time_limit_seconds": time_limit,
        "automatic_application_allowed": False,
    }


def _build_profiles(current_attempts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in current_attempts:
        grouped.setdefault((
            str(item.get("source_host") or "unknown"),
            str(item["profile_key"]),
        ), []).append(item)
    profiles = []
    for source_host, key in sorted(grouped):
        attempts = grouped[(source_host, key)]
        successes = [item for item in attempts if item.get("state") in SUCCESS_STATES]
        sample_size_confidence = _confidence(len(successes))
        workload_coverage = _workload_coverage(key, successes)
        confidence, confidence_limitations = _diversity_aware_confidence(
            sample_size_confidence,
            workload_coverage,
        )
        states: dict[str, int] = {}
        for item in attempts:
            state = str(item.get("state") or "UNKNOWN")
            states[state] = states.get(state, 0) + 1
        profiles.append({
            "source_host": source_host,
            "profile_key": key,
            "confidence": confidence,
            "sample_size_confidence": sample_size_confidence,
            "confidence_limitations": confidence_limitations,
            "workload_coverage": workload_coverage,
            "observation_counts": {
                "attempts": len(attempts),
                "terminal": sum(bool(item.get("terminal")) for item in attempts),
                "successful": len(successes),
                "timeout": states.get("TIMEOUT", 0),
                "out_of_memory": states.get("OUT_OF_MEMORY", 0),
                "failed": sum(states.get(value, 0) for value in FAILED_STATES),
                "cancelled": states.get("CANCELLED", 0),
                "states": states,
            },
            "successful_attempt_statistics": {
                "elapsed_seconds": _summary(
                    item.get("elapsed_seconds") for item in successes
                ),
                "max_rss_bytes": _summary(
                    item.get("max_rss_bytes") for item in successes
                ),
                "cpu_efficiency": _summary(
                    item.get("cpu_efficiency") for item in successes
                ),
                "memory_efficiency": _summary(
                    item.get("memory_efficiency") for item in successes
                ),
            },
            "censored_evidence": {
                "timeout_attempts_are_runtime_lower_bounds": states.get("TIMEOUT", 0),
                "oom_attempts_are_memory_lower_bounds": states.get("OUT_OF_MEMORY", 0),
            },
            "proposed_resources": _profile_recommendation(
                successes, workload_coverage
            ),
        })
    return profiles


def _build_anomalies(current_attempts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    anomalies = []
    cpu_by_profile: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in current_attempts:
        if item.get("state") == "COMPLETED" and item.get("cpu_efficiency") is not None:
            cpu_by_profile.setdefault((
                str(item.get("source_host") or "unknown"),
                str(item["profile_key"]),
            ), []).append(item)
    for source_host, profile_key in sorted(cpu_by_profile):
        measured = cpu_by_profile[(source_host, profile_key)]
        low = [item for item in measured if float(item["cpu_efficiency"]) < 0.20]
        low_fraction = len(low) / len(measured)
        if len(low) >= 3 and low_fraction >= 0.50:
            example_ids = sorted(
                (str(item["attempt_id"]) for item in low),
                key=_attempt_sort_key,
            )[:20]
            anomalies.append({
                "anomaly_id": "systematic_low_cpu_efficiency:{}:{}".format(
                    source_host, profile_key
                ),
                "type": "systematic_low_cpu_efficiency_pattern",
                "severity": "review",
                "source_host": source_host,
                "profile_key": profile_key,
                "unit_id": "",
                "attempt_ids": example_ids,
                "evidence": {
                    "measured_successful_attempts": len(measured),
                    "low_efficiency_attempts": len(low),
                    "low_efficiency_fraction": low_fraction,
                    "threshold": 0.20,
                    "cpu_efficiency_p50": _nearest_rank(
                        [float(item["cpu_efficiency"]) for item in measured], 0.50
                    ),
                    "cpu_efficiency_p95": _nearest_rank(
                        [float(item["cpu_efficiency"]) for item in measured], 0.95
                    ),
                    "example_attempt_ids_truncated": len(low) > len(example_ids),
                },
                "interpretation_boundary": (
                    "Low allocated-CPU efficiency is widespread in this profile and is "
                    "therefore represented as one cohort pattern. It may reflect storage or "
                    "database waiting, scheduler accounting limitations, or workload behavior; "
                    "it does not by itself justify changing CPU allocation."
                ),
            })
        else:
            for item in low:
                anomalies.append({
                    "anomaly_id": "low_cpu_efficiency:{}:{}".format(
                        source_host, item["attempt_id"]
                    ),
                    "type": "low_cpu_efficiency_possible_waiting",
                    "severity": "review",
                    "source_host": source_host,
                    "profile_key": item["profile_key"],
                    "unit_id": item.get("unit_id", ""),
                    "attempt_ids": [item["attempt_id"]],
                    "evidence": {
                        "cpu_efficiency": float(item["cpu_efficiency"])
                    },
                    "interpretation_boundary": (
                        "Low allocated-CPU efficiency may reflect storage or database waiting, "
                        "scheduler accounting limitations, or workload behavior; it does not by "
                        "itself justify changing CPU allocation."
                    ),
                })

    by_unit: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for item in current_attempts:
        unit_id = str(item.get("unit_id") or "")
        if unit_id:
            by_unit.setdefault((
                str(item.get("source_host") or "unknown"),
                str(item["profile_key"]),
                unit_id,
            ), []).append(item)
    for (source_host, profile_key, unit_id), attempts in sorted(by_unit.items()):
        timeouts = [item for item in attempts if item.get("state") == "TIMEOUT"]
        successes = [item for item in attempts if item.get("state") == "COMPLETED"]
        if not timeouts or not successes:
            continue
        timeout_attempt = max(
            timeouts, key=lambda item: float(item.get("elapsed_seconds") or 0)
        )
        success_attempt = min(
            successes, key=lambda item: float(item.get("elapsed_seconds") or math.inf)
        )
        timeout_elapsed = float(timeout_attempt.get("elapsed_seconds") or 0)
        success_elapsed = float(success_attempt.get("elapsed_seconds") or 0)
        if not success_elapsed or timeout_elapsed / success_elapsed < 3:
            continue
        timeout_rss = timeout_attempt.get("max_rss_bytes")
        success_rss = success_attempt.get("max_rss_bytes")
        rss_ratio = None
        if timeout_rss and success_rss:
            rss_ratio = max(float(timeout_rss), float(success_rss)) / min(
                float(timeout_rss), float(success_rss)
            )
        anomaly_type = "runtime_instability_across_attempts"
        if rss_ratio is not None and rss_ratio <= 1.25:
            anomaly_type = "probable_transient_runtime_stall"
        anomalies.append({
            "anomaly_id": "runtime_variation:{}:{}:{}".format(
                source_host, profile_key, unit_id
            ),
            "type": anomaly_type,
            "severity": "high_review",
            "source_host": source_host,
            "profile_key": profile_key,
            "unit_id": unit_id,
            "attempt_ids": [timeout_attempt["attempt_id"], success_attempt["attempt_id"]],
            "evidence": {
                "timeout_elapsed_seconds": timeout_elapsed,
                "successful_elapsed_seconds": success_elapsed,
                "elapsed_ratio": timeout_elapsed / success_elapsed,
                "peak_memory_ratio": rss_ratio,
                "timeout_node": timeout_attempt.get("node", ""),
                "successful_node": success_attempt.get("node", ""),
            },
            "interpretation_boundary": (
                "The same unit completed much faster on a later attempt. Treat the timeout as "
                "censored runtime evidence and investigate node, storage, database and accounting "
                "conditions before changing the resource profile."
            ),
        })
    return sorted(anomalies, key=lambda item: item["anomaly_id"])


def _recommendation(
    identifier: str,
    priority: str,
    action: str,
    reason: str,
    scope: Optional[Mapping[str, Any]] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    approval: bool = False,
) -> dict[str, Any]:
    return {
        "recommendation_id": identifier,
        "priority": priority,
        "action": action,
        "reason": reason,
        "scope": dict(scope or {}),
        "evidence": dict(evidence or {}),
        "requires_researcher_approval": bool(approval),
        "execution_enabled": False,
    }


def _build_recommendations(
    profiles: Sequence[Mapping[str, Any]],
    anomalies: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    recommendations = []
    if anomalies:
        recommendations.append(_recommendation(
            "review_resource_anomalies",
            "high",
            "review_resource_anomalies",
            "Resource evidence contains deterministic attempt-level or cohort-level anomaly flags.",
            scope={"anomaly_ids": [item["anomaly_id"] for item in anomalies]},
            evidence={"anomaly_count": len(anomalies)},
        ))
    insufficient = [
        {
            "source_host": item.get("source_host", "unknown"),
            "profile_key": item["profile_key"],
        }
        for item in profiles if item["confidence"] == "insufficient"
    ]
    if insufficient:
        recommendations.append(_recommendation(
            "collect_more_successful_resource_evidence",
            "normal",
            "collect_more_successful_resource_evidence",
            "At least three comparable successful attempts are required before proposing resources.",
            scope={"profiles": insufficient},
        ))
    diversity_limited = [
        item for item in profiles
        if item.get("proposed_resources", {}).get("status")
        == "insufficient_workload_diversity"
    ]
    if diversity_limited:
        recommendations.append(_recommendation(
            "collect_diverse_resource_evidence",
            "normal",
            "collect_diverse_resource_evidence",
            "Successful attempts do not yet cover enough scatter batches and chromosomes for a reusable resource proposal.",
            scope={
                "profiles": [{
                    "source_host": item.get("source_host", "unknown"),
                    "profile_key": item["profile_key"],
                    "limitations": item.get("confidence_limitations", []),
                    "workload_coverage": item.get("workload_coverage", {}),
                } for item in diversity_limited],
            },
        ))
    proposals = [
        item for item in profiles
        if item.get("proposed_resources", {}).get("status")
        == "proposal_available_for_researcher_review"
    ]
    if proposals:
        recommendations.append(_recommendation(
            "review_resource_profile_proposals",
            "normal",
            "review_resource_profile_proposals",
            "Cohort-derived resource proposals are available for researcher review.",
            scope={"profiles": [{
                "source_host": item.get("source_host", "unknown"),
                "profile_key": item["profile_key"],
            } for item in proposals]},
            approval=True,
        ))
    return {
        "schema_version": "1.0",
        "policy_version": RESOURCE_POLICY_VERSION,
        "mode": "proposal_only",
        "automatic_execution_allowed": False,
        "automatic_scheduler_changes_allowed": False,
        "fresh_resource_observation_required_before_execution": True,
        "recommendations": recommendations,
    }


def _profile_summary_rows(profiles: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for profile in profiles:
        counts = profile["observation_counts"]
        statistics = profile["successful_attempt_statistics"]
        proposal = profile["proposed_resources"]
        rows.append({
            "source_host": profile.get("source_host", "unknown"),
            "profile_key": profile["profile_key"],
            "confidence": profile["confidence"],
            "sample_size_confidence": profile.get("sample_size_confidence", ""),
            "unique_batches": profile.get("workload_coverage", {}).get(
                "unique_batches", 0
            ),
            "unique_chromosomes": profile.get("workload_coverage", {}).get(
                "unique_chromosomes", 0
            ),
            "confidence_limitations": ",".join(
                profile.get("confidence_limitations", [])
            ),
            "attempts": counts["attempts"],
            "terminal": counts["terminal"],
            "successful": counts["successful"],
            "timeout": counts["timeout"],
            "out_of_memory": counts["out_of_memory"],
            "failed": counts["failed"],
            "cancelled": counts["cancelled"],
            "elapsed_p50_seconds": statistics["elapsed_seconds"]["p50"],
            "elapsed_p95_seconds": statistics["elapsed_seconds"]["p95"],
            "max_rss_p50_bytes": statistics["max_rss_bytes"]["p50"],
            "max_rss_p95_bytes": statistics["max_rss_bytes"]["p95"],
            "cpu_efficiency_p50": statistics["cpu_efficiency"]["p50"],
            "memory_efficiency_p50": statistics["memory_efficiency"]["p50"],
            "proposal_status": proposal["status"],
            "proposed_memory_bytes": proposal.get("memory_bytes"),
            "proposed_time_limit_seconds": proposal.get("time_limit_seconds"),
        })
    return rows


def _anomaly_summary_rows(anomalies: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "anomaly_id": item["anomaly_id"],
        "severity": item["severity"],
        "type": item["type"],
        "source_host": item.get("source_host", "unknown"),
        "profile_key": item["profile_key"],
        "unit_id": item.get("unit_id", ""),
        "attempt_ids": ",".join(str(value) for value in item.get("attempt_ids", [])),
        "evidence": json.dumps(item.get("evidence", {}), sort_keys=True),
    } for item in anomalies]


def _report(
    task: str,
    snapshots: int,
    observations: int,
    current_attempts: int,
    profiles: Sequence[Mapping[str, Any]],
    anomalies: Sequence[Mapping[str, Any]],
    recommendations: Mapping[str, Any],
) -> str:
    lines = [
        "# GenomeAgent Resource Evidence: {}".format(task),
        "",
        "- Source snapshots: {}".format(snapshots),
        "- Recorded observations: {}".format(observations),
        "- Current attempt records: {}".format(current_attempts),
        "- Resource profiles: {}".format(len(profiles)),
        "- Anomaly flags: {}".format(len(anomalies)),
        "- Recommendations: {}".format(len(recommendations["recommendations"])),
        "- Automatic execution allowed: **no**",
        "",
        "## Profiles",
        "",
        "| Source | Profile | Confidence | Coverage | Successes | Timeouts | OOM | Failed | Cancelled | Proposal |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for profile in profiles:
        counts = profile["observation_counts"]
        proposal = profile["proposed_resources"]
        coverage = profile.get("workload_coverage", {})
        coverage_text = (
            "{} batches / {} chromosomes".format(
                coverage.get("unique_batches", 0),
                coverage.get("unique_chromosomes", 0),
            )
            if coverage.get("diversity_required")
            else "not applicable"
        )
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                profile.get("source_host", "unknown"),
                profile["profile_key"],
                profile["confidence"],
                coverage_text,
                counts["successful"],
                counts["timeout"],
                counts["out_of_memory"],
                counts["failed"],
                counts["cancelled"],
                proposal["status"],
            )
        )
    lines.extend(["", "## Anomalies", ""])
    if anomalies:
        for anomaly in anomalies:
            unit = " for unit {}".format(anomaly["unit_id"]) if anomaly.get("unit_id") else ""
            lines.append(
                "- **{}**{}: {}".format(
                    anomaly["type"], unit, anomaly["interpretation_boundary"]
                )
            )
    else:
        lines.append("No deterministic resource anomalies were detected.")
    lines.extend(["", "## Recommendations", ""])
    if recommendations["recommendations"]:
        for item in recommendations["recommendations"]:
            lines.append(
                "- **{}**: {}".format(item["action"], item["reason"])
            )
    else:
        lines.append("No resource recommendations are currently available.")
    lines.extend([
        "",
        "## Interpretation boundary",
        "",
        "Successful comparable attempts define ordinary resource profiles. TIMEOUT and "
        "OUT_OF_MEMORY attempts remain censored lower-bound evidence. These artifacts do "
        "not submit jobs, change scheduler requests or authorize execution.",
        "",
    ])
    return "\n".join(lines)


def _verify_append_only(provenance_path: Path, current: Mapping[str, str]) -> None:
    if not provenance_path.exists():
        return
    try:
        old = json.loads(provenance_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ResourceEvidenceError("Invalid existing provenance: {}".format(exc)) from exc
    previous = {
        str(item["evidence_id"]): str(item["sha256"])
        for item in old.get("source_snapshots", [])
    }
    missing = sorted(set(previous) - set(current))
    if missing:
        raise ResourceEvidenceError(
            "Previously ingested evidence snapshots are missing: " + ", ".join(missing)
        )
    changed = sorted(
        key for key, digest in previous.items() if current.get(key) != digest
    )
    if changed:
        raise ResourceEvidenceError(
            "Previously ingested evidence snapshots changed content: " + ", ".join(changed)
        )


class ResourceEvidenceCore:
    """Reduce immutable resource observations into cautious reusable knowledge."""

    schema_version = "1.0"

    def __init__(
        self,
        evidence_root: Path = Path("workspace/task_resource_evidence"),
        resource_root: Path = Path("workspace/task_resources"),
    ):
        self.evidence_root = Path(evidence_root)
        self.resource_root = Path(resource_root)

    def ingest(self, task: str) -> ResourceIngestResult:
        task = _validate_task(task)
        paths = sorted((self.evidence_root / task).glob("*.json"))
        if not paths:
            raise ResourceEvidenceError(
                "No resource evidence snapshots found under {}".format(
                    self.evidence_root / task
                )
            )
        source_snapshots = []
        observations = []
        digests: dict[str, str] = {}
        for path in paths:
            payload, digest = _read_snapshot(path, task)
            evidence_id = path.stem
            if evidence_id in digests and digests[evidence_id] != digest:
                raise ResourceEvidenceError(
                    "Conflicting evidence snapshots share ID {}".format(evidence_id)
                )
            digests[evidence_id] = digest
            observed_at = str(payload.get("observed_at_utc") or evidence_id)
            for raw in payload["observations"]:
                if not isinstance(raw, Mapping):
                    raise ResourceEvidenceError(
                        "Non-object observation in {}".format(path)
                    )
                observations.append(_normalise_observation(
                    raw,
                    task,
                    evidence_id,
                    digest,
                    observed_at,
                    str(payload.get("host") or "unknown"),
                ))
            source_snapshots.append({
                "evidence_id": evidence_id,
                "path": str(path),
                "sha256": digest,
                "observed_at_utc": observed_at,
                "observation_count": len(payload["observations"]),
                "host": payload.get("host", ""),
                "source_mode": payload.get("source_mode", ""),
            })

        resource_dir = self.resource_root / task
        provenance_path = resource_dir / "provenance.json"
        _verify_append_only(provenance_path, digests)
        observations.sort(key=lambda item: (
            str(item.get("observed_at_utc") or ""),
            str(item.get("source_host") or "unknown"),
            str(item.get("attempt_id") or ""),
            str(item.get("observation_id") or ""),
        ))
        latest_by_attempt: dict[tuple[str, str], Mapping[str, Any]] = {}
        for item in observations:
            latest_by_attempt[(
                str(item.get("source_host") or "unknown"),
                str(item["attempt_id"]),
            )] = item
        current_attempts = [latest_by_attempt[key] for key in sorted(latest_by_attempt)]
        profiles = _build_profiles(current_attempts)
        anomalies = _build_anomalies(current_attempts)
        recommendations = _build_recommendations(profiles, anomalies)
        recommendations.update({
            "task": task,
            "source_evidence_sha256": _sha256(_canonical_bytes(source_snapshots)),
            "policy_version": RESOURCE_POLICY_VERSION,
        })
        profile_document = {
            "schema_version": self.schema_version,
            "policy_version": RESOURCE_POLICY_VERSION,
            "task": task,
            "learning_mode": "deterministic_empirical_profiles",
            "successful_attempts_define_typical_resources": True,
            "failed_attempts_are_censored_evidence": True,
            "minimum_successes_for_proposal": 3,
            "profiles": profiles,
        }
        anomaly_document = {
            "schema_version": self.schema_version,
            "policy_version": RESOURCE_POLICY_VERSION,
            "task": task,
            "automatic_interpretation_allowed": False,
            "anomalies": anomalies,
        }
        provenance = {
            "schema_version": self.schema_version,
            "resource_policy_version": RESOURCE_POLICY_VERSION,
            "task": task,
            "source_mode": "immutable_read_only_scheduler_accounting_replay",
            "source_snapshots": source_snapshots,
            "observation_count": len(observations),
            "current_attempt_count": len(current_attempts),
            "derivation_identity_sha256": _sha256(_canonical_bytes({
                "source_snapshots": source_snapshots,
                "resource_policy_version": RESOURCE_POLICY_VERSION,
            })),
            "safety": {
                "remote_reads_during_ingest": False,
                "remote_writes": False,
                "job_submission": False,
                "job_cancellation": False,
                "automatic_scheduler_changes": False,
            },
        }

        observations_path = resource_dir / "resource_observations.jsonl"
        profiles_path = resource_dir / "resource_profiles.json"
        anomalies_path = resource_dir / "resource_anomalies.json"
        recommendations_path = resource_dir / "resource_recommendations.json"
        profile_summary_path = resource_dir / "resource_profile_summary.tsv"
        anomaly_summary_path = resource_dir / "resource_anomalies.tsv"
        report_path = resource_dir / "report.md"
        observation_text = "".join(
            json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n"
            for item in observations
        )
        _atomic_write(observations_path, observation_text)
        _atomic_write(profiles_path, _pretty_json(profile_document))
        _atomic_write(anomalies_path, _pretty_json(anomaly_document))
        _atomic_write(recommendations_path, _pretty_json(recommendations))
        profile_fields = (
            "source_host", "profile_key", "confidence", "sample_size_confidence",
            "unique_batches", "unique_chromosomes", "confidence_limitations",
            "attempts", "terminal", "successful",
            "timeout", "out_of_memory", "failed", "cancelled",
            "elapsed_p50_seconds", "elapsed_p95_seconds", "max_rss_p50_bytes",
            "max_rss_p95_bytes", "cpu_efficiency_p50", "memory_efficiency_p50",
            "proposal_status", "proposed_memory_bytes", "proposed_time_limit_seconds",
        )
        anomaly_fields = (
            "anomaly_id", "severity", "type", "source_host", "profile_key", "unit_id",
            "attempt_ids", "evidence",
        )
        _atomic_write(
            profile_summary_path,
            _tsv(_profile_summary_rows(profiles), profile_fields),
        )
        _atomic_write(
            anomaly_summary_path,
            _tsv(_anomaly_summary_rows(anomalies), anomaly_fields),
        )
        _atomic_write(
            report_path,
            _report(
                task,
                len(source_snapshots),
                len(observations),
                len(current_attempts),
                profiles,
                anomalies,
                recommendations,
            ),
        )
        _atomic_write(provenance_path, _pretty_json(provenance))

        return ResourceIngestResult(
            task=task,
            resource_dir=resource_dir,
            source_snapshots=len(source_snapshots),
            observations=len(observations),
            current_attempts=len(current_attempts),
            profiles=len(profiles),
            anomalies=len(anomalies),
            recommendations=len(recommendations["recommendations"]),
            artifact_paths=(
                observations_path,
                profiles_path,
                anomalies_path,
                recommendations_path,
                profile_summary_path,
                anomaly_summary_path,
                report_path,
                provenance_path,
            ),
        )
