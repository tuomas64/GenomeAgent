"""Explicitly authorized, integrity-bound atomic model publication.

Publication is intentionally separate from download and verification.  A fresh
read-only preflight binds the successful verification result and manifest candidate
to current staging metadata.  One explicitly authorized CPU job then re-hashes the
same files, removes only the registered transfer cache, writes the installed
manifest and atomically renames staging to the final path.  It never allocates a GPU,
runs inference, edits the backend registry or activates the backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Protocol

from genomeagent.ai_evaluation import AIRegistry
from genomeagent.model_acquisition_download import _bundle_sources, _load_bundle
from genomeagent.model_integrity_verification import (
    _absolute_path,
    _digest,
    _identifier,
    _normalized_inventory,
    _parse_utc,
    _pretty_json,
    _read_json,
    _reviewer,
    _safe_relative_path,
    _sha256_bytes,
    _sha256_value,
    _source,
    _within,
    _write_immutable,
)


MODEL_PUBLICATION_POLICY_VERSION = "1.0"
ALLOWED_REMOTE_MUTATIONS = (
    "create_confined_publication_control_directory",
    "write_confined_publication_spec_worker_logs_and_results",
    "submit_one_exact_cpu_publication_job",
    "remove_exact_staging_download_cache_after_reverification",
    "write_verified_installed_manifest_into_staging",
    "atomically_rename_exact_staging_directory_to_installation_path",
)
POLICY_FALSE_FIELDS = (
    "automatic_authorization_allowed",
    "automatic_remote_execution_allowed",
    "automatic_job_submission_allowed",
    "unbounded_remote_deletion_allowed",
    "overwrite_existing_installation_allowed",
    "cross_filesystem_publication_allowed",
    "gpu_allocation_allowed",
    "inference_allowed",
    "training_allowed",
    "registry_update_allowed",
    "backend_activation_allowed",
)
OBSERVATION_FALSE_FIELDS = (
    "remote_writes",
    "model_file_contents_read",
    "large_file_hashing",
    "job_submission",
    "staging_mutation",
    "cache_removal",
    "manifest_write",
    "publication",
    "gpu_allocation",
    "inference",
    "training",
    "registry_update",
    "backend_activation",
)
TERMINAL_STATES = {
    "published_ready_for_installed_model_evidence",
    "publication_failed_integrity",
    "publication_failed_runtime",
    "publication_failed_after_staging_mutation",
}


class ModelPublicationError(RuntimeError):
    """Raised when a controlled-publication boundary is not satisfied."""


class PythonRemoteRunner(Protocol):
    host: str

    def run_python(self, program: str, timeout_seconds: int = 240) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class PublicationPreflightResult:
    backend_id: str
    bundle_id: str
    verification_id: str
    evidence_id: str
    evidence_path: Path
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class PublicationStateResult:
    backend_id: str
    bundle_id: str
    source_snapshots: int
    latest_evidence_id: str
    status: str
    next_safe_action: str
    blockers: tuple[str, ...]
    state_dir: Path


@dataclass(frozen=True)
class PublicationAuthorizationResult:
    backend_id: str
    bundle_id: str
    authorization_id: str
    authorization_path: Path
    created: bool
    status: str
    expires_at_utc: str


@dataclass(frozen=True)
class PublicationLaunchResult:
    backend_id: str
    bundle_id: str
    authorization_id: str
    publication_id: str
    job_id: str
    launch_path: Path
    created: bool
    remote_status: str


@dataclass(frozen=True)
class PublicationStatusResult:
    backend_id: str
    bundle_id: str
    publication_id: str
    observation_id: str
    observation_path: Path
    status: str
    job_id: Optional[str]


def _false_fields(value: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise ModelPublicationError("{} safety field set is invalid.".format(label))
    if any(value[field] is not False for field in fields):
        raise ModelPublicationError("{} grants forbidden authority.".format(label))


def _validate_policy(path: Path, backend_id: str) -> tuple[dict[str, Any], bytes]:
    policy, raw = _read_json(path, "model publication policy")
    if (
        policy.get("schema_version") != "1.0"
        or policy.get("policy_version") != MODEL_PUBLICATION_POLICY_VERSION
        or policy.get("backend_id") != backend_id
        or policy.get("ssh_host") != "roihu"
        or policy.get("worker_python") != "/usr/bin/python3"
        or policy.get("download_cache_directory") != ".cache"
        or policy.get("installed_manifest_filename")
        != ".genomeagent-model-manifest.json"
    ):
        raise ModelPublicationError("Unsupported model publication policy.")
    for field in ("remote_python", "worker_python"):
        if not re.fullmatch(r"[A-Za-z0-9_./+-]+", str(policy.get(field) or "")):
            raise ModelPublicationError("Unsafe publication runtime field.")
    control = _absolute_path(policy.get("control_root"), "publication control root")
    if not control.startswith("/scratch/project_"):
        raise ModelPublicationError("Publication control root must be in project scratch.")
    expected_limits = {
        "preflight_timeout_seconds": (60, 600),
        "preflight_freshness_seconds": (300, 3600),
        "authorization_validity_seconds": (300, 900),
        "maximum_inventory_entries": (100, 10000),
        "maximum_approved_files": (1, 5000),
        "maximum_manifest_bytes": (10000, 10000000),
        "maximum_command_output_characters": (1000, 100000),
        "hash_chunk_bytes": (1048576, 67108864),
        "status_log_tail_bytes": (1000, 20000),
    }
    limits = policy.get("limits")
    if not isinstance(limits, Mapping) or set(limits) != set(expected_limits):
        raise ModelPublicationError("Publication limit field set is invalid.")
    for name, (low, high) in expected_limits.items():
        value = limits[name]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ModelPublicationError("Invalid publication limit {}.".format(name))
    expected_slurm = {
        "account": "project_2001113",
        "partition": "small",
        "time_limit": "02:00:00",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 1,
        "memory": "4G",
        "job_name": "GAmodelpublish",
    }
    if policy.get("slurm") != expected_slurm:
        raise ModelPublicationError("Unsupported publication Slurm contract.")
    if tuple(policy.get("allowed_remote_mutations") or ()) != ALLOWED_REMOTE_MUTATIONS:
        raise ModelPublicationError("Publication mutation allow-list is invalid.")
    _false_fields(policy.get("safety"), POLICY_FALSE_FIELDS, "Publication policy")
    return policy, raw


def _verification_inputs(
    verification_root: Path,
    backend_id: str,
    bundle_id: str,
    verification_id: str,
) -> tuple[dict[str, Any], bytes, Path, dict[str, Any], bytes, Path]:
    verification_id = _digest(verification_id, "verification ID")
    root = verification_root / backend_id / bundle_id / verification_id
    launch_path = root / "launch.json"
    launch, launch_raw = _read_json(launch_path, "model verification launch evidence")
    if (
        launch.get("source_mode")
        != "explicit_authorized_cpu_model_hash_verification_submission"
        or launch.get("backend_id") != backend_id
        or launch.get("bundle_id") != bundle_id
        or launch.get("verification_id") != verification_id
        or launch.get("publication") is not False
        or launch.get("gpu_allocation") is not False
    ):
        raise ModelPublicationError("Verification launch evidence is invalid.")
    status_paths = sorted((root / "status").glob("*.json"))
    if not status_paths:
        raise ModelPublicationError("No model verification status evidence exists.")
    status_path = status_paths[-1]
    status, status_raw = _read_json(status_path, "model verification status evidence")
    if (
        status.get("source_mode")
        != "bounded_read_only_model_integrity_status_observation"
        or status.get("backend_id") != backend_id
        or status.get("bundle_id") != bundle_id
        or status.get("verification_id") != verification_id
        or status.get("status") != "verified_ready_for_publication_review"
        or status.get("publication") is not False
        or status.get("staging_mutation") is not False
    ):
        raise ModelPublicationError("Latest verification is not publication-ready.")
    return launch, launch_raw, launch_path, status, status_raw, status_path


def _validate_manifest_candidate(
    manifest: Mapping[str, Any],
    backend_id: str,
    bundle_id: str,
    verification_id: str,
    bundle: Mapping[str, Any],
    inventory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("manifest_type") != "verified_model_candidate"
        or manifest.get("backend_id") != backend_id
        or manifest.get("bundle_id") != bundle_id
        or manifest.get("verification_id") != verification_id
        or manifest.get("repository") != bundle.get("repository")
        or manifest.get("resolved_revision") != bundle.get("resolved_revision")
        or manifest.get("staging_path") != bundle["target"]["staging_path"]
        or manifest.get("installation_path") != bundle["target"]["installation_path"]
        or manifest.get("publication_performed") is not False
    ):
        raise ModelPublicationError("Verified manifest candidate identity is invalid.")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(inventory):
        raise ModelPublicationError("Verified manifest file set is invalid.")
    expected = {item["path"]: item for item in inventory}
    normalized = []
    seen = set()
    for item in files:
        if not isinstance(item, Mapping):
            raise ModelPublicationError("Verified manifest contains a non-object file.")
        path = _safe_relative_path(item.get("path"), "verified manifest path")
        if path in seen or path not in expected:
            raise ModelPublicationError("Verified manifest path set is invalid.")
        seen.add(path)
        source = expected[path]
        digest = _digest(item.get("local_sha256"), "verified local SHA-256")
        provider = source.get("provider_lfs_sha256")
        if (
            item.get("size_bytes") != source["size_bytes"]
            or item.get("provider_lfs_sha256") != provider
            or item.get("provider_lfs_sha256_match")
            is not (None if provider is None else True)
        ):
            raise ModelPublicationError("Verified manifest file metadata is invalid.")
        normalized.append({
            "path": path,
            "size_bytes": source["size_bytes"],
            "local_sha256": digest,
            "provider_lfs_sha256": provider,
            "provider_lfs_sha256_match": None if provider is None else True,
            "provider_git_blob_id": item.get("provider_git_blob_id"),
            "provider_xet_hash": item.get("provider_xet_hash"),
        })
    normalized.sort(key=lambda item: item["path"])
    if set(expected) != seen:
        raise ModelPublicationError("Verified manifest is missing approved files.")
    total = sum(item["size_bytes"] for item in normalized)
    if manifest.get("file_count") != len(normalized) or manifest.get("total_bytes") != total:
        raise ModelPublicationError("Verified manifest totals are invalid.")
    return normalized


def _validate_publication_layout(
    bundle: Mapping[str, Any], policy: Mapping[str, Any], inventory: list[dict[str, Any]]
) -> None:
    target = bundle.get("target")
    if not isinstance(target, Mapping):
        raise ModelPublicationError("Bundle publication target is missing.")
    staging = PurePosixPath(str(target.get("staging_path") or ""))
    installation = PurePosixPath(str(target.get("installation_path") or ""))
    if (
        staging == installation
        or staging.parent != installation.parent
        or str(target.get("manifest_path") or "")
        != str(installation / policy["installed_manifest_filename"])
    ):
        raise ModelPublicationError(
            "Publication requires distinct sibling staging and installation directories."
        )
    reserved = policy["installed_manifest_filename"]
    if any(item["path"] == reserved for item in inventory):
        raise ModelPublicationError(
            "Approved model inventory conflicts with the installed manifest filename."
        )


def _preflight_program(
    backend_id: str,
    bundle_id: str,
    verification_id: str,
    bundle: Mapping[str, Any],
    policy: Mapping[str, Any],
    verification_control: str,
    expected_files: list[dict[str, Any]],
) -> str:
    config = {
        "backend_id": backend_id,
        "bundle_id": bundle_id,
        "verification_id": verification_id,
        "project_storage_root": bundle["target"]["project_storage_root"],
        "staging_path": bundle["target"]["staging_path"],
        "installation_path": bundle["target"]["installation_path"],
        "verification_control": verification_control,
        "publication_control_root": policy["control_root"],
        "cache_name": policy["download_cache_directory"],
        "worker_python": policy["worker_python"],
        "maximum_entries": policy["limits"]["maximum_inventory_entries"],
        "maximum_manifest_bytes": policy["limits"]["maximum_manifest_bytes"],
        "maximum_output": policy["limits"]["maximum_command_output_characters"],
        "expected_files": [
            {"path": item["path"], "size_bytes": item["size_bytes"]}
            for item in expected_files
        ],
        "slurm": policy["slurm"],
    }
    encoded = repr(json.dumps(config, sort_keys=True))
    program = r'''import json
import os
import stat
import subprocess
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_PUBLICATION_PREFLIGHT_CONFIG__)

def state(path_text):
    path = Path(path_text)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"path":path_text, "lexists":False, "exists":False,
            "is_symlink":False, "is_directory":False, "device":None}
    try: resolved=str(path.resolve(strict=True))
    except OSError: resolved=None
    return {"path":path_text, "lexists":True, "exists":path.exists(),
        "is_symlink":stat.S_ISLNK(info.st_mode),
        "is_directory":stat.S_ISDIR(info.st_mode), "is_file_followed":path.is_file(),
        "executable_by_access_check":os.access(path,os.X_OK),
        "resolved_path":resolved,"device":info.st_dev}

def chain_symlinks(path_text, root_text):
    root = Path(root_text); path = Path(path_text)
    try: relative = path.relative_to(root)
    except ValueError: return ["outside_project_root"]
    found = []; current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            found.append(str(current))
    return found

def small_json(path):
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return {"error":"not_regular"}
        if info.st_size > int(CONFIG["maximum_manifest_bytes"]):
            return {"error":"too_large", "size_bytes":info.st_size}
        descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
        try:
            opened=os.fstat(descriptor)
            if (opened.st_dev,opened.st_ino,opened.st_size)!=(info.st_dev,info.st_ino,info.st_size):
                return {"error":"changed_before_read"}
            chunks=[]; total=0
            while True:
                block=os.read(descriptor,min(65536,int(CONFIG["maximum_manifest_bytes"])+1-total))
                if not block: break
                chunks.append(block); total+=len(block)
                if total>int(CONFIG["maximum_manifest_bytes"]): break
            raw=b"".join(chunks)
            after=os.fstat(descriptor)
        finally: os.close(descriptor)
        if len(raw)>int(CONFIG["maximum_manifest_bytes"]): return {"error":"too_large"}
        if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)!=(
            info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns
        ): return {"error":"changed_during_read"}
        value = json.loads(raw)
        return {"value":value, "sha256":__import__("hashlib").sha256(raw).hexdigest(),
            "size_bytes":len(raw)}
    except BaseException as exc:
        return {"error":type(exc).__name__ + ":" + str(exc)[:500]}

def bounded(command):
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=15, check=False)
        limit = int(CONFIG["maximum_output"])
        return {"returncode":completed.returncode, "stdout":completed.stdout[:limit],
            "stderr":completed.stderr[:limit], "timed_out":False}
    except subprocess.TimeoutExpired as exc:
        return {"returncode":None, "stdout":"", "stderr":str(exc)[:1000],
            "timed_out":True}

staging = Path(CONFIG["staging_path"]); cache_name = CONFIG["cache_name"]
entries=[]; symlinks=[]; special=[]; cache={"present":False,"regular_files":0,
    "directories":0,"bytes":0}; walk_error=None
if staging.is_dir() and not staging.is_symlink():
    try:
        for current, directories, files in os.walk(staging, followlinks=False):
            current_path=Path(current); rel_current=current_path.relative_to(staging)
            if rel_current.parts and rel_current.parts[0] == cache_name:
                cache["present"]=True; cache["directories"]+=1
            for name in list(directories):
                path=current_path/name; relative=path.relative_to(staging).as_posix()
                info=os.lstat(path)
                if stat.S_ISLNK(info.st_mode): symlinks.append(relative); directories.remove(name)
                elif not stat.S_ISDIR(info.st_mode): special.append(relative); directories.remove(name)
            for name in files:
                path=current_path/name; relative=path.relative_to(staging).as_posix()
                info=os.lstat(path)
                if stat.S_ISLNK(info.st_mode): symlinks.append(relative)
                elif not stat.S_ISREG(info.st_mode): special.append(relative)
                elif relative == cache_name or relative.startswith(cache_name + "/"):
                    cache["present"]=True; cache["regular_files"]+=1; cache["bytes"]+=info.st_size
                else: entries.append({"path":relative,"size_bytes":info.st_size})
                if len(entries)+cache["regular_files"]+len(symlinks)+len(special) > int(CONFIG["maximum_entries"]):
                    raise RuntimeError("inventory_limit_exceeded")
    except BaseException as exc: walk_error=type(exc).__name__+":"+str(exc)[:500]
entries.sort(key=lambda item:item["path"])
control=Path(CONFIG["verification_control"])
partition=bounded(["sinfo","-h","-p",CONFIG["slurm"]["partition"],"-o","%P|%a|%l|%D"])
observation={"schema_version":"1.0", "observation_type":"model_publication_preflight",
    "backend_id":CONFIG["backend_id"], "bundle_id":CONFIG["bundle_id"],
    "verification_id":CONFIG["verification_id"],
    "identity":{"hostname":__import__("socket").gethostname(),
        "machine":__import__("platform").machine()},
    "paths":{"project_storage":state(CONFIG["project_storage_root"]),
        "staging":state(CONFIG["staging_path"]),
        "installation":state(CONFIG["installation_path"]),
        "installation_parent":state(str(Path(CONFIG["installation_path"]).parent)),
        "verification_control":state(CONFIG["verification_control"]),
        "publication_control_root":state(CONFIG["publication_control_root"]),
        "worker_python":state(CONFIG["worker_python"]),
        "staging_chain_symlinks":chain_symlinks(CONFIG["staging_path"],CONFIG["project_storage_root"]),
        "installation_chain_symlinks":chain_symlinks(str(Path(CONFIG["installation_path"]).parent),CONFIG["project_storage_root"]),
        "verification_control_chain_symlinks":chain_symlinks(CONFIG["verification_control"],CONFIG["project_storage_root"]),
        "publication_control_chain_symlinks":chain_symlinks(CONFIG["publication_control_root"],CONFIG["project_storage_root"])},
    "inventory":{"files":entries,"file_count":len(entries),
        "total_bytes":sum(i["size_bytes"] for i in entries),"symlinks":sorted(symlinks),
        "special_entries":sorted(special),"walk_error":walk_error,"download_cache":cache},
    "verification":{"result":small_json(control/"result.json"),
        "manifest_candidate":small_json(control/"manifest_candidate.json")},
    "scheduler":{"sbatch_path":__import__("shutil").which("sbatch"),
        "squeue_path":__import__("shutil").which("squeue"),
        "sacct_path":__import__("shutil").which("sacct"),
        "sinfo_path":__import__("shutil").which("sinfo"),"partition_query":partition},
    "safety":{"remote_writes":False,"model_file_contents_read":False,
        "large_file_hashing":False,"job_submission":False,"staging_mutation":False,
        "cache_removal":False,"manifest_write":False,"publication":False,
        "gpu_allocation":False,"inference":False,"training":False,
        "registry_update":False,"backend_activation":False}}
print(json.dumps(observation,sort_keys=True,separators=(",",":")))
'''
    return program.replace("__GENOMEAGENT_PUBLICATION_PREFLIGHT_CONFIG__", encoded)


def _partition_ready(value: Any, expected: str) -> bool:
    if not isinstance(value, Mapping) or value.get("returncode") != 0:
        return False
    if value.get("timed_out") is not False:
        return False
    return any(
        len(parts := line.split("|")) >= 2
        and parts[0].rstrip("*") == expected
        and parts[1] == "up"
        for line in str(value.get("stdout") or "").splitlines()
    )


def _evaluate_preflight(
    observation: Mapping[str, Any],
    backend_id: str,
    bundle_id: str,
    verification_id: str,
    bundle: Mapping[str, Any],
    inventory: list[dict[str, Any]],
    policy: Mapping[str, Any],
) -> tuple[str, str, list[str], dict[str, Any], dict[str, Any]]:
    if (
        observation.get("observation_type") != "model_publication_preflight"
        or observation.get("backend_id") != backend_id
        or observation.get("bundle_id") != bundle_id
        or observation.get("verification_id") != verification_id
    ):
        raise ModelPublicationError("Remote publication preflight identity is invalid.")
    _false_fields(observation.get("safety"), OBSERVATION_FALSE_FIELDS, "Preflight")
    blockers: list[str] = []
    paths = observation.get("paths")
    observed_inventory = observation.get("inventory")
    verification = observation.get("verification")
    scheduler = observation.get("scheduler")
    if not all(isinstance(value, Mapping) for value in (
        paths, observed_inventory, verification, scheduler
    )):
        raise ModelPublicationError("Remote publication preflight is incomplete.")
    project = paths.get("project_storage")
    staging = paths.get("staging")
    installation = paths.get("installation")
    installation_parent = paths.get("installation_parent")
    control = paths.get("verification_control")
    worker = paths.get("worker_python")
    if paths.get("staging_chain_symlinks") != []:
        blockers.append("staging_parent_symlink_detected")
    if paths.get("installation_chain_symlinks") != []:
        blockers.append("installation_parent_symlink_detected")
    if paths.get("verification_control_chain_symlinks") != []:
        blockers.append("verification_control_parent_symlink_detected")
    if paths.get("publication_control_chain_symlinks") != []:
        blockers.append("publication_control_parent_symlink_detected")
    if not isinstance(project, Mapping) or not (
        project.get("path") == bundle["target"]["project_storage_root"]
        and project.get("exists") is True
        and project.get("is_directory") is True
        and project.get("is_symlink") is False
    ):
        blockers.append("project_storage_unverified")
    if not isinstance(staging, Mapping) or not (
        staging.get("path") == bundle["target"]["staging_path"]
        and staging.get("exists") is True
        and staging.get("is_directory") is True
        and staging.get("is_symlink") is False
    ):
        blockers.append("staging_directory_unverified")
    if not isinstance(installation, Mapping) or not (
        installation.get("path") == bundle["target"]["installation_path"]
        and installation.get("lexists") is False
    ):
        blockers.append("installation_target_not_absent")
    if not isinstance(installation_parent, Mapping) or not (
        installation_parent.get("exists") is True
        and installation_parent.get("is_directory") is True
        and installation_parent.get("is_symlink") is False
    ):
        blockers.append("installation_parent_unverified")
    if (
        isinstance(staging, Mapping)
        and isinstance(installation_parent, Mapping)
        and staging.get("device") != installation_parent.get("device")
    ):
        blockers.append("atomic_same_filesystem_contract_unverified")
    if not isinstance(control, Mapping) or not (
        control.get("exists") is True
        and control.get("is_directory") is True
        and control.get("is_symlink") is False
    ):
        blockers.append("verification_control_unverified")
    if not isinstance(worker, Mapping) or not (
        worker.get("exists") is True
        and str(worker.get("path") or "") == policy["worker_python"]
        and worker.get("is_file_followed") is True
        and worker.get("executable_by_access_check") is True
        and str(worker.get("resolved_path") or "").startswith("/usr/bin/python3")
    ):
        blockers.append("worker_python_unverified")
    expected_sizes = [
        {"path": item["path"], "size_bytes": item["size_bytes"]}
        for item in inventory
    ]
    if observed_inventory.get("walk_error") is not None:
        blockers.append("staging_inventory_walk_failed")
    if observed_inventory.get("symlinks") != []:
        blockers.append("staging_symlinks_detected")
    if observed_inventory.get("special_entries") != []:
        blockers.append("staging_special_entries_detected")
    if observed_inventory.get("files") != expected_sizes:
        blockers.append("staged_paths_or_sizes_mismatch")
    if observed_inventory.get("file_count") != len(expected_sizes):
        blockers.append("staged_file_count_mismatch")
    expected_bytes = sum(item["size_bytes"] for item in inventory)
    if observed_inventory.get("total_bytes") != expected_bytes:
        blockers.append("staged_total_bytes_mismatch")
    result_record = verification.get("result")
    manifest_record = verification.get("manifest_candidate")
    result_value = result_record.get("value") if isinstance(result_record, Mapping) else None
    manifest_value = (
        manifest_record.get("value") if isinstance(manifest_record, Mapping) else None
    )
    if not isinstance(result_value, Mapping) or not (
        result_value.get("status") == "verified_ready_for_publication_review"
        and result_value.get("verification_id") == verification_id
        and result_value.get("verified_file_count") == len(inventory)
        and result_value.get("verified_total_bytes") == expected_bytes
        and result_value.get("publication") is False
        and result_value.get("gpu_allocation") is False
    ):
        blockers.append("remote_verification_result_unverified")
    normalized_manifest: list[dict[str, Any]] = []
    if not isinstance(manifest_value, Mapping):
        blockers.append("remote_verified_manifest_missing_or_invalid")
    else:
        try:
            normalized_manifest = _validate_manifest_candidate(
                manifest_value,
                backend_id,
                bundle_id,
                verification_id,
                bundle,
                inventory,
            )
        except Exception:
            blockers.append("remote_verified_manifest_contract_mismatch")
    for record, label in (
        (result_record, "verification_result_digest_missing"),
        (manifest_record, "verified_manifest_digest_missing"),
    ):
        if not isinstance(record, Mapping) or not re.fullmatch(
            r"[0-9a-f]{64}", str(record.get("sha256") or "")
        ):
            blockers.append(label)
    if not all(str(scheduler.get(name) or "").startswith("/") for name in (
        "sbatch_path", "squeue_path", "sacct_path", "sinfo_path"
    )):
        blockers.append("slurm_commands_unverified")
    elif not _partition_ready(scheduler.get("partition_query"), policy["slurm"]["partition"]):
        blockers.append("cpu_partition_unverified")
    identity = observation.get("identity")
    if not isinstance(identity, Mapping) or identity.get("machine") not in {
        "x86_64", "amd64"
    }:
        blockers.append("cpu_architecture_unverified")
    cache = observed_inventory.get("download_cache")
    if not isinstance(cache, Mapping):
        blockers.append("download_cache_inventory_missing")
        cache = {}
    blockers = list(dict.fromkeys(blockers))
    summary = {
        "expected_files": len(inventory),
        "observed_files": observed_inventory.get("file_count"),
        "expected_bytes": expected_bytes,
        "observed_bytes": observed_inventory.get("total_bytes"),
        "download_cache": dict(cache),
        "verification_result_sha256": (
            result_record.get("sha256") if isinstance(result_record, Mapping) else None
        ),
        "verified_manifest_sha256": (
            manifest_record.get("sha256") if isinstance(manifest_record, Mapping) else None
        ),
    }
    manifest = {
        "files": normalized_manifest,
        "raw_candidate": dict(manifest_value) if isinstance(manifest_value, Mapping) else {},
    }
    if blockers:
        return (
            "publication_preflight_attention_required",
            "review_publication_preflight_before_authorization",
            blockers,
            summary,
            manifest,
        )
    return (
        "ready_for_publication_authorization_review",
        "review_and_issue_fresh_atomic_publication_authorization",
        ["fresh_publication_authorization_missing"],
        summary,
        manifest,
    )


def _preflight_report(evidence: Mapping[str, Any]) -> str:
    summary = evidence["summary"]
    cache = summary.get("download_cache") or {}
    return "\n".join([
        "# GenomeAgent Controlled Model Publication Preflight",
        "",
        "- Backend: `{}`".format(evidence["backend_id"]),
        "- Bundle: `{}`".format(evidence["bundle_id"]),
        "- Verification: `{}`".format(evidence["verification_id"]),
        "- Evidence: `{}`".format(evidence["evidence_id"]),
        "- Status: **{}**".format(evidence["readiness"]["status"]),
        "- Expected/observed files: `{}/{}`".format(
            summary["expected_files"], summary["observed_files"]
        ),
        "- Transfer-cache files/bytes: `{}/{}`".format(
            cache.get("regular_files", 0), cache.get("bytes", 0)
        ),
        "",
        "## Blockers",
        "",
        *["- `{}`".format(item) for item in evidence["readiness"]["blockers"]],
        "",
        "This preflight read metadata and the small verification result and manifest "
        "candidate only. It did not read model contents, mutate staging, submit a job "
        "or publish the model.",
        "",
    ])


class ModelPublicationPreflightCollector:
    """Collect fresh read-only evidence before publication authorization."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/model_publication"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
        verification_root: Path = Path("workspace/model_integrity_verifications"),
        evidence_root: Path = Path("workspace/model_publication_preflight_evidence"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.bundle_root = Path(bundle_root)
        self.plan_root = Path(plan_root)
        self.verification_root = Path(verification_root)
        self.evidence_root = Path(evidence_root)

    def _inputs(self, backend_id: str, bundle_id: str, verification_id: str):
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        verification_id = _digest(verification_id, "verification ID")
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        policy_path = self.policy_root / (backend_id + ".json")
        policy, policy_raw = _validate_policy(policy_path, backend_id)
        bundle, _, _ = _load_bundle(
            backend_id, bundle_id, self.bundle_root, self.plan_root, backend
        )
        if bundle["target"].get("publication_strategy") != "atomic_directory_rename_same_filesystem":
            raise ModelPublicationError("Bundle does not require atomic publication.")
        inventory = _normalized_inventory(bundle, policy["download_cache_directory"])
        _validate_publication_layout(bundle, policy, inventory)
        if len(inventory) > policy["limits"]["maximum_approved_files"]:
            raise ModelPublicationError("Approved inventory exceeds publication policy bound.")
        launch, launch_raw, launch_path, status, status_raw, status_path = (
            _verification_inputs(
                self.verification_root, backend_id, bundle_id, verification_id
            )
        )
        control = str(launch.get("control_directory") or "")
        if not control or not _within(control, bundle["target"]["project_storage_root"]):
            raise ModelPublicationError("Verification control path is unsafe.")
        sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "model_publication_policy"),
            *_bundle_sources(self.bundle_root, backend_id, bundle_id),
            _source(launch_path, launch_raw, "model_verification_launch_evidence"),
            _source(status_path, status_raw, "model_verification_status_evidence"),
        ]
        return backend, policy, bundle, inventory, control, sources

    def collect(
        self,
        backend_id: str,
        bundle_id: str,
        verification_id: str,
        runner: PythonRemoteRunner,
        stamp: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> PublicationPreflightResult:
        backend, policy, bundle, inventory, control, sources = self._inputs(
            backend_id, bundle_id, verification_id
        )
        if str(runner.host) != policy["ssh_host"]:
            raise ModelPublicationError("Publication preflight host is not Roihu-CPU.")
        observed = observed_at or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            raise ModelPublicationError("Preflight timestamp must include timezone.")
        observed = observed.astimezone(timezone.utc)
        evidence_id = stamp or observed.strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", evidence_id):
            raise ModelPublicationError("Invalid publication preflight evidence ID.")
        try:
            observation = runner.run_python(
                _preflight_program(
                    backend_id,
                    bundle_id,
                    verification_id,
                    bundle,
                    policy,
                    control,
                    inventory,
                ),
                timeout_seconds=policy["limits"]["preflight_timeout_seconds"],
            )
        except Exception as exc:
            raise ModelPublicationError(
                "Read-only publication preflight failed: {}".format(exc)
            ) from exc
        if not isinstance(observation, Mapping):
            raise ModelPublicationError("Publication preflight result is not an object.")
        status, next_action, blockers, summary, manifest = _evaluate_preflight(
            observation,
            backend_id,
            bundle_id,
            verification_id,
            bundle,
            inventory,
            policy,
        )
        expires = observed + timedelta(
            seconds=policy["limits"]["preflight_freshness_seconds"]
        )
        evidence = {
            "schema_version": "1.0",
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "source_mode": "bounded_read_only_model_publication_preflight",
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "verification_id": verification_id,
            "evidence_id": evidence_id,
            "observed_at_utc": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at_utc": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_artifacts": sources,
            "source_artifact_sha256": {
                "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
                for item in sources
            },
            "summary": summary,
            "verified_manifest": manifest,
            "observation": dict(observation),
            "readiness": {
                "status": status,
                "next_safe_action": next_action,
                "blockers": blockers,
                "automatic_execution_allowed": False,
                "automatic_job_submission_allowed": False,
                "automatic_publication_allowed": False,
            },
            "safety": {field: False for field in OBSERVATION_FALSE_FIELDS},
        }
        path = self.evidence_root / backend_id / bundle_id / (evidence_id + ".json")
        _write_immutable(path, _pretty_json(evidence))
        _write_immutable(path.with_suffix(".md"), _preflight_report(evidence).encode())
        return PublicationPreflightResult(
            backend_id,
            bundle_id,
            verification_id,
            evidence_id,
            path,
            status,
            tuple(blockers),
        )


def _authorization_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "policy_version": value.get("policy_version"),
        "backend_id": value.get("backend_id"),
        "bundle_id": value.get("bundle_id"),
        "verification_id": value.get("verification_id"),
        "publication_evidence_id": value.get("publication_evidence_id"),
        "publication_evidence_sha256": value.get("publication_evidence_sha256"),
        "source_artifact_sha256": value.get("source_artifact_sha256"),
        "verified_manifest_sha256": value.get("verified_manifest_sha256"),
        "verification_result_sha256": value.get("verification_result_sha256"),
        "reviewer": value.get("reviewer"),
        "authorization_scope": value.get("authorization_scope"),
        "remote_host": value.get("remote_host"),
        "slurm": value.get("slurm"),
        "allowed_remote_mutations": value.get("allowed_remote_mutations"),
        "download_cache_removal_confirmed": value.get(
            "download_cache_removal_confirmed"
        ),
    }


