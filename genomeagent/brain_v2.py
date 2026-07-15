#!/usr/bin/env python3
"""Deterministic knowledge promotion and environment-aware workflow transfer."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from genomeagent.resource_decision import ACTIONABLE_DECISIONS
from genomeagent.task_state import SUPPORTED_TASKS


BRAIN_POLICY_VERSION = "2.0"
WORKFLOW_TRANSFER_POLICY_VERSION = "1.0"


class BrainV2Error(RuntimeError):
    """Raised when knowledge cannot be promoted without ambiguity."""


@dataclass(frozen=True)
class KnowledgePromotionResult:
    task: str
    workflow_id: str
    snapshot_id: str
    promoted_claims: int
    candidate_claims: int
    knowledge_dir: Path
    snapshot_created: bool
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True)
class WorkflowTransferResult:
    workflow_id: str
    task: str
    target_environment: str
    status: str
    transfer_dir: Path
    artifact_paths: tuple[Path, ...]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


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


def _write_immutable(path: Path, text: str) -> bool:
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise BrainV2Error(
                "Immutable knowledge snapshot already exists with different content: {}".format(
                    path
                )
            )
        return False
    _atomic_write(path, text)
    return True


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise BrainV2Error("Unsafe or empty {}: {!r}".format(label, value))
    return normalized


def _task(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in SUPPORTED_TASKS:
        raise BrainV2Error(
            "Unsupported task {!r}; expected one of {}".format(
                value, sorted(SUPPORTED_TASKS)
            )
        )
    return normalized


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise BrainV2Error("{} does not exist: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrainV2Error("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise BrainV2Error("{} must be a JSON object: {}".format(label, path))
    return value, raw


def _optional_json(path: Path, label: str) -> tuple[Optional[dict[str, Any]], bytes]:
    if not path.exists():
        return None, b""
    return _read_json(path, label)


def _evidence(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "path": str(path),
        "sha256": _sha256_bytes(raw),
    }


def _claim(
    knowledge_class: str,
    subject_type: str,
    subject_id: str,
    predicate: str,
    value: Any,
    scope: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    confidence: str = "deterministic",
    status: str = "promoted_deterministic",
) -> dict[str, Any]:
    identity = {
        "knowledge_class": knowledge_class,
        "subject": {"type": subject_type, "id": subject_id},
        "predicate": predicate,
        "scope": dict(scope),
    }
    version = {
        **identity,
        "value": value,
        "evidence": [dict(item) for item in evidence],
        "confidence": confidence,
        "status": status,
    }
    return {
        "schema_version": "1.0",
        "claim_id": _sha256_value(identity),
        "claim_version_id": _sha256_value(version),
        **version,
    }


def _workflow_configuration(path: Path) -> tuple[dict[str, Any], bytes]:
    value, raw = _read_json(path, "workflow template")
    workflow_id = _identifier(value.get("workflow_id"), "workflow_id")
    if path.stem != workflow_id:
        raise BrainV2Error(
            "Workflow filename {} must match workflow_id {}.".format(
                path.name, workflow_id
            )
        )
    _task(str(value.get("task") or ""))
    if not str(value.get("resource_profile_key") or "").strip():
        raise BrainV2Error(
            "Workflow {} requires resource_profile_key.".format(workflow_id)
        )
    requirements = value.get("requirements", {})
    if not isinstance(requirements, Mapping):
        raise BrainV2Error("Workflow requirements must be an object: {}".format(path))
    for key in ("software", "environment_bindings"):
        items = requirements.get(key, [])
        if not isinstance(items, list):
            raise BrainV2Error("Workflow requirements.{} must be a list.".format(key))
    workflow_parameters = value.get("workflow_parameters", [])
    if not isinstance(workflow_parameters, list):
        raise BrainV2Error("workflow_parameters must be a list: {}".format(path))
    parameter_sets = value.get("source_environment_parameters", {})
    if not isinstance(parameter_sets, Mapping):
        raise BrainV2Error(
            "source_environment_parameters must be an object: {}".format(path)
        )
    validation = value.get("validation_contract", {})
    if not isinstance(validation, Mapping) or not validation:
        raise BrainV2Error("Workflow requires a validation_contract: {}".format(path))
    execution = value.get("execution_policy", {})
    if not isinstance(execution, Mapping):
        raise BrainV2Error("Workflow requires an execution_policy: {}".format(path))
    if execution.get("automatic_execution_allowed") is not False:
        raise BrainV2Error(
            "Workflow must explicitly disable automatic execution: {}".format(path)
        )
    if execution.get("researcher_approval_required") is not True:
        raise BrainV2Error(
            "Workflow must explicitly require researcher approval: {}".format(path)
        )
    return value, raw


def _environment_documents(root: Path) -> list[tuple[Path, dict[str, Any], bytes]]:
    records = []
    for path in sorted(root.glob("*.json")):
        value, raw = _read_json(path, "environment configuration")
        environment_id = _identifier(value.get("environment_id"), "environment_id")
        if path.stem != environment_id:
            raise BrainV2Error(
                "Environment filename {} must match environment_id {}.".format(
                    path.name, environment_id
                )
            )
        records.append((path, value, raw))
    if not records:
        raise BrainV2Error("No environment configurations found under {}.".format(root))
    return records


def _jsonl(items: Iterable[Mapping[str, Any]]) -> str:
    return "".join(
        json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n"
        for item in items
    )


def _state_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    source = state.get("source", {})
    if not isinstance(source, Mapping):
        source = {}
    health = state.get("observation_health", {})
    if not isinstance(health, Mapping):
        health = {}
    return {
        "overall_status": state.get("overall_status", "unknown"),
        "current_stage": state.get("current_stage", "unknown"),
        "next_safe_action": state.get("next_safe_action", "unknown"),
        "unit_state_counts": dict(state.get("unit_state_counts", {})),
        "source_scan_id": source.get("scan_id", ""),
        "observation_healthy_for_action_planning": bool(
            health.get("healthy_for_action_planning")
        ),
    }


def _snapshot_order_key(state: Mapping[str, Any], snapshot_id: str) -> str:
    source = state.get("source", {})
    if isinstance(source, Mapping) and source.get("scan_id"):
        return "{}:{}".format(source.get("scan_id"), snapshot_id)
    return snapshot_id


def _knowledge_report(document: Mapping[str, Any]) -> str:
    promoted = document.get("promoted_claims", [])
    candidates = document.get("candidate_claims", [])
    classes: dict[str, int] = {}
    for item in promoted:
        key = str(item.get("knowledge_class") or "unknown")
        classes[key] = classes.get(key, 0) + 1
    lines = [
        "# GenomeAgent Brain v2 Knowledge: {}".format(document.get("task", "")),
        "",
        "- Workflow: `{}`".format(document.get("workflow_id", "")),
        "- Snapshot: `{}`".format(document.get("snapshot_id", "")),
        "- Deterministically promoted claims: {}".format(len(promoted)),
        "- Review-required candidate claims: {}".format(len(candidates)),
        "- Automatic execution allowed: **no**",
        "",
        "## Promoted knowledge",
        "",
        "| Knowledge class | Claims |",
        "|---|---:|",
    ]
    for key in sorted(classes):
        lines.append("| {} | {} |".format(key, classes[key]))
    if not classes:
        lines.append("| none | 0 |")
    lines.extend([
        "",
        "## Promotion boundary",
        "",
        "Only structured facts produced by deterministic GenomeAgent components are "
        "promoted automatically. Legacy Brain v1 or other AI-derived project knowledge "
        "is retained as a candidate and requires explicit researcher review before it "
        "can become authoritative knowledge.",
        "",
        "Knowledge promotion does not contact a cluster, modify a scheduler, submit jobs "
        "or authorize execution.",
        "",
    ])
    return "\n".join(lines)


class BrainKnowledgeCore:
    """Promote deterministic operational artifacts into replayable knowledge."""

    def __init__(
        self,
        state_root: Path = Path("workspace/task_state"),
        resource_root: Path = Path("workspace/task_resources"),
        plan_root: Path = Path("workspace/task_plans"),
        workflow_root: Path = Path("config/workflows"),
        environment_root: Path = Path("config/environments"),
        knowledge_root: Path = Path("workspace/brain_knowledge"),
        legacy_knowledge_path: Path = Path("workspace/project_knowledge.json"),
    ):
        self.state_root = Path(state_root)
        self.resource_root = Path(resource_root)
        self.plan_root = Path(plan_root)
        self.workflow_root = Path(workflow_root)
        self.environment_root = Path(environment_root)
        self.knowledge_root = Path(knowledge_root)
        self.legacy_knowledge_path = Path(legacy_knowledge_path)

    def ingest(self, task: str, workflow_id: Optional[str] = None) -> KnowledgePromotionResult:
        task = _task(task)
        selected_workflow = _identifier(workflow_id or task, "workflow_id")
        workflow_path = self.workflow_root / (selected_workflow + ".json")
        workflow, workflow_raw = _workflow_configuration(workflow_path)
        if workflow.get("task") != task:
            raise BrainV2Error(
                "Workflow {} belongs to task {}, not {}.".format(
                    selected_workflow, workflow.get("task"), task
                )
            )

        state_path = self.state_root / task / "current_state.json"
        state, state_raw = _read_json(state_path, "current task state")
        if state.get("task") != task:
            raise BrainV2Error("Current task state belongs to another task.")

        inputs = [
            _evidence(state_path, state_raw, "task_state"),
            _evidence(workflow_path, workflow_raw, "workflow_template"),
        ]
        promoted = [
            _claim(
                "operational_state",
                "task",
                task,
                "has_current_operational_state",
                _state_summary(state),
                {"task": task},
                [inputs[0]],
            ),
            _claim(
                "workflow_template",
                "workflow",
                selected_workflow,
                "has_versioned_template",
                workflow,
                {"task": task, "workflow_id": selected_workflow},
                [inputs[1]],
            ),
        ]

        profile_path = self.resource_root / task / "resource_profiles.json"
        profiles, profiles_raw = _optional_json(profile_path, "resource profiles")
        if profiles is not None:
            if profiles.get("task") != task:
                raise BrainV2Error("Resource profiles belong to another task.")
            item_evidence = _evidence(profile_path, profiles_raw, "resource_profiles")
            inputs.append(item_evidence)
            for profile in profiles.get("profiles", []):
                if not isinstance(profile, Mapping):
                    raise BrainV2Error("Resource profile entries must be objects.")
                profile_key = str(profile.get("profile_key") or "unresolved")
                source_host = str(profile.get("source_host") or "unknown")
                promoted.append(_claim(
                    "resource_profile",
                    "resource_profile",
                    "{}:{}".format(source_host, profile_key),
                    "has_empirical_resource_profile",
                    dict(profile),
                    {
                        "task": task,
                        "profile_key": profile_key,
                        "source_host": source_host,
                    },
                    [item_evidence],
                ))

        anomaly_path = self.resource_root / task / "resource_anomalies.json"
        anomalies, anomalies_raw = _optional_json(anomaly_path, "resource anomalies")
        if anomalies is not None:
            item_evidence = _evidence(anomaly_path, anomalies_raw, "resource_anomalies")
            inputs.append(item_evidence)
            for anomaly in anomalies.get("anomalies", []):
                if not isinstance(anomaly, Mapping):
                    raise BrainV2Error("Resource anomaly entries must be objects.")
                anomaly_id = str(anomaly.get("anomaly_id") or _sha256_value(anomaly))
                promoted.append(_claim(
                    "resource_anomaly",
                    "resource_anomaly",
                    anomaly_id,
                    "has_deterministic_anomaly_flag",
                    dict(anomaly),
                    {"task": task},
                    [item_evidence],
                ))

        decision_root = self.plan_root / task
        for decision_path in sorted(decision_root.glob("*/*/resource_decision.json")):
            decision, raw = _read_json(decision_path, "resource decision")
            if decision.get("task") != task:
                raise BrainV2Error("Resource decision belongs to another task: {}".format(decision_path))
            item_evidence = _evidence(decision_path, raw, "resource_decision")
            decision_evidence = [item_evidence]
            inputs.append(item_evidence)
            decision_provenance_path = decision_path.with_name("provenance.json")
            decision_provenance, decision_provenance_raw = _optional_json(
                decision_provenance_path, "resource decision provenance"
            )
            if decision_provenance is not None:
                provenance_evidence = _evidence(
                    decision_provenance_path,
                    decision_provenance_raw,
                    "resource_decision_provenance",
                )
                inputs.append(provenance_evidence)
                decision_evidence.append(provenance_evidence)
            target = str(decision.get("target_environment") or "unknown")
            profile_key = str(decision.get("profile_key") or "unresolved")
            promoted.append(_claim(
                "resource_decision",
                "resource_transfer",
                "{}:{}:{}".format(task, target, profile_key),
                "has_current_resource_transfer_decision",
                decision,
                {
                    "task": task,
                    "target_environment": target,
                    "profile_key": profile_key,
                },
                decision_evidence,
            ))

        for path, environment, raw in _environment_documents(self.environment_root):
            item_evidence = _evidence(path, raw, "environment_configuration")
            inputs.append(item_evidence)
            environment_id = str(environment.get("environment_id"))
            promoted.append(_claim(
                "environment_record",
                "environment",
                environment_id,
                "has_registered_environment_record",
                environment,
                {"environment_id": environment_id},
                [item_evidence],
            ))

        candidates = []
        legacy, legacy_raw = _optional_json(
            self.legacy_knowledge_path, "legacy Brain v1 knowledge"
        )
        if legacy is not None:
            item_evidence = _evidence(
                self.legacy_knowledge_path, legacy_raw, "legacy_ai_knowledge"
            )
            inputs.append(item_evidence)
            for key in sorted(legacy):
                candidates.append(_claim(
                    "legacy_ai_candidate",
                    "project",
                    str(legacy.get("project_name") or "GenomeAgent project"),
                    "candidate_{}".format(key),
                    legacy[key],
                    {"task": task, "source": "brain_v1"},
                    [item_evidence],
                    confidence="unverified_ai_derived",
                    status="candidate_requires_researcher_review",
                ))

        inputs = sorted(inputs, key=lambda item: (item["artifact_type"], item["path"]))
        promoted = sorted(promoted, key=lambda item: item["claim_version_id"])
        candidates = sorted(candidates, key=lambda item: item["claim_version_id"])
        snapshot_identity = {
            "policy_version": BRAIN_POLICY_VERSION,
            "task": task,
            "workflow_id": selected_workflow,
            "source_artifacts": inputs,
        }
        snapshot_id = _sha256_value(snapshot_identity)
        snapshot = {
            "schema_version": "1.0",
            "brain_version": BRAIN_POLICY_VERSION,
            "snapshot_id": snapshot_id,
            "snapshot_order_key": _snapshot_order_key(state, snapshot_id),
            "task": task,
            "workflow_id": selected_workflow,
            "source_artifacts": inputs,
            "promoted_claims": promoted,
            "candidate_claims": candidates,
            "automatic_execution_allowed": False,
            "knowledge_promotion_requires_deterministic_source": True,
            "ai_candidates_require_researcher_review": True,
        }

        knowledge_dir = self.knowledge_root / task
        snapshot_path = knowledge_dir / "snapshots" / (snapshot_id + ".json")
        snapshot_created = _write_immutable(snapshot_path, _pretty_json(snapshot))

        snapshots = []
        for path in sorted((knowledge_dir / "snapshots").glob("*.json")):
            document, _ = _read_json(path, "knowledge snapshot")
            snapshots.append(document)
        snapshots.sort(key=lambda item: (
            str(item.get("snapshot_order_key") or ""),
            str(item.get("snapshot_id") or ""),
        ))
        # The snapshot selected by this ingest is the authoritative current input
        # even when several derived artifacts changed under the same Task Scanner ID.
        # Historical ordering is used only to enumerate superseded versions.
        snapshots = [
            item for item in snapshots if item.get("snapshot_id") != snapshot_id
        ] + [snapshot]

        promoted_versions: dict[str, dict[str, Any]] = {}
        candidate_versions: dict[str, dict[str, Any]] = {}
        current_by_claim: dict[str, dict[str, Any]] = {}
        prior_versions: dict[str, list[str]] = {}
        for document in snapshots:
            for item in document.get("promoted_claims", []):
                version_id = str(item["claim_version_id"])
                promoted_versions[version_id] = dict(item)
                claim_id = str(item["claim_id"])
                previous = current_by_claim.get(claim_id)
                if previous and previous["claim_version_id"] != version_id:
                    prior_versions.setdefault(claim_id, []).append(
                        str(previous["claim_version_id"])
                    )
                current_by_claim[claim_id] = dict(item)
            for item in document.get("candidate_claims", []):
                candidate_versions[str(item["claim_version_id"])] = dict(item)

        current_claims = []
        for claim_id in sorted(current_by_claim):
            item = dict(current_by_claim[claim_id])
            item["supersedes_claim_versions"] = list(dict.fromkeys(
                prior_versions.get(claim_id, [])
            ))
            current_claims.append(item)

        current = {
            "schema_version": "1.0",
            "brain_version": BRAIN_POLICY_VERSION,
            "task": task,
            "workflow_id": selected_workflow,
            "current_snapshot_id": snapshot_id,
            "snapshot_count": len(snapshots),
            "claims": current_claims,
            "candidate_claims": candidates,
            "automatic_execution_allowed": False,
        }
        provenance = {
            "schema_version": "1.0",
            "brain_version": BRAIN_POLICY_VERSION,
            "task": task,
            "current_snapshot_id": snapshot_id,
            "source_artifacts": inputs,
            "immutable_snapshot_sha256": _sha256_bytes(snapshot_path.read_bytes()),
            "derivation": "deterministic_local_replay",
            "remote_access": False,
            "execution_enabled": False,
        }
        report_document = dict(snapshot)
        report_path = knowledge_dir / "report.md"
        paths = (
            knowledge_dir / "current_knowledge.json",
            knowledge_dir / "claims.jsonl",
            knowledge_dir / "candidate_claims.jsonl",
            report_path,
            knowledge_dir / "provenance.json",
            snapshot_path,
        )
        _atomic_write(paths[0], _pretty_json(current))
        _atomic_write(paths[1], _jsonl(
            promoted_versions[key] for key in sorted(promoted_versions)
        ))
        _atomic_write(paths[2], _jsonl(
            candidate_versions[key] for key in sorted(candidate_versions)
        ))
        _atomic_write(report_path, _knowledge_report(report_document))
        _atomic_write(paths[4], _pretty_json(provenance))
        return KnowledgePromotionResult(
            task=task,
            workflow_id=selected_workflow,
            snapshot_id=snapshot_id,
            promoted_claims=len(promoted),
            candidate_claims=len(candidates),
            knowledge_dir=knowledge_dir,
            snapshot_created=snapshot_created,
            artifact_paths=paths,
        )


def _software_capabilities(environment: Mapping[str, Any]) -> Mapping[str, Any]:
    capabilities = environment.get("capabilities", {})
    if not isinstance(capabilities, Mapping):
        return {}
    software = capabilities.get("software", {})
    return software if isinstance(software, Mapping) else {}


def _binding_capabilities(environment: Mapping[str, Any]) -> Mapping[str, Any]:
    capabilities = environment.get("capabilities", {})
    if not isinstance(capabilities, Mapping):
        return {}
    bindings = capabilities.get("bindings", {})
    return bindings if isinstance(bindings, Mapping) else {}


def _compatibility_rows(
    workflow: Mapping[str, Any], environment: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows = []
    expected_scheduler = str(workflow.get("scheduler") or "")
    observed_scheduler = str(environment.get("scheduler") or "")
    rows.append({
        "requirement_type": "scheduler",
        "requirement": expected_scheduler,
        "expected": expected_scheduler,
        "observed": observed_scheduler or "unknown",
        "status": "compatible" if observed_scheduler == expected_scheduler else "incompatible",
        "evidence": "registered_environment_configuration",
    })
    requirements = workflow.get("requirements", {})
    if not isinstance(requirements, Mapping):
        requirements = {}
    software = _software_capabilities(environment)
    for required in requirements.get("software", []):
        if not isinstance(required, Mapping):
            raise BrainV2Error("Workflow software requirements must be objects.")
        name = str(required.get("name") or "")
        version = str(required.get("version") or "")
        comparison = str(required.get("comparison") or "exact")
        capability = software.get(name, {})
        if not isinstance(capability, Mapping):
            capability = {}
        status = str(capability.get("status") or "unknown")
        versions = capability.get("versions", [])
        if not isinstance(versions, list):
            versions = []
        if status not in {"verified", "observed"}:
            result = "unknown"
        elif version:
            observed_versions = [str(item) for item in versions]
            if comparison == "compatible_minor":
                matches = any(
                    item == version or item.startswith(version + ".")
                    for item in observed_versions
                )
            else:
                matches = version in observed_versions
            result = "compatible" if matches else "incompatible"
        else:
            result = "compatible"
        rows.append({
            "requirement_type": "software",
            "requirement": name,
            "expected": version or "any verified version",
            "observed": ",".join(str(item) for item in versions) or "unknown",
            "status": result,
            "evidence": str(capability.get("evidence") or "not_registered"),
        })
    bindings = _binding_capabilities(environment)
    for name in requirements.get("environment_bindings", []):
        key = str(name)
        capability = bindings.get(key, {})
        if not isinstance(capability, Mapping):
            capability = {}
        observed_status = str(capability.get("status") or "unknown")
        result = "compatible" if observed_status in {"verified", "observed"} else "unknown"
        rows.append({
            "requirement_type": "environment_binding",
            "requirement": key,
            "expected": "verified target binding",
            "observed": str(capability.get("value") or "unknown"),
            "status": result,
            "evidence": str(capability.get("evidence") or "not_registered"),
        })
    environment_id = str(environment.get("environment_id") or "unknown")
    parameter_sets = workflow.get("source_environment_parameters", {})
    if not isinstance(parameter_sets, Mapping):
        parameter_sets = {}
    target_parameters = parameter_sets.get(environment_id, {})
    if not isinstance(target_parameters, Mapping):
        target_parameters = {}
    workflow_parameters = workflow.get("workflow_parameters", [])
    if not isinstance(workflow_parameters, list):
        raise BrainV2Error("workflow_parameters must be a list.")
    for name in workflow_parameters:
        key = str(name)
        value = target_parameters.get(key)
        available = value is not None and value != "" and value != [] and value != {}
        rows.append({
            "requirement_type": "workflow_parameter",
            "requirement": key,
            "expected": "explicit target value",
            "observed": json.dumps(value, sort_keys=True) if available else "unknown",
            "status": "compatible" if available else "unknown",
            "evidence": (
                "versioned_workflow_template" if available else "not_registered_for_target"
            ),
        })
    return rows


def _compatibility_tsv(rows: Sequence[Mapping[str, Any]]) -> str:
    columns = [
        "requirement_type", "requirement", "expected", "observed", "status", "evidence"
    ]
    values = ["\t".join(columns)]
    for row in rows:
        values.append("\t".join(
            str(row.get(column, "")).replace("\t", " ").replace("\n", " ")
            for column in columns
        ))
    return "\n".join(values) + "\n"


def _resource_decision_freshness(
    provenance: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    if provenance is None:
        return {
            "status": "unverifiable",
            "checked_inputs": 0,
            "stale_inputs": [],
            "reason": "resource_decision_provenance_missing",
        }
    inputs = provenance.get("inputs", {})
    if not isinstance(inputs, Mapping):
        return {
            "status": "unverifiable",
            "checked_inputs": 0,
            "stale_inputs": [],
            "reason": "resource_decision_provenance_inputs_invalid",
        }
    records = []
    for name, item in inputs.items():
        values = item if isinstance(item, list) else [item]
        for value in values:
            if not isinstance(value, Mapping):
                continue
            if value.get("available") is False:
                continue
            path = str(value.get("path") or "")
            digest = str(value.get("sha256") or "")
            if path and digest:
                records.append((str(name), path, digest))
    if not records:
        return {
            "status": "unverifiable",
            "checked_inputs": 0,
            "stale_inputs": [],
            "reason": "resource_decision_provenance_has_no_verifiable_inputs",
        }
    stale = []
    for name, path_text, expected in records:
        path = Path(path_text)
        observed = _sha256_bytes(path.read_bytes()) if path.is_file() else "missing"
        if observed != expected:
            stale.append({
                "input": name,
                "path": path_text,
                "expected_sha256": expected,
                "observed_sha256": observed,
            })
    return {
        "status": "stale" if stale else "current",
        "checked_inputs": len(records),
        "stale_inputs": stale,
        "reason": "input_digest_mismatch" if stale else "all_recorded_input_digests_match",
    }


def _transfer_report(plan: Mapping[str, Any]) -> str:
    rows = plan.get("compatibility", [])
    lines = [
        "# GenomeAgent Workflow Transfer: {}".format(plan.get("workflow_id", "")),
        "",
        "- Target environment: `{}`".format(plan.get("target_environment", "")),
        "- Status: **{}**".format(plan.get("status", "")),
        "- Resource gate: `{}`".format(plan.get("resource_gate", {}).get("status", "")),
        "- Execution enabled: **no**",
        "",
        "## Compatibility",
        "",
        "| Type | Requirement | Expected | Observed | Status |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append("| {} | {} | {} | {} | {} |".format(
            row.get("requirement_type", ""),
            row.get("requirement", ""),
            row.get("expected", ""),
            row.get("observed", ""),
            row.get("status", ""),
        ))
    lines.extend([
        "",
        "## Blockers",
        "",
    ])
    blockers = plan.get("blockers", [])
    if blockers:
        lines.extend("- `{}`".format(item) for item in blockers)
    else:
        lines.append("No deterministic planning blockers were found.")
    lines.extend([
        "",
        "## Safety boundary",
        "",
        "This is a non-executable transfer proposal. It performs no SSH access, path "
        "creation, software installation or scheduler action. A future Execution Engine "
        "must still require researcher approval, a fresh observation and a bounded pilot "
        "when the resource profile originates in another environment.",
        "",
    ])
    return "\n".join(lines)


class WorkflowTransferCore:
    """Evaluate whether promoted knowledge can seed a workflow on a target environment."""

    def __init__(
        self,
        workflow_root: Path = Path("config/workflows"),
        environment_root: Path = Path("config/environments"),
        knowledge_root: Path = Path("workspace/brain_knowledge"),
        plan_root: Path = Path("workspace/task_plans"),
        transfer_root: Path = Path("workspace/workflow_transfers"),
    ):
        self.workflow_root = Path(workflow_root)
        self.environment_root = Path(environment_root)
        self.knowledge_root = Path(knowledge_root)
        self.plan_root = Path(plan_root)
        self.transfer_root = Path(transfer_root)

    def plan(self, workflow_id: str, target_environment: str) -> WorkflowTransferResult:
        workflow_id = _identifier(workflow_id, "workflow_id")
        target_id = _identifier(target_environment, "target_environment")
        workflow_path = self.workflow_root / (workflow_id + ".json")
        workflow, workflow_raw = _workflow_configuration(workflow_path)
        task = _task(str(workflow.get("task")))
        environment_path = self.environment_root / (target_id + ".json")
        environment, environment_raw = _read_json(
            environment_path, "target environment configuration"
        )
        if environment.get("environment_id") != target_id:
            raise BrainV2Error("Target environment ID does not match its filename.")

        knowledge_path = self.knowledge_root / task / "current_knowledge.json"
        knowledge, knowledge_raw = _optional_json(knowledge_path, "Brain v2 knowledge")
        profile_key = str(workflow.get("resource_profile_key"))
        decision_path = (
            self.plan_root / task / target_id / profile_key / "resource_decision.json"
        )
        decision, decision_raw = _optional_json(decision_path, "resource decision")
        decision_provenance_path = decision_path.with_name("provenance.json")
        decision_provenance, decision_provenance_raw = _optional_json(
            decision_provenance_path, "resource decision provenance"
        )
        rows = _compatibility_rows(workflow, environment)
        blockers = []
        unknown = [row for row in rows if row["status"] == "unknown"]
        incompatible = [row for row in rows if row["status"] == "incompatible"]
        if knowledge is None:
            blockers.append("brain_v2_knowledge_missing")
        if unknown:
            blockers.append("target_environment_requirements_unknown")
        if incompatible:
            blockers.append("target_environment_requirements_incompatible")

        if decision is None:
            resource_gate = {
                "status": "missing_resource_decision",
                "decision": "unknown",
                "allocation_proposal_available": False,
            }
            blockers.append("target_resource_decision_missing")
        else:
            decision_name = str(decision.get("decision") or "unknown")
            freshness = _resource_decision_freshness(decision_provenance)
            resource_gate = {
                "status": (
                    "passed_for_review"
                    if decision_name in ACTIONABLE_DECISIONS
                    and freshness["status"] == "current"
                    else "not_actionable"
                ),
                "decision": decision_name,
                "evidence_status": decision.get("evidence_status", "unknown"),
                "allocation_proposal_available": bool(
                    decision.get("allocation_proposal_available")
                ),
                "source_environment": decision.get("source_environment", "unknown"),
                "freshness": freshness,
            }
            if decision_name not in ACTIONABLE_DECISIONS:
                blockers.append("resource_decision_not_actionable")
            if freshness["status"] == "unverifiable":
                blockers.append("resource_decision_provenance_unverifiable")
            elif freshness["status"] == "stale":
                blockers.append("resource_decision_stale")

        if incompatible:
            status = "incompatible_target_environment"
        elif unknown:
            status = "insufficient_environment_knowledge"
        elif knowledge is None:
            status = "knowledge_promotion_required"
        elif resource_gate["status"] != "passed_for_review":
            status = "blocked_by_resource_decision"
        else:
            status = "workflow_transfer_proposal_available"

        source_artifacts = [
            _evidence(workflow_path, workflow_raw, "workflow_template"),
            _evidence(environment_path, environment_raw, "environment_configuration"),
        ]
        if knowledge is not None:
            source_artifacts.append(_evidence(
                knowledge_path, knowledge_raw, "brain_v2_current_knowledge"
            ))
        if decision is not None:
            source_artifacts.append(_evidence(
                decision_path, decision_raw, "resource_decision"
            ))
        if decision_provenance is not None:
            source_artifacts.append(_evidence(
                decision_provenance_path,
                decision_provenance_raw,
                "resource_decision_provenance",
            ))
        source_artifacts.sort(key=lambda item: (item["artifact_type"], item["path"]))
        plan = {
            "schema_version": "1.0",
            "policy_version": WORKFLOW_TRANSFER_POLICY_VERSION,
            "workflow_id": workflow_id,
            "workflow_version": workflow.get("workflow_version", "unknown"),
            "task": task,
            "target_environment": target_id,
            "status": status,
            "compatibility": rows,
            "resource_gate": resource_gate,
            "blockers": list(dict.fromkeys(blockers)),
            "validation_contract": workflow.get("validation_contract", {}),
            "source_artifacts": source_artifacts,
            "requires_researcher_approval": True,
            "fresh_pre_execution_observation_required": True,
            "automatic_execution_allowed": False,
            "execution_enabled": False,
        }
        provenance = {
            "schema_version": "1.0",
            "policy_version": WORKFLOW_TRANSFER_POLICY_VERSION,
            "workflow_id": workflow_id,
            "target_environment": target_id,
            "source_artifacts": source_artifacts,
            "transfer_plan_sha256": _sha256_value(plan),
            "derivation": "deterministic_local_compatibility_evaluation",
            "remote_access": False,
            "execution_enabled": False,
        }
        transfer_dir = self.transfer_root / workflow_id / target_id
        paths = (
            transfer_dir / "transfer_plan.json",
            transfer_dir / "compatibility_matrix.tsv",
            transfer_dir / "report.md",
            transfer_dir / "provenance.json",
        )
        _atomic_write(paths[0], _pretty_json(plan))
        _atomic_write(paths[1], _compatibility_tsv(rows))
        _atomic_write(paths[2], _transfer_report(plan))
        _atomic_write(paths[3], _pretty_json(provenance))
        return WorkflowTransferResult(
            workflow_id=workflow_id,
            task=task,
            target_environment=target_id,
            status=status,
            transfer_dir=transfer_dir,
            artifact_paths=paths,
        )
