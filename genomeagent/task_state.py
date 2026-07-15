#!/usr/bin/env python3
"""Deterministic, read-only knowledge bridge for Task Scanner observations."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SUPPORTED_TASKS = {"gam_deduplication", "scattered_joint_calling"}


class TaskStateError(RuntimeError):
    """Raised when scan history cannot be reduced into trustworthy task state."""


@dataclass(frozen=True)
class ScanBundle:
    task: str
    scan_id: str
    path: Path
    digest: str
    created_at_utc: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class TaskStateIngestResult:
    task: str
    state_dir: Path
    source_scans: int
    latest_scan_id: str
    overall_status: str
    events: int
    recommendations: int
    artifact_paths: tuple[Path, ...]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaskStateError(f"Scan bundle does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TaskStateError(f"Invalid JSON in scan bundle {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskStateError(f"Scan bundle must contain a JSON object: {path}")
    return value


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _validate_task_name(task: str) -> str:
    normalized = str(task).strip()
    if normalized not in SUPPORTED_TASKS or not re.fullmatch(r"[a-z0-9_]+", normalized):
        raise TaskStateError(
            f"Unsupported task {task!r}; expected one of {sorted(SUPPORTED_TASKS)}"
        )
    return normalized


def _created_sort_key(bundle: ScanBundle) -> tuple[datetime, str]:
    raw = bundle.created_at_utc
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        parsed = datetime.min.replace(tzinfo=timezone.utc)
    return parsed, bundle.scan_id


def discover_scan_bundles(scan_root: Path, task: str) -> list[ScanBundle]:
    """Load every immutable, successfully written scan bundle for one task."""

    task = _validate_task_name(task)
    task_root = Path(scan_root) / task
    paths = sorted(task_root.glob("*/task_scan.json"))
    if not paths:
        raise TaskStateError(
            f"No task_scan.json bundles found under {task_root}. Run the Task Scanner first."
        )

    bundles = []
    seen_scan_ids: dict[str, str] = {}
    for path in paths:
        raw = path.read_bytes()
        digest = _sha256_bytes(raw)
        payload = _read_json(path)
        payload_task = str(payload.get("task", ""))
        if payload_task != task:
            raise TaskStateError(
                f"Task mismatch in {path}: expected {task}, found {payload_task or 'missing'}"
            )
        if payload.get("mode") != "read_only":
            raise TaskStateError(
                f"Refusing non-read-only source bundle {path}: mode={payload.get('mode')!r}"
            )
        if not isinstance(payload.get("observation"), Mapping):
            raise TaskStateError(f"Scan bundle has no observation object: {path}")
        if not isinstance(payload.get("status_summary"), Mapping):
            raise TaskStateError(f"Scan bundle has no status_summary object: {path}")

        scan_id = path.parent.name
        previous_digest = seen_scan_ids.get(scan_id)
        if previous_digest and previous_digest != digest:
            raise TaskStateError(
                f"Conflicting scan bundles share scan ID {scan_id} under {task_root}"
            )
        seen_scan_ids[scan_id] = digest
        bundles.append(
            ScanBundle(
                task=task,
                scan_id=scan_id,
                path=path,
                digest=digest,
                created_at_utc=str(payload.get("created_at_utc") or scan_id),
                payload=payload,
            )
        )

    return sorted(bundles, key=_created_sort_key)


def _health_check(name: str, passed: bool, detail: str, critical: bool = True) -> dict[str, Any]:
    return {
        "check": name,
        "passed": bool(passed),
        "critical_for_action_planning": bool(critical),
        "detail": detail,
    }


def _scheduler_health(observation: Mapping[str, Any]) -> list[dict[str, Any]]:
    jobs = observation.get("jobs", {})
    if not isinstance(jobs, Mapping):
        jobs = {}
    checks = []
    for command in ("squeue", "sacct"):
        returncode = jobs.get(f"{command}_returncode")
        stderr = str(jobs.get(f"{command}_stderr") or "").strip()
        detail = f"returncode={returncode!r}"
        if stderr:
            detail += f"; stderr={stderr[-500:]}"
        checks.append(
            _health_check(
                f"scheduler_{command}",
                returncode == 0,
                detail,
            )
        )
    return checks


def _normalize_scattered_units(
    observation: Mapping[str, Any],
    status: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows = status.get("intervals", [])
    if not isinstance(rows, list) or not rows:
        rows = observation.get("interval_table", {}).get("records", [])
    units = []
    legacy_states = {
        "completed_atomic_publish_contract": "completed",
        "vcf_present_index_missing": "partial_output",
        "index_present_vcf_missing": "partial_output",
        "pending": "unresolved_legacy_observation",
        "output_absent": "unresolved_legacy_observation",
    }
    for row in rows:
        if not isinstance(row, Mapping) or row.get("task") is None:
            continue
        task_number = int(row["task"])
        state = str(
            row.get("lifecycle_state")
            or legacy_states.get(str(row.get("state")), str(row.get("state") or "unknown"))
        )
        units.append({
            "unit_id": str(task_number),
            "unit_type": "interval",
            "state": state,
            "task": task_number,
            "batch": row.get("batch", ""),
            "chromosome": row.get("chromosome", ""),
            "interval": row.get("interval", ""),
            "output_state": row.get("output_state", row.get("state", "")),
            "scheduler_job_id": row.get("scheduler_job_id", ""),
            "scheduler_state": row.get("scheduler_state", ""),
            "vcf": row.get("vcf", ""),
            "index": row.get("index", ""),
            "stdout_log": row.get("stdout_log", ""),
            "stderr_log": row.get("stderr_log", ""),
        })
    return sorted(units, key=lambda item: int(item["unit_id"]))


def _normalize_gam_units(observation: Mapping[str, Any]) -> list[dict[str, Any]]:
    units = []
    for dataset in observation.get("datasets", []):
        if not isinstance(dataset, Mapping):
            continue
        dataset_name = str(dataset.get("name", ""))
        for sample in dataset.get("samples", []):
            if not isinstance(sample, Mapping) or not sample.get("sample"):
                continue
            sample_name = str(sample["sample"])
            units.append({
                "unit_id": f"{dataset_name}:{sample_name}",
                "unit_type": "sample",
                "state": str(sample.get("state") or "unknown"),
                "dataset": dataset_name,
                "sample": sample_name,
                "input_gam": sample.get("input_gam", ""),
                "output_gam": sample.get("output_gam", ""),
                "summary": sample.get("summary", ""),
                "summary_type": sample.get("summary_type", ""),
                "exact_template_pair_match": bool(
                    sample.get("exact_template_pair_match")
                ),
                "residual_qc_state": sample.get("residual_qc_state", ""),
                "residual_primary_reads_after_dedup": sample.get(
                    "residual_primary_reads_after_dedup"
                ),
                "residual_duplicate_primary_reads_after_dedup": sample.get(
                    "residual_duplicate_primary_reads_after_dedup"
                ),
                "residual_duplicate_primary_pct_after_dedup": sample.get(
                    "residual_duplicate_primary_pct_after_dedup"
                ),
                "source_input_deleted_after_success": bool(
                    sample.get("source_input_deleted_after_success")
                ),
            })
    return sorted(units, key=lambda item: item["unit_id"])


def _normalization_health(
    task: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    observation = payload["observation"]
    checks = [
        _health_check(
            "source_read_only",
            payload.get("mode") == "read_only",
            f"mode={payload.get('mode')!r}",
        ),
        *_scheduler_health(observation),
    ]

    if task == "scattered_joint_calling":
        table = observation.get("interval_table", {})
        if not isinstance(table, Mapping):
            table = {}
        integrity_issues = sum(
            len(table.get(key, []) or [])
            for key in (
                "malformed_lines",
                "task_line_mismatches",
                "duplicate_task_ids",
                "duplicate_output_paths",
            )
        )
        output_errors = len(table.get("output_scan_errors", []) or [])
        checks.extend([
            _health_check(
                "interval_manifest_integrity",
                bool(table.get("exists")) and integrity_issues == 0,
                f"exists={bool(table.get('exists'))}; integrity_issues={integrity_issues}",
            ),
            _health_check(
                "interval_output_discovery",
                output_errors == 0,
                f"directory_scan_errors={output_errors}",
            ),
        ])
    else:
        directory_errors = 0
        manifest_issues = 0
        for dataset in observation.get("datasets", []):
            if not isinstance(dataset, Mapping):
                continue
            directory_errors += len(dataset.get("directory_errors", []) or [])
            manifest = dataset.get("manifest", {})
            if isinstance(manifest, Mapping):
                manifest_issues += len(manifest.get("duplicate_samples", []) or [])
                manifest_issues += len(manifest.get("malformed_lines", []) or [])
        checks.extend([
            _health_check(
                "dataset_directory_observation",
                directory_errors == 0,
                f"directory_errors={directory_errors}",
            ),
            _health_check(
                "dataset_manifest_integrity",
                manifest_issues == 0,
                f"manifest_issues={manifest_issues}",
            ),
        ])

    blockers = [
        item["check"]
        for item in checks
        if item["critical_for_action_planning"] and not item["passed"]
    ]
    return {
        "healthy_for_action_planning": not blockers,
        "blockers": blockers,
        "checks": checks,
    }


def normalize_scan(bundle: ScanBundle) -> dict[str, Any]:
    payload = bundle.payload
    status = payload["status_summary"]
    observation = payload["observation"]
    health = _normalization_health(bundle.task, payload)

    if bundle.task == "scattered_joint_calling":
        units = _normalize_scattered_units(observation, status)
        groups = [dict(item) for item in status.get("batches", [])]
        group_type = "batch"
    else:
        units = _normalize_gam_units(observation)
        groups = [dict(item) for item in status.get("datasets", [])]
        group_type = "dataset"

    unit_state_counts: dict[str, int] = {}
    for unit in units:
        state = str(unit["state"])
        unit_state_counts[state] = unit_state_counts.get(state, 0) + 1

    configuration = payload.get("configuration", {})
    if not isinstance(configuration, Mapping):
        configuration = {}
    source = {
        "scan_id": bundle.scan_id,
        "task_scan_path": str(bundle.path),
        "task_scan_sha256": bundle.digest,
        "created_at_utc": bundle.created_at_utc,
        "scanned_at_cluster_time": observation.get("scanned_at_cluster_time", ""),
        "host": payload.get("host", ""),
        "mode": payload.get("mode", ""),
        "scanner_schema_version": payload.get("schema_version", ""),
        "profile_version": configuration.get("profile_version", ""),
        "remote_python": configuration.get("remote_python", ""),
        "configuration_sha256": _sha256_bytes(_json_bytes(configuration)),
    }
    return {
        "schema_version": "1.0",
        "task": bundle.task,
        "source": source,
        "overall_status": status.get("overall_status", "unknown"),
        "current_stage": status.get("current_stage", "unknown"),
        "next_safe_action": status.get("next_safe_action", "unknown"),
        "warnings": list(status.get("warnings", [])),
        "counts": dict(status.get("counts", {})),
        "unit_type": units[0]["unit_type"] if units else "unknown",
        "unit_count": len(units),
        "unit_state_counts": unit_state_counts,
        "units": units,
        "group_type": group_type,
        "groups": groups,
        "observation_health": health,
        "execution_gate": {
            "automatic_execution_allowed": False,
            "researcher_approval_required": True,
            "fresh_pre_execution_scan_required": True,
            "evidence_sufficient_for_action_planning": health[
                "healthy_for_action_planning"
            ],
            "blockers": health["blockers"],
        },
    }


def _recommendation(
    identifier: str,
    priority: str,
    action: str,
    reason: str,
    scope: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    requires_approval: bool = False,
) -> dict[str, Any]:
    return {
        "recommendation_id": identifier,
        "priority": priority,
        "action": action,
        "reason": reason,
        "scope": dict(scope or {}),
        "evidence": dict(evidence or {}),
        "requires_researcher_approval": bool(requires_approval),
        "execution_enabled": False,
    }


def build_recommendations(state: Mapping[str, Any]) -> dict[str, Any]:
    task = str(state["task"])
    source = state["source"]
    health = state["observation_health"]
    recommendations = []

    if not health["healthy_for_action_planning"]:
        recommendations.append(_recommendation(
            "repair_observation_before_action",
            "critical",
            "repair_observation_before_action",
            "One or more critical observation checks failed; cluster actions cannot be planned safely.",
            evidence={"blockers": health["blockers"]},
        ))
    elif task == "scattered_joint_calling":
        by_state: dict[str, list[Mapping[str, Any]]] = {}
        for unit in state["units"]:
            by_state.setdefault(str(unit["state"]), []).append(unit)

        failed = by_state.get("failed_needs_review", [])
        if failed:
            recommendations.append(_recommendation(
                "inspect_failed_intervals",
                "high",
                "inspect_failed_intervals",
                "The latest scheduler attempt failed for one or more intervals.",
                scope={"tasks": [item["task"] for item in failed]},
                evidence={
                    "failures": [
                        {
                            "task": item["task"],
                            "interval": item.get("interval", ""),
                            "scheduler_job_id": item.get("scheduler_job_id", ""),
                            "scheduler_state": item.get("scheduler_state", ""),
                            "stdout_log": item.get("stdout_log", ""),
                            "stderr_log": item.get("stderr_log", ""),
                        }
                        for item in failed
                    ]
                },
            ))

        inconsistent = [
            *by_state.get("partial_output", []),
            *by_state.get("scheduler_completed_output_missing", []),
            *by_state.get("unmapped_interval", []),
        ]
        if inconsistent:
            recommendations.append(_recommendation(
                "review_inconsistent_interval_state",
                "high",
                "review_inconsistent_interval_state",
                "Published outputs, scheduler state, or batch coverage are inconsistent.",
                scope={"tasks": [item["task"] for item in inconsistent]},
            ))

        running = len(by_state.get("running", []))
        queued = len(by_state.get("queued", []))
        if running or queued:
            recommendations.append(_recommendation(
                "wait_and_rescan_active_intervals",
                "normal",
                "wait_and_rescan",
                "Active scheduler work should continue without duplicate submission.",
                evidence={"running": running, "queued": queued},
            ))

        unsubmitted_batches = [
            item.get("batch")
            for item in state.get("groups", [])
            if item.get("status") == "not_submitted"
        ]
        if unsubmitted_batches:
            recommendations.append(_recommendation(
                "review_unsubmitted_batches",
                "low" if running or queued else "normal",
                "review_unsubmitted_batches",
                "Batches without submission evidence remain; review capacity and dependencies before submission.",
                scope={"batches": unsubmitted_batches},
                requires_approval=True,
            ))

        if state["unit_count"] and state["unit_state_counts"].get("completed") == state["unit_count"]:
            recommendations.append(_recommendation(
                "review_gather_prerequisites",
                "normal",
                "review_gather_prerequisites",
                "Every interval is completed; verify completeness before gather or merge.",
            ))
    else:
        by_state: dict[str, list[Mapping[str, Any]]] = {}
        for unit in state["units"]:
            by_state.setdefault(str(unit["state"]), []).append(unit)
        problematic = [
            *by_state.get("summary_present_output_missing", []),
            *by_state.get("missing_input_no_completion_evidence", []),
        ]
        if problematic:
            recommendations.append(_recommendation(
                "review_problematic_gam_samples",
                "high",
                "review_problematic_gam_samples",
                "One or more samples lack required input or have summary/output inconsistency.",
                scope={"samples": [item["unit_id"] for item in problematic]},
            ))
        unvalidated = by_state.get("output_present_unvalidated", [])
        if unvalidated:
            recommendations.append(_recommendation(
                "validate_unconfirmed_gam_outputs",
                "high",
                "validate_unconfirmed_gam_outputs",
                "Output GAMs require EXACT_TEMPLATE_PAIR_MATCH evidence.",
                scope={"samples": [item["unit_id"] for item in unvalidated]},
            ))
        residual_duplicates = [
            item for item in state["units"]
            if item.get("residual_qc_state") == "residual_duplicates_detected"
        ]
        if residual_duplicates:
            recommendations.append(_recommendation(
                "review_observed_residual_duplication",
                "normal",
                "review_observed_residual_duplication",
                "Residual summaries report duplicate primary reads after exact template-pair removal; no downstream acceptance threshold has been approved.",
                scope={"samples": [item["unit_id"] for item in residual_duplicates]},
                evidence={
                    "observations": [
                        {
                            "sample": item["unit_id"],
                            "residual_duplicate_primary_reads": item.get(
                                "residual_duplicate_primary_reads_after_dedup"
                            ),
                            "residual_duplicate_primary_pct": item.get(
                                "residual_duplicate_primary_pct_after_dedup"
                            ),
                            "summary": item.get("summary", ""),
                        }
                        for item in residual_duplicates
                    ]
                },
            ))
        active = by_state.get("assigned_pending_or_running", [])
        if active:
            recommendations.append(_recommendation(
                "wait_and_rescan_active_samples",
                "normal",
                "wait_and_rescan",
                "Assigned samples remain active or pending; avoid duplicate processing.",
                evidence={"active_or_pending_samples": len(active)},
            ))
        if state.get("overall_status") == "deduplication_validated":
            recommendations.append(_recommendation(
                "review_residual_duplication_qc",
                "normal",
                "review_residual_duplication_qc",
                "Template-pair accounting is complete; residual-duplication QC remains a separate gate.",
            ))

    if not recommendations:
        recommendations.append(_recommendation(
            "follow_scanner_next_safe_action",
            "normal",
            str(state.get("next_safe_action") or "review_current_state"),
            "No more specific bridge rule applies; retain the scanner's deterministic recommendation.",
        ))

    return {
        "schema_version": "1.0",
        "task": task,
        "source_scan_id": source["scan_id"],
        "source_task_scan_sha256": source["task_scan_sha256"],
        "mode": "read_only_recommendations",
        "automatic_execution_allowed": False,
        "fresh_pre_execution_scan_required": True,
        "recommendations": recommendations,
    }


def _event(
    task: str,
    source: Mapping[str, Any],
    event_type: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        "task": task,
        "observed_at_utc": source.get("created_at_utc", ""),
        "source_scan_id": source.get("scan_id", ""),
        "source_task_scan_sha256": source.get("task_scan_sha256", ""),
        "event_type": event_type,
        "details": dict(details),
    }
    return {"event_id": _sha256_bytes(_json_bytes(value)), **value}


def build_events(states: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not states:
        return []
    task = str(states[0]["task"])
    events = [_event(
        task,
        states[0]["source"],
        "baseline_created",
        {
            "overall_status": states[0]["overall_status"],
            "unit_count": states[0]["unit_count"],
            "unit_state_counts": states[0]["unit_state_counts"],
        },
    )]

    for previous, current in zip(states, states[1:]):
        source = current["source"]
        if previous["overall_status"] != current["overall_status"]:
            events.append(_event(
                task,
                source,
                "overall_status_changed",
                {
                    "from": previous["overall_status"],
                    "to": current["overall_status"],
                },
            ))

        previous_health = previous["observation_health"]["healthy_for_action_planning"]
        current_health = current["observation_health"]["healthy_for_action_planning"]
        if previous_health != current_health:
            events.append(_event(
                task,
                source,
                "observation_health_changed",
                {
                    "from": previous_health,
                    "to": current_health,
                    "blockers": current["observation_health"]["blockers"],
                },
            ))

        old_units = {item["unit_id"]: item for item in previous["units"]}
        new_units = {item["unit_id"]: item for item in current["units"]}
        for unit_id in sorted(set(old_units) | set(new_units)):
            old = old_units.get(unit_id)
            new = new_units.get(unit_id)
            if old is None:
                events.append(_event(
                    task,
                    source,
                    "unit_discovered",
                    {"unit_id": unit_id, "state": new["state"]},
                ))
            elif new is None:
                events.append(_event(
                    task,
                    source,
                    "unit_missing_from_latest_scan",
                    {"unit_id": unit_id, "previous_state": old["state"]},
                ))
            elif old["state"] != new["state"]:
                details = {
                    "unit_id": unit_id,
                    "unit_type": new.get("unit_type", ""),
                    "from": old["state"],
                    "to": new["state"],
                }
                for key in ("task", "interval", "batch", "dataset", "sample"):
                    if new.get(key) not in (None, ""):
                        details[key] = new[key]
                evidence = {
                    key: new[key]
                    for key in (
                        "scheduler_job_id",
                        "scheduler_state",
                        "stdout_log",
                        "stderr_log",
                        "vcf",
                        "index",
                        "input_gam",
                        "output_gam",
                        "summary",
                    )
                    if new.get(key) not in (None, "")
                }
                if evidence:
                    details["evidence"] = evidence
                events.append(_event(task, source, "unit_state_changed", details))

        group_name_key = "batch" if current["group_type"] == "batch" else "dataset"
        old_groups = {
            str(item.get(group_name_key, "")): item
            for item in previous.get("groups", [])
        }
        new_groups = {
            str(item.get(group_name_key, "")): item
            for item in current.get("groups", [])
        }
        for name in sorted(set(old_groups) & set(new_groups)):
            old_status = old_groups[name].get("status")
            new_status = new_groups[name].get("status")
            if old_status != new_status:
                events.append(_event(
                    task,
                    source,
                    f"{current['group_type']}_status_changed",
                    {current["group_type"]: name, "from": old_status, "to": new_status},
                ))

        old_counts = previous.get("counts", {})
        new_counts = current.get("counts", {})
        for name in sorted(set(old_counts) | set(new_counts)):
            old_value = old_counts.get(name)
            new_value = new_counts.get(name)
            if isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
                if old_value != new_value:
                    events.append(_event(
                        task,
                        source,
                        "progress_count_changed",
                        {"count": name, "from": old_value, "to": new_value},
                    ))
    return events


def _provenance(task: str, scan_root: Path, states: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task": task,
        "scan_root": str(scan_root),
        "ingested_scan_count": len(states),
        "latest_scan_id": states[-1]["source"]["scan_id"],
        "scans": [
            {
                **state["source"],
                "overall_status": state["overall_status"],
                "observation_healthy_for_action_planning": state[
                    "observation_health"
                ]["healthy_for_action_planning"],
            }
            for state in states
        ],
    }


def _verify_append_only_history(
    task: str,
    provenance_path: Path,
    bundles: Sequence[ScanBundle],
) -> None:
    """Reject mutation or disappearance of a previously ingested source scan."""

    if not provenance_path.exists():
        return
    previous = _read_json(provenance_path)
    if previous.get("task") != task:
        raise TaskStateError(
            f"Existing provenance task mismatch in {provenance_path}"
        )
    old_scans = {
        str(item.get("scan_id", "")): str(item.get("task_scan_sha256", ""))
        for item in previous.get("scans", [])
        if isinstance(item, Mapping) and item.get("scan_id")
    }
    current_scans = {bundle.scan_id: bundle.digest for bundle in bundles}
    missing = sorted(set(old_scans) - set(current_scans))
    if missing:
        raise TaskStateError(
            "Previously ingested source scans are missing: " + ", ".join(missing)
        )
    changed = sorted(
        scan_id
        for scan_id, digest in old_scans.items()
        if current_scans.get(scan_id) != digest
    )
    if changed:
        raise TaskStateError(
            "Previously ingested source scans changed content: " + ", ".join(changed)
        )


class TaskStateBridge:
    """Replay Task Scanner bundles into canonical operational knowledge."""

    schema_version = "1.0"

    def __init__(
        self,
        scan_root: Path = Path("workspace/task_scans"),
        state_root: Path = Path("workspace/task_state"),
    ):
        self.scan_root = Path(scan_root)
        self.state_root = Path(state_root)

    def ingest(self, task: str) -> TaskStateIngestResult:
        task = _validate_task_name(task)
        bundles = discover_scan_bundles(self.scan_root, task)
        state_dir = self.state_root / task
        provenance_path = state_dir / "provenance.json"
        _verify_append_only_history(task, provenance_path, bundles)
        states = [normalize_scan(bundle) for bundle in bundles]
        current = dict(states[-1])
        recommendations = build_recommendations(current)
        events = build_events(states)
        provenance = _provenance(task, self.scan_root, states)
        current["recommendation_summary"] = {
            "recommendation_count": len(recommendations["recommendations"]),
            "automatic_execution_allowed": False,
        }

        current_path = state_dir / "current_state.json"
        events_path = state_dir / "events.jsonl"
        recommendations_path = state_dir / "recommendations.json"

        _atomic_write(current_path, _pretty_json(current))
        event_text = "".join(
            json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n"
            for item in events
        )
        _atomic_write(events_path, event_text)
        _atomic_write(recommendations_path, _pretty_json(recommendations))
        _atomic_write(provenance_path, _pretty_json(provenance))

        return TaskStateIngestResult(
            task=task,
            state_dir=state_dir,
            source_scans=len(states),
            latest_scan_id=str(current["source"]["scan_id"]),
            overall_status=str(current["overall_status"]),
            events=len(events),
            recommendations=len(recommendations["recommendations"]),
            artifact_paths=(
                current_path,
                events_path,
                recommendations_path,
                provenance_path,
            ),
        )