def _authorization_record_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": value.get("schema_version"),
        "source_mode": value.get("source_mode"),
        "authorization_id": value.get("authorization_id"),
        **_authorization_identity(value),
        "authorized_at_utc": value.get("authorized_at_utc"),
        "expires_at_utc": value.get("expires_at_utc"),
        "source_artifacts": value.get("source_artifacts"),
        "automatic_execution_allowed": value.get("automatic_execution_allowed"),
        "automatic_job_submission_allowed": value.get(
            "automatic_job_submission_allowed"
        ),
        "automatic_backend_activation_allowed": value.get(
            "automatic_backend_activation_allowed"
        ),
    }


def _validate_authorization(
    value: Mapping[str, Any], authorization_id: str, backend_id: str, bundle_id: str
) -> None:
    if (
        value.get("schema_version") != "1.0"
        or value.get("policy_version") != MODEL_PUBLICATION_POLICY_VERSION
        or value.get("source_mode")
        != "explicit_researcher_atomic_model_publication_authorization"
        or value.get("authorization_id") != authorization_id
        or value.get("backend_id") != backend_id
        or value.get("bundle_id") != bundle_id
        or value.get("authorization_scope")
        != "submit_one_reverified_atomic_model_publication"
        or tuple(value.get("allowed_remote_mutations") or ())
        != ALLOWED_REMOTE_MUTATIONS
        or value.get("download_cache_removal_confirmed") is not True
        or value.get("automatic_execution_allowed") is not False
        or value.get("automatic_job_submission_allowed") is not False
        or value.get("automatic_backend_activation_allowed") is not False
    ):
        raise ModelPublicationError("Publication authorization identity is invalid.")
    _reviewer(value.get("reviewer"))
    _digest(value.get("verification_id"), "verification ID")
    _digest(value.get("publication_evidence_sha256"), "publication evidence digest")
    _digest(value.get("verified_manifest_sha256"), "verified manifest digest")
    _digest(value.get("verification_result_sha256"), "verification result digest")
    source_digests = value.get("source_artifact_sha256")
    if not isinstance(source_digests, Mapping) or not source_digests:
        raise ModelPublicationError("Publication authorization source set is invalid.")
    for digest in source_digests.values():
        _digest(digest, "publication authorization source digest")
    if _sha256_value(_authorization_identity(value)) != authorization_id:
        raise ModelPublicationError("Publication authorization content digest mismatch.")
    if _sha256_value(_authorization_record_identity(value)) != value.get(
        "authorization_record_sha256"
    ):
        raise ModelPublicationError("Publication authorization record digest mismatch.")
    _parse_utc(value.get("authorized_at_utc"), "publication authorization time")
    _parse_utc(value.get("expires_at_utc"), "publication authorization expiry")


