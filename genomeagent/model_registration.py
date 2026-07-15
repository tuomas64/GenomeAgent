#!/usr/bin/env python3
"""Installed-model evidence and controlled AI backend registration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Protocol

from genomeagent.ai_evaluation import AIEvaluationError, AIRegistry


MODEL_REGISTRATION_POLICY_VERSION = "1.0"
REQUIRED_FALSE_POLICY_FIELDS = (
    "remote_writes_allowed",
    "model_weight_reads_allowed",
    "large_file_hashing_allowed",
    "job_submission_allowed",
    "gpu_allocation_allowed",
    "inference_allowed",
    "training_allowed",
    "automatic_registry_update_allowed",
    "automatic_backend_activation_allowed",
)
OBSERVATION_FALSE_FIELDS = (
    "remote_writes",
    "model_weight_contents_read",
    "large_file_hashing",
    "job_submission",
    "gpu_allocation",
    "inference",
    "training",
    "registry_update",
    "backend_activation",
)


class ModelRegistrationError(RuntimeError):
    """Raised when installed-model registration evidence is unsafe or stale."""


class PythonRemoteRunner(Protocol):
    host: str

    def run_python(self, program: str, timeout_seconds: int = 240) -> dict[str, Any]:
        """Run one bounded read-only remote observation."""


@dataclass(frozen=True)
class InstalledModelCollectionResult:
    backend_id: str
    bundle_id: str
    publication_id: str
    evidence_id: str
    evidence_path: Path
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class InstalledModelIngestResult:
    backend_id: str
    state_dir: Path
    snapshots: int
    status: str
    next_safe_action: str
    blockers: tuple[str, ...]
    proposal_status: str
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True)
class ModelRegistrationApprovalResult:
    backend_id: str
    approval_id: str
    approval_path: Path
    backend_path: Path
    applied: bool
    already_applied: bool
    status: str
    next_safe_action: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
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
            raise ModelRegistrationError(
                "Immutable installed-model artifact exists with different content: {}".format(
                    path
                )
            )
        return False
    _atomic_write(path, text)
    return True


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ModelRegistrationError("{} does not exist: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelRegistrationError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise ModelRegistrationError("{} must be a JSON object: {}".format(label, path))
    return value, raw


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "path": str(path),
        "sha256": _sha256_bytes(raw),
    }


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise ModelRegistrationError("Unsafe or empty {}: {!r}".format(label, value))
    return normalized


def _digest(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ModelRegistrationError("Invalid {}.".format(label))
    return normalized


def _revision(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise ModelRegistrationError("Invalid {}.".format(label))
    return normalized


def _timestamp(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", text):
        raise ModelRegistrationError("Invalid {}.".format(label))
    return text


def _safe_absolute(value: Any, label: str) -> str:
    text = str(value or "").strip()
    path = PurePosixPath(text)
    if (
        not text
        or not path.is_absolute()
        or "\x00" in text
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ModelRegistrationError("{} must be a safe absolute path.".format(label))
    return text


def _safe_relative(value: Any, label: str) -> str:
    text = str(value or "").strip()
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ModelRegistrationError("{} must be a safe relative path.".format(label))
    return path.as_posix()


def _false_fields(value: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(value, Mapping):
        raise ModelRegistrationError("{} has no safety record.".format(label))
    for field in fields:
        if value.get(field) is not False:
            raise ModelRegistrationError(
                "{} must explicitly disable {}.".format(label, field)
            )


def _validate_policy(
    path: Path, expected_backend_id: Optional[str] = None
) -> tuple[dict[str, Any], bytes]:
    policy, raw = _read_json(path, "model registration policy")
    backend_id = _identifier(policy.get("backend_id"), "backend_id")
    if path.stem != backend_id or (
        expected_backend_id is not None and backend_id != expected_backend_id
    ):
        raise ModelRegistrationError("Registration policy backend identity mismatch.")
    if policy.get("schema_version") != "1.0" or policy.get(
        "policy_version"
    ) != MODEL_REGISTRATION_POLICY_VERSION:
        raise ModelRegistrationError("Unsupported model registration policy.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", str(policy.get("ssh_host") or "")):
        raise ModelRegistrationError("Unsafe registration SSH host.")
    if not re.fullmatch(r"[A-Za-z0-9_./+-]+", str(policy.get("remote_python") or "")):
        raise ModelRegistrationError("Unsafe registration remote Python.")
    if not re.fullmatch(
        r"[A-Za-z0-9._-]+", str(policy.get("installed_manifest_filename") or "")
    ):
        raise ModelRegistrationError("Unsafe installed manifest filename.")
    limits = policy.get("limits")
    if not isinstance(limits, Mapping):
        raise ModelRegistrationError("Registration policy has no limits.")
    ranges = {
        "collection_timeout_seconds": (5, 240),
        "maximum_inventory_entries": (1, 10000),
        "maximum_manifest_bytes": (1024, 4 * 1024 * 1024),
    }
    for field, bounds in ranges.items():
        value = limits.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not (
            bounds[0] <= value <= bounds[1]
        ):
            raise ModelRegistrationError("Invalid registration limit {}.".format(field))
    contract = policy.get("registration_contract")
    if not isinstance(contract, Mapping) or (
        contract.get("installation_status") != "verified_present"
        or contract.get("backend_status") != "validated_candidate"
        or contract.get("weights_digest_type")
        != "verified_model_candidate_manifest_sha256"
        or contract.get("benchmark_status_after_registration") != "not_run"
    ):
        raise ModelRegistrationError("Unsupported registration contract.")
    safety = policy.get("safety")
    if not isinstance(safety, Mapping):
        raise ModelRegistrationError("Registration policy has no safety object.")
    for field in REQUIRED_FALSE_POLICY_FIELDS:
        if safety.get(field) is not False:
            raise ModelRegistrationError(
                "Registration policy must explicitly set {} to false.".format(field)
            )
    return policy, raw


def _installed_observation_program(
    backend_id: str,
    project_root: str,
    installation_path: str,
    manifest_name: str,
    maximum_entries: int,
    maximum_manifest_bytes: int,
) -> str:
    config = {
        "backend_id": backend_id,
        "project_root": project_root,
        "installation_path": installation_path,
        "manifest_name": manifest_name,
        "maximum_entries": maximum_entries,
        "maximum_manifest_bytes": maximum_manifest_bytes,
    }
    encoded = repr(json.dumps(config, sort_keys=True))
    program = r'''import hashlib
import json
import os
import platform
import stat
from pathlib import Path

CONFIG=json.loads(__GENOMEAGENT_CONFIG__)
root=Path(CONFIG["project_root"]); installation=Path(CONFIG["installation_path"])
manifest_path=installation/CONFIG["manifest_name"]

def path_state(path):
    return {"path":str(path),"lexists":os.path.lexists(str(path)),
        "exists":path.exists(),"is_directory":path.is_dir(),"is_symlink":path.is_symlink()}

def confined(path):
    try: path.relative_to(root); return True
    except ValueError: return False

def chain_symlinks(path):
    found=[]
    try: relative=path.relative_to(root)
    except ValueError: return ["outside_project_root"]
    if os.path.lexists(str(root)) and root.is_symlink(): found.append(str(root))
    current=root
    for part in relative.parts:
        current=current/part
        if os.path.lexists(str(current)) and current.is_symlink(): found.append(str(current))
    return found

def read_manifest(path):
    record={"path":str(path),"exists":path.is_file(),"is_symlink":path.is_symlink()}
    if not path.is_file() or path.is_symlink(): return record
    before=os.lstat(path)
    if not stat.S_ISREG(before.st_mode): return record
    if before.st_size>int(CONFIG["maximum_manifest_bytes"]):
        record.update({"status":"too_large","size_bytes":before.st_size}); return record
    descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
    try:
        opened=os.fstat(descriptor); raw=b""
        while len(raw)<=int(CONFIG["maximum_manifest_bytes"]):
            block=os.read(descriptor,65536)
            if not block: break
            raw+=block
        after=os.fstat(descriptor)
    finally: os.close(descriptor)
    stable=(opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)==(
        after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
    record.update({"size_bytes":len(raw),"stable_read":stable,
        "sha256":hashlib.sha256(raw).hexdigest()})
    try:
        value=json.loads(raw.decode("utf-8"))
        record["value"]=value if isinstance(value,dict) else None
        record["status"]="parsed" if isinstance(value,dict) else "not_object"
    except Exception as exc:
        record.update({"status":"unreadable","error":str(exc)[:1000]})
    return record

observation={"schema_version":"1.0","observation_type":"installed_model_evidence",
    "backend_id":CONFIG["backend_id"],"identity":{"hostname":__import__("socket").gethostname(),
    "machine":platform.machine()},"paths":{"project_root":path_state(root),
    "installation":path_state(installation),"chain_symlinks":chain_symlinks(installation)},
    "inventory":{"files":[],"symlinks":[],"special_entries":[],"walk_error":None,
    "entries_truncated":False,"download_cache_present":False},
    "manifest":{},"safety":{"remote_writes":False,"installed_manifest_read":True,
    "model_weight_contents_read":False,"large_file_hashing":False,"job_submission":False,
    "gpu_allocation":False,"inference":False,"training":False,"registry_update":False,
    "backend_activation":False}}

if confined(installation) and installation.is_dir() and not installation.is_symlink():
    count=0
    try:
        for current,directories,files in os.walk(installation,followlinks=False):
            current_path=Path(current)
            for name in list(directories):
                path=current_path/name; relative=path.relative_to(installation).as_posix()
                info=os.lstat(path); count+=1
                if stat.S_ISLNK(info.st_mode):
                    observation["inventory"]["symlinks"].append(relative); directories.remove(name)
                elif not stat.S_ISDIR(info.st_mode):
                    observation["inventory"]["special_entries"].append(relative); directories.remove(name)
                if relative==".cache" or relative.startswith(".cache/"):
                    observation["inventory"]["download_cache_present"]=True
                if count>int(CONFIG["maximum_entries"]):
                    observation["inventory"]["entries_truncated"]=True; raise StopIteration
            for name in files:
                path=current_path/name; relative=path.relative_to(installation).as_posix()
                info=os.lstat(path); count+=1
                if stat.S_ISLNK(info.st_mode): observation["inventory"]["symlinks"].append(relative)
                elif not stat.S_ISREG(info.st_mode): observation["inventory"]["special_entries"].append(relative)
                elif relative!=CONFIG["manifest_name"]:
                    observation["inventory"]["files"].append(
                        {"path":relative,"size_bytes":info.st_size})
                if relative==".cache" or relative.startswith(".cache/"):
                    observation["inventory"]["download_cache_present"]=True
                if count>int(CONFIG["maximum_entries"]):
                    observation["inventory"]["entries_truncated"]=True; raise StopIteration
    except StopIteration: pass
    except Exception as exc: observation["inventory"]["walk_error"]=str(exc)[:1000]
observation["inventory"]["files"].sort(key=lambda item:item["path"])
observation["inventory"]["symlinks"].sort(); observation["inventory"]["special_entries"].sort()
observation["inventory"]["file_count"]=len(observation["inventory"]["files"])
observation["inventory"]["total_bytes"]=sum(i["size_bytes"] for i in observation["inventory"]["files"])
observation["manifest"]=read_manifest(manifest_path)
print(json.dumps(observation,sort_keys=True,separators=(",",":")))
'''
    return program.replace("__GENOMEAGENT_CONFIG__", encoded)


def _normalize_verified_files(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ModelRegistrationError("Verified model file inventory is missing.")
    normalized = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ModelRegistrationError("Verified inventory contains a non-object file.")
        path = _safe_relative(item.get("path"), "verified file path")
        if path in seen:
            raise ModelRegistrationError("Verified inventory contains duplicate paths.")
        seen.add(path)
        size = item.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ModelRegistrationError("Verified file has an invalid size.")
        local_sha = _digest(item.get("local_sha256"), "verified local file SHA-256")
        provider_sha = item.get("provider_lfs_sha256")
        if provider_sha is not None:
            provider_sha = _digest(provider_sha, "provider LFS SHA-256")
            if item.get("provider_lfs_sha256_match") is not True:
                raise ModelRegistrationError("Provider LFS SHA-256 was not verified.")
        normalized.append({
            "path": path,
            "size_bytes": size,
            "local_sha256": local_sha,
            "provider_lfs_sha256": provider_sha,
            "provider_lfs_sha256_match": None if provider_sha is None else True,
            "provider_git_blob_id": item.get("provider_git_blob_id"),
            "provider_xet_hash": item.get("provider_xet_hash"),
        })
    normalized.sort(key=lambda item: item["path"])
    return normalized


def _latest_json(directory: Path, label: str) -> tuple[Path, dict[str, Any], bytes]:
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise ModelRegistrationError("No {} exists under {}.".format(label, directory))
    path = paths[-1]
    value, raw = _read_json(path, label)
    return path, value, raw


def _load_inputs(
    registry: AIRegistry,
    policy_root: Path,
    specification_root: Path,
    bundle_root: Path,
    publication_root: Path,
    verification_root: Path,
    backend_id: str,
    bundle_id: str,
    publication_id: str,
) -> dict[str, Any]:
    backend_id = _identifier(backend_id, "backend_id")
    bundle_id = _digest(bundle_id, "bundle ID")
    publication_id = _digest(publication_id, "publication ID")
    backend_path, backend, backend_raw = registry.backend(backend_id)
    policy_path = policy_root / (backend_id + ".json")
    policy, policy_raw = _validate_policy(policy_path, backend_id)
    specification_path = specification_root / (backend_id + ".json")
    specification, specification_raw = _read_json(
        specification_path, "model acquisition specification"
    )
    source = specification.get("source")
    target = specification.get("target")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        raise ModelRegistrationError("Acquisition specification is incomplete.")
    source_revision = _revision(source.get("resolved_revision"), "approved revision")
    if source.get("license_review_status") != "reviewed_accepted":
        raise ModelRegistrationError("Model license approval is not recorded.")
    bundle_path = bundle_root / backend_id / bundle_id / "acquisition_bundle.json"
    bundle, bundle_raw = _read_json(bundle_path, "model acquisition bundle")
    if (
        bundle.get("source_mode")
        != "deterministic_local_execution_bundle_preparation"
        or bundle.get("backend_id") != backend_id
        or bundle.get("bundle_id") != bundle_id
        or bundle.get("repository") != source.get("repository")
        or bundle.get("resolved_revision") != source_revision
    ):
        raise ModelRegistrationError("Acquisition bundle identity is invalid.")
    bundle_target = bundle.get("target")
    if not isinstance(bundle_target, Mapping):
        raise ModelRegistrationError("Acquisition bundle target is missing.")
    installation_path = _safe_absolute(
        bundle_target.get("installation_path"), "bundle installation path"
    )
    project_root = _safe_absolute(
        bundle_target.get("project_storage_root"), "bundle project storage root"
    )
    if installation_path != _safe_absolute(
        target.get("installation_path"), "specified installation path"
    ):
        raise ModelRegistrationError("Bundle and specification installation paths differ.")
    if installation_path != _safe_absolute(
        backend.get("installation", {}).get("path"), "backend installation path"
    ):
        raise ModelRegistrationError("Bundle and backend installation paths differ.")
    if not PurePosixPath(installation_path).is_relative_to(PurePosixPath(project_root)):
        raise ModelRegistrationError("Installation path escapes project storage.")
    publication_dir = publication_root / backend_id / bundle_id / publication_id
    launch_path = publication_dir / "launch.json"
    launch, launch_raw = _read_json(launch_path, "model publication launch")
    if (
        launch.get("source_mode")
        != "explicit_authorized_atomic_model_publication_submission"
        or launch.get("backend_id") != backend_id
        or launch.get("bundle_id") != bundle_id
        or launch.get("publication_id") != publication_id
        or launch.get("installation_path") != installation_path
        or launch.get("publication_started") is not False
        or launch.get("gpu_allocation") is not False
        or launch.get("inference") is not False
        or launch.get("registry_update") is not False
    ):
        raise ModelRegistrationError("Publication launch identity is invalid.")
    verification_id = _digest(launch.get("verification_id"), "verification ID")
    verified_manifest_sha = _digest(
        launch.get("verified_manifest_sha256"), "verified manifest SHA-256"
    )
    verification_result_sha = _digest(
        launch.get("verification_result_sha256"), "verification result SHA-256"
    )
    verified_files = _normalize_verified_files(launch.get("verified_files"))
    publication_status_path, publication_status, publication_status_raw = _latest_json(
        publication_dir / "status", "model publication status"
    )
    if (
        publication_status.get("source_mode")
        != "bounded_read_only_model_publication_status_observation"
        or publication_status.get("backend_id") != backend_id
        or publication_status.get("bundle_id") != bundle_id
        or publication_status.get("publication_id") != publication_id
        or publication_status.get("status")
        != "published_ready_for_installed_model_evidence"
        or publication_status.get("publication") is not False
        or publication_status.get("model_file_contents_read") is not False
        or publication_status.get("large_file_hashing") is not False
        or publication_status.get("registry_update") is not False
        or publication_status.get("backend_activation") is not False
    ):
        raise ModelRegistrationError("Latest publication status is not registration-ready.")
    remote_publication = publication_status.get("remote_observation")
    if not isinstance(remote_publication, Mapping):
        raise ModelRegistrationError("Publication status has no remote observation.")
    publication_result = remote_publication.get("result")
    installed = remote_publication.get("installed")
    publication_manifest_record = (
        installed.get("manifest") if isinstance(installed, Mapping) else None
    )
    publication_manifest = (
        publication_manifest_record.get("value")
        if isinstance(publication_manifest_record, Mapping)
        else None
    )
    if not isinstance(publication_result, Mapping) or not isinstance(
        publication_manifest, Mapping
    ):
        raise ModelRegistrationError("Publication result has no installed manifest.")
    installed_manifest_sha = _digest(
        publication_result.get("installed_manifest_sha256"),
        "installed manifest SHA-256",
    )
    if publication_manifest_record.get("sha256") != installed_manifest_sha:
        raise ModelRegistrationError("Publication manifest digest is inconsistent.")
    if (
        publication_manifest.get("manifest_type") != "genomeagent_installed_model"
        or publication_manifest.get("backend_id") != backend_id
        or publication_manifest.get("bundle_id") != bundle_id
        or publication_manifest.get("verification_id") != verification_id
        or publication_manifest.get("publication_id") != publication_id
        or publication_manifest.get("repository") != bundle.get("repository")
        or publication_manifest.get("resolved_revision") != source_revision
        or publication_manifest.get("installation_path") != installation_path
        or publication_manifest.get("source_verified_manifest_sha256")
        != verified_manifest_sha
        or publication_manifest.get("source_verification_result_sha256")
        != verification_result_sha
        or _normalize_verified_files(publication_manifest.get("files")) != verified_files
        or publication_manifest.get("download_cache_removed") is not True
        or publication_manifest.get("atomic_directory_rename") is not True
        or publication_manifest.get("backend_activated") is not False
        or publication_manifest.get("inference_benchmarked") is not False
    ):
        raise ModelRegistrationError("Installed publication manifest contract mismatch.")
    verification_dir = verification_root / backend_id / bundle_id / verification_id
    verification_status_path, verification_status, verification_status_raw = _latest_json(
        verification_dir / "status", "model integrity verification status"
    )
    if (
        verification_status.get("source_mode")
        != "bounded_read_only_model_integrity_status_observation"
        or verification_status.get("backend_id") != backend_id
        or verification_status.get("bundle_id") != bundle_id
        or verification_status.get("verification_id") != verification_id
        or verification_status.get("status")
        != "verified_ready_for_publication_review"
        or verification_status.get("publication") is not False
        or verification_status.get("staging_mutation") is not False
    ):
        raise ModelRegistrationError("Integrity verification evidence is invalid.")
    sources = [
        _source(backend_path, backend_raw, "ai_backend_configuration"),
        _source(policy_path, policy_raw, "model_registration_policy"),
        _source(specification_path, specification_raw, "model_acquisition_specification"),
        _source(bundle_path, bundle_raw, "model_acquisition_bundle"),
        _source(launch_path, launch_raw, "model_publication_launch"),
        _source(
            publication_status_path,
            publication_status_raw,
            "terminal_model_publication_status",
        ),
        _source(
            verification_status_path,
            verification_status_raw,
            "terminal_model_integrity_status",
        ),
    ]
    return {
        "backend_id": backend_id,
        "bundle_id": bundle_id,
        "publication_id": publication_id,
        "verification_id": verification_id,
        "backend_path": backend_path,
        "backend": backend,
        "backend_raw": backend_raw,
        "policy": policy,
        "source": dict(source),
        "bundle": bundle,
        "installation_path": installation_path,
        "project_root": project_root,
        "verified_files": verified_files,
        "verified_manifest_sha256": verified_manifest_sha,
        "verification_result_sha256": verification_result_sha,
        "installed_manifest_sha256": installed_manifest_sha,
        "publication_manifest": dict(publication_manifest),
        "source_artifacts": sources,
    }


def _validate_observation(observation: Mapping[str, Any], backend_id: str) -> None:
    if (
        observation.get("schema_version") != "1.0"
        or observation.get("observation_type") != "installed_model_evidence"
        or observation.get("backend_id") != backend_id
    ):
        raise ModelRegistrationError("Remote installed-model observation is invalid.")
    _false_fields(observation.get("safety"), OBSERVATION_FALSE_FIELDS, "Observation")
    if observation.get("safety", {}).get("installed_manifest_read") is not True:
        raise ModelRegistrationError("Observation did not record the manifest read boundary.")


def _interpret(inputs: Mapping[str, Any], observation: Mapping[str, Any]) -> dict[str, Any]:
    blockers = []
    paths = observation.get("paths")
    inventory = observation.get("inventory")
    manifest_record = observation.get("manifest")
    if not isinstance(paths, Mapping) or not isinstance(inventory, Mapping):
        blockers.append("installed_model_observation_incomplete")
        paths = {}
        inventory = {}
    installation = paths.get("installation") if isinstance(paths, Mapping) else None
    project_root = paths.get("project_root") if isinstance(paths, Mapping) else None
    if not isinstance(project_root, Mapping) or not (
        project_root.get("path") == inputs["project_root"]
        and project_root.get("exists") is True
        and project_root.get("is_directory") is True
        and project_root.get("is_symlink") is False
    ):
        blockers.append("project_storage_root_unverified")
    if not isinstance(installation, Mapping) or not (
        installation.get("path") == inputs["installation_path"]
        and installation.get("exists") is True
        and installation.get("is_directory") is True
        and installation.get("is_symlink") is False
    ):
        blockers.append("installed_model_path_unverified")
    if paths.get("chain_symlinks") != []:
        blockers.append("installed_model_path_symlink_detected")
    expected_sizes = [
        {"path": item["path"], "size_bytes": item["size_bytes"]}
        for item in inputs["verified_files"]
    ]
    if inventory.get("walk_error") is not None:
        blockers.append("installed_inventory_walk_failed")
    if inventory.get("entries_truncated") is not False:
        blockers.append("installed_inventory_truncated")
    if inventory.get("symlinks") != []:
        blockers.append("installed_model_symlinks_detected")
    if inventory.get("special_entries") != []:
        blockers.append("installed_model_special_entries_detected")
    if inventory.get("download_cache_present") is not False:
        blockers.append("download_cache_still_present")
    if inventory.get("files") != expected_sizes:
        blockers.append("installed_paths_or_sizes_mismatch")
    if inventory.get("file_count") != len(expected_sizes):
        blockers.append("installed_file_count_mismatch")
    total_bytes = sum(item["size_bytes"] for item in expected_sizes)
    if inventory.get("total_bytes") != total_bytes:
        blockers.append("installed_total_bytes_mismatch")
    fresh_manifest = None
    if not isinstance(manifest_record, Mapping) or not (
        manifest_record.get("exists") is True
        and manifest_record.get("is_symlink") is False
        and manifest_record.get("status") == "parsed"
        and manifest_record.get("stable_read") is True
        and manifest_record.get("sha256") == inputs["installed_manifest_sha256"]
        and isinstance(manifest_record.get("value"), Mapping)
    ):
        blockers.append("installed_manifest_unverified")
    else:
        fresh_manifest = dict(manifest_record["value"])
        if fresh_manifest != inputs["publication_manifest"]:
            blockers.append("installed_manifest_changed_after_publication")
    blockers = list(dict.fromkeys(blockers))
    if blockers:
        status = "installed_model_evidence_attention_required"
        next_action = "review_installed_model_evidence_before_registration"
    else:
        status = "ready_for_backend_registration_review"
        next_action = "review_and_register_verified_model_identity"
        blockers = ["fresh_backend_registration_approval_missing"]
    return {
        "schema_version": "1.0",
        "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
        "backend_id": inputs["backend_id"],
        "status": status,
        "next_safe_action": next_action,
        "blockers": blockers,
        "summary": {
            "repository": inputs["bundle"]["repository"],
            "resolved_revision": inputs["bundle"]["resolved_revision"],
            "installation_path": inputs["installation_path"],
            "bundle_id": inputs["bundle_id"],
            "verification_id": inputs["verification_id"],
            "publication_id": inputs["publication_id"],
            "installed_manifest_sha256": inputs["installed_manifest_sha256"],
            "verified_inventory_sha256": inputs["verified_manifest_sha256"],
            "verification_result_sha256": inputs["verification_result_sha256"],
            "file_count": len(expected_sizes),
            "total_bytes": total_bytes,
        },
        "automatic_registry_update_allowed": False,
        "automatic_backend_activation_allowed": False,
        "automatic_execution_allowed": False,
        "gpu_allocation_allowed": False,
        "inference_allowed": False,
        "training_allowed": False,
    }


def _registration_proposal(
    backend: Mapping[str, Any],
    evidence: Mapping[str, Any],
    evidence_sha256: str,
    backend_before_sha256: str,
) -> dict[str, Any]:
    readiness = evidence.get("readiness")
    summary = readiness.get("summary") if isinstance(readiness, Mapping) else None
    ready = (
        isinstance(readiness, Mapping)
        and readiness.get("status") == "ready_for_backend_registration_review"
        and isinstance(summary, Mapping)
    )
    proposed_values = {
        "backend_status": "validated_candidate",
        "model_revision": summary.get("resolved_revision") if ready else None,
        "model_weights_sha256": (
            summary.get("verified_inventory_sha256") if ready else None
        ),
        "model_weights_digest_type": "verified_model_candidate_manifest_sha256",
        "installation_status": "verified_present",
        "installation_manifest_sha256": (
            summary.get("installed_manifest_sha256") if ready else None
        ),
        "installation_verified_inventory_sha256": (
            summary.get("verified_inventory_sha256") if ready else None
        ),
        "benchmark_status": "not_run",
    }
    model = backend.get("model") if isinstance(backend.get("model"), Mapping) else {}
    installation = (
        backend.get("installation")
        if isinstance(backend.get("installation"), Mapping)
        else {}
    )
    reflected = ready and (
        backend.get("status") == proposed_values["backend_status"]
        and model.get("revision") == proposed_values["model_revision"]
        and model.get("weights_sha256")
        == proposed_values["model_weights_sha256"]
        and model.get("weights_digest_type")
        == proposed_values["model_weights_digest_type"]
        and installation.get("status") == proposed_values["installation_status"]
        and installation.get("manifest_sha256")
        == proposed_values["installation_manifest_sha256"]
        and installation.get("verified_inventory_sha256")
        == proposed_values["installation_verified_inventory_sha256"]
    )
    if not ready:
        status = "not_applicable"
        applicable = False
    elif reflected:
        status = "verified_installation_already_registered"
        applicable = False
    else:
        status = "researcher_review_required"
        applicable = True
    changes = []
    if ready:
        current_values = {
            "status": backend.get("status"),
            "model.revision": model.get("revision"),
            "model.weights_sha256": model.get("weights_sha256"),
            "model.weights_digest_type": model.get("weights_digest_type"),
            "installation.status": installation.get("status"),
            "installation.manifest_sha256": installation.get("manifest_sha256"),
            "installation.verified_inventory_sha256": installation.get(
                "verified_inventory_sha256"
            ),
            "benchmark_status": backend.get("benchmark_status"),
        }
        proposed_fields = {
            "status": proposed_values["backend_status"],
            "model.revision": proposed_values["model_revision"],
            "model.weights_sha256": proposed_values["model_weights_sha256"],
            "model.weights_digest_type": proposed_values["model_weights_digest_type"],
            "installation.status": proposed_values["installation_status"],
            "installation.manifest_sha256": proposed_values[
                "installation_manifest_sha256"
            ],
            "installation.verified_inventory_sha256": proposed_values[
                "installation_verified_inventory_sha256"
            ],
            "benchmark_status": proposed_values["benchmark_status"],
        }
        changes = [
            {"field": field, "current": current_values[field], "proposed": value}
            for field, value in proposed_fields.items()
            if current_values[field] != value
        ]
    return {
        "schema_version": "1.0",
        "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
        "backend_id": evidence.get("backend_id"),
        "status": status,
        "applicable": applicable,
        "source_evidence_id": evidence.get("evidence_id"),
        "source_evidence_sha256": evidence_sha256,
        "backend_configuration_before_sha256": backend_before_sha256,
        "proposed_values": proposed_values,
        "installation_identity": dict(summary) if ready else {},
        "changes": changes,
        "researcher_approval_required": True,
        "automatic_application_allowed": False,
        "automatic_backend_activation_allowed": False,
        "gpu_benchmark_required_after_registration": True,
    }


def _snapshot_report(snapshot: Mapping[str, Any]) -> str:
    readiness = snapshot["readiness"]
    summary = readiness.get("summary", {})
    lines = [
        "# GenomeAgent Installed Model Evidence",
        "",
        "- Backend: `{}`".format(snapshot["backend_id"]),
        "- Evidence ID: `{}`".format(snapshot["evidence_id"]),
        "- Publication: `{}`".format(snapshot["publication_id"]),
        "- Status: **{}**".format(readiness["status"]),
        "- Installed path: `{}`".format(summary.get("installation_path", "")),
        "- Verified files: {}".format(summary.get("file_count", 0)),
        "- Verified bytes: {}".format(summary.get("total_bytes", 0)),
        "- Model weight contents read: **no**",
        "- GPU/inference/training: **no**",
        "",
        "## Blockers",
        "",
    ]
    lines.extend("- `{}`".format(item) for item in readiness["blockers"])
    lines.extend([
        "",
        "## Trust boundary",
        "",
        "The collector read the small installed manifest and recursively compared only "
        "filesystem paths and sizes. It did not read or hash model weights. Trust in the "
        "file hashes comes from the bound integrity verification and atomic publication "
        "records; the fresh manifest digest proves that the installed identity has not "
        "changed since publication.",
        "",
    ])
    return "\n".join(lines)


def _state_report(state: Mapping[str, Any], proposal: Mapping[str, Any]) -> str:
    latest = state["latest"]
    readiness = latest["readiness"]
    lines = [
        "# GenomeAgent Installed Model Registration State",
        "",
        "- Backend: `{}`".format(state["backend_id"]),
        "- Source snapshots: {}".format(state["source_snapshots"]),
        "- Latest evidence: `{}`".format(latest["evidence_id"]),
        "- Status: **{}**".format(readiness["status"]),
        "- Proposal: **{}**".format(proposal["status"]),
        "- Automatic registry update: **no**",
        "- Backend activation: **no**",
        "",
        "## Current blockers",
        "",
    ]
    lines.extend("- `{}`".format(item) for item in readiness["blockers"])
    lines.extend([
        "",
        "## Next boundary",
        "",
        "Explicit researcher approval may update only the registered model revision, "
        "verified inventory digest, installed manifest identity and installation status. "
        "A separate bounded GPU benchmark is still required before activation.",
        "",
    ])
    return "\n".join(lines)


class InstalledModelEvidenceCollector:
    """Collect fresh, bounded installed-model identity evidence."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/model_registration"),
        specification_root: Path = Path("config/ai/acquisition"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        publication_root: Path = Path("workspace/model_publications"),
        verification_root: Path = Path("workspace/model_integrity_verifications"),
        evidence_root: Path = Path("workspace/installed_model_evidence"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.specification_root = Path(specification_root)
        self.bundle_root = Path(bundle_root)
        self.publication_root = Path(publication_root)
        self.verification_root = Path(verification_root)
        self.evidence_root = Path(evidence_root)

    def policy(self, backend_id: str) -> tuple[Path, dict[str, Any], bytes]:
        backend_id = _identifier(backend_id, "backend_id")
        path = self.policy_root / (backend_id + ".json")
        value, raw = _validate_policy(path, backend_id)
        return path, value, raw

    def collect(
        self,
        backend_id: str,
        bundle_id: str,
        publication_id: str,
        runner: PythonRemoteRunner,
        stamp: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> InstalledModelCollectionResult:
        inputs = _load_inputs(
            self.registry,
            self.policy_root,
            self.specification_root,
            self.bundle_root,
            self.publication_root,
            self.verification_root,
            backend_id,
            bundle_id,
            publication_id,
        )
        policy = inputs["policy"]
        if str(runner.host) != policy["ssh_host"]:
            raise ModelRegistrationError("Installed-model evidence host mismatch.")
        timeout = (
            int(timeout_seconds)
            if timeout_seconds is not None
            else int(policy["limits"]["collection_timeout_seconds"])
        )
        if not 5 <= timeout <= 240:
            raise ModelRegistrationError("Collection timeout must be between 5 and 240 seconds.")
        now = datetime.now(timezone.utc)
        evidence_id = stamp or now.strftime("%Y%m%dT%H%M%S%fZ")
        _timestamp(evidence_id, "installed-model evidence ID")
        evidence_path = self.evidence_root / inputs["backend_id"] / (
            evidence_id + ".json"
        )
        report_path = evidence_path.with_suffix(".md")
        if evidence_path.exists() or report_path.exists():
            raise ModelRegistrationError("Refusing to overwrite installed-model evidence.")
        try:
            observation = runner.run_python(
                _installed_observation_program(
                    inputs["backend_id"],
                    inputs["project_root"],
                    inputs["installation_path"],
                    policy["installed_manifest_filename"],
                    int(policy["limits"]["maximum_inventory_entries"]),
                    int(policy["limits"]["maximum_manifest_bytes"]),
                ),
                timeout_seconds=timeout,
            )
        except Exception as exc:
            raise ModelRegistrationError(
                "Read-only installed-model observation failed: {}".format(exc)
            ) from exc
        if not isinstance(observation, Mapping):
            raise ModelRegistrationError("Installed-model observation must be an object.")
        observation = dict(observation)
        _validate_observation(observation, inputs["backend_id"])
        current = _load_inputs(
            self.registry,
            self.policy_root,
            self.specification_root,
            self.bundle_root,
            self.publication_root,
            self.verification_root,
            backend_id,
            bundle_id,
            publication_id,
        )
        if current["source_artifacts"] != inputs["source_artifacts"]:
            raise ModelRegistrationError(
                "Registration inputs changed during remote observation; no evidence written."
            )
        readiness = _interpret(inputs, observation)
        snapshot = {
            "schema_version": "1.0",
            "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
            "source_mode": "bounded_read_only_installed_model_observation",
            "backend_id": inputs["backend_id"],
            "bundle_id": inputs["bundle_id"],
            "verification_id": inputs["verification_id"],
            "publication_id": inputs["publication_id"],
            "evidence_id": evidence_id,
            "observed_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": str(runner.host),
            "source_artifacts": inputs["source_artifacts"],
            "observation": observation,
            "readiness": readiness,
            "safety": {
                "remote_writes": False,
                "model_weight_contents_read": False,
                "large_file_hashing": False,
                "job_submission": False,
                "gpu_allocation": False,
                "inference": False,
                "training": False,
                "automatic_registry_update": False,
                "backend_activation": False,
            },
        }
        _write_immutable(evidence_path, _pretty_json(snapshot))
        _write_immutable(report_path, _snapshot_report(snapshot))
        return InstalledModelCollectionResult(
            inputs["backend_id"],
            inputs["bundle_id"],
            inputs["publication_id"],
            evidence_id,
            evidence_path,
            str(readiness["status"]),
            tuple(str(item) for item in readiness["blockers"]),
        )


def _validate_snapshot(value: Mapping[str, Any], path: Path, backend_id: str) -> None:
    if (
        value.get("schema_version") != "1.0"
        or value.get("policy_version") != MODEL_REGISTRATION_POLICY_VERSION
        or value.get("source_mode")
        != "bounded_read_only_installed_model_observation"
        or value.get("backend_id") != backend_id
    ):
        raise ModelRegistrationError("Invalid installed-model snapshot: {}".format(path))
    if path.stem != _timestamp(value.get("evidence_id"), "evidence ID"):
        raise ModelRegistrationError("Installed-model evidence filename mismatch.")
    for field in ("bundle_id", "verification_id", "publication_id"):
        _digest(value.get(field), field)
    _false_fields(value.get("safety"), (
        "remote_writes", "model_weight_contents_read", "large_file_hashing",
        "job_submission", "gpu_allocation", "inference", "training",
        "automatic_registry_update", "backend_activation",
    ), "Snapshot")
    observation = value.get("observation")
    if not isinstance(observation, Mapping):
        raise ModelRegistrationError("Installed-model snapshot has no observation.")
    _validate_observation(observation, backend_id)


class InstalledModelEvidenceCore:
    """Replay installed-model snapshots and build a scoped registry proposal."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/model_registration"),
        specification_root: Path = Path("config/ai/acquisition"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        publication_root: Path = Path("workspace/model_publications"),
        verification_root: Path = Path("workspace/model_integrity_verifications"),
        evidence_root: Path = Path("workspace/installed_model_evidence"),
        state_root: Path = Path("workspace/installed_model_state"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.specification_root = Path(specification_root)
        self.bundle_root = Path(bundle_root)
        self.publication_root = Path(publication_root)
        self.verification_root = Path(verification_root)
        self.evidence_root = Path(evidence_root)
        self.state_root = Path(state_root)

    def _inputs_for(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return _load_inputs(
            self.registry,
            self.policy_root,
            self.specification_root,
            self.bundle_root,
            self.publication_root,
            self.verification_root,
            str(value.get("backend_id") or ""),
            str(value.get("bundle_id") or ""),
            str(value.get("publication_id") or ""),
        )

    def ingest(self, backend_id: str) -> InstalledModelIngestResult:
        backend_id = _identifier(backend_id, "backend_id")
        paths = sorted((self.evidence_root / backend_id).glob("*.json"))
        if not paths:
            raise ModelRegistrationError(
                "No installed-model evidence exists for {}.".format(backend_id)
            )
        records = []
        for path in paths:
            value, raw = _read_json(path, "installed-model evidence")
            _validate_snapshot(value, path, backend_id)
            inputs = self._inputs_for(value)
            current_sources = inputs["source_artifacts"]
            source_current = value.get("source_artifacts") == current_sources
            readiness = _interpret(inputs, value["observation"])
            if not source_current:
                readiness = copy.deepcopy(readiness)
                readiness["status"] = "installed_model_evidence_stale"
                readiness["next_safe_action"] = "collect_fresh_installed_model_evidence"
                readiness["blockers"] = ["registration_sources_changed_since_observation"]
            records.append({
                "evidence_id": value["evidence_id"],
                "observed_at_utc": value.get("observed_at_utc", ""),
                "source_path": str(path),
                "source_sha256": _sha256_bytes(raw),
                "source_artifacts": value.get("source_artifacts"),
                "source_current": source_current,
                "bundle_id": value["bundle_id"],
                "verification_id": value["verification_id"],
                "publication_id": value["publication_id"],
                "readiness": readiness,
                "backend": inputs["backend"],
            })
        records.sort(key=lambda item: (item["observed_at_utc"], item["evidence_id"]))
        latest = records[-1]
        backend_source = next(
            (
                item
                for item in latest["source_artifacts"]
                if item.get("artifact_type") == "ai_backend_configuration"
            ),
            None,
        )
        if not isinstance(backend_source, Mapping):
            raise ModelRegistrationError("Installed evidence has no backend source.")
        evidence, _ = _read_json(Path(latest["source_path"]), "installed-model evidence")
        proposal = _registration_proposal(
            latest["backend"],
            evidence,
            latest["source_sha256"],
            _digest(backend_source.get("sha256"), "backend source SHA-256"),
        )
        state = {
            "schema_version": "1.0",
            "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
            "backend_id": backend_id,
            "source_snapshots": len(records),
            "latest": {
                key: value
                for key, value in latest.items()
                if key not in {"backend", "source_artifacts"}
            },
            "automatic_registry_update_allowed": False,
            "automatic_backend_activation_allowed": False,
            "gpu_benchmark_required": True,
        }
        history = [
            {
                key: value
                for key, value in record.items()
                if key not in {"backend", "source_artifacts"}
            }
            for record in records
        ]
        provenance = {
            "schema_version": "1.0",
            "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
            "backend_id": backend_id,
            "evidence_snapshots": [
                {
                    "evidence_id": record["evidence_id"],
                    "path": record["source_path"],
                    "sha256": record["source_sha256"],
                }
                for record in records
            ],
            "latest_source_artifacts": latest["source_artifacts"],
            "derivation": "deterministic_local_installed_model_evidence_replay",
            "remote_access_during_ingest": False,
            "automatic_registry_update": False,
            "backend_activation": False,
        }
        state_dir = self.state_root / backend_id
        artifacts = (
            state_dir / "current_installed_model.json",
            state_dir / "evidence_history.jsonl",
            state_dir / "backend_registration_proposal.json",
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
        readiness = latest["readiness"]
        return InstalledModelIngestResult(
            backend_id,
            state_dir,
            len(records),
            str(readiness["status"]),
            str(readiness["next_safe_action"]),
            tuple(str(item) for item in readiness["blockers"]),
            str(proposal["status"]),
            artifacts,
        )


def _approval_report(value: Mapping[str, Any]) -> str:
    identity = value["installation_identity"]
    return "\n".join([
        "# GenomeAgent Verified Model Registration Approval",
        "",
        "- Backend: `{}`".format(value["backend_id"]),
        "- Approval ID: `{}`".format(value["approval_id"]),
        "- Evidence: `{}`".format(value["source_evidence_id"]),
        "- Publication: `{}`".format(identity["publication_id"]),
        "- Revision: `{}`".format(identity["resolved_revision"]),
        "- Reviewer: `{}`".format(value["reviewer"]),
        "- Registered at: `{}`".format(value["registered_at_utc"]),
        "- Authorized mutation: **AI backend identity fields only**",
        "- GPU/inference/training: **no**",
        "- Backend activation: **no**",
        "",
        "This approval links the installed manifest to the earlier integrity and atomic "
        "publication evidence. It does not claim that inference works and does not "
        "authorize a GPU job or activate the backend.",
        "",
    ])


def _validate_approval(
    value: Mapping[str, Any], approval_id: str, backend_id: str
) -> None:
    if (
        value.get("schema_version") != "1.0"
        or value.get("policy_version") != MODEL_REGISTRATION_POLICY_VERSION
        or value.get("source_mode") != "explicit_verified_model_registration_approval"
        or value.get("backend_id") != backend_id
        or value.get("approval_id") != approval_id
    ):
        raise ModelRegistrationError("Model registration approval identity is invalid.")
    identity = {
        "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
        "backend_id": backend_id,
        "source_evidence_id": value.get("source_evidence_id"),
        "source_evidence_sha256": value.get("source_evidence_sha256"),
        "proposal_sha256": value.get("proposal_sha256"),
        "reviewer": value.get("reviewer"),
        "installation_identity": value.get("installation_identity"),
        "proposed_values": value.get("proposed_values"),
    }
    if _sha256_value(identity) != approval_id:
        raise ModelRegistrationError("Model registration approval digest mismatch.")
    for field in (
        "source_evidence_sha256", "proposal_sha256",
        "backend_configuration_before_sha256", "backend_configuration_after_sha256",
    ):
        _digest(value.get(field), field)
    safety = value.get("safety")
    if not isinstance(safety, Mapping) or safety.get("backend_registry_update") is not True:
        raise ModelRegistrationError("Approval has no explicit registry mutation scope.")
    _false_fields(safety, (
        "remote_access", "remote_writes", "model_weight_contents_read",
        "large_file_hashing", "job_submission", "gpu_allocation", "inference",
        "training", "automatic_registry_update", "backend_activation",
    ), "Approval")


class ModelRegistrationApprovalCore:
    """Apply one explicitly approved, evidence-bound backend registry update."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/model_registration"),
        specification_root: Path = Path("config/ai/acquisition"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        publication_root: Path = Path("workspace/model_publications"),
        verification_root: Path = Path("workspace/model_integrity_verifications"),
        evidence_root: Path = Path("workspace/installed_model_evidence"),
        state_root: Path = Path("workspace/installed_model_state"),
        approval_root: Path = Path("workspace/model_registration_approvals"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.specification_root = Path(specification_root)
        self.bundle_root = Path(bundle_root)
        self.publication_root = Path(publication_root)
        self.verification_root = Path(verification_root)
        self.evidence_root = Path(evidence_root)
        self.state_root = Path(state_root)
        self.approval_root = Path(approval_root)

    def _core(self) -> InstalledModelEvidenceCore:
        return InstalledModelEvidenceCore(
            self.registry,
            self.policy_root,
            self.specification_root,
            self.bundle_root,
            self.publication_root,
            self.verification_root,
            self.evidence_root,
            self.state_root,
        )

    def approve(
        self,
        backend_id: str,
        evidence_id: str,
        reviewer: str,
        confirmation: bool,
        registered_at: Optional[datetime] = None,
    ) -> ModelRegistrationApprovalResult:
        if confirmation is not True:
            raise ModelRegistrationError(
                "Explicit --confirm-register-verified-installation is required."
            )
        backend_id = _identifier(backend_id, "backend_id")
        evidence_id = _timestamp(evidence_id, "installed-model evidence ID")
        reviewer = str(reviewer or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,127}", reviewer):
            raise ModelRegistrationError("Unsafe or empty reviewer identifier.")
        persisted_state_path = self.state_root / backend_id / "current_installed_model.json"
        persisted_proposal_path = (
            self.state_root / backend_id / "backend_registration_proposal.json"
        )
        if persisted_state_path.is_file() and persisted_proposal_path.is_file():
            persisted_state, _ = _read_json(
                persisted_state_path, "installed-model state"
            )
            persisted_proposal, persisted_proposal_raw = _read_json(
                persisted_proposal_path, "backend registration proposal"
            )
            persisted_latest = persisted_state.get("latest")
            persisted_identity = persisted_proposal.get("installation_identity")
            persisted_values = persisted_proposal.get("proposed_values")
            persisted_evidence_path = (
                self.evidence_root / backend_id / (evidence_id + ".json")
            )
            if (
                isinstance(persisted_latest, Mapping)
                and persisted_latest.get("evidence_id") == evidence_id
                and isinstance(persisted_identity, Mapping)
                and isinstance(persisted_values, Mapping)
                and persisted_evidence_path.is_file()
            ):
                _, persisted_evidence_raw = _read_json(
                    persisted_evidence_path, "installed-model evidence"
                )
                persisted_approval_id = _sha256_value({
                    "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
                    "backend_id": backend_id,
                    "source_evidence_id": evidence_id,
                    "source_evidence_sha256": _sha256_bytes(persisted_evidence_raw),
                    "proposal_sha256": _sha256_bytes(persisted_proposal_raw),
                    "reviewer": reviewer,
                    "installation_identity": dict(persisted_identity),
                    "proposed_values": dict(persisted_values),
                })
                persisted_approval_path = (
                    self.approval_root
                    / backend_id
                    / (persisted_approval_id + ".json")
                )
                if persisted_approval_path.is_file():
                    existing_approval, _ = _read_json(
                        persisted_approval_path, "model registration approval"
                    )
                    _validate_approval(
                        existing_approval, persisted_approval_id, backend_id
                    )
                    backend_path, _, backend_raw = self.registry.backend(backend_id)
                    if _sha256_bytes(backend_raw) == existing_approval.get(
                        "backend_configuration_after_sha256"
                    ):
                        return ModelRegistrationApprovalResult(
                            backend_id,
                            persisted_approval_id,
                            persisted_approval_path,
                            backend_path,
                            False,
                            True,
                            "verified_model_registration_already_applied",
                            "collect_fresh_backend_evidence",
                        )
        ingested = self._core().ingest(backend_id)
        state_path = ingested.state_dir / "current_installed_model.json"
        proposal_path = ingested.state_dir / "backend_registration_proposal.json"
        state, _ = _read_json(state_path, "installed-model state")
        proposal, proposal_raw = _read_json(proposal_path, "backend registration proposal")
        latest = state.get("latest")
        if not isinstance(latest, Mapping) or latest.get("evidence_id") != evidence_id:
            raise ModelRegistrationError("Approval evidence is not the latest observation.")
        if latest.get("readiness", {}).get("status") != (
            "ready_for_backend_registration_review"
        ):
            raise ModelRegistrationError("Installed model is not registration-ready.")
        if proposal.get("status") != "researcher_review_required" or proposal.get(
            "applicable"
        ) is not True:
            raise ModelRegistrationError("No applicable backend registration proposal exists.")
        evidence_path = self.evidence_root / backend_id / (evidence_id + ".json")
        evidence, evidence_raw = _read_json(evidence_path, "installed-model evidence")
        if _sha256_bytes(evidence_raw) != proposal.get("source_evidence_sha256"):
            raise ModelRegistrationError("Installed-model evidence digest changed.")
        inputs = _load_inputs(
            self.registry,
            self.policy_root,
            self.specification_root,
            self.bundle_root,
            self.publication_root,
            self.verification_root,
            backend_id,
            str(evidence.get("bundle_id") or ""),
            str(evidence.get("publication_id") or ""),
        )
        if inputs["source_artifacts"] != evidence.get("source_artifacts"):
            raise ModelRegistrationError("Registration sources changed after observation.")
        backend_path = inputs["backend_path"]
        if backend_path.is_symlink():
            raise ModelRegistrationError("Refusing to update a symlinked backend registry file.")
        backend_config, backend_raw = _read_json(backend_path, "AI backend configuration")
        before_sha = _sha256_bytes(backend_raw)
        if before_sha != proposal.get("backend_configuration_before_sha256"):
            raise ModelRegistrationError("Backend configuration changed after observation.")
        installation_identity = proposal.get("installation_identity")
        proposed_values = proposal.get("proposed_values")
        if not isinstance(installation_identity, Mapping) or not isinstance(
            proposed_values, Mapping
        ):
            raise ModelRegistrationError("Registration proposal is incomplete.")
        identity = {
            "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
            "backend_id": backend_id,
            "source_evidence_id": evidence_id,
            "source_evidence_sha256": _sha256_bytes(evidence_raw),
            "proposal_sha256": _sha256_bytes(proposal_raw),
            "reviewer": reviewer,
            "installation_identity": dict(installation_identity),
            "proposed_values": dict(proposed_values),
        }
        approval_id = _sha256_value(identity)
        approval_path = self.approval_root / backend_id / (approval_id + ".json")
        report_path = approval_path.with_suffix(".md")
        existing = None
        if approval_path.exists():
            existing, _ = _read_json(approval_path, "model registration approval")
            _validate_approval(existing, approval_id, backend_id)
            if before_sha == existing.get("backend_configuration_after_sha256"):
                return ModelRegistrationApprovalResult(
                    backend_id, approval_id, approval_path, backend_path, False, True,
                    "verified_model_registration_already_applied",
                    "collect_fresh_backend_evidence",
                )
            if before_sha != existing.get("backend_configuration_before_sha256"):
                raise ModelRegistrationError(
                    "Backend configuration changed after the approval record."
                )
        registered_at = registered_at or datetime.now(timezone.utc)
        if registered_at.tzinfo is None:
            raise ModelRegistrationError("Registration timestamp must include a timezone.")
        registered_at_utc = registered_at.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        if existing is not None:
            registered_at_utc = str(existing["registered_at_utc"])
        registration_record = {
            "schema_version": "1.0",
            "approval_id": approval_id,
            "source_evidence_id": evidence_id,
            "source_evidence_sha256": _sha256_bytes(evidence_raw),
            "reviewer": reviewer,
            "registered_at_utc": registered_at_utc,
            "bundle_id": installation_identity["bundle_id"],
            "verification_id": installation_identity["verification_id"],
            "publication_id": installation_identity["publication_id"],
            "installed_manifest_sha256": installation_identity[
                "installed_manifest_sha256"
            ],
            "verified_inventory_sha256": installation_identity[
                "verified_inventory_sha256"
            ],
            "file_count": installation_identity["file_count"],
            "total_bytes": installation_identity["total_bytes"],
        }
        updated = copy.deepcopy(backend_config)
        updated["status"] = proposed_values["backend_status"]
        updated["benchmark_status"] = proposed_values["benchmark_status"]
        updated.setdefault("model", {})["revision"] = proposed_values["model_revision"]
        updated["model"]["weights_sha256"] = proposed_values["model_weights_sha256"]
        updated["model"]["weights_digest_type"] = proposed_values[
            "model_weights_digest_type"
        ]
        updated.setdefault("installation", {})["status"] = proposed_values[
            "installation_status"
        ]
        updated["installation"]["manifest_sha256"] = proposed_values[
            "installation_manifest_sha256"
        ]
        updated["installation"]["verified_inventory_sha256"] = proposed_values[
            "installation_verified_inventory_sha256"
        ]
        updated["installation"]["registration"] = registration_record
        updated_text = _pretty_json(updated)
        after_sha = _sha256_bytes(updated_text.encode("utf-8"))
        with tempfile.TemporaryDirectory(
            prefix=".genomeagent-model-registration-", dir=str(backend_path.parent)
        ) as temporary_dir:
            candidate = Path(temporary_dir) / backend_path.name
            _atomic_write(candidate, updated_text)
            candidate_registry = AIRegistry(
                backend_root=candidate.parent,
                prompt_root=self.registry.prompt_root,
                suite_root=self.registry.suite_root,
                case_root=self.registry.case_root,
            )
            try:
                _, validated, _ = candidate_registry.backend(backend_id)
            except AIEvaluationError as exc:
                raise ModelRegistrationError(
                    "Proposed backend registration is invalid: {}".format(exc)
                ) from exc
            blockers = validated.get("derived_readiness", {}).get("blockers")
            if blockers != ["inference_not_benchmarked"]:
                raise ModelRegistrationError(
                    "Registration must leave inference benchmarking as the only blocker."
                )
        approval = existing
        if approval is None:
            approval = {
                "schema_version": "1.0",
                "policy_version": MODEL_REGISTRATION_POLICY_VERSION,
                "source_mode": "explicit_verified_model_registration_approval",
                "backend_id": backend_id,
                "approval_id": approval_id,
                "source_evidence_id": evidence_id,
                "source_evidence_sha256": _sha256_bytes(evidence_raw),
                "proposal_sha256": _sha256_bytes(proposal_raw),
                "reviewer": reviewer,
                "registered_at_utc": registered_at_utc,
                "installation_identity": dict(installation_identity),
                "proposed_values": dict(proposed_values),
                "registration_record": registration_record,
                "backend_configuration_path": str(backend_path),
                "backend_configuration_before_sha256": before_sha,
                "backend_configuration_after_sha256": after_sha,
                "safety": {
                    "backend_registry_update": True,
                    "remote_access": False,
                    "remote_writes": False,
                    "model_weight_contents_read": False,
                    "large_file_hashing": False,
                    "job_submission": False,
                    "gpu_allocation": False,
                    "inference": False,
                    "training": False,
                    "automatic_registry_update": False,
                    "backend_activation": False,
                },
            }
            _validate_approval(approval, approval_id, backend_id)
            _write_immutable(approval_path, _pretty_json(approval))
            _write_immutable(report_path, _approval_report(approval))
        elif after_sha != approval.get("backend_configuration_after_sha256"):
            raise ModelRegistrationError(
                "Recorded approval does not reproduce the backend configuration."
            )
        if _sha256_bytes(backend_path.read_bytes()) != before_sha:
            raise ModelRegistrationError(
                "Backend configuration changed during approval; no update applied."
            )
        _atomic_write(backend_path, updated_text)
        try:
            _, final_backend, _ = self.registry.backend(backend_id)
        except AIEvaluationError as exc:
            raise ModelRegistrationError(
                "Updated backend registry failed validation: {}".format(exc)
            ) from exc
        if final_backend.get("derived_readiness", {}).get("blockers") != [
            "inference_not_benchmarked"
        ]:
            raise ModelRegistrationError("Updated backend readiness is unexpected.")
        return ModelRegistrationApprovalResult(
            backend_id, approval_id, approval_path, backend_path, True, False,
            "verified_model_registration_recorded", "collect_fresh_backend_evidence",
        )
