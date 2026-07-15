#!/usr/bin/env python3
"""Bounded read-only source metadata evidence for registered AI models."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Protocol

from genomeagent.ai_evaluation import AIRegistry
from genomeagent.model_acquisition import (
    ModelAcquisitionError,
    validate_acquisition_specification,
)


MODEL_SOURCE_EVIDENCE_POLICY_VERSION = "1.0"
MODEL_SOURCE_APPROVAL_POLICY_VERSION = "1.0"
PROVIDER = "huggingface_hub"
FALSE_SAFETY_FIELDS = (
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
WEIGHT_SUFFIXES = (
    ".safetensors",
    ".bin",
    ".pt",
    ".pth",
    ".gguf",
)
LICENSE_FILES = {"license", "license.txt", "license.md", "copying", "copying.txt"}


class ModelSourceEvidenceError(RuntimeError):
    """Raised when source evidence is unsafe, malformed or inconsistent."""


@dataclass(frozen=True)
class ProviderResponse:
    payload: dict[str, Any]
    body_sha256: str
    body_bytes: int
    final_url: str
    status_code: int


class ModelMetadataClient(Protocol):
    endpoint: str

    def model_info(
        self,
        repository: str,
        revision: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> ProviderResponse:
        """Fetch one model-info response without downloading repository files."""


@dataclass(frozen=True)
class ModelSourceCollectionResult:
    backend_id: str
    evidence_id: str
    evidence_path: Path
    status: str
    resolved_revision: str
    source_total_bytes: int
    source_inventory_sha256: str


@dataclass(frozen=True)
class ModelSourceIngestResult:
    backend_id: str
    state_dir: Path
    snapshots: int
    status: str
    next_safe_action: str
    blockers: tuple[str, ...]
    proposal_status: str
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True)
class ModelSourceApprovalResult:
    backend_id: str
    approval_id: str
    approval_path: Path
    specification_path: Path
    applied: bool
    already_applied: bool
    status: str
    next_safe_action: str


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
            raise ModelSourceEvidenceError(
                "Immutable model source evidence exists with different content: {}".format(
                    path
                )
            )
        return False
    _atomic_write(path, text)
    return True


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise ModelSourceEvidenceError("Unsafe or empty {}: {!r}".format(label, value))
    return normalized


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ModelSourceEvidenceError("{} does not exist: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelSourceEvidenceError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise ModelSourceEvidenceError("{} must be a JSON object: {}".format(label, path))
    return value, raw


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "path": str(path),
        "sha256": _sha256_bytes(raw),
    }


def _validate_repository(value: Any) -> str:
    repository = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository):
        raise ModelSourceEvidenceError("Unsafe or empty model repository.")
    return repository


def _validate_revision(value: Any, allow_symbolic: bool = True) -> str:
    revision = str(value or "").strip()
    pattern = r"[0-9a-f]{40}" if not allow_symbolic else r"[A-Za-z0-9._/-]+"
    if not re.fullmatch(pattern, revision) or ".." in revision.split("/"):
        raise ModelSourceEvidenceError("Unsafe or invalid model revision.")
    return revision


def _validate_policy(
    path: Path,
    backend: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    policy, raw = _read_json(path, "model source evidence policy")
    backend_id = _identifier(policy.get("backend_id"), "backend_id")
    if path.stem != backend_id or backend_id != backend.get("backend_id"):
        raise ModelSourceEvidenceError(
            "Source evidence policy filename and backend_id must match the registry."
        )
    if policy.get("schema_version") != "1.0":
        raise ModelSourceEvidenceError("Unsupported source evidence schema version.")
    if policy.get("policy_version") != MODEL_SOURCE_EVIDENCE_POLICY_VERSION:
        raise ModelSourceEvidenceError("Unsupported source evidence policy version.")
    if policy.get("provider") != PROVIDER:
        raise ModelSourceEvidenceError("Unsupported model metadata provider.")
    endpoint = str(policy.get("endpoint") or "").strip().rstrip("/")
    if endpoint != "https://huggingface.co":
        raise ModelSourceEvidenceError("Only the allow-listed Hugging Face endpoint is supported.")
    repository = _validate_repository(policy.get("repository"))
    if repository != str(backend.get("model", {}).get("repository") or ""):
        raise ModelSourceEvidenceError("Policy repository must match the backend registry.")
    _validate_revision(policy.get("requested_revision"), allow_symbolic=True)
    limits = policy.get("limits")
    if not isinstance(limits, Mapping):
        raise ModelSourceEvidenceError("Source evidence policy requires limits.")
    ranges = {
        "timeout_seconds": (5, 120),
        "maximum_requests": (1, 2),
        "maximum_response_bytes": (1000, 20 * 1024 * 1024),
        "maximum_files": (1, 5000),
    }
    for field, (minimum, maximum) in ranges.items():
        value = limits.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ModelSourceEvidenceError(
                "{} must be an integer between {} and {}.".format(field, minimum, maximum)
            )
    if int(limits["maximum_requests"]) != 2:
        raise ModelSourceEvidenceError(
            "The initial symbolic revision policy requires exactly two bounded requests."
        )
    safety = policy.get("safety")
    if not isinstance(safety, Mapping):
        raise ModelSourceEvidenceError("Source evidence policy requires safety controls.")
    if safety.get("public_metadata_queries_allowed") is not True:
        raise ModelSourceEvidenceError("Public read-only metadata queries must be explicit.")
    for field in FALSE_SAFETY_FIELDS:
        if safety.get(field) is not False:
            raise ModelSourceEvidenceError(
                "Source evidence policy must explicitly set {} to false.".format(field)
            )
    return policy, raw


class HuggingFacePublicMetadataClient:
    """Minimal unauthenticated client for bounded public model-info responses."""

    def __init__(self, endpoint: str = "https://huggingface.co"):
        self.endpoint = endpoint.rstrip("/")
        if self.endpoint != "https://huggingface.co":
            raise ModelSourceEvidenceError("Unsafe Hugging Face endpoint.")

    def model_info(
        self,
        repository: str,
        revision: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
    ) -> ProviderResponse:
        repository = _validate_repository(repository)
        revision = _validate_revision(revision, allow_symbolic=True)
        quoted_repository = urllib.parse.quote(repository, safe="/")
        quoted_revision = urllib.parse.quote(revision, safe="")
        url = "{}/api/models/{}/revision/{}?blobs=true".format(
            self.endpoint, quoted_repository, quoted_revision
        )
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "GenomeAgent-model-source-evidence/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                final_url = str(response.geturl())
                parsed = urllib.parse.urlparse(final_url)
                if parsed.scheme != "https" or parsed.hostname != "huggingface.co":
                    raise ModelSourceEvidenceError(
                        "Provider redirected outside the allow-listed endpoint."
                    )
                body = response.read(maximum_response_bytes + 1)
                if len(body) > maximum_response_bytes:
                    raise ModelSourceEvidenceError(
                        "Provider metadata response exceeded the configured bound."
                    )
                status_code = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            raise ModelSourceEvidenceError(
                "Hugging Face metadata request failed with HTTP {}.".format(exc.code)
            ) from exc
        except urllib.error.URLError as exc:
            raise ModelSourceEvidenceError(
                "Hugging Face metadata request failed: {}".format(exc.reason)
            ) from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelSourceEvidenceError("Provider returned invalid JSON metadata.") from exc
        if not isinstance(payload, dict):
            raise ModelSourceEvidenceError("Provider metadata must be a JSON object.")
        return ProviderResponse(
            payload=payload,
            body_sha256=_sha256_bytes(body),
            body_bytes=len(body),
            final_url=final_url,
            status_code=status_code,
        )


def _safe_file_path(value: Any) -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if (
        not text
        or text.startswith("/")
        or "\\" in text
        or "//" in text
        or "\x00" in text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ModelSourceEvidenceError("Provider returned an unsafe repository file path.")
    return text


def _hex_or_none(value: Any, lengths: tuple[int, ...]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not any(re.fullmatch(r"[0-9a-f]{{{}}}".format(length), normalized) for length in lengths):
        return None
    return normalized


def _license_identifier(payload: Mapping[str, Any]) -> Optional[str]:
    declared = None
    card = payload.get("cardData")
    if isinstance(card, Mapping):
        declared = card.get("license")
    if not declared:
        tags = payload.get("tags")
        if isinstance(tags, list):
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("license:"):
                    declared = tag.split(":", 1)[1]
                    break
    normalized = str(declared or "").strip().lower()
    mapping = {
        "apache-2.0": "Apache-2.0",
        "mit": "MIT",
        "bsd-3-clause": "BSD-3-Clause",
    }
    return mapping.get(normalized)


def _normalize_provider_payload(
    payload: Mapping[str, Any],
    repository: str,
    maximum_files: int,
) -> dict[str, Any]:
    provider_id = str(payload.get("id") or payload.get("modelId") or "")
    if provider_id and provider_id.lower() != repository.lower():
        raise ModelSourceEvidenceError("Provider repository identity mismatch.")
    resolved_revision = _validate_revision(payload.get("sha"), allow_symbolic=False)
    siblings = payload.get("siblings")
    if not isinstance(siblings, list) or not siblings:
        raise ModelSourceEvidenceError("Provider returned no repository file inventory.")
    if len(siblings) > maximum_files:
        raise ModelSourceEvidenceError("Provider file inventory exceeded the configured bound.")
    files = []
    seen = set()
    missing_sizes = []
    weight_files = 0
    weight_provider_sha256 = 0
    for sibling in siblings:
        if not isinstance(sibling, Mapping):
            raise ModelSourceEvidenceError("Provider file inventory contains a non-object.")
        file_path = _safe_file_path(sibling.get("rfilename") or sibling.get("path"))
        if file_path in seen:
            raise ModelSourceEvidenceError("Provider file inventory contains duplicate paths.")
        seen.add(file_path)
        size = sibling.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            normalized_size = None
            missing_sizes.append(file_path)
        else:
            normalized_size = size
        lfs = sibling.get("lfs")
        if not isinstance(lfs, Mapping):
            lfs = {}
        lfs_sha256 = _hex_or_none(lfs.get("sha256") or lfs.get("oid"), (64,))
        blob_id = _hex_or_none(sibling.get("blobId") or sibling.get("blob_id"), (40, 64))
        xet_hash = _hex_or_none(sibling.get("xetHash") or sibling.get("xet_hash"), (64,))
        is_weight = file_path.lower().endswith(WEIGHT_SUFFIXES)
        if is_weight:
            weight_files += 1
            if lfs_sha256:
                weight_provider_sha256 += 1
        files.append({
            "path": file_path,
            "size_bytes": normalized_size,
            "blob_id": blob_id,
            "lfs_sha256": lfs_sha256,
            "lfs_size_bytes": (
                lfs.get("size")
                if isinstance(lfs.get("size"), int) and not isinstance(lfs.get("size"), bool)
                else None
            ),
            "xet_hash": xet_hash,
            "is_weight_file": is_weight,
        })
    files.sort(key=lambda item: item["path"])
    inventory_sha256 = _sha256_value(files)
    source_total = None if missing_sizes else sum(int(item["size_bytes"]) for item in files)
    license_files = [
        item["path"] for item in files
        if PurePosixPath(item["path"]).name.lower() in LICENSE_FILES
    ]
    limitations = []
    if missing_sizes:
        limitations.append("file_sizes_incomplete")
    if weight_files and weight_provider_sha256 != weight_files:
        limitations.append("provider_weight_sha256_incomplete")
    license_identifier = _license_identifier(payload)
    if license_identifier is None:
        limitations.append("recognized_license_metadata_missing")
    if not license_files:
        limitations.append("license_file_not_listed")
    return {
        "repository": repository,
        "resolved_revision": resolved_revision,
        "private": bool(payload.get("private")),
        "gated": payload.get("gated", False),
        "files": files,
        "file_count": len(files),
        "files_missing_size": missing_sizes,
        "source_total_bytes": source_total,
        "source_inventory_sha256": inventory_sha256,
        "provider_reported_used_storage_bytes": payload.get("usedStorage"),
        "weight_file_count": weight_files,
        "weight_files_with_provider_lfs_sha256": weight_provider_sha256,
        "license_identifier": license_identifier,
        "license_files": license_files,
        "limitations": limitations,
    }


def _readiness(
    backend: Mapping[str, Any],
    normalized: Mapping[str, Any],
    configuration_current: bool,
) -> dict[str, Any]:
    blockers = []
    if not configuration_current:
        blockers.append("source_evidence_configuration_changed")
    if normalized.get("private"):
        blockers.append("source_repository_private")
    gated = normalized.get("gated")
    if gated not in {False, None}:
        blockers.append("source_repository_gated")
    if normalized.get("source_total_bytes") is None:
        blockers.append("source_inventory_size_incomplete")
    observed_license = normalized.get("license_identifier")
    expected_license = backend.get("model", {}).get("license")
    if observed_license != expected_license:
        blockers.append("source_license_mismatch_or_unknown")
    if not normalized.get("license_files"):
        blockers.append("source_license_file_missing")
    if blockers:
        status = (
            "source_metadata_evidence_stale"
            if blockers == ["source_evidence_configuration_changed"]
            else "source_metadata_attention_required"
        )
        next_action = "review_source_metadata_blockers"
    else:
        status = "source_metadata_ready_for_researcher_review"
        next_action = "review_and_apply_acquisition_spec_proposal"
    return {
        "status": status,
        "next_safe_action": next_action,
        "blockers": blockers,
        "limitations": list(normalized.get("limitations", [])),
        "automatic_specification_update_allowed": False,
        "automatic_model_download_allowed": False,
    }


def _validate_snapshot(payload: Mapping[str, Any], path: Path, backend_id: str) -> None:
    if payload.get("schema_version") != "1.0":
        raise ModelSourceEvidenceError("Unsupported model source snapshot schema.")
    if payload.get("policy_version") != MODEL_SOURCE_EVIDENCE_POLICY_VERSION:
        raise ModelSourceEvidenceError("Unsupported model source snapshot policy.")
    if payload.get("source_mode") != "bounded_public_metadata_queries":
        raise ModelSourceEvidenceError("Refusing model source evidence with unsafe mode.")
    if payload.get("backend_id") != backend_id:
        raise ModelSourceEvidenceError("Model source snapshot backend mismatch.")
    evidence_id = str(payload.get("evidence_id") or "")
    if path.stem != evidence_id or not re.fullmatch(
        r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", evidence_id
    ):
        raise ModelSourceEvidenceError("Model source evidence filename and ID mismatch.")
    safety = payload.get("safety")
    if not isinstance(safety, Mapping) or safety.get("public_metadata_queries") is not True:
        raise ModelSourceEvidenceError("Model source snapshot has invalid safety evidence.")
    for field in FALSE_SAFETY_FIELDS:
        if safety.get(field) is not False:
            raise ModelSourceEvidenceError(
                "Model source snapshot must explicitly disable {}.".format(field)
            )


def _snapshot_report(snapshot: Mapping[str, Any]) -> str:
    normalized = snapshot["normalized_metadata"]
    readiness = snapshot["readiness"]
    lines = [
        "# GenomeAgent Model Source Metadata Evidence",
        "",
        "- Backend: `{}`".format(snapshot["backend_id"]),
        "- Repository: `{}`".format(normalized["repository"]),
        "- Requested revision: `{}`".format(snapshot["requested_revision"]),
        "- Resolved revision: `{}`".format(normalized["resolved_revision"]),
        "- Files: {}".format(normalized["file_count"]),
        "- Source bytes: `{}`".format(normalized["source_total_bytes"]),
        "- Inventory SHA-256: `{}`".format(normalized["source_inventory_sha256"]),
        "- License metadata: `{}`".format(normalized["license_identifier"]),
        "- Status: **{}**".format(readiness["status"]),
        "- Automatic specification update: **no**",
        "",
        "## Limitations",
        "",
    ]
    if readiness["limitations"]:
        lines.extend("- `{}`".format(item) for item in readiness["limitations"])
    else:
        lines.append("No deterministic metadata limitations were detected.")
    lines.extend([
        "",
        "## Safety boundary",
        "",
        "This evidence used two bounded, unauthenticated, read-only public model-info "
        "queries: one symbolic revision lookup and one immutable-revision confirmation. "
        "It did not download repository files or model weights, contact Roihu, submit a "
        "job, allocate a GPU, hash large files or update GenomeAgent configuration.",
        "",
    ])
    return "\n".join(lines)


class ModelSourceEvidenceCollector:
    """Collect one immutable public model metadata snapshot."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/source_evidence"),
        evidence_root: Path = Path("workspace/model_source_evidence"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.evidence_root = Path(evidence_root)

    def policy(
        self, backend_id: str
    ) -> tuple[Path, dict[str, Any], bytes, dict[str, Any], bytes]:
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        path = self.policy_root / (str(backend["backend_id"]) + ".json")
        policy, policy_raw = _validate_policy(path, backend)
        return path, policy, policy_raw, backend, backend_raw

    def collect(
        self,
        backend_id: str,
        client: Optional[ModelMetadataClient] = None,
        stamp: Optional[str] = None,
    ) -> ModelSourceCollectionResult:
        policy_path, policy, policy_raw, backend, backend_raw = self.policy(backend_id)
        backend_path, _, _ = self.registry.backend(backend_id)
        client = client or HuggingFacePublicMetadataClient(str(policy["endpoint"]))
        if client.endpoint.rstrip("/") != str(policy["endpoint"]).rstrip("/"):
            raise ModelSourceEvidenceError("Metadata client endpoint does not match policy.")
        now = datetime.now(timezone.utc)
        evidence_id = stamp or now.strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", evidence_id):
            raise ModelSourceEvidenceError("Unsafe or invalid evidence timestamp.")
        timeout = int(policy["limits"]["timeout_seconds"])
        maximum_response = int(policy["limits"]["maximum_response_bytes"])
        repository = str(policy["repository"])
        requested_revision = str(policy["requested_revision"])
        symbolic = client.model_info(
            repository, requested_revision, timeout, maximum_response
        )
        resolved_revision = _validate_revision(
            symbolic.payload.get("sha"), allow_symbolic=False
        )
        pinned = client.model_info(
            repository, resolved_revision, timeout, maximum_response
        )
        pinned_revision = _validate_revision(
            pinned.payload.get("sha"), allow_symbolic=False
        )
        if pinned_revision != resolved_revision:
            raise ModelSourceEvidenceError("Immutable revision confirmation mismatch.")
        normalized = _normalize_provider_payload(
            pinned.payload,
            repository,
            int(policy["limits"]["maximum_files"]),
        )
        readiness = _readiness(backend, normalized, configuration_current=True)
        sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "model_source_evidence_policy"),
        ]
        sources.sort(key=lambda item: (item["artifact_type"], item["path"]))
        snapshot = {
            "schema_version": "1.0",
            "policy_version": MODEL_SOURCE_EVIDENCE_POLICY_VERSION,
            "source_mode": "bounded_public_metadata_queries",
            "backend_id": backend["backend_id"],
            "environment_id": backend["environment_id"],
            "provider": policy["provider"],
            "endpoint": policy["endpoint"],
            "repository": repository,
            "requested_revision": requested_revision,
            "evidence_id": evidence_id,
            "observed_at_utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "query": {
                "requests_performed": 2,
                "maximum_requests": policy["limits"]["maximum_requests"],
                "timeout_seconds_per_request": timeout,
                "maximum_response_bytes_per_request": maximum_response,
                "files_metadata": True,
                "authentication_used": False,
                "repository_files_requested": False,
            },
            "provider_responses": [
                {
                    "purpose": "resolve_symbolic_revision",
                    "requested_revision": requested_revision,
                    "resolved_revision": resolved_revision,
                    "body_sha256": symbolic.body_sha256,
                    "body_bytes": symbolic.body_bytes,
                    "final_url": symbolic.final_url,
                    "status_code": symbolic.status_code,
                },
                {
                    "purpose": "confirm_immutable_revision_inventory",
                    "requested_revision": resolved_revision,
                    "resolved_revision": pinned_revision,
                    "body_sha256": pinned.body_sha256,
                    "body_bytes": pinned.body_bytes,
                    "final_url": pinned.final_url,
                    "status_code": pinned.status_code,
                },
            ],
            "source_artifacts": sources,
            "normalized_metadata": normalized,
            "readiness": readiness,
            "safety": {
                "public_metadata_queries": True,
                **{field: False for field in FALSE_SAFETY_FIELDS},
            },
        }
        evidence_dir = self.evidence_root / str(backend["backend_id"])
        evidence_path = evidence_dir / (evidence_id + ".json")
        report_path = evidence_dir / (evidence_id + ".md")
        if evidence_path.exists() or report_path.exists():
            raise ModelSourceEvidenceError(
                "Refusing to overwrite immutable model source evidence: {}".format(evidence_id)
            )
        _write_immutable(evidence_path, _pretty_json(snapshot))
        _write_immutable(report_path, _snapshot_report(snapshot))
        return ModelSourceCollectionResult(
            backend_id=str(backend["backend_id"]),
            evidence_id=evidence_id,
            evidence_path=evidence_path,
            status=str(readiness["status"]),
            resolved_revision=str(normalized["resolved_revision"]),
            source_total_bytes=int(normalized["source_total_bytes"] or 0),
            source_inventory_sha256=str(normalized["source_inventory_sha256"]),
        )