class ModelPublicationCore:
    """Replay preflight evidence, authorize publication, submit and observe it."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/model_publication"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
        verification_root: Path = Path("workspace/model_integrity_verifications"),
        evidence_root: Path = Path("workspace/model_publication_preflight_evidence"),
        state_root: Path = Path("workspace/model_publication_preflight_state"),
        authorization_root: Path = Path("workspace/model_publication_authorizations"),
        publication_root: Path = Path("workspace/model_publications"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.bundle_root = Path(bundle_root)
        self.plan_root = Path(plan_root)
        self.verification_root = Path(verification_root)
        self.evidence_root = Path(evidence_root)
        self.state_root = Path(state_root)
        self.authorization_root = Path(authorization_root)
        self.publication_root = Path(publication_root)

    def policy(self, backend_id: str) -> tuple[Path, dict[str, Any], bytes]:
        backend_id = _identifier(backend_id, "backend ID")
        path = self.policy_root / (backend_id + ".json")
        policy, raw = _validate_policy(path, backend_id)
        return path, policy, raw

    def _collector(self) -> ModelPublicationPreflightCollector:
        return ModelPublicationPreflightCollector(
            registry=self.registry,
            policy_root=self.policy_root,
            bundle_root=self.bundle_root,
            plan_root=self.plan_root,
            verification_root=self.verification_root,
            evidence_root=self.evidence_root,
        )

    def _validate_evidence(self, value: Mapping[str, Any]) -> None:
        if (
            value.get("schema_version") != "1.0"
            or value.get("policy_version") != MODEL_PUBLICATION_POLICY_VERSION
            or value.get("source_mode")
            != "bounded_read_only_model_publication_preflight"
        ):
            raise ModelPublicationError("Publication preflight evidence is invalid.")
        _identifier(value.get("backend_id"), "evidence backend ID")
        _digest(value.get("bundle_id"), "evidence bundle ID")
        _digest(value.get("verification_id"), "evidence verification ID")
        _parse_utc(value.get("observed_at_utc"), "evidence observation time")
        _parse_utc(value.get("expires_at_utc"), "evidence expiry")
        _false_fields(value.get("safety"), OBSERVATION_FALSE_FIELDS, "Evidence")
        readiness = value.get("readiness")
        if not isinstance(readiness, Mapping) or any(
            readiness.get(field) is not False
            for field in (
                "automatic_execution_allowed",
                "automatic_job_submission_allowed",
                "automatic_publication_allowed",
            )
        ):
            raise ModelPublicationError("Publication evidence grants authority.")

    def ingest(
        self,
        backend_id: str,
        bundle_id: str,
        now: Optional[datetime] = None,
    ) -> PublicationStateResult:
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        paths = sorted((self.evidence_root / backend_id / bundle_id).glob("*.json"))
        if not paths:
            raise ModelPublicationError("No publication preflight evidence exists.")
        snapshots = []
        for path in paths:
            value, raw = _read_json(path, "publication preflight evidence")
            self._validate_evidence(value)
            if value["backend_id"] != backend_id or value["bundle_id"] != bundle_id:
                raise ModelPublicationError("Publication evidence path identity mismatch.")
            snapshots.append((path, value, raw))
        latest_path, latest, latest_raw = snapshots[-1]
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ModelPublicationError("Publication ingest time must include timezone.")
        current = current.astimezone(timezone.utc)
        stale = []
        for item in latest.get("source_artifacts") or []:
            if not isinstance(item, Mapping):
                stale.append("invalid_source_record")
                continue
            path = Path(str(item.get("path") or ""))
            try:
                raw = path.read_bytes() if not path.is_symlink() else b""
            except OSError:
                raw = b""
            if _sha256_bytes(raw) != item.get("sha256"):
                stale.append(str(path))
        fresh = current <= _parse_utc(latest["expires_at_utc"], "evidence expiry")
        if stale:
            status = "stale_publication_preflight"
            blockers = ["publication_source_artifacts_changed"]
            next_action = "collect_and_ingest_fresh_publication_preflight"
        elif not fresh:
            status = "stale_publication_preflight"
            blockers = ["publication_preflight_expired"]
            next_action = "collect_and_ingest_fresh_publication_preflight"
        else:
            status = str(latest["readiness"]["status"])
            blockers = list(latest["readiness"]["blockers"])
            next_action = str(latest["readiness"]["next_safe_action"])
        state = {
            "schema_version": "1.0",
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "verification_id": latest["verification_id"],
            "latest_evidence_id": latest["evidence_id"],
            "latest_source_sha256": _sha256_bytes(latest_raw),
            "latest_observed_at_utc": latest["observed_at_utc"],
            "latest_expires_at_utc": latest["expires_at_utc"],
            "source_snapshots": len(snapshots),
            "inputs_current": not stale,
            "evidence_fresh": fresh,
            "stale_inputs": stale,
            "status": status,
            "next_safe_action": next_action,
            "blockers": blockers,
            "summary": latest["summary"],
            "automatic_execution_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_publication_allowed": False,
        }
        readiness = {
            "schema_version": "1.0",
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "status": status,
            "next_safe_action": next_action,
            "blockers": blockers,
            "inputs_current": not stale,
            "evidence_fresh": fresh,
            "automatic_execution_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_publication_allowed": False,
        }
        provenance = {
            "schema_version": "1.0",
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "evidence_snapshots": [
                {
                    "evidence_id": value["evidence_id"],
                    "path": str(path),
                    "sha256": _sha256_bytes(raw),
                }
                for path, value, raw in snapshots
            ],
            "derivation": "deterministic_local_publication_preflight_replay",
            "remote_access": False,
            "job_submission": False,
            "publication": False,
        }
        state_dir = self.state_root / backend_id / bundle_id
        for name, value in (
            ("current_evidence.json", state),
            ("readiness.json", readiness),
            ("provenance.json", provenance),
        ):
            path = state_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(_pretty_json(value))
            os.replace(temporary, path)
        (state_dir / "report.md").write_text("\n".join([
            "# GenomeAgent Model Publication Preflight State",
            "",
            "- Backend: `{}`".format(backend_id),
            "- Bundle: `{}`".format(bundle_id),
            "- Latest evidence: `{}`".format(latest["evidence_id"]),
            "- Status: **{}**".format(status),
            "- Next safe action: `{}`".format(next_action),
            "- Automatic publication: **no**",
            "",
        ]))
        return PublicationStateResult(
            backend_id,
            bundle_id,
            len(snapshots),
            latest["evidence_id"],
            status,
            next_action,
            tuple(blockers),
            state_dir,
        )

    def _current_inputs(self, backend_id: str, bundle_id: str, verification_id: str):
        return self._collector()._inputs(backend_id, bundle_id, verification_id)

    def authorize(
        self,
        backend_id: str,
        bundle_id: str,
        verification_id: str,
        evidence_id: str,
        reviewer: str,
        confirm_publication: bool,
        confirm_cache_removal: bool,
        now: Optional[datetime] = None,
    ) -> PublicationAuthorizationResult:
        if not confirm_publication:
            raise ModelPublicationError(
                "Explicit --confirm-atomic-model-publication is required."
            )
        if not confirm_cache_removal:
            raise ModelPublicationError(
                "Explicit --confirm-remove-download-cache is required."
            )
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        verification_id = _digest(verification_id, "verification ID")
        reviewer = _reviewer(reviewer)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ModelPublicationError("Authorization time must include timezone.")
        current = current.astimezone(timezone.utc)
        state_dir = self.state_root / backend_id / bundle_id
        state_path = state_dir / "current_evidence.json"
        readiness_path = state_dir / "readiness.json"
        provenance_path = state_dir / "provenance.json"
        state, state_raw = _read_json(state_path, "publication current state")
        readiness, readiness_raw = _read_json(readiness_path, "publication readiness")
        provenance, provenance_raw = _read_json(
            provenance_path, "publication provenance"
        )
        if (
            state.get("verification_id") != verification_id
            or state.get("latest_evidence_id") != evidence_id
            or state.get("status") != "ready_for_publication_authorization_review"
            or state.get("blockers") != ["fresh_publication_authorization_missing"]
            or state.get("inputs_current") is not True
            or state.get("evidence_fresh") is not True
            or readiness.get("status")
            != "ready_for_publication_authorization_review"
            or readiness.get("automatic_publication_allowed") is not False
        ):
            raise ModelPublicationError("Publication state is not authorization-ready.")
        if current > _parse_utc(state["latest_expires_at_utc"], "preflight expiry"):
            raise ModelPublicationError("Publication preflight expired; collect again.")
        evidence_path = self.evidence_root / backend_id / bundle_id / (evidence_id + ".json")
        evidence, evidence_raw = _read_json(evidence_path, "publication evidence")
        self._validate_evidence(evidence)
        if (
            evidence.get("verification_id") != verification_id
            or _sha256_bytes(evidence_raw) != state.get("latest_source_sha256")
            or evidence.get("readiness", {}).get("status")
            != "ready_for_publication_authorization_review"
            or current > _parse_utc(evidence["expires_at_utc"], "evidence expiry")
        ):
            raise ModelPublicationError("Direct publication evidence is not current-ready.")
        records = provenance.get("evidence_snapshots")
        if not isinstance(records, list) or not any(
            isinstance(item, Mapping)
            and item.get("evidence_id") == evidence_id
            and item.get("sha256") == _sha256_bytes(evidence_raw)
            for item in records
        ):
            raise ModelPublicationError("Publication provenance does not bind evidence.")
        backend, policy, bundle, inventory, control, current_sources = self._current_inputs(
            backend_id, bundle_id, verification_id
        )
        current_map = {
            "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
            for item in current_sources
        }
        if current_map != evidence.get("source_artifact_sha256"):
            raise ModelPublicationError("Publication source artifacts changed after preflight.")
        files = evidence.get("verified_manifest", {}).get("files")
        if not isinstance(files, list) or len(files) != len(inventory):
            raise ModelPublicationError("Publication evidence lacks verified file hashes.")
        summary = evidence.get("summary")
        if not isinstance(summary, Mapping):
            raise ModelPublicationError("Publication evidence summary is invalid.")
        verified_manifest_sha = _digest(
            summary.get("verified_manifest_sha256"), "verified manifest digest"
        )
        verification_result_sha = _digest(
            summary.get("verification_result_sha256"), "verification result digest"
        )
        sources = [
            *current_sources,
            _source(state_path, state_raw, "model_publication_preflight_state"),
            _source(readiness_path, readiness_raw, "model_publication_readiness"),
            _source(provenance_path, provenance_raw, "model_publication_provenance"),
            _source(evidence_path, evidence_raw, "model_publication_preflight_evidence"),
        ]
        identity = {
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "verification_id": verification_id,
            "publication_evidence_id": evidence_id,
            "publication_evidence_sha256": _sha256_bytes(evidence_raw),
            "source_artifact_sha256": {
                "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
                for item in sources
            },
            "verified_manifest_sha256": verified_manifest_sha,
            "verification_result_sha256": verification_result_sha,
            "reviewer": reviewer,
            "authorization_scope": "submit_one_reverified_atomic_model_publication",
            "remote_host": policy["ssh_host"],
            "slurm": policy["slurm"],
            "allowed_remote_mutations": list(ALLOWED_REMOTE_MUTATIONS),
            "download_cache_removal_confirmed": True,
        }
        authorization_id = _sha256_value(identity)
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        if path.exists():
            existing, _ = _read_json(path, "publication authorization")
            _validate_authorization(existing, authorization_id, backend_id, bundle_id)
            return PublicationAuthorizationResult(
                backend_id,
                bundle_id,
                authorization_id,
                path,
                False,
                "atomic_publication_already_authorized",
                existing["expires_at_utc"],
            )
        expiry = min(
            _parse_utc(state["latest_expires_at_utc"], "preflight expiry"),
            current + timedelta(seconds=policy["limits"]["authorization_validity_seconds"]),
        )
        authorization = {
            "schema_version": "1.0",
            "source_mode": "explicit_researcher_atomic_model_publication_authorization",
            "authorization_id": authorization_id,
            **identity,
            "authorized_at_utc": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at_utc": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_artifacts": sources,
            "automatic_execution_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_backend_activation_allowed": False,
        }
        authorization["authorization_record_sha256"] = _sha256_value(
            _authorization_record_identity(authorization)
        )
        _validate_authorization(authorization, authorization_id, backend_id, bundle_id)
        created = _write_immutable(path, _pretty_json(authorization))
        _write_immutable(path.with_suffix(".md"), "\n".join([
            "# GenomeAgent Atomic Model Publication Authorization",
            "",
            "- Backend: `{}`".format(backend_id),
            "- Bundle: `{}`".format(bundle_id),
            "- Verification: `{}`".format(verification_id),
            "- Authorization: `{}`".format(authorization_id),
            "- Reviewer: `{}`".format(reviewer),
            "- Expires: `{}`".format(authorization["expires_at_utc"]),
            "- Exact transfer-cache removal: **approved**",
            "- GPU/inference/activation: **forbidden**",
            "",
        ]).encode())
        return PublicationAuthorizationResult(
            backend_id,
            bundle_id,
            authorization_id,
            path,
            created,
            "atomic_model_publication_authorized",
            authorization["expires_at_utc"],
        )

    def _authorization_inputs(
        self,
        backend_id: str,
        bundle_id: str,
        authorization_id: str,
        now: Optional[datetime] = None,
        require_fresh: bool = True,
    ):
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        authorization_id = _digest(authorization_id, "authorization ID")
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        authorization, raw = _read_json(path, "publication authorization")
        _validate_authorization(authorization, authorization_id, backend_id, bundle_id)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ModelPublicationError("Publication time must include timezone.")
        current = current.astimezone(timezone.utc)
        if require_fresh and current > _parse_utc(
            authorization["expires_at_utc"], "publication authorization expiry"
        ):
            raise ModelPublicationError("Publication authorization has expired.")
        for item in authorization.get("source_artifacts") or []:
            if not isinstance(item, Mapping):
                raise ModelPublicationError("Publication authorization source is invalid.")
            source_path = Path(str(item.get("path") or ""))
            try:
                source_raw = source_path.read_bytes() if not source_path.is_symlink() else b""
            except OSError:
                source_raw = b""
            if _sha256_bytes(source_raw) != item.get("sha256"):
                raise ModelPublicationError(
                    "Publication authorization source changed: {}".format(source_path)
                )
        verification_id = authorization["verification_id"]
        backend, policy, bundle, inventory, control, current_sources = self._current_inputs(
            backend_id, bundle_id, verification_id
        )
        evidence_id = authorization["publication_evidence_id"]
        evidence_path = self.evidence_root / backend_id / bundle_id / (evidence_id + ".json")
        evidence, evidence_raw = _read_json(evidence_path, "publication preflight evidence")
        self._validate_evidence(evidence)
        if (
            _sha256_bytes(evidence_raw) != authorization["publication_evidence_sha256"]
            or evidence.get("verification_id") != verification_id
            or evidence.get("summary", {}).get("verified_manifest_sha256")
            != authorization["verified_manifest_sha256"]
            or evidence.get("summary", {}).get("verification_result_sha256")
            != authorization["verification_result_sha256"]
        ):
            raise ModelPublicationError("Publication authorization evidence binding changed.")
        verified_files = evidence.get("verified_manifest", {}).get("files")
        if not isinstance(verified_files, list) or len(verified_files) != len(inventory):
            raise ModelPublicationError("Publication verified-file set is invalid.")
        return (
            backend,
            policy,
            bundle,
            inventory,
            verified_files,
            control,
            authorization,
            raw,
            path,
        )

    def launch(
        self,
        backend_id: str,
        bundle_id: str,
        authorization_id: str,
        runner: PythonRemoteRunner,
        confirmed: bool,
        now: Optional[datetime] = None,
    ) -> PublicationLaunchResult:
        if not confirmed:
            raise ModelPublicationError(
                "Explicit --confirm-submit-model-publication is required."
            )
        current = now or datetime.now(timezone.utc)
        values = self._authorization_inputs(
            backend_id, bundle_id, authorization_id, current, require_fresh=True
        )
        (
            backend,
            policy,
            bundle,
            inventory,
            verified_files,
            verification_control,
            authorization,
            authorization_raw,
            authorization_path,
        ) = values
        if str(runner.host) != authorization["remote_host"]:
            raise ModelPublicationError("Publication runner host mismatch.")
        publication_id = _sha256_value({
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(authorization_raw),
            "backend_id": backend_id,
            "bundle_id": bundle_id,
        })
        local_dir = self.publication_root / backend_id / bundle_id / publication_id
        launch_path = local_dir / "launch.json"
        if launch_path.exists():
            launch, _ = _read_json(launch_path, "model publication launch evidence")
            if (
                launch.get("publication_id") != publication_id
                or launch.get("authorization_id") != authorization_id
            ):
                raise ModelPublicationError("Existing publication launch identity mismatch.")
            return PublicationLaunchResult(
                backend_id,
                bundle_id,
                authorization_id,
                publication_id,
                str(launch["job_id"]),
                launch_path,
                False,
                str(launch["remote_result"]["status"]),
            )
        intent_path = local_dir / "submission_intent.json"
        if intent_path.exists():
            raise ModelPublicationError(
                "Publication submission outcome is ambiguous; inspect the recorded intent "
                "and remote control path before any retry."
            )
        intent = {
            "schema_version": "1.0",
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "authorization_id": authorization_id,
            "publication_id": publication_id,
            "authorization_sha256": _sha256_bytes(authorization_raw),
            "recorded_at_utc": current.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "status": "local_submission_intent_recorded",
        }
        _write_immutable(intent_path, _pretty_json(intent))
        try:
            result = runner.run_python(
                _publication_submission_program(
                    backend_id,
                    bundle_id,
                    publication_id,
                    bundle,
                    verified_files,
                    policy,
                    authorization,
                    verification_control,
                ),
                timeout_seconds=120,
            )
        except Exception as exc:
            raise ModelPublicationError(
                "Publication submission result is ambiguous; no automatic retry is allowed: "
                "{}".format(exc)
            ) from exc
        expected_control = str(Path(policy["control_root"]) / publication_id)
        if (
            not isinstance(result, Mapping)
            or result.get("status") != "publication_submitted"
            or result.get("publication_id") != publication_id
            or not re.fullmatch(r"[0-9]+", str(result.get("job_id") or ""))
            or result.get("control_directory") != expected_control
            or result.get("publication") is not False
            or result.get("gpu_allocation") is not False
        ):
            raise ModelPublicationError("Remote publication submission result is invalid.")
        evidence = {
            "schema_version": "1.0",
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "source_mode": "explicit_authorized_atomic_model_publication_submission",
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "verification_id": authorization["verification_id"],
            "authorization_id": authorization_id,
            "publication_id": publication_id,
            "job_id": str(result["job_id"]),
            "submitted_at_utc": current.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "control_directory": expected_control,
            "staging_path": bundle["target"]["staging_path"],
            "installation_path": bundle["target"]["installation_path"],
            "expected_file_count": len(inventory),
            "expected_total_bytes": sum(item["size_bytes"] for item in inventory),
            "verified_files": verified_files,
            "verified_manifest_sha256": authorization["verified_manifest_sha256"],
            "verification_result_sha256": authorization["verification_result_sha256"],
            "remote_result": dict(result),
            "publication_started": False,
            "gpu_allocation": False,
            "inference": False,
            "training": False,
            "registry_update": False,
            "backend_activation": False,
        }
        created = _write_immutable(launch_path, _pretty_json(evidence))
        _write_immutable(local_dir / "report.md", "\n".join([
            "# GenomeAgent Atomic Model Publication Submission",
            "",
            "- Publication: `{}`".format(publication_id),
            "- Job ID: `{}`".format(result["job_id"]),
            "- Partition: `small`",
            "- Final path: `{}`".format(bundle["target"]["installation_path"]),
            "- GPU/inference/activation: **no**",
            "",
        ]).encode())
        return PublicationLaunchResult(
            backend_id,
            bundle_id,
            authorization_id,
            publication_id,
            str(result["job_id"]),
            launch_path,
            created,
            str(result["status"]),
        )

    def status(
        self,
        backend_id: str,
        bundle_id: str,
        authorization_id: str,
        runner: PythonRemoteRunner,
        stamp: Optional[str] = None,
    ) -> PublicationStatusResult:
        values = self._authorization_inputs(
            backend_id, bundle_id, authorization_id, require_fresh=False
        )
        (
            backend,
            policy,
            bundle,
            inventory,
            verified_files,
            verification_control,
            authorization,
            authorization_raw,
            authorization_path,
        ) = values
        if str(runner.host) != authorization["remote_host"]:
            raise ModelPublicationError("Publication status host mismatch.")
        publication_id = _sha256_value({
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(authorization_raw),
            "backend_id": backend_id,
            "bundle_id": bundle_id,
        })
        local_dir = self.publication_root / backend_id / bundle_id / publication_id
        launch_path = local_dir / "launch.json"
        launch, _ = _read_json(launch_path, "model publication launch evidence")
        if launch.get("publication_id") != publication_id:
            raise ModelPublicationError("Publication launch identity mismatch.")
        try:
            remote = runner.run_python(
                _publication_status_program(
                    backend_id,
                    bundle_id,
                    publication_id,
                    launch["control_directory"],
                    launch["staging_path"],
                    launch["installation_path"],
                    policy["installed_manifest_filename"],
                    policy["download_cache_directory"],
                    policy["limits"]["maximum_inventory_entries"],
                    policy["limits"]["maximum_manifest_bytes"],
                    policy["limits"]["status_log_tail_bytes"],
                ),
                timeout_seconds=60,
            )
        except Exception as exc:
            raise ModelPublicationError(
                "Read-only publication status failed: {}".format(exc)
            ) from exc
        if (
            not isinstance(remote, Mapping)
            or remote.get("backend_id") != backend_id
            or remote.get("bundle_id") != bundle_id
            or remote.get("publication_id") != publication_id
        ):
            raise ModelPublicationError("Remote publication status identity is invalid.")
        _false_fields(remote.get("safety"), OBSERVATION_FALSE_FIELDS, "Status")
        result = remote.get("result")
        status = str(result.get("status") or "") if isinstance(result, Mapping) else ""
        if status == "published_ready_for_installed_model_evidence":
            expected_sizes = [
                {"path": item["path"], "size_bytes": item["size_bytes"]}
                for item in verified_files
            ]
            paths = remote.get("paths")
            installed = remote.get("installed")
            manifest_record = installed.get("manifest") if isinstance(installed, Mapping) else None
            manifest = manifest_record.get("value") if isinstance(manifest_record, Mapping) else None
            manifest_files = manifest.get("files") if isinstance(manifest, Mapping) else None
            contract_ok = (
                isinstance(paths, Mapping)
                and paths.get("staging", {}).get("lexists") is False
                and paths.get("installation", {}).get("exists") is True
                and paths.get("installation", {}).get("is_directory") is True
                and paths.get("installation", {}).get("is_symlink") is False
                and isinstance(installed, Mapping)
                and installed.get("files") == expected_sizes
                and installed.get("file_count") == len(expected_sizes)
                and installed.get("total_bytes")
                == sum(item["size_bytes"] for item in verified_files)
                and installed.get("symlinks") == []
                and installed.get("special_entries") == []
                and installed.get("download_cache_present") is False
                and installed.get("walk_error") is None
                and isinstance(manifest, Mapping)
                and manifest_record.get("sha256") == result.get("installed_manifest_sha256")
                and manifest.get("manifest_type") == "genomeagent_installed_model"
                and manifest.get("backend_id") == backend_id
                and manifest.get("bundle_id") == bundle_id
                and manifest.get("verification_id") == authorization["verification_id"]
                and manifest.get("publication_id") == publication_id
                and manifest.get("installation_path") == launch["installation_path"]
                and manifest.get("source_verified_manifest_sha256")
                == launch["verified_manifest_sha256"]
                and manifest.get("source_verification_result_sha256")
                == launch["verification_result_sha256"]
                and manifest_files == verified_files
                and manifest.get("download_cache_removed") is True
                and manifest.get("atomic_directory_rename") is True
                and manifest.get("backend_activated") is False
                and result.get("publication") is True
                and result.get("gpu_allocation") is False
                and result.get("inference") is False
                and result.get("backend_activation") is False
            )
            if not contract_ok:
                status = "publication_result_contract_mismatch"
        if status not in TERMINAL_STATES and status != "publication_result_contract_mismatch":
            worker = remote.get("worker_status")
            if isinstance(worker, Mapping):
                candidate = str(worker.get("status") or "")
                if candidate:
                    status = candidate
        if not status:
            scheduler = remote.get("scheduler")
            queue = scheduler.get("squeue") if isinstance(scheduler, Mapping) else None
            queue_text = str(queue.get("stdout") or "") if isinstance(queue, Mapping) else ""
            if "|RUNNING|" in queue_text:
                status = "publication_rehashing"
            elif "|PENDING|" in queue_text:
                status = "publication_queued"
            else:
                accounting = scheduler.get("sacct") if isinstance(scheduler, Mapping) else None
                accounting_text = str(accounting.get("stdout") or "") if isinstance(
                    accounting, Mapping
                ) else ""
                if re.search(
                    r"\|(FAILED|CANCELLED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL)(?:\||\s)",
                    accounting_text,
                ):
                    status = "publication_job_failed"
                elif "|COMPLETED|" in accounting_text:
                    status = "scheduler_completed_publication_result_missing"
                else:
                    status = "publication_state_unresolved"
        now = datetime.now(timezone.utc)
        observation_id = stamp or now.strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", observation_id):
            raise ModelPublicationError("Invalid publication status observation ID.")
        path = local_dir / "status" / (observation_id + ".json")
        evidence = {
            "schema_version": "1.0",
            "policy_version": MODEL_PUBLICATION_POLICY_VERSION,
            "source_mode": "bounded_read_only_model_publication_status_observation",
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "authorization_id": authorization_id,
            "publication_id": publication_id,
            "observation_id": observation_id,
            "observed_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "job_id": launch.get("job_id"),
            "status": status,
            "remote_observation": dict(remote),
            "remote_writes": False,
            "model_file_contents_read": False,
            "large_file_hashing": False,
            "job_submission": False,
            "staging_mutation": False,
            "publication": False,
            "gpu_allocation": False,
            "inference": False,
            "training": False,
            "registry_update": False,
            "backend_activation": False,
        }
        _write_immutable(path, _pretty_json(evidence))
        return PublicationStatusResult(
            backend_id,
            bundle_id,
            publication_id,
            observation_id,
            path,
            status,
            str(launch.get("job_id")) if launch.get("job_id") else None,
        )


def _publication_submission_program(
    backend_id: str,
    bundle_id: str,
    publication_id: str,
    bundle: Mapping[str, Any],
    verified_files: list[dict[str, Any]],
    policy: Mapping[str, Any],
    authorization: Mapping[str, Any],
    verification_control: str,
) -> str:
    control = str(Path(policy["control_root"]) / publication_id)
    config = {
        "backend_id": backend_id,
        "bundle_id": bundle_id,
        "verification_id": authorization["verification_id"],
        "authorization_id": authorization["authorization_id"],
        "publication_id": publication_id,
        "repository": bundle["repository"],
        "resolved_revision": bundle["resolved_revision"],
        "project_storage_root": bundle["target"]["project_storage_root"],
        "staging_path": bundle["target"]["staging_path"],
        "installation_path": bundle["target"]["installation_path"],
        "verification_control": verification_control,
        "verification_result_sha256": authorization["verification_result_sha256"],
        "verified_manifest_sha256": authorization["verified_manifest_sha256"],
        "control_root": policy["control_root"],
        "control_directory": control,
        "cache_name": policy["download_cache_directory"],
        "installed_manifest_filename": policy["installed_manifest_filename"],
        "maximum_entries": policy["limits"]["maximum_inventory_entries"],
        "maximum_manifest_bytes": policy["limits"]["maximum_manifest_bytes"],
        "hash_chunk_bytes": policy["limits"]["hash_chunk_bytes"],
        "worker_python": policy["worker_python"],
        "slurm": policy["slurm"],
        "verified_files": verified_files,
    }
    encoded = repr(json.dumps(config, sort_keys=True))
    program = r"""import json
