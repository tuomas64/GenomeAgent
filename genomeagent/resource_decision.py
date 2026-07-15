#!/usr/bin/env python3
"""Deterministic resource decisions across known and new HPC environments."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from genomeagent.task_state import SUPPORTED_TASKS


RESOURCE_DECISION_POLICY_VERSION = "1.1"
ACTIONABLE_DECISIONS = {
    "target_environment_proposal_available",
    "cross_environment_pilot_proposed",
}
BLOCKING_ANOMALY_SEVERITIES = {"critical", "block", "high", "high_review"}
ACTIVE_TASK_STATUSES = {
    "awaiting_submission",
    "running",
    "running_with_warnings",
}


class ResourceDecisionError(RuntimeError):
    """Raised when deterministic planning inputs are malformed or ambiguous."""


@dataclass(frozen=True)
class EnvironmentRecord:
    environment_id: str
    path: Path
    digest: str
    configuration: dict[str, Any]


@dataclass(frozen=True)
class ResourceDecisionResult:
    task: str
    profile_key: str
    source_environment: str
    target_environment: str
    decision: str
    confidence: str
    plan_dir: Path
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


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise ResourceDecisionError(
            "{} must contain only lower-case letters, digits, dots, underscores or hyphens.".format(
                label
            )
        )
    return normalized


def _profile_identifier(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or not re.fullmatch(r"[A-Za-z0-9._:/-]+", normalized):
        raise ResourceDecisionError("Unsafe or empty resource profile key: {!r}".format(value))
    return normalized


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ResourceDecisionError("{} does not exist: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResourceDecisionError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise ResourceDecisionError("{} must be a JSON object: {}".format(label, path))
    return value, _sha256(raw)


def _optional_json(path: Path, label: str) -> tuple[Optional[dict[str, Any]], str]:
    if not path.exists():
        return None, ""
    value, digest = _read_json(path, label)
    return value, digest


def _load_environments(root: Path) -> dict[str, EnvironmentRecord]:
    paths = sorted(Path(root).glob("*.json"))
    if not paths:
        raise ResourceDecisionError("No environment configurations found under {}.".format(root))
    records: dict[str, EnvironmentRecord] = {}
    aliases: dict[str, str] = {}
    for path in paths:
        config, digest = _read_json(path, "environment configuration")
        environment_id = _identifier(config.get("environment_id"), "environment_id")
        if path.stem != environment_id:
            raise ResourceDecisionError(
                "Environment filename {} must match environment_id {}.".format(
                    path.name, environment_id
                )
            )
        if environment_id in records:
            raise ResourceDecisionError("Duplicate environment_id: {}".format(environment_id))
        scheduler = str(config.get("scheduler") or "").strip().lower()
        if not scheduler:
            raise ResourceDecisionError(
                "Environment {} requires a scheduler value.".format(environment_id)
            )
        raw_aliases = config.get("host_aliases", [])
        if not isinstance(raw_aliases, list) or not all(
            isinstance(item, str) and item.strip() for item in raw_aliases
        ):
            raise ResourceDecisionError(
                "Environment {} requires a host_aliases list of strings.".format(
                    environment_id
                )
            )
        all_aliases = {environment_id, *(item.strip().lower() for item in raw_aliases)}
        for alias in all_aliases:
            previous = aliases.get(alias)
            if previous and previous != environment_id:
                raise ResourceDecisionError(
                    "Host alias {} belongs to both {} and {}.".format(
                        alias, previous, environment_id
                    )
                )
            aliases[alias] = environment_id
        records[environment_id] = EnvironmentRecord(
            environment_id=environment_id,
            path=path,
            digest=digest,
            configuration=config,
        )
    return records


def _environment_for_host(
    source_host: str,
    environments: Mapping[str, EnvironmentRecord],
) -> str:
    host = str(source_host or "unknown").strip().lower()
    matches = []
    for environment_id, record in environments.items():
        aliases = {
            environment_id,
            *(str(item).strip().lower() for item in record.configuration.get("host_aliases", [])),
        }
        if host in aliases:
            matches.append(environment_id)
    return matches[0] if len(matches) == 1 else "unknown"


def _task(task: str) -> str:
    value = str(task).strip()
    if value not in SUPPORTED_TASKS or not re.fullmatch(r"[a-z0-9_]+", value):
        raise ResourceDecisionError(
            "Unsupported task {!r}; expected one of {}".format(task, sorted(SUPPORTED_TASKS))
        )
    return value


def _profile_candidates(
    profiles: Sequence[Mapping[str, Any]],
    profile_key: Optional[str],
) -> tuple[str, list[dict[str, Any]]]:
    available_keys = sorted({
        str(item.get("profile_key") or "")
        for item in profiles
        if str(item.get("profile_key") or "")
    })
    if profile_key is None:
        if len(available_keys) > 1:
            raise ResourceDecisionError(
                "Multiple resource profiles are available; choose --profile-key from: {}".format(
                    ", ".join(available_keys)
                )
            )
        selected_key = available_keys[0] if available_keys else "unresolved"
    else:
        selected_key = _profile_identifier(profile_key)
    return selected_key, [
        dict(item) for item in profiles
        if str(item.get("profile_key") or "") == selected_key
    ]


def _select_source_profile(
    candidates: Sequence[Mapping[str, Any]],
    target_environment: str,
    source_environment: Optional[str],
    environments: Mapping[str, EnvironmentRecord],
) -> tuple[Optional[dict[str, Any]], str, str]:
    described = []
    for item in candidates:
        row = dict(item)
        row["resolved_source_environment"] = _environment_for_host(
            str(row.get("source_host") or "unknown"), environments
        )
        described.append(row)

    if source_environment is not None:
        requested = _identifier(source_environment, "source environment")
        matches = [
            item for item in described
            if item["resolved_source_environment"] == requested
        ]
        if len(matches) == 1:
            return matches[0], requested, "selected_explicit_source_environment"
        if not matches:
            return None, requested, "requested_source_environment_has_no_profile"
        return None, requested, "multiple_profiles_for_requested_source_environment"

    target_matches = [
        item for item in described
        if item["resolved_source_environment"] == target_environment
    ]
    if len(target_matches) == 1:
        return target_matches[0], target_environment, "preferred_target_environment_evidence"
    if len(target_matches) > 1:
        return None, target_environment, "multiple_target_environment_profiles"
    if len(described) == 1:
        selected = described[0]
        return (
            selected,
            str(selected["resolved_source_environment"]),
            "selected_only_available_source_profile",
        )
    if not described:
        return None, "unknown", "no_matching_profile"
    return None, "ambiguous", "multiple_external_source_profiles"


def _allocation(
    proposal: Mapping[str, Any],
    target: EnvironmentRecord,
    source_environment: str,
    target_validated: bool,
) -> dict[str, Any]:
    defaults = target.configuration.get("scheduler_defaults", {})
    if not isinstance(defaults, Mapping):
        defaults = {}
    memory = proposal.get("memory_bytes")
    time_limit = proposal.get("time_limit_seconds")
    cpus = defaults.get("cpus_per_task")
    partition = defaults.get("partition")
    missing = []
    if memory is None:
        missing.append("memory_bytes")
    if time_limit is None:
        missing.append("time_limit_seconds")
    if cpus is None:
        missing.append("cpus_per_task")
    if not partition:
        missing.append("partition")
    return {
        "status": "partial_proposal" if missing else "complete_proposal",
        "intended_use": (
            "target_environment_review"
            if target_validated else "bounded_cross_environment_pilot_only"
        ),
        "source_environment": source_environment,
        "target_environment": target.environment_id,
        "target_validated": bool(target_validated),
        "memory_bytes": memory,
        "time_limit_seconds": time_limit,
        "cpus_per_task": cpus,
        "partition": partition,
        "missing_fields": missing,
        "automatic_application_allowed": False,
    }


def _withheld_allocation(target_environment: str) -> dict[str, Any]:
    return {
        "status": "withheld",
        "intended_use": "none",
        "source_environment": "unknown",
        "target_environment": target_environment,
        "target_validated": False,
        "memory_bytes": None,
        "time_limit_seconds": None,
        "cpus_per_task": None,
        "partition": None,
        "missing_fields": [
            "memory_bytes", "time_limit_seconds", "cpus_per_task", "partition"
        ],
        "automatic_application_allowed": False,
    }


def _decision(
    task: str,
    profile_key: str,
    profile: Optional[Mapping[str, Any]],
    source_environment: str,
    source_selection: str,
    target: EnvironmentRecord,
    anomalies: Sequence[Mapping[str, Any]],
    resource_knowledge_available: bool,
) -> dict[str, Any]:
    target_id = target.environment_id
    blocking = [
        dict(item) for item in anomalies
        if str(item.get("severity") or "").lower() in BLOCKING_ANOMALY_SEVERITIES
    ]
    allocation = _withheld_allocation(target_id)
    confidence = "insufficient"
    proposal_status = "unavailable"
    counts: dict[str, Any] = {}
    statistics: dict[str, Any] = {}
    evidence_status = "unavailable"

    if not resource_knowledge_available:
        decision = "no_resource_knowledge"
        reason = "No derived resource knowledge exists for this task."
        next_action = "collect_successful_resource_evidence_or_run_bounded_benchmark"
    elif profile is None and source_selection == "no_matching_profile":
        decision = "no_matching_task_profile"
        evidence_status = "no_matching_profile"
        reason = "No resource profile matches the requested task profile key."
        next_action = "collect_matching_resource_evidence"
    elif profile is None and source_selection in {
        "multiple_external_source_profiles",
        "multiple_target_environment_profiles",
        "multiple_profiles_for_requested_source_environment",
    }:
        decision = "ambiguous_source_resource_profiles"
        evidence_status = "ambiguous_source_profiles"
        reason = "More than one source profile is eligible; GenomeAgent will not choose silently."
        next_action = "select_source_environment_explicitly"
    elif profile is None:
        decision = "no_matching_source_environment_profile"
        evidence_status = "no_matching_source_environment_profile"
        reason = "The requested source environment has no matching resource profile."
        next_action = "collect_matching_source_environment_evidence"
    else:
        confidence = str(profile.get("confidence") or "insufficient")
        counts = dict(profile.get("observation_counts", {}))
        raw_statistics = profile.get("successful_attempt_statistics", {})
        if isinstance(raw_statistics, Mapping):
            statistics = dict(raw_statistics)
        proposal = profile.get("proposed_resources", {})
        if not isinstance(proposal, Mapping):
            proposal = {}
        proposal_status = str(proposal.get("status") or "unavailable")
        successful = int(counts.get("successful", 0))
        if proposal_status == "proposal_available_for_researcher_review":
            evidence_status = "sufficient_for_source_environment_proposal"
        elif proposal_status == "insufficient_workload_diversity":
            evidence_status = "substantial_but_narrow"
        elif successful == 0:
            evidence_status = "present_without_successful_attempts"
        elif successful < 3:
            evidence_status = "limited_successful_evidence"
        else:
            evidence_status = "present_without_reviewable_proposal"
        if source_environment == "unknown":
            decision = "source_environment_unresolved"
            reason = "The resource profile cannot be assigned to a registered source environment."
            next_action = "register_source_environment_and_rebuild_resource_knowledge"
        elif proposal_status == "insufficient_workload_diversity":
            decision = "insufficient_transferable_evidence"
            reason = (
                "Substantial successful evidence exists, but it does not cover enough "
                "workload diversity for transfer to the target environment."
            )
            next_action = "collect_diverse_successful_resource_evidence"
        elif successful < 3:
            decision = "insufficient_successful_evidence"
            reason = "Fewer than three comparable successful attempts are available."
            next_action = "collect_more_successful_resource_evidence"
        elif proposal_status == "proposal_available_for_researcher_review" and blocking:
            decision = "blocked_by_resource_anomalies"
            reason = "A source proposal exists, but high-review anomalies block its use."
            next_action = "review_blocking_resource_anomalies"
        elif proposal_status == "proposal_available_for_researcher_review":
            same_environment = source_environment == target_id
            allocation = _allocation(
                proposal,
                target,
                source_environment,
                target_validated=same_environment,
            )
            if same_environment:
                decision = "target_environment_proposal_available"
                reason = "A reviewable proposal is supported by evidence from the target environment."
                next_action = "review_and_complete_scheduler_allocation"
            else:
                decision = "cross_environment_pilot_proposed"
                confidence = "low"
                reason = (
                    "A source-environment proposal is available, but it is not validated on "
                    "the target environment and may only seed a bounded pilot."
                )
                next_action = "review_and_run_bounded_target_environment_pilot"
        else:
            decision = "no_reviewable_resource_proposal"
            reason = "The resource policy has not produced a reviewable allocation proposal."
            next_action = "review_resource_profile_limitations"

    return {
        "schema_version": "1.0",
        "policy_version": RESOURCE_DECISION_POLICY_VERSION,
        "task": task,
        "profile_key": profile_key,
        "decision": decision,
        "confidence": confidence,
        "reason": reason,
        "source_environment": source_environment,
        "source_selection": source_selection,
        "target_environment": target_id,
        "target_environment_name": target.configuration.get("display_name", target_id),
        "resource_knowledge_available": bool(resource_knowledge_available),
        "evidence_status": evidence_status,
        "allocation_proposal_available": allocation.get("status") != "withheld",
        "source_profile": dict(profile or {}),
        "source_profile_proposal_status": proposal_status,
        "source_profile_observation_counts": counts,
        "source_profile_statistics": statistics,
        "relevant_anomalies": [dict(item) for item in anomalies],
        "blocking_anomaly_ids": [item.get("anomaly_id", "") for item in blocking],
        "proposed_allocation": allocation,
        "next_action": next_action,
        "requires_researcher_approval": True,
        "automatic_execution_allowed": False,
        "execution_enabled": False,
    }


def _readiness(
    decision: Mapping[str, Any],
    state: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    blockers = [
        "automatic_execution_disabled",
        "researcher_approval_required",
        "fresh_pre_execution_scan_required",
    ]
    decision_name = str(decision.get("decision") or "")
    allocation = decision.get("proposed_allocation", {})
    if decision_name not in ACTIONABLE_DECISIONS:
        blockers.append("resource_decision_not_actionable")
    if isinstance(allocation, Mapping) and allocation.get("missing_fields"):
        blockers.append("scheduler_allocation_incomplete")
    if decision.get("relevant_anomalies"):
        blockers.append("resource_anomaly_review_required")

    state_summary: dict[str, Any] = {
        "available": state is not None,
        "overall_status": "unknown",
        "source_scan_id": "",
        "observation_healthy_for_action_planning": False,
    }
    if state is None:
        blockers.append("current_task_state_missing")
    else:
        overall = str(state.get("overall_status") or "unknown")
        health = state.get("observation_health", {})
        if not isinstance(health, Mapping):
            health = {}
        source = state.get("source", {})
        if not isinstance(source, Mapping):
            source = {}
        healthy = bool(health.get("healthy_for_action_planning"))
        state_summary.update({
            "overall_status": overall,
            "source_scan_id": str(source.get("scan_id") or ""),
            "observation_healthy_for_action_planning": healthy,
        })
        if not healthy:
            blockers.append("task_observation_unhealthy")
        if overall in ACTIVE_TASK_STATUSES:
            blockers.append("active_workflow_scope_review_required")

    blockers = list(dict.fromkeys(blockers))
    return {
        "schema_version": "1.0",
        "policy_version": RESOURCE_DECISION_POLICY_VERSION,
        "task": decision.get("task", ""),
        "profile_key": decision.get("profile_key", ""),
        "target_environment": decision.get("target_environment", ""),
        "planning_status": (
            "resource_proposal_available_for_review"
            if decision_name in ACTIONABLE_DECISIONS
            else "resource_evidence_insufficient_or_blocked"
        ),
        "execution_status": "not_ready_for_execution",
        "automatic_execution_allowed": False,
        "researcher_approval_required": True,
        "fresh_pre_execution_scan_required": True,
        "blockers": blockers,
        "task_state": state_summary,
    }


def _report(
    decision: Mapping[str, Any],
    readiness: Mapping[str, Any],
) -> str:
    allocation = decision.get("proposed_allocation", {})
    if not isinstance(allocation, Mapping):
        allocation = {}
    counts = decision.get("source_profile_observation_counts", {})
    if not isinstance(counts, Mapping):
        counts = {}
    statistics = decision.get("source_profile_statistics", {})
    if not isinstance(statistics, Mapping):
        statistics = {}
    elapsed = statistics.get("elapsed_seconds", {})
    if not isinstance(elapsed, Mapping):
        elapsed = {}
    memory = statistics.get("max_rss_bytes", {})
    if not isinstance(memory, Mapping):
        memory = {}
    lines = [
        "# GenomeAgent Resource Decision: {}".format(decision.get("task", "")),
        "",
        "- Profile: `{}`".format(decision.get("profile_key", "")),
        "- Source environment: `{}`".format(decision.get("source_environment", "unknown")),
        "- Target environment: `{}`".format(decision.get("target_environment", "")),
        "- Decision: **{}**".format(decision.get("decision", "")),
        "- Confidence: **{}**".format(decision.get("confidence", "")),
        "- Evidence status: **{}**".format(decision.get("evidence_status", "")),
        "- Allocation proposal available: **{}**".format(
            "yes" if decision.get("allocation_proposal_available") else "no"
        ),
        "- Automatic execution allowed: **no**",
        "",
        "## Interpretation",
        "",
        str(decision.get("reason") or ""),
        "",
        "## Source evidence",
        "",
        "| Attempts | Successful | Timeouts | OOM | Failed | Cancelled |",
        "|---:|---:|---:|---:|---:|---:|",
        "| {} | {} | {} | {} | {} | {} |".format(
            counts.get("attempts", 0),
            counts.get("successful", 0),
            counts.get("timeout", 0),
            counts.get("out_of_memory", 0),
            counts.get("failed", 0),
            counts.get("cancelled", 0),
        ),
        "",
        "| Observed successful metric | p50 | p95 |",
        "|---|---:|---:|",
        "| Elapsed seconds | {} | {} |".format(
            elapsed.get("p50"), elapsed.get("p95")
        ),
        "| Peak RSS bytes | {} | {} |".format(
            memory.get("p50"), memory.get("p95")
        ),
        "",
        "## Proposed allocation",
        "",
    ]
    if allocation.get("status") == "withheld":
        lines.append("No allocation values are proposed because the evidence gate did not pass.")
    else:
        lines.extend([
            "| Field | Value |",
            "|---|---|",
            "| Intended use | {} |".format(allocation.get("intended_use", "")),
            "| Memory bytes | {} |".format(allocation.get("memory_bytes")),
            "| Time limit seconds | {} |".format(allocation.get("time_limit_seconds")),
            "| CPUs per task | {} |".format(allocation.get("cpus_per_task")),
            "| Partition | {} |".format(allocation.get("partition")),
            "| Target validated | {} |".format(
                "yes" if allocation.get("target_validated") else "no"
            ),
        ])
    lines.extend([
        "",
        "## Execution readiness",
        "",
        "- Status: **{}**".format(readiness.get("execution_status", "")),
        "- Next action: `{}`".format(decision.get("next_action", "")),
        "- Blockers: {}".format(
            ", ".join(str(item) for item in readiness.get("blockers", []))
        ),
        "",
        "## Safety boundary",
        "",
        "This plan was derived locally. It performs no SSH access, job submission, job "
        "cancellation or scheduler modification. Cross-environment values are pilot-only "
        "until successful target-environment evidence is collected and reviewed.",
        "",
    ])
    return "\n".join(lines)


class ResourceDecisionCore:
    """Convert resource knowledge into non-executable environment-aware plans."""

    schema_version = "1.0"

    def __init__(
        self,
        resource_root: Path = Path("workspace/task_resources"),
        state_root: Path = Path("workspace/task_state"),
        environment_root: Path = Path("config/environments"),
        plan_root: Path = Path("workspace/task_plans"),
    ):
        self.resource_root = Path(resource_root)
        self.state_root = Path(state_root)
        self.environment_root = Path(environment_root)
        self.plan_root = Path(plan_root)

    def plan_resources(
        self,
        task: str,
        target_environment: str,
        profile_key: Optional[str] = None,
        source_environment: Optional[str] = None,
    ) -> ResourceDecisionResult:
        task = _task(task)
        target_id = _identifier(target_environment, "target environment")
        environments = _load_environments(self.environment_root)
        if target_id not in environments:
            raise ResourceDecisionError(
                "Target environment {!r} is not registered under {}.".format(
                    target_id, self.environment_root
                )
            )
        target = environments[target_id]
        source_id = None
        if source_environment is not None:
            source_id = _identifier(source_environment, "source environment")
            if source_id not in environments:
                raise ResourceDecisionError(
                    "Source environment {!r} is not registered under {}.".format(
                        source_id, self.environment_root
                    )
                )

        resource_dir = self.resource_root / task
        profile_path = resource_dir / "resource_profiles.json"
        anomaly_path = resource_dir / "resource_anomalies.json"
        provenance_path = resource_dir / "provenance.json"
        profile_document, profile_digest = _optional_json(
            profile_path, "resource profile document"
        )
        anomaly_document, anomaly_digest = _optional_json(
            anomaly_path, "resource anomaly document"
        )
        provenance_document, provenance_digest = _optional_json(
            provenance_path, "resource provenance document"
        )
        for document, label in (
            (profile_document, "resource profiles"),
            (anomaly_document, "resource anomalies"),
            (provenance_document, "resource provenance"),
        ):
            if document is not None and document.get("task") != task:
                raise ResourceDecisionError(
                    "{} task mismatch: expected {}, found {}.".format(
                        label, task, document.get("task")
                    )
                )

        profiles_raw = (profile_document or {}).get("profiles", [])
        if not isinstance(profiles_raw, list) or not all(
            isinstance(item, Mapping) for item in profiles_raw
        ):
            raise ResourceDecisionError("resource_profiles.json has an invalid profiles list.")
        selected_key, candidates = _profile_candidates(profiles_raw, profile_key)
        selected, selected_source, selection_reason = _select_source_profile(
            candidates,
            target_id,
            source_id,
            environments,
        )

        anomalies_raw = (anomaly_document or {}).get("anomalies", [])
        if not isinstance(anomalies_raw, list) or not all(
            isinstance(item, Mapping) for item in anomalies_raw
        ):
            raise ResourceDecisionError("resource_anomalies.json has an invalid anomalies list.")
        relevant_anomalies = []
        if selected is not None:
            selected_host = str(selected.get("source_host") or "unknown")
            relevant_anomalies = [
                dict(item) for item in anomalies_raw
                if str(item.get("profile_key") or "") == selected_key
                and str(item.get("source_host") or "unknown") == selected_host
            ]

        resource_knowledge_available = profile_document is not None and bool(profiles_raw)
        decision = _decision(
            task,
            selected_key,
            selected,
            selected_source,
            selection_reason,
            target,
            relevant_anomalies,
            resource_knowledge_available,
        )

        state_path = self.state_root / task / "current_state.json"
        state_document, state_digest = _optional_json(state_path, "current task state")
        if state_document is not None and state_document.get("task") != task:
            raise ResourceDecisionError(
                "Current task state mismatch: expected {}, found {}.".format(
                    task, state_document.get("task")
                )
            )
        readiness = _readiness(decision, state_document)

        safe_profile = re.sub(r"[^A-Za-z0-9._-]+", "_", selected_key).strip("_")
        plan_dir = self.plan_root / task / target_id / (safe_profile or "unresolved")
        decision_path = plan_dir / "resource_decision.json"
        readiness_path = plan_dir / "execution_readiness.json"
        report_path = plan_dir / "report.md"
        provenance_output_path = plan_dir / "provenance.json"
        environment_sources = [{
            "environment_id": item.environment_id,
            "path": str(item.path),
            "sha256": item.digest,
        } for item in sorted(environments.values(), key=lambda value: value.environment_id)]
        provenance = {
            "schema_version": self.schema_version,
            "resource_decision_policy_version": RESOURCE_DECISION_POLICY_VERSION,
            "task": task,
            "profile_key": selected_key,
            "source_environment": selected_source,
            "target_environment": target_id,
            "source_mode": "local_deterministic_resource_knowledge_transfer",
            "inputs": {
                "resource_profiles": {
                    "path": str(profile_path),
                    "sha256": profile_digest,
                    "available": profile_document is not None,
                },
                "resource_anomalies": {
                    "path": str(anomaly_path),
                    "sha256": anomaly_digest,
                    "available": anomaly_document is not None,
                },
                "resource_provenance": {
                    "path": str(provenance_path),
                    "sha256": provenance_digest,
                    "available": provenance_document is not None,
                },
                "current_task_state": {
                    "path": str(state_path),
                    "sha256": state_digest,
                    "available": state_document is not None,
                },
                "environment_configurations": environment_sources,
            },
            "derivation_identity_sha256": _sha256(_canonical_bytes({
                "decision": decision,
                "readiness": readiness,
                "input_digests": {
                    "profiles": profile_digest,
                    "anomalies": anomaly_digest,
                    "resource_provenance": provenance_digest,
                    "state": state_digest,
                    "environments": environment_sources,
                },
                "policy_version": RESOURCE_DECISION_POLICY_VERSION,
            })),
            "safety": {
                "remote_access": False,
                "remote_writes": False,
                "job_submission": False,
                "job_cancellation": False,
                "automatic_scheduler_changes": False,
                "automatic_execution_allowed": False,
            },
        }

        _atomic_write(decision_path, _pretty_json(decision))
        _atomic_write(readiness_path, _pretty_json(readiness))
        _atomic_write(report_path, _report(decision, readiness))
        _atomic_write(provenance_output_path, _pretty_json(provenance))

        return ResourceDecisionResult(
            task=task,
            profile_key=selected_key,
            source_environment=selected_source,
            target_environment=target_id,
            decision=str(decision["decision"]),
            confidence=str(decision["confidence"]),
            plan_dir=plan_dir,
            artifact_paths=(
                decision_path,
                readiness_path,
                report_path,
                provenance_output_path,
            ),
        )
