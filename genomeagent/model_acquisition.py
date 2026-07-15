#!/usr/bin/env python3
"""Deterministic, non-executable planning for pinned AI model acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, Mapping, Optional

from genomeagent.ai_evaluation import AIRegistry


MODEL_ACQUISITION_POLICY_VERSION = "1.0"
SOURCE_PROVIDERS = {"huggingface_hub"}
LICENSE_REVIEW_STATUSES = {"unreviewed", "reviewed_accepted", "reviewed_rejected"}
REQUIRED_FALSE_SAFETY_FIELDS = (
    "remote_access_allowed",
    "remote_writes_allowed",
    "model_download_allowed",
    "model_import_allowed",
    "job_submission_allowed",
    "gpu_allocation_allowed",
    "large_file_hashing_allowed",
    "automatic_registry_update_allowed",
    "automatic_backend_activation_allowed",
)
DECIMAL_SIZE_MULTIPLIERS = {
    "": Decimal(1),
    "K": Decimal(1000),
    "M": Decimal(1000 ** 2),
    "G": Decimal(1000 ** 3),
    "T": Decimal(1000 ** 4),
    "P": Decimal(1000 ** 5),
}


class ModelAcquisitionError(RuntimeError):
    """Raised when an acquisition plan input or artifact is unsafe or inconsistent."""


@dataclass(frozen=True)
class ModelAcquisitionPlanResult:
    backend_id: str
    plan_id: str
    status: str
    next_safe_action: str
    blockers: tuple[str, ...]
    plan_dir: Path
    created: bool
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
            raise ModelAcquisitionError(
                "Immutable model acquisition artifact exists with different content: {}".format(
                    path
                )
            )
        return False
    _atomic_write(path, text)
    return True


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise ModelAcquisitionError("Unsafe or empty {}: {!r}".format(label, value))
    return normalized


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ModelAcquisitionError("{} does not exist: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelAcquisitionError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise ModelAcquisitionError("{} must be a JSON object: {}".format(label, path))
    return value, raw


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "path": str(path),
        "sha256": _sha256_bytes(raw),
    }


def _absolute_path(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or not Path(text).is_absolute() or "\x00" in text:
        raise ModelAcquisitionError("{} must be a safe absolute path.".format(label))
    if any(part in {"", ".", ".."} for part in Path(text).parts[1:]):
        raise ModelAcquisitionError("{} contains an unsafe path segment.".format(label))
    return text


def _optional_digest(value: Any, label: str, length: int = 64) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{{{}}}".format(length), normalized):
        raise ModelAcquisitionError("{} must be null or a {}-hex digest.".format(label, length))
    return normalized


def _positive_int(value: Any, label: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ModelAcquisitionError("{} must be an integer >= {}.".format(label, minimum))
    return value


def _validate_spec(
    path: Path,
    backend: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    spec, raw = _read_json(path, "model acquisition specification")
    backend_id = _identifier(spec.get("backend_id"), "backend_id")
    if path.stem != backend_id or backend_id != backend.get("backend_id"):
        raise ModelAcquisitionError(
            "Acquisition specification filename and backend_id must match the registry."
        )
    if spec.get("schema_version") != "1.0":
        raise ModelAcquisitionError("Unsupported model acquisition schema version.")
    if spec.get("policy_version") != MODEL_ACQUISITION_POLICY_VERSION:
        raise ModelAcquisitionError("Unsupported model acquisition policy version.")

    source = spec.get("source")
    target = spec.get("target")
    representation = spec.get("representation")
    storage = spec.get("storage_policy")
    integrity = spec.get("integrity_policy")
    approval = spec.get("approval_policy")
    safety = spec.get("safety")
    for value, label in (
        (source, "source"),
        (target, "target"),
        (representation, "representation"),
        (storage, "storage_policy"),
        (integrity, "integrity_policy"),
        (approval, "approval_policy"),
        (safety, "safety"),
    ):
        if not isinstance(value, Mapping):
            raise ModelAcquisitionError("{} must be an object.".format(label))

    provider = str(source.get("provider") or "").strip()
    if provider not in SOURCE_PROVIDERS:
        raise ModelAcquisitionError("Unsupported model source provider: {}".format(provider))
    repository = str(source.get("repository") or "").strip()
    backend_repository = str(backend.get("model", {}).get("repository") or "").strip()
    if repository != backend_repository or not re.fullmatch(
        r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository
    ):
        raise ModelAcquisitionError("Model source repository must match the backend registry.")
    revision = source.get("resolved_revision")
    if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", str(revision)):
        raise ModelAcquisitionError(
            "resolved_revision must be null or an immutable 40-hex commit."
        )
    _optional_digest(source.get("source_inventory_sha256"), "source_inventory_sha256")
    source_total = source.get("source_total_bytes")
    if source_total is not None:
        _positive_int(source_total, "source_total_bytes")
    license_identifier = str(source.get("license_identifier") or "").strip()
    if not license_identifier or license_identifier != str(
        backend.get("model", {}).get("license") or ""
    ):
        raise ModelAcquisitionError("Model license must match the backend registry.")
    review_status = str(source.get("license_review_status") or "")
    if review_status not in LICENSE_REVIEW_STATUSES:
        raise ModelAcquisitionError("Unsupported model license review status.")

    if target.get("environment_id") != backend.get("environment_id"):
        raise ModelAcquisitionError("Target environment must match the backend registry.")
    project_storage_root = _absolute_path(
        target.get("project_storage_root"), "project_storage_root"
    )
    installation_path = _absolute_path(
        target.get("installation_path"), "installation_path"
    )
    if installation_path != str(backend.get("installation", {}).get("path") or ""):
        raise ModelAcquisitionError("Installation path must match the backend registry.")
    try:
        Path(installation_path).relative_to(project_storage_root)
    except ValueError as exc:
        raise ModelAcquisitionError(
            "Installation path must be inside project_storage_root."
        ) from exc
    manifest_name = str(target.get("manifest_filename") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", manifest_name):
        raise ModelAcquisitionError("Unsafe model manifest filename.")
    if target.get("publication_strategy") != "atomic_directory_rename_same_filesystem":
        raise ModelAcquisitionError("Acquisition requires atomic same-filesystem publication.")

    precision = str(representation.get("precision") or "").strip()
    if precision != str(backend.get("resource_request", {}).get("dtype") or ""):
        raise ModelAcquisitionError("Model precision must match the backend resource request.")
    _positive_int(representation.get("bytes_per_parameter"), "bytes_per_parameter")
    _positive_int(
        storage.get("complete_copies_during_acquisition"),
        "complete_copies_during_acquisition",
    )
    headroom = storage.get("headroom_basis_points")
    if isinstance(headroom, bool) or not isinstance(headroom, int) or not 0 <= headroom <= 10000:
        raise ModelAcquisitionError("headroom_basis_points must be between 0 and 10000.")

    required_true = (
        "require_recursive_file_inventory",
        "require_sha256_for_all_regular_files",
        "require_revision_in_manifest",
        "reject_external_symlinks",
    )
    for field in required_true:
        if integrity.get(field) is not True:
            raise ModelAcquisitionError("Integrity policy must require {}.".format(field))
    if approval.get("researcher_approval_required") is not True:
        raise ModelAcquisitionError("Researcher approval must be required.")
    if approval.get("fresh_environment_evidence_required") is not True:
        raise ModelAcquisitionError("Fresh environment evidence must be required.")
    for field in REQUIRED_FALSE_SAFETY_FIELDS:
        if safety.get(field) is not False:
            raise ModelAcquisitionError(
                "Model acquisition planning must explicitly set {} to false.".format(field)
            )
    return spec, raw


def _theoretical_parameter_bytes(
    backend: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> Optional[int]:
    parameters = backend.get("model", {}).get("total_parameters_billions")
    if isinstance(parameters, bool) or not isinstance(parameters, (int, float)):
        return None
    bytes_per_parameter = int(spec["representation"]["bytes_per_parameter"])
    value = (
        Decimal(str(parameters))
        * Decimal(1_000_000_000)
        * Decimal(bytes_per_parameter)
    )
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _required_working_bytes(spec: Mapping[str, Any]) -> Optional[int]:
    source_bytes = spec["source"].get("source_total_bytes")
    if source_bytes is None:
        return None
    copies = int(spec["storage_policy"]["complete_copies_during_acquisition"])
    headroom = int(spec["storage_policy"]["headroom_basis_points"])
    numerator = int(source_bytes) * (copies * 10000 + headroom)
    return (numerator + 9999) // 10000


def _decimal_size_bytes(value: str) -> Optional[int]:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTP]?)", value.strip().upper())
    if not match:
        return None
    number = Decimal(match.group(1)) * DECIMAL_SIZE_MULTIPLIERS[match.group(2)]
    return int(number.to_integral_value(rounding=ROUND_CEILING))


def _project_quota_evidence(
    state: Optional[Mapping[str, Any]],
    project_storage_root: str,
) -> dict[str, Any]:
    result = {
        "status": "unverified",
        "source": "csc_workspaces_project_quota",
        "project_storage_root": project_storage_root,
        "used_bytes": None,
        "capacity_bytes": None,
        "available_bytes": None,
    }
    if state is None:
        return result
    observation = state.get("observation", {})
    if not isinstance(observation, Mapping):
        return result
    storage = observation.get("storage", {})
    if not isinstance(storage, Mapping):
        return result
    result["filesystem_available_bytes_informational"] = storage.get(
        "filesystem_available_bytes"
    )
    workspace = storage.get("workspace_command", {})
    if not isinstance(workspace, Mapping) or workspace.get("returncode") != 0:
        return result
    for line in str(workspace.get("stdout") or "").splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] != project_storage_root:
            continue
        used_capacity = fields[1].split("/", 1)
        if len(used_capacity) != 2:
            continue
        used = _decimal_size_bytes(used_capacity[0].lstrip("*"))
        capacity = _decimal_size_bytes(used_capacity[1].lstrip("*"))
        if used is None or capacity is None:
            continue
        result.update({
            "status": "verified",
            "used_bytes": used,
            "capacity_bytes": capacity,
            "available_bytes": max(0, capacity - used),
            "raw_capacity_field": fields[1],
        })
        return result
    return result


def _environment_evidence(
    state_root: Path,
    backend_id: str,
    backend_digest: str,
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], list[str]]:
    state_path = state_root / backend_id / "current_evidence.json"
    provenance_path = state_root / backend_id / "provenance.json"
    if not state_path.exists() or not provenance_path.exists():
        return None, [], ["backend_environment_evidence_missing"]
    state, state_raw = _read_json(state_path, "AI backend current evidence")
    provenance, provenance_raw = _read_json(
        provenance_path, "AI backend evidence provenance"
    )
    if state.get("backend_id") != backend_id or provenance.get("backend_id") != backend_id:
        raise ModelAcquisitionError("Backend evidence identity does not match the plan.")
    if state.get("automatic_execution_allowed") is not False:
        raise ModelAcquisitionError("Backend evidence must disable automatic execution.")
    if state.get("automatic_registry_update_allowed") is not False:
        raise ModelAcquisitionError("Backend evidence must disable registry updates.")
    source_artifacts = provenance.get("source_artifacts")
    if not isinstance(source_artifacts, list):
        raise ModelAcquisitionError("Backend evidence provenance has no source artifacts.")
    recorded_backend_digests = {
        str(item.get("sha256") or "")
        for item in source_artifacts
        if isinstance(item, Mapping)
        and item.get("artifact_type") == "ai_backend_configuration"
    }
    blockers = []
    if recorded_backend_digests != {backend_digest}:
        blockers.append("backend_environment_evidence_stale")
    readiness = state.get("readiness")
    if not isinstance(readiness, Mapping):
        raise ModelAcquisitionError("Backend evidence has no readiness object.")
    environment_blockers = readiness.get("environment_blockers")
    if not isinstance(environment_blockers, list):
        raise ModelAcquisitionError("Backend evidence has invalid environment blockers.")
    if environment_blockers:
        blockers.append("backend_environment_not_verified")
    if readiness.get("status") == "environment_evidence_stale":
        blockers.append("backend_environment_evidence_stale")
    sources = [
        _source(state_path, state_raw, "ai_backend_current_evidence"),
        _source(provenance_path, provenance_raw, "ai_backend_evidence_provenance"),
    ]
    return state, sources, list(dict.fromkeys(blockers))


def _metadata_request(
    backend: Mapping[str, Any],
    spec: Mapping[str, Any],
    plan_id: str,
) -> dict[str, Any]:
    source = spec["source"]
    return {
        "schema_version": "1.0",
        "policy_version": MODEL_ACQUISITION_POLICY_VERSION,
        "backend_id": backend["backend_id"],
        "plan_id": plan_id,
        "provider": source["provider"],
        "repository": source["repository"],
        "resolved_revision": source.get("resolved_revision"),
        "required_revision_kind": "immutable_40_hex_commit",
        "required_metadata": [
            "resolved_commit_revision",
            "license_at_resolved_revision",
            "complete_repository_file_inventory",
            "file_size_bytes",
            "git_lfs_sha256_for_weight_files",
            "git_blob_id_for_regular_files",
            "canonical_source_inventory_sha256",
            "source_total_bytes",
        ],
        "accepted_source_inventory_digest": source.get("source_inventory_sha256"),
        "accepted_source_total_bytes": source.get("source_total_bytes"),
        "remote_access_allowed": False,
        "model_download_allowed": False,
        "registry_update_allowed": False,
        "note": (
            "This request describes evidence needed from a future bounded metadata "
            "collector. The planner performs no provider request."
        ),
    }


def _integrity_plan(
    backend: Mapping[str, Any],
    spec: Mapping[str, Any],
    plan_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "policy_version": MODEL_ACQUISITION_POLICY_VERSION,
        "backend_id": backend["backend_id"],
        "plan_id": plan_id,
        "installation_path": spec["target"]["installation_path"],
        "manifest_filename": spec["target"]["manifest_filename"],
        "publication_strategy": spec["target"]["publication_strategy"],
        "pre_publication_requirements": [
            "resolved_revision_matches_approved_source_revision",
            "downloaded_file_set_matches_approved_source_inventory",
            "every_regular_file_sha256_matches_approved_inventory",
            "no_symlink_resolves_outside_approved_model_storage",
            "manifest_records_file_paths_sizes_and_sha256_digests",
            "manifest_records_runtime_model_and_environment_identity",
        ],
        "post_publication_requirements": [
            "read_only_backend_evidence_observes_published_model",
            "backend_registry_updated_only_after_researcher_review",
            "bounded_gpu_benchmark_passes_before_backend_activation",
        ],
        "large_file_hashing_planned": True,
        "large_file_hashing_performed": False,
        "model_download_performed": False,
        "automatic_publication_allowed": False,
        "automatic_backend_activation_allowed": False,
    }


def _report(plan: Mapping[str, Any]) -> str:
    storage = plan["storage"]
    lines = [
        "# GenomeAgent Pinned Model Acquisition Plan",
        "",
        "- Backend: `{}`".format(plan["backend_id"]),
        "- Plan ID: `{}`".format(plan["plan_id"]),
        "- Status: **{}**".format(plan["status"]),
        "- Next safe action: `{}`".format(plan["next_safe_action"]),
        "- Source repository: `{}`".format(plan["source"]["repository"]),
        "- Resolved revision: `{}`".format(
            plan["source"].get("resolved_revision") or "unresolved"
        ),
        "- Automatic acquisition: **no**",
        "",
        "## Blockers",
        "",
    ]
    if plan["blockers"]:
        lines.extend("- `{}`".format(item) for item in plan["blockers"])
    else:
        lines.append("No planning blockers remain; explicit researcher review is still required.")
    lines.extend([
        "",
        "## Storage evidence",
        "",
        "- Theoretical parameter-byte lower bound: `{}`".format(
            storage.get("theoretical_parameter_bytes_lower_bound")
        ),
        "- Provider inventory size: `{}`".format(storage.get("source_total_bytes")),
        "- Required working free bytes: `{}`".format(
            storage.get("required_working_free_bytes")
        ),
        "- Observed available bytes: `{}`".format(storage.get("observed_available_bytes")),
        "- Storage gate: **{}**".format(storage.get("status")),
        "",
        "The theoretical estimate represents registered parameters multiplied by the "
        "configured bytes per parameter. It excludes tokenizer, configuration, cache, "
        "filesystem and temporary-publication overhead and cannot authorize acquisition.",
        "",
        "## Safety boundary",
        "",
        "This deterministic plan used local configuration and saved evidence only. It "
        "did not contact a model provider or Roihu, download or import weights, write "
        "remotely, hash model files, submit a job, allocate a GPU, edit the backend "
        "registry or activate an AI backend.",
        "",
    ])
    return "\n".join(lines)


class ModelAcquisitionPlanner:
    """Build immutable model-acquisition plans from local registry and evidence."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        specification_root: Path = Path("config/ai/acquisition"),
        backend_state_root: Path = Path("workspace/ai_backend_state"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
    ):
        self.registry = registry or AIRegistry()
        self.specification_root = Path(specification_root)
        self.backend_state_root = Path(backend_state_root)
        self.plan_root = Path(plan_root)

    def plan(self, backend_id: str) -> ModelAcquisitionPlanResult:
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        normalized_id = str(backend["backend_id"])
        spec_path = self.specification_root / (normalized_id + ".json")
        spec, spec_raw = _validate_spec(spec_path, backend)
        backend_digest = _sha256_bytes(backend_raw)
        state, evidence_sources, environment_blockers = _environment_evidence(
            self.backend_state_root,
            normalized_id,
            backend_digest,
        )
        sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(spec_path, spec_raw, "model_acquisition_specification"),
            *evidence_sources,
        ]
        sources.sort(key=lambda item: (item["artifact_type"], item["path"]))
        identity = {
            "policy_version": MODEL_ACQUISITION_POLICY_VERSION,
            "backend_id": normalized_id,
            "sources": sources,
        }
        plan_id = _sha256_value(identity)

        source = spec["source"]
        identity_blockers = []
        if not source.get("resolved_revision"):
            identity_blockers.append("model_revision_unpinned")
        if not source.get("source_inventory_sha256"):
            identity_blockers.append("source_inventory_digest_missing")
        if source.get("source_total_bytes") is None:
            identity_blockers.append("source_inventory_size_missing")
        if source.get("license_review_status") == "unreviewed":
            identity_blockers.append("model_license_review_required")
        elif source.get("license_review_status") == "reviewed_rejected":
            identity_blockers.append("model_license_rejected")

        quota = _project_quota_evidence(
            state, str(spec["target"]["project_storage_root"])
        )
        available_bytes = quota.get("available_bytes")
        required_bytes = _required_working_bytes(spec)
        storage_blockers = []
        if required_bytes is None:
            storage_status = "awaiting_source_inventory"
        elif available_bytes is None:
            storage_status = "available_capacity_unobserved"
            storage_blockers.append("target_storage_capacity_unverified")
        elif available_bytes < required_bytes:
            storage_status = "insufficient_observed_capacity"
            storage_blockers.append("insufficient_observed_storage")
        else:
            storage_status = "sufficient_observed_capacity_for_review"

        blockers = list(dict.fromkeys(
            environment_blockers + identity_blockers + storage_blockers
        ))
        if environment_blockers:
            status = "environment_evidence_required"
            next_action = "collect_fresh_read_only_backend_environment_evidence"
        elif identity_blockers:
            status = "model_identity_resolution_required"
            next_action = "resolve_and_review_pinned_source_metadata"
        elif storage_blockers:
            status = "storage_preflight_required"
            next_action = "review_target_storage_capacity_and_acquisition_layout"
        else:
            status = "ready_for_researcher_acquisition_review"
            next_action = "researcher_review_of_non_executable_acquisition_plan"

        plan = {
            "schema_version": "1.0",
            "policy_version": MODEL_ACQUISITION_POLICY_VERSION,
            "source_mode": "deterministic_local_planning",
            "backend_id": normalized_id,
            "plan_id": plan_id,
            "status": status,
            "next_safe_action": next_action,
            "blockers": blockers,
            "source": dict(source),
            "target": dict(spec["target"]),
            "storage": {
                "status": storage_status,
                "precision": spec["representation"]["precision"],
                "bytes_per_parameter": spec["representation"]["bytes_per_parameter"],
                "theoretical_parameter_bytes_lower_bound": _theoretical_parameter_bytes(
                    backend, spec
                ),
                "source_total_bytes": source.get("source_total_bytes"),
                "complete_copies_during_acquisition": spec["storage_policy"][
                    "complete_copies_during_acquisition"
                ],
                "headroom_basis_points": spec["storage_policy"][
                    "headroom_basis_points"
                ],
                "required_working_free_bytes": required_bytes,
                "observed_available_bytes": available_bytes,
                "project_quota_evidence": quota,
                "automatic_cleanup_allowed": False,
            },
            "planned_stages": [
                "resolve_pinned_source_metadata",
                "review_license_and_source_inventory",
                "verify_target_storage_preflight",
                "obtain_explicit_researcher_acquisition_approval",
                "acquire_into_same_filesystem_staging_directory",
                "verify_full_file_inventory_and_sha256_digests",
                "publish_model_directory_atomically",
                "collect_fresh_read_only_backend_evidence",
                "review_registry_update",
                "run_bounded_gpu_benchmark_after_separate_approval",
            ],
            "post_acquisition_gates": [
                "verified_model_inventory_missing",
                "model_weights_digest_missing",
                "inference_not_benchmarked",
            ],
            "automatic_execution_allowed": False,
            "automatic_model_download_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_registry_update_allowed": False,
            "automatic_backend_activation_allowed": False,
        }
        metadata_request = _metadata_request(backend, spec, plan_id)
        integrity = _integrity_plan(backend, spec, plan_id)
        readiness = {
            "schema_version": "1.0",
            "policy_version": MODEL_ACQUISITION_POLICY_VERSION,
            "backend_id": normalized_id,
            "plan_id": plan_id,
            "status": status,
            "next_safe_action": next_action,
            "environment_gate": {
                "status": "verified" if not environment_blockers else "blocked",
                "blockers": environment_blockers,
            },
            "source_identity_gate": {
                "status": "verified" if not identity_blockers else "blocked",
                "blockers": identity_blockers,
            },
            "storage_gate": {
                "status": storage_status,
                "blockers": storage_blockers,
            },
            "researcher_approval_required": True,
            "fresh_pre_acquisition_environment_evidence_required": True,
            "automatic_execution_allowed": False,
            "automatic_model_download_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_registry_update_allowed": False,
            "automatic_backend_activation_allowed": False,
        }
        provenance = {
            "schema_version": "1.0",
            "policy_version": MODEL_ACQUISITION_POLICY_VERSION,
            "backend_id": normalized_id,
            "plan_id": plan_id,
            "inputs": sources,
            "identity_sha256": _sha256_value(identity),
            "derivation": "deterministic_local_model_acquisition_planning",
            "safety": {
                "remote_access": False,
                "remote_writes": False,
                "model_download": False,
                "model_import": False,
                "job_submission": False,
                "gpu_allocation": False,
                "large_file_hashing": False,
                "registry_update": False,
                "backend_activation": False,
            },
        }

        plan_dir = self.plan_root / normalized_id / plan_id
        artifacts = (
            plan_dir / "model_acquisition_plan.json",
            plan_dir / "source_metadata_request.json",
            plan_dir / "integrity_plan.json",
            plan_dir / "execution_readiness.json",
            plan_dir / "report.md",
            plan_dir / "provenance.json",
        )
        payloads = (
            _pretty_json(plan),
            _pretty_json(metadata_request),
            _pretty_json(integrity),
            _pretty_json(readiness),
            _report(plan),
            _pretty_json(provenance),
        )
        created = False
        for path, payload in zip(artifacts, payloads):
            created = _write_immutable(path, payload) or created
        return ModelAcquisitionPlanResult(
            backend_id=normalized_id,
            plan_id=plan_id,
            status=status,
            next_safe_action=next_action,
            blockers=tuple(blockers),
            plan_dir=plan_dir,
            created=created,
            artifact_paths=artifacts,
        )