import os
import re
import stat
import subprocess
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_PUBLICATION_SUBMISSION_CONFIG__)

def fail(message):
    print(json.dumps({"schema_version":"1.0","status":"submission_rejected",
        "publication_id":CONFIG["publication_id"],"error":str(message)[:2000]},
        sort_keys=True))
    raise SystemExit(2)

def confined(path_text, root_text):
    try: relative=Path(path_text).relative_to(Path(root_text))
    except ValueError: return False
    return bool(relative.parts)

def ensure_directory(path_text, root_text):
    root=Path(root_text); path=Path(path_text)
    try: relative=path.relative_to(root)
    except ValueError: fail("control directory escaped project storage")
    if root.is_symlink() or not root.is_dir():
        fail("project root is absent, symlinked or not a directory")
    current=root
    for part in relative.parts:
        current=current/part
        if os.path.lexists(str(current)):
            if current.is_symlink() or not current.is_dir():
                fail("control path contains unsafe existing component")
        else:
            current.mkdir(mode=0o700)

def chain_safe(path_text, root_text):
    root=Path(root_text); path=Path(path_text)
    try: relative=path.relative_to(root)
    except ValueError: return False
    if root.is_symlink() or not root.is_dir(): return False
    current=root
    for part in relative.parts:
        current=current/part
        if os.path.lexists(str(current)) and current.is_symlink(): return False
    return True