def _proposal(
    backend: Mapping[str, Any],
    specification: Mapping[str, Any],
    latest: Mapping[str, Any],
) -> dict[str, Any]:
    metadata = latest["normalized_metadata"]
    readiness = latest["readiness"]
    current_source = specification.get("source", {})
    if not isinstance(current_source, Mapping):
        current_source = {}
    identity_fields = {
        "resolved_revision": metadata.get("resolved_revision"),
        "source_inventory_sha256": metadata.get("source_inventory_sha256"),
        "source_total_bytes": metadata.get("source_total_bytes"),
        "license_identifier": metadata.get("license_identifier"),
    }
    identity_matches = all(
        current_source.get(field) == value
        for field, value in identity_fields.items()
    )
    recorded_approval = current_source.get("license_approval")
    approval_matches = (
        identity_matches
        and current_source.get("license_review_status") == "reviewed_accepted"
        and isinstance(recorded_approval, Mapping)
        and recorded_approval.get("source_evidence_id") == latest.get("evidence_id")
        and recorded_approval.get("source_evidence_sha256") == latest.get("source_sha256")
        and recorded_approval.get("license_identifier") == metadata.get("license_identifier")
        and recorded_approval.get("resolved_revision") == metadata.get("resolved_revision")
    )
    proposed = {
        **identity_fields,
        "license_review_status": (
            "reviewed_accepted" if approval_matches else "unreviewed"
        ),
    }
    changes = []
    for field, value in proposed.items():
        if current_source.get(field) != value:
            changes.append({
                "field": "source.{}".format(field),
                "current": current_source.get(field),
                "proposed": value,
            })
    applicable = readiness.get("status") == "source_metadata_ready_for_researcher_review"
    if not applicable:
        status = "blocked_by_source_metadata_evidence"
    elif changes:
        status = "researcher_review_required"
    elif approval_matches:
        status = "identity_and_license_approval_reflected"
    else:
        status = "identity_values_already_reflected"
    revision = str(metadata.get("resolved_revision") or "")
    repository = str(metadata.get("repository") or "")
    license_files = list(metadata.get("license_files", []))
    license_path = license_files[0] if license_files else None
    return {
        "schema_version": "1.0",
        "policy_version": MODEL_SOURCE_EVIDENCE_POLICY_VERSION,
        "backend_id": backend["backend_id"],
        "status": status,
        "applicable": applicable,
        "source_evidence_id": latest["evidence_id"],
        "source_evidence_sha256": latest["source_sha256"],
        "target_specification": "config/ai/acquisition/{}.json".format(
            backend["backend_id"]
        ),
        "proposed_source_values": proposed,
        "changes": changes,
        "license_review": {
            "observed_identifier": metadata.get("license_identifier"),
            "license_files": license_files,
            "review_url": (
                "https://huggingface.co/{}/blob/{}/{}".format(
                    repository,
                    revision,
                    urllib.parse.quote(str(license_path), safe="/"),
                )
                if repository and revision and license_path
                else None
            ),
            "researcher_acceptance_recorded": approval_matches,
            "recorded_approval_id": (
                recorded_approval.get("approval_id") if approval_matches else None
            ),
        },
        "automatic_application_allowed": False,
        "automatic_license_acceptance_allowed": False,
        "automatic_registry_update_allowed": False,
    }


