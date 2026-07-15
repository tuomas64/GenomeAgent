#!/usr/bin/env python3
"""Explicit approval and non-executing bundle preparation for model acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

from genomeagent.ai_evaluation import AIRegistry
from genomeagent.model_acquisition import (
    MODEL_ACQUISITION_PLAN_POLICY_VERSION,
    ModelAcquisitionError,
    ModelAcquisitionPlanner,
    validate_acquisition_specification,
)


MODEL_ACQUISITION_EXECUTION_POLICY_VERSION = "1.0"
PLAN_ARTIFACT_NAMES = (
    "model_acquisition_plan.json",
    "source_metadata_request.json",
    "integrity_plan.json",
    "execution_readiness.json",
    "report.md",
    "provenance.json",
)
APPROVAL_FALSE_SAFETY_FIELDS = (
    "remote_access",
    "remote_writes",
    "model_download",
    "model_import",
    "job_submission",
    "gpu_allocation",
    "large_file_hashing",
    "atomic_publication",
    "registry_update",
    "backend_activation",
    "inference_execution",
    "training_execution",
)
SOURCE_EVIDENCE_FALSE_SAFETY_FIELDS = (
    "repository_file_download",
    "model_weight_download",
    "remote_cluster_access",
    "remote_cluster_writes",
    "job_submission",
    "gpu_allocation",
    "large_file_hashing",
    "automatic_specification_update",
    "automatic_registry_update",
    "automatic_backend_activation",
)


class ModelAcquisitionExecutionError(RuntimeError):
    """Raised when acquisition authorization or preparation evidence is unsafe."""


@dataclass(frozen=True)
class ModelAcquisitionApprovalResult:
    backend_id: str
    plan_id: str
    approval_id: str
    approval_path: Path
    created: bool
    status: str
    next_safe_action: str


@dataclass(frozen=True)
class ModelAcquisitionBundleResult:
    backend_id: str
    plan_id: str
    approval_id: str
    bundle_id: str
    bundle_dir: Path
    created: bool
    status: str
    blockers: tuple[str, ...]
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
            raise ModelAcquisitionExecutionError(
                "Immutable acquisition artifact exists with different content: {}".format(
                    path
                )
            )
        return False
    _atomic_write(path, text)
    return True


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise ModelAcquisitionExecutionError("Refusing symlinked {}: {}".format(label, path))
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ModelAcquisitionExecutionError("Missing {}: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelAcquisitionExecutionError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise ModelAcquisitionExecutionError("{} must be a JSON object.".format(label))
    return value, raw


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise ModelAcquisitionExecutionError("Unsafe or empty {}.".format(label))
    return normalized


def _digest(value: Any, label: str, length: int = 64) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{{{}}}".format(length), normalized):
        raise ModelAcquisitionExecutionError("Invalid {}.".format(label))
    return normalized


def _reviewer(value: Any) -> str:
    reviewer = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,127}", reviewer):
        raise ModelAcquisitionExecutionError("Unsafe or empty reviewer identifier.")
    return reviewer


def _safe_source_path(value: Any) -> str:
    text = str(value or "").strip()
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\x00" in text
    ):
        raise ModelAcquisitionExecutionError("Unsafe source inventory path: {!r}".format(value))
    return text


def _false_safety(value: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(value, Mapping):
        raise ModelAcquisitionExecutionError("{} has no safety object.".format(label))
    for field in fields:
        if value.get(field) is not False:
            raise ModelAcquisitionExecutionError(
                "{} must explicitly disable {}.".format(label, field)
            )


def _approval_identity(approval: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "policy_version": MODEL_ACQUISITION_EXECUTION_POLICY_VERSION,
        "backend_id": approval.get("backend_id"),
        "plan_id": approval.get("plan_id"),
        "plan_artifact_sha256": approval.get("plan_artifact_sha256"),
        "source_evidence_id": approval.get("source_evidence_id"),
        "source_evidence_sha256": approval.get("source_evidence_sha256"),
        "reviewer": approval.get("reviewer"),
        "authorization_scope": approval.get("authorization_scope"),
    }


def _approval_record_identity(approval: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": approval.get("schema_version"),
        "source_mode": approval.get("source_mode"),
        "approval_id": approval.get("approval_id"),
        **_approval_identity(approval),
        "approved_at_utc": approval.get("approved_at_utc"),
        "safety": approval.get("safety"),
    }


def _validate_approval(
    approval: Mapping[str, Any], approval_id: str, backend_id: str, plan_id: str
) -> None:
    if approval.get("schema_version") != "1.0":
        raise ModelAcquisitionExecutionError("Unsupported acquisition approval schema.")
    if approval.get("policy_version") != MODEL_ACQUISITION_EXECUTION_POLICY_VERSION:
        raise ModelAcquisitionExecutionError("Unsupported acquisition approval policy.")
    if approval.get("source_mode") != "explicit_researcher_execution_preparation_approval":
        raise ModelAcquisitionExecutionError("Invalid acquisition approval source mode.")
    if approval.get("backend_id") != backend_id or approval.get("plan_id") != plan_id:
        raise ModelAcquisitionExecutionError("Acquisition approval scope mismatch.")
    if approval.get("approval_id") != approval_id:
        raise ModelAcquisitionExecutionError("Acquisition approval ID mismatch.")
    _reviewer(approval.get("reviewer"))
    if approval.get("authorization_scope") != "prepare_controlled_acquisition_bundle_only":
        raise ModelAcquisitionExecutionError("Acquisition approval scope is unsupported.")
    artifacts = approval.get("plan_artifact_sha256")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(PLAN_ARTIFACT_NAMES):
        raise ModelAcquisitionExecutionError("Acquisition approval plan digest set is invalid.")
    for name, value in artifacts.items():
        _digest(value, "{} digest".format(name))
    if not re.fullmatch(
        r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z",
        str(approval.get("source_evidence_id") or ""),
    ):
        raise ModelAcquisitionExecutionError("Invalid approval source evidence ID.")
    _digest(approval.get("source_evidence_sha256"), "approval source evidence digest")
    if _sha256_value(_approval_identity(approval)) != approval_id:
        raise ModelAcquisitionExecutionError("Acquisition approval content digest mismatch.")
    if _sha256_value(_approval_record_identity(approval)) != approval.get(
        "approval_record_sha256"
    ):
        raise ModelAcquisitionExecutionError("Acquisition approval record digest mismatch.")
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        str(approval.get("approved_at_utc") or ""),
    ):
        raise ModelAcquisitionExecutionError("Invalid acquisition approval timestamp.")
    _false_safety(approval.get("safety"), APPROVAL_FALSE_SAFETY_FIELDS, "Approval")


def _approval_report(approval: Mapping[str, Any]) -> str:
    return "\n".join([
        "# GenomeAgent Model Acquisition Preparation Approval",
        "",
        "- Backend: `{}`".format(approval["backend_id"]),
        "- Plan ID: `{}`".format(approval["plan_id"]),
        "- Approval ID: `{}`".format(approval["approval_id"]),
        "- Reviewer: `{}`".format(approval["reviewer"]),
        "- Approved at: `{}`".format(approval["approved_at_utc"]),
        "- Authorized scope: **execution-bundle preparation only**",
        "- Remote access or writes: **no**",
        "- Model download or publication: **no**",
        "",
        "A separate fresh environment preflight and explicit execution authorization are "
        "required before any remote model acquisition action.",
        "",
    ])


def _bundle_report(bundle: Mapping[str, Any], readiness: Mapping[str, Any]) -> str:
    integrity = bundle["integrity_contract"]
    lines = [
        "# GenomeAgent Controlled Model Acquisition Bundle",
        "",
        "- Backend: `{}`".format(bundle["backend_id"]),
        "- Plan ID: `{}`".format(bundle["plan_id"]),
        "- Bundle ID: `{}`".format(bundle["bundle_id"]),
        "- Status: **{}**".format(readiness["status"]),
        "- Source files: {}".format(integrity["expected_file_count"]),
        "- Provider SHA-256 coverage: {}/{}".format(
            integrity["provider_sha256_file_count"],
            integrity["expected_file_count"],
        ),
        "- Locally computed SHA-256 required: {}/{}".format(
            integrity["local_sha256_required_file_count"],
            integrity["expected_file_count"],
        ),
        "- Remote execution: **disabled**",
        "",
        "## Remaining blockers",
        "",
    ]
    lines.extend("- `{}`".format(item) for item in readiness["blockers"])
    lines.extend([
        "",
        "The bundle contains data contracts only. It contains no shell command, Python "
        "worker, credential, token, Slurm submission or automatic execution authority.",
        "",
    ])
    return "\n".join(lines)


class ControlledModelAcquisitionCore:
    """Approve an exact current plan and prepare a still non-executable bundle."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        specification_root: Path = Path("config/ai/acquisition"),
        backend_state_root: Path = Path("workspace/ai_backend_state"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
        source_evidence_root: Path = Path("workspace/model_source_evidence"),
        approval_root: Path = Path("workspace/model_acquisition_approvals"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
    ):
        self.registry = registry or AIRegistry()
        self.specification_root = Path(specification_root)
        self.backend_state_root = Path(backend_state_root)
        self.plan_root = Path(plan_root)
        self.source_evidence_root = Path(source_evidence_root)
        self.approval_root = Path(approval_root)
        self.bundle_root = Path(bundle_root)

    def _current_plan(
        self, backend_id: str, plan_id: str
    ) -> tuple[str, dict[str, Any], dict[str, str], Path]:
        normalized_id = _identifier(backend_id, "backend_id")
        plan_id = _digest(plan_id, "plan ID")
        planner = ModelAcquisitionPlanner(
            registry=self.registry,
            specification_root=self.specification_root,
            backend_state_root=self.backend_state_root,
            plan_root=self.plan_root,
        )
        try:
            current = planner.plan(normalized_id)
        except ModelAcquisitionError as exc:
            raise ModelAcquisitionExecutionError(
                "Unable to rebuild current acquisition plan: {}".format(exc)
            ) from exc
        if current.plan_id != plan_id:
            raise ModelAcquisitionExecutionError(
                "Requested plan is not the current plan; rerun planning and review the new ID."
            )
        if current.status != "ready_for_researcher_acquisition_review" or current.blockers:
            raise ModelAcquisitionExecutionError(
                "Current acquisition plan is not ready for researcher approval."
            )
        plan_dir = self.plan_root / normalized_id / plan_id
        digests: dict[str, str] = {}
        objects: dict[str, dict[str, Any]] = {}
        for name in PLAN_ARTIFACT_NAMES:
            path = plan_dir / name
            if name.endswith(".json"):
                value, raw = _read_json(path, "plan artifact")
                if value.get("backend_id") != normalized_id or value.get("plan_id") != plan_id:
                    raise ModelAcquisitionExecutionError(
                        "Plan artifact identity mismatch: {}".format(name)
                    )
                objects[name] = value
            else:
                if path.is_symlink():
                    raise ModelAcquisitionExecutionError("Refusing symlinked plan report.")
                raw = path.read_bytes()
            digests[name] = _sha256_bytes(raw)
        plan = objects["model_acquisition_plan.json"]
        readiness = objects["execution_readiness.json"]
        integrity = objects["integrity_plan.json"]
        provenance = objects["provenance.json"]
        if plan.get("policy_version") != MODEL_ACQUISITION_PLAN_POLICY_VERSION:
            raise ModelAcquisitionExecutionError("Plan policy is not execution-compatible.")
        if readiness.get("status") != "ready_for_researcher_acquisition_review":
            raise ModelAcquisitionExecutionError("Plan readiness artifact is not review-ready.")
        if readiness.get("environment_gate", {}).get("status") != "verified":
            raise ModelAcquisitionExecutionError("Plan environment gate is not verified.")
        if readiness.get("source_identity_gate", {}).get("status") != "verified":
            raise ModelAcquisitionExecutionError("Plan source identity gate is not verified.")
        if readiness.get("storage_gate", {}).get("blockers"):
            raise ModelAcquisitionExecutionError("Plan storage gate has blockers.")
        for value, label in ((plan, "Plan"), (readiness, "Readiness")):
            for field in (
                "automatic_execution_allowed",
                "automatic_model_download_allowed",
                "automatic_job_submission_allowed",
                "automatic_registry_update_allowed",
                "automatic_backend_activation_allowed",
            ):
                if value.get(field) is not False:
                    raise ModelAcquisitionExecutionError(
                        "{} must explicitly disable {}.".format(label, field)
                    )
        if integrity.get("automatic_publication_allowed") is not False:
            raise ModelAcquisitionExecutionError("Integrity plan grants publication authority.")
        provenance_safety = provenance.get("safety", {})
        if not isinstance(provenance_safety, Mapping) or any(
            value is not False for value in provenance_safety.values()
        ):
            raise ModelAcquisitionExecutionError("Plan provenance grants execution authority.")
        return normalized_id, plan, digests, plan_dir

    def _approved_inventory(
        self, backend_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
        _, backend, _ = self.registry.backend(backend_id)
        spec_path = self.specification_root / (backend_id + ".json")
        try:
            spec, _ = validate_acquisition_specification(spec_path, backend)
        except ModelAcquisitionError as exc:
            raise ModelAcquisitionExecutionError(
                "Invalid acquisition specification: {}".format(exc)
            ) from exc
        source = spec["source"]
        approval = source.get("license_approval")
        if not isinstance(approval, Mapping):
            raise ModelAcquisitionExecutionError("Acquisition source has no license approval.")
        evidence_id = str(approval.get("source_evidence_id") or "")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", evidence_id):
            raise ModelAcquisitionExecutionError("Invalid approved source evidence ID.")
        evidence_path = self.source_evidence_root / backend_id / (evidence_id + ".json")
        snapshot, raw = _read_json(evidence_path, "approved source evidence")
        evidence_sha256 = _sha256_bytes(raw)
        if evidence_sha256 != approval.get("source_evidence_sha256"):
            raise ModelAcquisitionExecutionError("Approved source evidence digest mismatch.")
        if snapshot.get("source_mode") != "bounded_public_metadata_queries":
            raise ModelAcquisitionExecutionError("Source evidence mode is not read-only metadata.")
        if snapshot.get("backend_id") != backend_id:
            raise ModelAcquisitionExecutionError("Source evidence backend mismatch.")
        source_safety = snapshot.get("safety")
        if (
            not isinstance(source_safety, Mapping)
            or source_safety.get("public_metadata_queries") is not True
        ):
            raise ModelAcquisitionExecutionError("Source evidence safety record is invalid.")
        _false_safety(
            source_safety,
            SOURCE_EVIDENCE_FALSE_SAFETY_FIELDS,
            "Source evidence",
        )
        source_readiness = snapshot.get("readiness")
        if (
            not isinstance(source_readiness, Mapping)
            or source_readiness.get("status")
            != "source_metadata_ready_for_researcher_review"
        ):
            raise ModelAcquisitionExecutionError("Approved source evidence is not review-ready.")
        metadata = snapshot.get("normalized_metadata")
        if not isinstance(metadata, Mapping):
            raise ModelAcquisitionExecutionError("Source evidence has no normalized inventory.")
        if metadata.get("resolved_revision") != source.get("resolved_revision"):
            raise ModelAcquisitionExecutionError("Approved source revision mismatch.")
        files = metadata.get("files")
        if not isinstance(files, list) or not files:
            raise ModelAcquisitionExecutionError("Approved source inventory is empty.")
        normalized_files = []
        seen = set()
        total = 0
        for item in files:
            if not isinstance(item, Mapping):
                raise ModelAcquisitionExecutionError("Source inventory contains a non-object.")
            path = _safe_source_path(item.get("path"))
            if path in seen:
                raise ModelAcquisitionExecutionError("Source inventory contains duplicate paths.")
            seen.add(path)
            size = item.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ModelAcquisitionExecutionError("Source inventory size is incomplete.")
            lfs_sha256 = item.get("lfs_sha256")
            if lfs_sha256 is not None:
                lfs_sha256 = _digest(lfs_sha256, "provider LFS SHA-256")
            normalized_files.append({
                "path": path,
                "size_bytes": size,
                "provider_lfs_sha256": lfs_sha256,
                "git_blob_id": item.get("blob_id"),
                "xet_hash": item.get("xet_hash"),
                "is_weight_file": item.get("is_weight_file") is True,
            })
            total += size
        if _sha256_value(files) != source.get("source_inventory_sha256"):
            raise ModelAcquisitionExecutionError("Approved source inventory digest mismatch.")
        if total != source.get("source_total_bytes"):
            raise ModelAcquisitionExecutionError("Approved source inventory byte total mismatch.")
        return spec, normalized_files, evidence_id, evidence_sha256

    def approve(
        self,
        backend_id: str,
        plan_id: str,
        reviewer: str,
        confirmation: bool,
        approved_at: Optional[datetime] = None,
    ) -> ModelAcquisitionApprovalResult:
        if confirmation is not True:
            raise ModelAcquisitionExecutionError(
                "Explicit --confirm-execution-preparation is required."
            )
        reviewer = _reviewer(reviewer)
        backend_id, _, plan_digests, _ = self._current_plan(backend_id, plan_id)
        _, _, evidence_id, evidence_sha256 = self._approved_inventory(backend_id)
        identity = {
            "policy_version": MODEL_ACQUISITION_EXECUTION_POLICY_VERSION,
            "backend_id": backend_id,
            "plan_id": plan_id,
            "plan_artifact_sha256": plan_digests,
            "source_evidence_id": evidence_id,
            "source_evidence_sha256": evidence_sha256,
            "reviewer": reviewer,
            "authorization_scope": "prepare_controlled_acquisition_bundle_only",
        }
        approval_id = _sha256_value(identity)
        approval_dir = self.approval_root / backend_id
        approval_path = approval_dir / (approval_id + ".json")
        report_path = approval_dir / (approval_id + ".md")
        if approval_path.exists():
            existing, _ = _read_json(approval_path, "acquisition approval")
            _validate_approval(existing, approval_id, backend_id, plan_id)
            if existing.get("plan_artifact_sha256") != plan_digests:
                raise ModelAcquisitionExecutionError(
                    "Approved plan artifacts no longer match the current plan."
                )
            _write_immutable(report_path, _approval_report(existing))
            return ModelAcquisitionApprovalResult(
                backend_id=backend_id,
                plan_id=plan_id,
                approval_id=approval_id,
                approval_path=approval_path,
                created=False,
                status="execution_preparation_already_approved",
                next_safe_action="prepare_non_executable_acquisition_bundle",
            )
        approved_at = approved_at or datetime.now(timezone.utc)
        if approved_at.tzinfo is None:
            raise ModelAcquisitionExecutionError("Approval timestamp must include timezone.")
        approval = {
            "schema_version": "1.0",
            "policy_version": MODEL_ACQUISITION_EXECUTION_POLICY_VERSION,
            "source_mode": "explicit_researcher_execution_preparation_approval",
            "approval_id": approval_id,
            **identity,
            "approved_at_utc": approved_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "safety": {field: False for field in APPROVAL_FALSE_SAFETY_FIELDS},
        }
        approval["approval_record_sha256"] = _sha256_value(
            _approval_record_identity(approval)
        )
        _validate_approval(approval, approval_id, backend_id, plan_id)
        created = _write_immutable(approval_path, _pretty_json(approval))
        _write_immutable(report_path, _approval_report(approval))
        return ModelAcquisitionApprovalResult(
            backend_id=backend_id,
            plan_id=plan_id,
            approval_id=approval_id,
            approval_path=approval_path,
            created=created,
            status=(
                "execution_preparation_approved"
                if created else "execution_preparation_already_approved"
            ),
            next_safe_action="prepare_non_executable_acquisition_bundle",
        )

    def prepare(
        self, backend_id: str, plan_id: str, approval_id: str
    ) -> ModelAcquisitionBundleResult:
        approval_id = _digest(approval_id, "approval ID")
        backend_id, plan, plan_digests, plan_dir = self._current_plan(
            backend_id, plan_id
        )
        approval_path = self.approval_root / backend_id / (approval_id + ".json")
        approval, approval_raw = _read_json(approval_path, "acquisition approval")
        _validate_approval(approval, approval_id, backend_id, plan_id)
        if approval.get("plan_artifact_sha256") != plan_digests:
            raise ModelAcquisitionExecutionError("Approved plan artifacts have changed.")
        spec, files, evidence_id, evidence_sha256 = self._approved_inventory(backend_id)
        identity = {
            "policy_version": MODEL_ACQUISITION_EXECUTION_POLICY_VERSION,
            "backend_id": backend_id,
            "plan_id": plan_id,
            "approval_id": approval_id,
            "approval_sha256": _sha256_bytes(approval_raw),
            "source_evidence_id": evidence_id,
            "source_evidence_sha256": evidence_sha256,
        }
        bundle_id = _sha256_value(identity)
        final_path = Path(str(spec["target"]["installation_path"]))
        staging_path = final_path.parent / (
            ".genomeagent-staging-{}-{}".format(final_path.name, bundle_id[:16])
        )
        provider_count = sum(
            1 for item in files if item["provider_lfs_sha256"] is not None
        )
        bundle = {
            "schema_version": "1.0",
            "policy_version": MODEL_ACQUISITION_EXECUTION_POLICY_VERSION,
            "source_mode": "deterministic_local_execution_bundle_preparation",
            "bundle_id": bundle_id,
            **identity,
            "repository": plan["source"]["repository"],
            "resolved_revision": plan["source"]["resolved_revision"],
            "target": {
                "project_storage_root": spec["target"]["project_storage_root"],
                "staging_path": str(staging_path),
                "installation_path": str(final_path),
                "manifest_path": str(final_path / spec["target"]["manifest_filename"]),
                "publication_strategy": "atomic_directory_rename_same_filesystem",
            },
            "approved_source_inventory": files,
            "integrity_contract": {
                "expected_file_count": len(files),
                "expected_total_bytes": sum(item["size_bytes"] for item in files),
                "provider_sha256_file_count": provider_count,
                "provider_sha256_missing_file_count": len(files) - provider_count,
                "local_sha256_required_file_count": len(files),
                "verify_exact_paths_and_sizes": True,
                "verify_available_provider_lfs_sha256": True,
                "compute_sha256_for_every_regular_file": True,
                "git_blob_ids_are_not_raw_file_sha256": True,
                "reject_all_symlinks": True,
                "manifest_is_written_before_atomic_publication": True,
            },
            "execution_contract": {
                "remote_worker_generated": False,
                "remote_command_generated": False,
                "credentials_included": False,
                "fresh_environment_preflight_required": True,
                "separate_explicit_execution_authorization_required": True,
            },
            "automatic_execution_allowed": False,
            "automatic_remote_access_allowed": False,
            "automatic_model_download_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_publication_allowed": False,
            "automatic_registry_update_allowed": False,
            "automatic_backend_activation_allowed": False,
        }
        blockers = [
            "acquisition_runtime_unverified",
            "transfer_execution_context_unregistered",
            "remote_target_state_unverified",
            "fresh_execution_authorization_missing",
        ]
        readiness = {
            "schema_version": "1.0",
            "policy_version": MODEL_ACQUISITION_EXECUTION_POLICY_VERSION,
            "backend_id": backend_id,
            "plan_id": plan_id,
            "approval_id": approval_id,
            "bundle_id": bundle_id,
            "status": "environment_execution_preflight_required",
            "next_safe_action": "collect_read_only_acquisition_runtime_evidence",
            "blockers": blockers,
            "automatic_execution_allowed": False,
            "automatic_remote_access_allowed": False,
            "automatic_model_download_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_publication_allowed": False,
        }
        provenance = {
            "schema_version": "1.0",
            "policy_version": MODEL_ACQUISITION_EXECUTION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "plan_artifacts": [
                {"path": str(plan_dir / name), "sha256": plan_digests[name]}
                for name in PLAN_ARTIFACT_NAMES
            ],
            "approval": {
                "path": str(approval_path),
                "sha256": _sha256_bytes(approval_raw),
            },
            "source_evidence": {
                "evidence_id": evidence_id,
                "sha256": evidence_sha256,
            },
            "derivation": "deterministic_local_non_executable_bundle_preparation",
            "remote_access": False,
            "remote_writes": False,
            "model_download": False,
            "job_submission": False,
            "large_file_hashing": False,
            "publication": False,
        }
        bundle_dir = self.bundle_root / backend_id / bundle_id
        artifacts = (
            bundle_dir / "acquisition_bundle.json",
            bundle_dir / "execution_readiness.json",
            bundle_dir / "report.md",
            bundle_dir / "provenance.json",
        )
        payloads = (
            _pretty_json(bundle),
            _pretty_json(readiness),
            _bundle_report(bundle, readiness),
            _pretty_json(provenance),
        )
        created = False
        for path, payload in zip(artifacts, payloads):
            created = _write_immutable(path, payload) or created
        return ModelAcquisitionBundleResult(
            backend_id=backend_id,
            plan_id=plan_id,
            approval_id=approval_id,
            bundle_id=bundle_id,
            bundle_dir=bundle_dir,
            created=created,
            status=str(readiness["status"]),
            blockers=tuple(blockers),
            artifact_paths=artifacts,
        )