def read_small(path, expected_sha):
    info=os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("verification artifact is not a regular file")
    if info.st_size > int(CONFIG["maximum_manifest_bytes"]):
        fail("verification artifact exceeds size bound")
    descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
    try:
        opened=os.fstat(descriptor)
        if (opened.st_dev,opened.st_ino,opened.st_size)!=(info.st_dev,info.st_ino,info.st_size):
            fail("verification artifact changed before read")
        chunks=[]; total=0
        while True:
            block=os.read(descriptor,min(65536,int(CONFIG["maximum_manifest_bytes"])+1-total))
            if not block: break
            chunks.append(block); total+=len(block)
            if total>int(CONFIG["maximum_manifest_bytes"]): break
        raw=b"".join(chunks)
        after=os.fstat(descriptor)
    finally: os.close(descriptor)
    if len(raw)>int(CONFIG["maximum_manifest_bytes"]): fail("verification artifact too large")
    if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)!=(
        info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns
    ): fail("verification artifact changed during read")
    if __import__("hashlib").sha256(raw).hexdigest() != expected_sha:
        fail("verification artifact digest changed")
    return json.loads(raw)

root=CONFIG["project_storage_root"]
for path in (CONFIG["staging_path"],CONFIG["installation_path"],
    CONFIG["verification_control"],CONFIG["control_root"],CONFIG["control_directory"]):
    if not confined(path,root): fail("configured path escaped project storage")