def _state_report(state: Mapping[str, Any], proposal: Mapping[str, Any]) -> str:
    latest = state["latest"]
    metadata = latest["normalized_metadata"]
    lines = [
        "# GenomeAgent Model Source Evidence State",
        "",
        "- Backend: `{}`".format(state["backend_id"]),
        "- Source snapshots: {}".format(state["source_snapshots"]),
        "- Latest evidence: `{}`".format(latest["evidence_id"]),
        "- Resolved revision: `{}`".format(metadata["resolved_revision"]),
        "- Source bytes: `{}`".format(metadata["source_total_bytes"]),
        "- Inventory SHA-256: `{}`".format(metadata["source_inventory_sha256"]),
        "- Status: **{}**".format(latest["readiness"]["status"]),
        "- Proposal: **{}**".format(proposal["status"]),
        "- Automatic specification update: **no**",
        "",
        "## Next boundary",
        "",
        "A researcher must review the immutable revision, inventory, size and license "
        "evidence before editing the acquisition specification. This state does not "
        "download model files, approve a license, update configuration or authorize "
        "acquisition.",
        "",
    ]
    return "\n".join(lines)


class ModelSourceEvidenceCore:
    """Replay immutable model source snapshots into deterministic current state."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/source_evidence"),
        specification_root: Path = Path("config/ai/acquisition"),
        evidence_root: Path = Path("workspace/model_source_evidence"),
        state_root: Path = Path("workspace/model_source_state"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.specification_root = Path(specification_root)
        self.evidence_root = Path(evidence_root)
        self.state_root = Path(state_root)

    def ingest(self, backend_id: str) -> ModelSourceIngestResult:
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        normalized_id = str(backend["backend_id"])
        policy_path = self.policy_root / (normalized_id + ".json")
        policy, policy_raw = _validate_policy(policy_path, backend)
        specification_path = self.specification_root / (normalized_id + ".json")
        specification, specification_raw = _read_json(
            specification_path, "model acquisition specification"
        )
        current_sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "model_source_evidence_policy"),
        ]
        current_sources.sort(key=lambda item: (item["artifact_type"], item["path"]))
        paths = sorted((self.evidence_root / normalized_id).glob("*.json"))
        if not paths:
            raise ModelSourceEvidenceError(
                "No model source evidence snapshots found for {}.".format(normalized_id)
            )
        records = []
        for path in paths:
            payload, raw = _read_json(path, "model source evidence snapshot")
            _validate_snapshot(payload, path, normalized_id)
            normalized = payload.get("normalized_metadata")
            if not isinstance(normalized, Mapping):
                raise ModelSourceEvidenceError("Snapshot has no normalized metadata.")
            configuration_current = payload.get("source_artifacts") == current_sources
            readiness = _readiness(backend, normalized, configuration_current)
            records.append({
                "evidence_id": payload["evidence_id"],
                "observed_at_utc": payload.get("observed_at_utc", ""),
                "source_path": str(path),
                "source_sha256": _sha256_bytes(raw),
                "configuration_current": configuration_current,
                "normalized_metadata": dict(normalized),
                "readiness": readiness,
            })
        records.sort(key=lambda item: (item["observed_at_utc"], item["evidence_id"]))
        latest = records[-1]
        proposal = _proposal(backend, specification, latest)
        state = {
            "schema_version": "1.0",
            "policy_version": MODEL_SOURCE_EVIDENCE_POLICY_VERSION,
            "backend_id": normalized_id,
            "source_snapshots": len(records),
            "latest": latest,
            "automatic_specification_update_allowed": False,
            "automatic_model_download_allowed": False,
            "automatic_registry_update_allowed": False,
        }
        history = [
            {
                key: value for key, value in record.items()
                if key != "normalized_metadata"
            }
            for record in records
        ]
        provenance = {
            "schema_version": "1.0",
            "policy_version": MODEL_SOURCE_EVIDENCE_POLICY_VERSION,
            "backend_id": normalized_id,
            "source_artifacts": current_sources,
            "acquisition_specification": _source(
                specification_path,
                specification_raw,
                "model_acquisition_specification_proposal_target",
            ),
            "evidence_snapshots": [
                {
                    "evidence_id": record["evidence_id"],
                    "path": record["source_path"],
                    "sha256": record["source_sha256"],
                }
                for record in records
            ],
            "derivation": "deterministic_local_model_source_evidence_replay",
            "remote_access_during_ingest": False,
            "automatic_specification_update": False,
            "automatic_registry_update": False,
        }
        state_dir = self.state_root / normalized_id
        artifacts = (
            state_dir / "current_source_metadata.json",
            state_dir / "evidence_history.jsonl",
            state_dir / "acquisition_spec_proposal.json",
            state_dir / "report.md",
            state_dir / "provenance.json",
        )
        _atomic_write(artifacts[0], _pretty_json(state))
        _atomic_write(
            artifacts[1],
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in history),
        )
        _atomic_write(artifacts[2], _pretty_json(proposal))
        _atomic_write(artifacts[3], _state_report(state, proposal))
        _atomic_write(artifacts[4], _pretty_json(provenance))
        return ModelSourceIngestResult(
            backend_id=normalized_id,
            state_dir=state_dir,
            snapshots=len(records),
            status=str(latest["readiness"]["status"]),
            next_safe_action=str(latest["readiness"]["next_safe_action"]),
            blockers=tuple(latest["readiness"]["blockers"]),
            proposal_status=str(proposal["status"]),
            artifact_paths=artifacts,
        )


def _approval_report(approval: Mapping[str, Any]) -> str:
    return "\n".join([
        "# GenomeAgent Model Source Approval",
        "",
        "- Backend: `{}`".format(approval["backend_id"]),
        "- Approval ID: `{}`".format(approval["approval_id"]),
        "- Source evidence: `{}`".format(approval["source_evidence_id"]),
        "- Resolved revision: `{}`".format(
            approval["approved_source_values"]["resolved_revision"]
        ),
        "- License: `{}`".format(approval["accepted_license_identifier"]),
        "- Reviewer: `{}`".format(approval["reviewer"]),
        "- Accepted at: `{}`".format(approval["accepted_at_utc"]),
        "- Authorized change: **acquisition source identity and license record only**",
        "- Model download: **no**",
        "",
        "This immutable record proves explicit researcher approval for the exact source "
        "evidence above. It does not authorize model download, remote access, Slurm or "
        "GPU execution, registry updates, backend activation, inference or training.",
        "",
    ])


def _validate_approval_artifact(
    approval: Mapping[str, Any], approval_id: str, backend_id: str
) -> None:
    if approval.get("schema_version") != "1.0":
        raise ModelSourceEvidenceError("Unsupported model source approval schema.")
    if approval.get("policy_version") != MODEL_SOURCE_APPROVAL_POLICY_VERSION:
        raise ModelSourceEvidenceError("Unsupported model source approval policy.")
    if approval.get("source_mode") != "explicit_researcher_license_approval":
        raise ModelSourceEvidenceError("Invalid model source approval mode.")
    if approval.get("approval_id") != approval_id:
        raise ModelSourceEvidenceError("Model source approval ID mismatch.")
    if approval.get("backend_id") != backend_id:
        raise ModelSourceEvidenceError("Model source approval backend mismatch.")
    approved_values = approval.get("approved_source_values")
    if not isinstance(approved_values, Mapping):
        raise ModelSourceEvidenceError("Model source approval has no approved values.")
    identity = {
        "policy_version": MODEL_SOURCE_APPROVAL_POLICY_VERSION,
        "backend_id": backend_id,
        "source_evidence_id": approval.get("source_evidence_id"),
        "source_evidence_sha256": approval.get("source_evidence_sha256"),
        "reviewer": approval.get("reviewer"),
        "accepted_license_identifier": approval.get("accepted_license_identifier"),
        "review_url": approval.get("review_url"),
        "approved_source_values": dict(approved_values),
    }
    if _sha256_value(identity) != approval_id:
        raise ModelSourceEvidenceError("Model source approval content digest mismatch.")
    for field in (
        "source_evidence_sha256",
        "proposal_sha256",
        "specification_before_sha256",
        "specification_after_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(approval.get(field) or "")):
            raise ModelSourceEvidenceError(
                "Model source approval has invalid {}.".format(field)
            )
    approval_record = approval.get("license_approval_record")
    if not isinstance(approval_record, Mapping):
        raise ModelSourceEvidenceError("Model source approval has no license record.")
    expected_record_values = {
        "approval_id": approval_id,
        "source_evidence_id": approval.get("source_evidence_id"),
        "source_evidence_sha256": approval.get("source_evidence_sha256"),
        "reviewer": approval.get("reviewer"),
        "accepted_at_utc": approval.get("accepted_at_utc"),
        "license_identifier": approval.get("accepted_license_identifier"),
        "review_url": approval.get("review_url"),
        "resolved_revision": approved_values.get("resolved_revision"),
    }
    for field, expected in expected_record_values.items():
        if approval_record.get(field) != expected:
            raise ModelSourceEvidenceError(
                "Model source approval license record mismatch for {}.".format(field)
            )
    safety = approval.get("safety")
    required_false = (
        "model_download",
        "repository_file_download",
        "remote_access",
        "remote_writes",
        "job_submission",
        "gpu_allocation",
        "automatic_registry_update",
        "automatic_backend_activation",
        "inference_execution",
        "training_execution",
    )
    if not isinstance(safety, Mapping):
        raise ModelSourceEvidenceError("Model source approval has no safety record.")
    if safety.get("acquisition_source_configuration_update") is not True:
        raise ModelSourceEvidenceError("Approval does not identify its narrow mutation.")
    for field in required_false:
        if safety.get(field) is not False:
            raise ModelSourceEvidenceError(
                "Model source approval must explicitly disable {}.".format(field)
            )


class ModelSourceApprovalCore:
    """Apply an explicit researcher approval to one exact source evidence snapshot."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/source_evidence"),
        specification_root: Path = Path("config/ai/acquisition"),
        evidence_root: Path = Path("workspace/model_source_evidence"),
        state_root: Path = Path("workspace/model_source_state"),
        approval_root: Path = Path("workspace/model_source_approvals"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.specification_root = Path(specification_root)
        self.evidence_root = Path(evidence_root)
        self.state_root = Path(state_root)
        self.approval_root = Path(approval_root)

    def approve(
        self,
        backend_id: str,
        evidence_id: str,
        reviewer: str,
        accepted_license: str,
        confirmation: bool,
        accepted_at: Optional[datetime] = None,
    ) -> ModelSourceApprovalResult:
        if confirmation is not True:
            raise ModelSourceEvidenceError(
                "Explicit --confirm-license-review is required."
            )
        reviewer = str(reviewer or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,127}", reviewer):
            raise ModelSourceEvidenceError("Unsafe or empty reviewer identifier.")
        if not re.fullmatch(
            r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", str(evidence_id or "")
        ):
            raise ModelSourceEvidenceError("Unsafe or invalid source evidence ID.")
        accepted_license = str(accepted_license or "").strip()

        core = ModelSourceEvidenceCore(
            registry=self.registry,
            policy_root=self.policy_root,
            specification_root=self.specification_root,
            evidence_root=self.evidence_root,
            state_root=self.state_root,
        )
        ingested = core.ingest(backend_id)
        state_path = ingested.state_dir / "current_source_metadata.json"
        proposal_path = ingested.state_dir / "acquisition_spec_proposal.json"
        state, _ = _read_json(state_path, "current model source state")
        proposal, proposal_raw = _read_json(
            proposal_path, "model acquisition specification proposal"
        )
        latest = state.get("latest")
        if not isinstance(latest, Mapping):
            raise ModelSourceEvidenceError("Current source state has no latest evidence.")
        if latest.get("evidence_id") != evidence_id:
            raise ModelSourceEvidenceError(
                "Approval evidence ID is not the latest current source observation."
            )
        readiness = latest.get("readiness")
        if not isinstance(readiness, Mapping) or readiness.get("status") != (
            "source_metadata_ready_for_researcher_review"
        ):
            raise ModelSourceEvidenceError(
                "Current source metadata is not ready for researcher approval."
            )
        if proposal.get("source_evidence_id") != evidence_id:
            raise ModelSourceEvidenceError("Proposal source evidence ID mismatch.")
        if proposal.get("applicable") is not True:
            raise ModelSourceEvidenceError("Source acquisition proposal is not applicable.")
        license_review = proposal.get("license_review")
        if not isinstance(license_review, Mapping):
            raise ModelSourceEvidenceError("Proposal has no license review evidence.")
        observed_license = str(license_review.get("observed_identifier") or "")
        if accepted_license != observed_license:
            raise ModelSourceEvidenceError(
                "Accepted license must exactly match observed license metadata."
            )
        review_url = str(license_review.get("review_url") or "")
        if not review_url.startswith("https://huggingface.co/"):
            raise ModelSourceEvidenceError("Proposal has no safe license review URL.")

        backend_path, backend, _ = self.registry.backend(backend_id)
        normalized_id = str(backend["backend_id"])
        specification_path = self.specification_root / (normalized_id + ".json")
        if specification_path.is_symlink():
            raise ModelSourceEvidenceError(
                "Refusing to update a symlinked acquisition specification."
            )
        try:
            specification, specification_raw = validate_acquisition_specification(
                specification_path, backend
            )
        except ModelAcquisitionError as exc:
            raise ModelSourceEvidenceError(
                "Current acquisition specification is invalid: {}".format(exc)
            ) from exc
        del backend_path
        if specification["source"].get("license_review_status") == "reviewed_rejected":
            raise ModelSourceEvidenceError(
                "The acquisition specification records a rejected license."
            )

        proposed_values = proposal.get("proposed_source_values")
        if not isinstance(proposed_values, Mapping):
            raise ModelSourceEvidenceError("Proposal has no source values.")
        approved_values = {
            "resolved_revision": proposed_values.get("resolved_revision"),
            "source_inventory_sha256": proposed_values.get(
                "source_inventory_sha256"
            ),
            "source_total_bytes": proposed_values.get("source_total_bytes"),
            "license_identifier": proposed_values.get("license_identifier"),
            "license_review_status": "reviewed_accepted",
        }
        if approved_values["license_identifier"] != accepted_license:
            raise ModelSourceEvidenceError("Proposal license identity changed.")
        source_evidence_sha256 = str(proposal.get("source_evidence_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", source_evidence_sha256):
            raise ModelSourceEvidenceError("Proposal source evidence digest is invalid.")
        identity = {
            "policy_version": MODEL_SOURCE_APPROVAL_POLICY_VERSION,
            "backend_id": normalized_id,
            "source_evidence_id": evidence_id,
            "source_evidence_sha256": source_evidence_sha256,
            "reviewer": reviewer,
            "accepted_license_identifier": accepted_license,
            "review_url": review_url,
            "approved_source_values": approved_values,
        }
        approval_id = _sha256_value(identity)
        approval_dir = self.approval_root / normalized_id
        approval_path = approval_dir / (approval_id + ".json")
        report_path = approval_dir / (approval_id + ".md")

        current_specification_sha256 = _sha256_bytes(specification_raw)
        existing = None
        if approval_path.exists():
            existing, _ = _read_json(approval_path, "model source approval")
            _validate_approval_artifact(existing, approval_id, normalized_id)
            _write_immutable(report_path, _approval_report(existing))
            expected_after = str(existing.get("specification_after_sha256") or "")
            expected_before = str(existing.get("specification_before_sha256") or "")
            if current_specification_sha256 == expected_after:
                if specification["source"].get("license_approval") != existing.get(
                    "license_approval_record"
                ):
                    raise ModelSourceEvidenceError(
                        "Approved specification and approval provenance disagree."
                    )
                return ModelSourceApprovalResult(
                    backend_id=normalized_id,
                    approval_id=approval_id,
                    approval_path=approval_path,
                    specification_path=specification_path,
                    applied=False,
                    already_applied=True,
                    status="approved_source_identity_already_recorded",
                    next_safe_action="rerun_model_acquisition_plan",
                )
            if current_specification_sha256 != expected_before:
                raise ModelSourceEvidenceError(
                    "Acquisition specification changed after the approval record."
                )

        if existing is None:
            accepted_at = accepted_at or datetime.now(timezone.utc)
            if accepted_at.tzinfo is None:
                raise ModelSourceEvidenceError("Approval timestamp must include a timezone.")
            accepted_at_utc = accepted_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            license_approval_record = {
                "schema_version": "1.0",
                "approval_id": approval_id,
                "source_evidence_id": evidence_id,
                "source_evidence_sha256": source_evidence_sha256,
                "reviewer": reviewer,
                "accepted_at_utc": accepted_at_utc,
                "license_identifier": accepted_license,
                "review_url": review_url,
                "resolved_revision": approved_values["resolved_revision"],
            }
        else:
            license_approval_record = dict(existing["license_approval_record"])
            approved_values = dict(existing["approved_source_values"])
            accepted_at_utc = str(existing["accepted_at_utc"])

        updated_specification = copy.deepcopy(specification)
        updated_specification["source"].update(approved_values)
        updated_specification["source"]["license_approval"] = license_approval_record
        updated_text = _pretty_json(updated_specification)
        updated_raw = updated_text.encode("utf-8")
        updated_sha256 = _sha256_bytes(updated_raw)
        if existing is not None and updated_sha256 != existing.get(
            "specification_after_sha256"
        ):
            raise ModelSourceEvidenceError(
                "Recorded approval does not reproduce the approved specification."
            )

        with tempfile.TemporaryDirectory(
            prefix=".genomeagent-source-approval-",
            dir=str(specification_path.parent),
        ) as temporary_dir:
            candidate_path = Path(temporary_dir) / specification_path.name
            _atomic_write(candidate_path, updated_text)
            try:
                validate_acquisition_specification(candidate_path, backend)
            except ModelAcquisitionError as exc:
                raise ModelSourceEvidenceError(
                    "Approved acquisition specification would be invalid: {}".format(exc)
                ) from exc

        if existing is None:
            approval = {
                "schema_version": "1.0",
                "policy_version": MODEL_SOURCE_APPROVAL_POLICY_VERSION,
                "source_mode": "explicit_researcher_license_approval",
                "backend_id": normalized_id,
                "approval_id": approval_id,
                "source_evidence_id": evidence_id,
                "source_evidence_sha256": source_evidence_sha256,
                "proposal_sha256": _sha256_bytes(proposal_raw),
                "reviewer": reviewer,
                "accepted_at_utc": accepted_at_utc,
                "accepted_license_identifier": accepted_license,
                "review_url": review_url,
                "approved_source_values": approved_values,
                "license_approval_record": license_approval_record,
                "specification_path": str(specification_path),
                "specification_before_sha256": current_specification_sha256,
                "specification_after_sha256": updated_sha256,
                "safety": {
                    "acquisition_source_configuration_update": True,
                    "model_download": False,
                    "repository_file_download": False,
                    "remote_access": False,
                    "remote_writes": False,
                    "job_submission": False,
                    "gpu_allocation": False,
                    "automatic_registry_update": False,
                    "automatic_backend_activation": False,
                    "inference_execution": False,
                    "training_execution": False,
                },
            }
            _validate_approval_artifact(approval, approval_id, normalized_id)
            _write_immutable(approval_path, _pretty_json(approval))
            _write_immutable(report_path, _approval_report(approval))

        latest_specification_raw = specification_path.read_bytes()
        if _sha256_bytes(latest_specification_raw) != current_specification_sha256:
            raise ModelSourceEvidenceError(
                "Acquisition specification changed during approval; no update applied."
            )
        _atomic_write(specification_path, updated_text)
        try:
            validate_acquisition_specification(specification_path, backend)
        except ModelAcquisitionError as exc:
            raise ModelSourceEvidenceError(
                "Updated acquisition specification failed validation: {}".format(exc)
            ) from exc
        core.ingest(normalized_id)
        return ModelSourceApprovalResult(
            backend_id=normalized_id,
            approval_id=approval_id,
            approval_path=approval_path,
            specification_path=specification_path,
            applied=True,
            already_applied=False,
            status="approved_source_identity_recorded",
            next_safe_action="rerun_model_acquisition_plan",
        )