if not chain_safe(CONFIG["staging_path"],root) or not chain_safe(
    str(Path(CONFIG["installation_path"]).parent),root
) or not chain_safe(CONFIG["verification_control"],root):
    fail("staging, installation or verification path contains symlink")
staging=Path(CONFIG["staging_path"]); target=Path(CONFIG["installation_path"])
if not staging.is_dir() or staging.is_symlink(): fail("staging is absent or unsafe")
if os.path.lexists(str(target)): fail("installation target already exists")
if os.lstat(staging).st_dev != os.lstat(target.parent).st_dev:
    fail("staging and target parent are not on the same filesystem")
verification=Path(CONFIG["verification_control"])
result=read_small(verification/"result.json",CONFIG["verification_result_sha256"])
manifest=read_small(verification/"manifest_candidate.json",CONFIG["verified_manifest_sha256"])
if result.get("status") != "verified_ready_for_publication_review" or result.get(
    "verification_id") != CONFIG["verification_id"]:
    fail("verification result is no longer publication-ready")
if manifest.get("verification_id") != CONFIG["verification_id"] or manifest.get(
    "publication_performed") is not False:
    fail("manifest candidate identity is invalid")
control=Path(CONFIG["control_directory"])
if os.path.lexists(str(control)):
    fail("publication control directory already exists; refusing duplicate submission")
ensure_directory(CONFIG["control_root"],root)
control.mkdir(mode=0o700)

def atomic_json(path,value):
    temporary=path.with_name(path.name+".tmp")
    with temporary.open("x",encoding="utf-8") as handle:
        json.dump(value,handle,sort_keys=True,separators=(",",":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.rename(temporary,path)

spec={key:CONFIG[key] for key in (
    "backend_id","bundle_id","verification_id","authorization_id","publication_id",
    "repository","resolved_revision","project_storage_root","staging_path",
    "installation_path","cache_name","installed_manifest_filename","maximum_entries",
    "hash_chunk_bytes","verified_files","verified_manifest_sha256",
    "verification_result_sha256")}
atomic_json(control/"publication_spec.json",spec)
worker = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import stat
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

spec_path=Path(sys.argv[1]); control=spec_path.parent
with spec_path.open("r",encoding="utf-8") as handle: spec=json.load(handle)
staging=Path(spec["staging_path"]); target=Path(spec["installation_path"])
root=Path(spec["project_storage_root"]); cache_name=spec["cache_name"]
manifest_name=spec["installed_manifest_filename"]
mutation_started=False; published=False

class IntegrityError(RuntimeError): pass

def utc(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def atomic_json(path,value):
    temporary=path.with_name(path.name+".tmp")
    with temporary.open("x",encoding="utf-8") as handle:
        json.dump(value,handle,sort_keys=True,separators=(",",":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.rename(temporary,path)

def write_status(status,**extra):
    value={"schema_version":"1.0","publication_id":spec["publication_id"],
        "status":status,"updated_at_utc":utc(),**extra}
    temporary=control/"worker_status.tmp"
    with temporary.open("w",encoding="utf-8") as handle:
        json.dump(value,handle,sort_keys=True,separators=(",",":")); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary,control/"worker_status.json")

def safe_chain(path):
    try: relative=path.relative_to(root)
    except ValueError: return False
    if root.is_symlink() or not root.is_dir(): return False
    current=root
    for part in relative.parts:
        current=current/part
        if os.path.lexists(str(current)) and current.is_symlink(): return False
    return True

def scan_model():
    entries=[]; symlinks=[]; special=[]; cache={"present":False,
        "regular_files":0,"directories":0,"bytes":0}
    for current,directories,files in os.walk(staging,followlinks=False):
        current_path=Path(current); relative_current=current_path.relative_to(staging)
        if relative_current.parts and relative_current.parts[0] == cache_name:
            cache["present"]=True; cache["directories"]+=1
        for name in list(directories):
            path=current_path/name; relative=path.relative_to(staging).as_posix()
            info=os.lstat(path)
            if stat.S_ISLNK(info.st_mode): symlinks.append(relative); directories.remove(name)
            elif not stat.S_ISDIR(info.st_mode): special.append(relative); directories.remove(name)
        for name in files:
            path=current_path/name; relative=path.relative_to(staging).as_posix()
            info=os.lstat(path)
            if stat.S_ISLNK(info.st_mode): symlinks.append(relative)
            elif not stat.S_ISREG(info.st_mode): special.append(relative)
            elif relative == cache_name or relative.startswith(cache_name+"/"):
                cache["present"]=True; cache["regular_files"]+=1; cache["bytes"]+=info.st_size
            else: entries.append({"path":relative,"size_bytes":info.st_size})
            if len(entries)+cache["regular_files"]+len(symlinks)+len(special) > int(spec["maximum_entries"]):
                raise RuntimeError("inventory_limit_exceeded")
    entries.sort(key=lambda item:item["path"])
    return entries,sorted(symlinks),sorted(special),cache

def validate_cache(path):
    if not os.path.lexists(str(path)): return
    root_info=os.lstat(path)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise IntegrityError("download cache root is unsafe")
    count=0
    for current,directories,files in os.walk(path,followlinks=False):
        current_path=Path(current)
        for name in directories:
            info=os.lstat(current_path/name); count+=1
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise IntegrityError("download cache contains unsafe directory")
        for name in files:
            info=os.lstat(current_path/name); count+=1
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise IntegrityError("download cache contains unsafe file")
        if count > int(spec["maximum_entries"]):
            raise IntegrityError("download cache exceeds bounded inventory")

try:
    write_status("publication_reverification_starting")
    if os.path.lexists(str(target)): raise RuntimeError("installation target exists")
    if not staging.is_dir() or staging.is_symlink(): raise RuntimeError("staging absent or unsafe")
    if not safe_chain(staging) or not safe_chain(target.parent):
        raise RuntimeError("publication path contains symlink")
    if os.lstat(staging).st_dev != os.lstat(target.parent).st_dev:
        raise RuntimeError("publication would cross filesystems")
    entries,symlinks,special,cache=scan_model()
    expected_sizes=[{"path":item["path"],"size_bytes":item["size_bytes"]}
        for item in spec["verified_files"]]
    errors=[]
    if symlinks: errors.append("symlinks_detected")
    if special: errors.append("special_entries_detected")
    if entries != expected_sizes: errors.append("paths_or_sizes_mismatch")
    if errors:
        atomic_json(control/"result.json",{"schema_version":"1.0",
            "publication_id":spec["publication_id"],"status":"publication_failed_integrity",
            "integrity_errors":errors,"publication":False,"gpu_allocation":False})
        write_status("publication_failed_integrity",integrity_errors=errors)
        raise SystemExit(3)
    write_status("publication_rehashing",files_total=len(expected_sizes),files_completed=0)
    for number,item in enumerate(spec["verified_files"],1):
        path=staging/item["path"]; before=os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise IntegrityError("approved file became unsafe: "+item["path"])
        descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)); digest=hashlib.sha256(); read=0
        try:
            opened=os.fstat(descriptor)
            if (opened.st_dev,opened.st_ino,opened.st_size)!=(before.st_dev,before.st_ino,before.st_size):
                raise IntegrityError("approved file changed before rehash: "+item["path"])
            while True:
                block=os.read(descriptor,int(spec["hash_chunk_bytes"]))
                if not block: break
                digest.update(block); read+=len(block)
            after=os.fstat(descriptor)
        finally: os.close(descriptor)
        if read != item["size_bytes"] or (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)!=(
            before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns
        ) or digest.hexdigest()!=item["local_sha256"]:
            raise IntegrityError("approved file failed publication rehash: "+item["path"])
        write_status("publication_rehashing",files_total=len(expected_sizes),
            files_completed=number,last_completed_path=item["path"])
    if os.path.lexists(str(target)): raise RuntimeError("installation target appeared")
    cache_path=staging/cache_name; validate_cache(cache_path)
    if os.path.lexists(str(cache_path)):
        mutation_started=True; shutil.rmtree(cache_path)
    installed_at=utc()
    installed_manifest={"schema_version":"1.0","manifest_type":"genomeagent_installed_model",
        "backend_id":spec["backend_id"],"bundle_id":spec["bundle_id"],
        "verification_id":spec["verification_id"],"publication_id":spec["publication_id"],
        "repository":spec["repository"],"resolved_revision":spec["resolved_revision"],
        "installation_path":str(target),"installed_at_utc":installed_at,
        "source_verified_manifest_sha256":spec["verified_manifest_sha256"],
        "source_verification_result_sha256":spec["verification_result_sha256"],
        "files":spec["verified_files"],"file_count":len(spec["verified_files"]),
        "total_bytes":sum(i["size_bytes"] for i in spec["verified_files"]),
        "download_cache_removed":True,"atomic_directory_rename":True,
        "backend_activated":False,"inference_benchmarked":False}
    manifest_path=staging/manifest_name
    mutation_started=True; atomic_json(manifest_path,installed_manifest)
    with manifest_path.open("rb") as handle: manifest_raw=handle.read()
    manifest_sha=hashlib.sha256(manifest_raw).hexdigest()
    directory_fd=os.open(staging,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)
    if os.path.lexists(str(target)): raise RuntimeError("installation target appeared before rename")
    if os.lstat(staging).st_dev != os.lstat(target.parent).st_dev:
        raise RuntimeError("filesystem changed before publication")
    os.rename(staging,target); published=True
    parent_fd=os.open(target.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
    try: os.fsync(parent_fd)
    finally: os.close(parent_fd)
    result={"schema_version":"1.0","publication_id":spec["publication_id"],
        "status":"published_ready_for_installed_model_evidence","finished_at_utc":utc(),
        "installation_path":str(target),"installed_manifest_path":str(target/manifest_name),
        "installed_manifest_sha256":manifest_sha,"published_file_count":len(spec["verified_files"]),
        "published_total_bytes":sum(i["size_bytes"] for i in spec["verified_files"]),
        "download_cache_removed":True,"atomic_directory_rename":True,
        "publication":True,"gpu_allocation":False,"inference":False,
        "training":False,"registry_update":False,"backend_activation":False}
    atomic_json(control/"result.json",result)
    write_status("published_ready_for_installed_model_evidence",
        installed_manifest_sha256=manifest_sha)
except SystemExit: raise
except BaseException as exc:
    status=("publication_failed_after_staging_mutation" if mutation_started else
        ("publication_failed_integrity" if isinstance(exc,IntegrityError) else
        "publication_failed_runtime"))
    failure={"schema_version":"1.0","publication_id":spec.get("publication_id"),
        "status":status,"failed_at_utc":utc(),"error_type":type(exc).__name__,
        "error":str(exc)[:2000],"integrity_errors":[str(exc)[:500]] if isinstance(exc,IntegrityError) else [],
        "staging_mutation_started":mutation_started,
        "publication":published,"gpu_allocation":False,"inference":False,
        "training":False,"registry_update":False,"backend_activation":False}
    try:
        if not (control/"result.json").exists(): atomic_json(control/"result.json",failure)
        write_status(status,error_type=type(exc).__name__,error=str(exc)[:2000])
    finally: traceback.print_exc()
    raise
'''
worker_path=control/"publish_worker.py"
with worker_path.open("x",encoding="utf-8") as handle:
    handle.write(worker); handle.flush(); os.fsync(handle.fileno())
os.chmod(worker_path,stat.S_IRUSR|stat.S_IWUSR)
job_path=control/"publish_job.sh"
job_text="#!/bin/bash\nset -euo pipefail\nexec "+CONFIG["worker_python"]+" "+str(worker_path)+" "+str(control/"publication_spec.json")+"\n"
with job_path.open("x",encoding="utf-8") as handle:
    handle.write(job_text); handle.flush(); os.fsync(handle.fileno())
os.chmod(job_path,stat.S_IRUSR|stat.S_IWUSR|stat.S_IXUSR)
atomic_json(control/"submission_intent.json",{"schema_version":"1.0",
    "publication_id":CONFIG["publication_id"],"status":"submission_intent_recorded",
    "job_script":str(job_path),"publication":False,"gpu_allocation":False})
slurm=CONFIG["slurm"]
command=["sbatch","--parsable","--job-name="+slurm["job_name"],
    "--account="+slurm["account"],"--partition="+slurm["partition"],
    "--time="+slurm["time_limit"],"--nodes="+str(slurm["nodes"]),
    "--ntasks="+str(slurm["ntasks"]),"--cpus-per-task="+str(slurm["cpus_per_task"]),
    "--mem="+slurm["memory"],"--output="+str(control/"slurm-%j.out"),
    "--error="+str(control/"slurm-%j.err"),str(job_path)]
completed=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,
    text=True,timeout=30,check=False)
if completed.returncode != 0:
    atomic_json(control/"submission_failure.json",{"schema_version":"1.0",
        "publication_id":CONFIG["publication_id"],"returncode":completed.returncode,
        "stdout":completed.stdout[:2000],"stderr":completed.stderr[:2000]})
    fail("sbatch rejected publication job")
job_id=completed.stdout.strip().split(";",1)[0]
if not re.fullmatch(r"[0-9]+",job_id): fail("sbatch returned invalid job ID")
atomic_json(control/"submission.json",{"schema_version":"1.0",
    "publication_id":CONFIG["publication_id"],"status":"publication_submitted",
    "job_id":job_id,"publication":False,"gpu_allocation":False})
print(json.dumps({"schema_version":"1.0","status":"publication_submitted",
    "publication_id":CONFIG["publication_id"],"job_id":job_id,
    "control_directory":str(control),"publication":False,"gpu_allocation":False},
    sort_keys=True,separators=(",",":")))
"""
    return program.replace("__GENOMEAGENT_PUBLICATION_SUBMISSION_CONFIG__", encoded)


def _publication_status_program(
    backend_id: str,
    bundle_id: str,
    publication_id: str,
    control_directory: str,
    staging_path: str,
    installation_path: str,
    manifest_name: str,
    cache_name: str,
    maximum_entries: int,
    maximum_manifest_bytes: int,
    tail_bytes: int,
) -> str:
    config = {
        "backend_id": backend_id,
        "bundle_id": bundle_id,
        "publication_id": publication_id,
        "control_directory": control_directory,
        "staging_path": staging_path,
        "installation_path": installation_path,
        "manifest_name": manifest_name,
        "cache_name": cache_name,
        "maximum_entries": maximum_entries,
        "maximum_manifest_bytes": maximum_manifest_bytes,
        "tail_bytes": tail_bytes,
    }
    encoded = repr(json.dumps(config, sort_keys=True))
    program = r'''import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

CONFIG=json.loads(__GENOMEAGENT_PUBLICATION_STATUS_CONFIG__)

def read_json(path,limit):
    try:
        info=os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size>limit:
            return None
        descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
        try:
            opened=os.fstat(descriptor)
            if (opened.st_dev,opened.st_ino,opened.st_size)!=(info.st_dev,info.st_ino,info.st_size):
                return None
            chunks=[]; total=0
            while True:
                block=os.read(descriptor,min(65536,limit+1-total))
                if not block: break
                chunks.append(block); total+=len(block)
                if total>limit: break
            raw=b"".join(chunks); after=os.fstat(descriptor)
        finally: os.close(descriptor)
        if len(raw)>limit or (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)!=(
            info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns
        ): return None
        value=json.loads(raw)
        return {"value":value,"sha256":hashlib.sha256(raw).hexdigest(),"size_bytes":len(raw)}
    except BaseException: return None

def tail(path):
    try:
        info=os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode): return ""
        with open(path,"rb") as handle:
            if info.st_size>CONFIG["tail_bytes"]: handle.seek(-CONFIG["tail_bytes"],2)
            return handle.read(CONFIG["tail_bytes"]).decode("utf-8","replace")
    except OSError: return ""

def scheduler(command):
    try:
        completed=subprocess.run(command,stdin=subprocess.DEVNULL,capture_output=True,
            text=True,timeout=15,check=False)
        return {"returncode":completed.returncode,"stdout":completed.stdout[:8000],
            "stderr":completed.stderr[:8000],"timed_out":False}
    except subprocess.TimeoutExpired as exc:
        return {"returncode":None,"stdout":"","stderr":str(exc)[:1000],"timed_out":True}

def path_state(path):
    try: info=os.lstat(path)
    except FileNotFoundError:
        return {"path":str(path),"lexists":False,"exists":False,"is_symlink":False,
            "is_directory":False}
    return {"path":str(path),"lexists":True,"exists":path.exists(),
        "is_symlink":stat.S_ISLNK(info.st_mode),"is_directory":stat.S_ISDIR(info.st_mode)}

control=Path(CONFIG["control_directory"]); staging=Path(CONFIG["staging_path"])
target=Path(CONFIG["installation_path"]); small=int(CONFIG["maximum_manifest_bytes"])
submission_record=read_json(control/"submission.json",small)
submission=submission_record["value"] if submission_record else None
worker_record=read_json(control/"worker_status.json",small)
worker=worker_record["value"] if worker_record else None
result_record=read_json(control/"result.json",small)
result=result_record["value"] if result_record else None
job_id=str((submission or {}).get("job_id") or "")
squeue=scheduler(["squeue","-h","-j",job_id,"-o","%i|%T|%M|%l|%R"]) if job_id else None
sacct=scheduler(["sacct","-X","-j",job_id,"-n","-P",
    "--format=JobIDRaw,State,Elapsed,Timelimit,ExitCode"]) if job_id else None
files=[]; symlinks=[]; special=[]; cache_present=False; walk_error=None
manifest_record=None
if target.is_dir() and not target.is_symlink():
    try:
        for current,directories,names in os.walk(target,followlinks=False):
            current_path=Path(current)
            for name in list(directories):
                path=current_path/name; relative=path.relative_to(target).as_posix(); info=os.lstat(path)
                if relative==CONFIG["cache_name"] or relative.startswith(CONFIG["cache_name"]+"/"):
                    cache_present=True
                if stat.S_ISLNK(info.st_mode): symlinks.append(relative); directories.remove(name)
                elif not stat.S_ISDIR(info.st_mode): special.append(relative); directories.remove(name)
            for name in names:
                path=current_path/name; relative=path.relative_to(target).as_posix(); info=os.lstat(path)
                if relative==CONFIG["manifest_name"]:
                    manifest_record=read_json(path,small)
                elif stat.S_ISLNK(info.st_mode): symlinks.append(relative)
                elif not stat.S_ISREG(info.st_mode): special.append(relative)
                elif relative==CONFIG["cache_name"] or relative.startswith(CONFIG["cache_name"]+"/"):
                    cache_present=True
                else: files.append({"path":relative,"size_bytes":info.st_size})
                if len(files)+len(symlinks)+len(special)>int(CONFIG["maximum_entries"]):
                    raise RuntimeError("installed_inventory_limit_exceeded")
    except BaseException as exc: walk_error=type(exc).__name__+":"+str(exc)[:500]
files.sort(key=lambda item:item["path"])
observation={"schema_version":"1.0","observation_type":"model_publication_status",
    "backend_id":CONFIG["backend_id"],"bundle_id":CONFIG["bundle_id"],
    "publication_id":CONFIG["publication_id"],"control_directory":str(control),
    "submission":submission,"worker_status":worker,"result":result,
    "result_record_sha256":result_record["sha256"] if result_record else None,
    "paths":{"staging":path_state(staging),"installation":path_state(target)},
    "installed":{"files":files,"file_count":len(files),
        "total_bytes":sum(i["size_bytes"] for i in files),"symlinks":sorted(symlinks),
        "special_entries":sorted(special),"download_cache_present":cache_present,
        "walk_error":walk_error,"manifest":manifest_record},
    "scheduler":{"squeue":squeue,"sacct":sacct},
    "stdout_tail":tail(control/("slurm-"+job_id+".out")) if job_id else "",
    "stderr_tail":tail(control/("slurm-"+job_id+".err")) if job_id else "",
    "safety":{"remote_writes":False,"model_file_contents_read":False,
        "large_file_hashing":False,"job_submission":False,"staging_mutation":False,
        "cache_removal":False,"manifest_write":False,"publication":False,
        "gpu_allocation":False,"inference":False,"training":False,
        "registry_update":False,"backend_activation":False}}
print(json.dumps(observation,sort_keys=True,separators=(",",":")))
'''
    return program.replace("__GENOMEAGENT_PUBLICATION_STATUS_CONFIG__", encoded)
