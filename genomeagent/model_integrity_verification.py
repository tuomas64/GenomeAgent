"""Staged model inventory evidence and controlled SHA-256 verification.

This module deliberately stops before publication.  It first observes only file
metadata, then permits one explicitly authorized serial Slurm job to read and hash
the exact staged model files while writing results only below a confined control
directory.
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
from genomeagent.model_acquisition_download import (
    BUNDLE_ARTIFACT_NAMES,
    _bundle_sources,
    _load_bundle,
)


MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION = "1.0"
TERMINAL_VERIFICATION_STATES = {
    "verified_ready_for_publication_review",
    "verification_failed_integrity",
    "verification_failed_runtime",
}
ALLOWED_REMOTE_MUTATIONS = (
    "create_confined_verification_control_directory",
    "write_confined_verification_spec_worker_logs_and_results",
    "submit_one_exact_cpu_integrity_verification_job",
)
POLICY_FALSE_SAFETY_FIELDS = (
    "automatic_authorization_allowed",
    "automatic_remote_execution_allowed",
    "automatic_job_submission_allowed",
    "remote_deletion_allowed",
    "staging_mutation_allowed",
    "publication_allowed",
    "gpu_allocation_allowed",
    "inference_allowed",
    "training_allowed",
    "registry_update_allowed",
    "backend_activation_allowed",
)
OBSERVATION_FALSE_SAFETY_FIELDS = (
    "remote_writes",
    "model_file_contents_read",
    "large_file_hashing",
    "job_submission",
    "staging_mutation",
    "publication",
    "gpu_allocation",
    "inference",
    "training",
)


class ModelIntegrityVerificationError(RuntimeError):
    """Raised when an integrity-verification boundary is not satisfied."""


class PythonRemoteRunner(Protocol):
    host: str

    def run_python(self, program: str, timeout_seconds: int = 240) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class InventoryCollectionResult:
    backend_id: str
    bundle_id: str
    execution_id: str
    evidence_id: str
    evidence_path: Path
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class InventoryStateResult:
    backend_id: str
    bundle_id: str
    source_snapshots: int
    latest_evidence_id: str
    status: str
    next_safe_action: str
    blockers: tuple[str, ...]
    state_dir: Path


@dataclass(frozen=True)
class VerificationAuthorizationResult:
    backend_id: str
    bundle_id: str
    authorization_id: str
    authorization_path: Path
    created: bool
    status: str
    expires_at_utc: str


@dataclass(frozen=True)
class VerificationLaunchResult:
    backend_id: str
    bundle_id: str
    authorization_id: str
    verification_id: str
    job_id: str
    launch_path: Path
    created: bool
    remote_status: str


@dataclass(frozen=True)
class VerificationStatusResult:
    backend_id: str
    bundle_id: str
    verification_id: str
    observation_id: str
    observation_path: Path
    status: str
    job_id: Optional[str]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _digest(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise ModelIntegrityVerificationError("Invalid {}.".format(label))
    return text


def _identifier(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", text):
        raise ModelIntegrityVerificationError("Invalid {}.".format(label))
    return text


def _reviewer(value: Any) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{1,127}", text):
        raise ModelIntegrityVerificationError("Invalid reviewer identifier.")
    return text


def _parse_utc(value: Any, label: str) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ModelIntegrityVerificationError("Invalid {}.".format(label)) from exc
    return parsed


def _absolute_path(value: Any, label: str) -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if (
        not text.startswith("/")
        or ".." in path.parts
        or "\x00" in text
        or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", text)
    ):
        raise ModelIntegrityVerificationError("Unsafe or non-absolute {}.".format(label))
    return text


def _safe_relative_path(value: Any, label: str = "inventory path") -> str:
    text = str(value or "")
    path = PurePosixPath(text)
    if (
        not text
        or text.startswith("/")
        or text.endswith("/")
        or "." in path.parts
        or ".." in path.parts
        or "\x00" in text
        or not re.fullmatch(r"[A-Za-z0-9_.+/@-]+(?:/[A-Za-z0-9_.+@-]+)*", text)
    ):
        raise ModelIntegrityVerificationError("Unsafe {}.".format(label))
    return text


def _within(path: str, root: str) -> bool:
    try:
        relative = PurePosixPath(path).relative_to(PurePosixPath(root))
    except ValueError:
        return False
    return bool(relative.parts)


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise ModelIntegrityVerificationError("Refusing symlinked {}.".format(label))
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ModelIntegrityVerificationError("Missing {}: {}".format(label, path)) from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelIntegrityVerificationError("Invalid JSON in {}.".format(label)) from exc
    if not isinstance(value, dict):
        raise ModelIntegrityVerificationError("{} must be a JSON object.".format(label))
    return value, raw


def _write_immutable(path: Path, payload: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
        return True
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ModelIntegrityVerificationError(
                "Refusing to overwrite immutable artifact: {}".format(path)
            )
        return False


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "path": str(path),
        "sha256": _sha256_bytes(raw),
    }


def _false_fields(value: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise ModelIntegrityVerificationError("{} safety field set is invalid.".format(label))
    if any(value[field] is not False for field in fields):
        raise ModelIntegrityVerificationError("{} grants forbidden authority.".format(label))


def _normalized_inventory(bundle: Mapping[str, Any], cache_name: str) -> list[dict[str, Any]]:
    inventory = bundle.get("approved_source_inventory")
    if not isinstance(inventory, list) or not inventory:
        raise ModelIntegrityVerificationError("Bundle approved source inventory is empty.")
    normalized = []
    seen = set()
    total = 0
    for item in inventory:
        if not isinstance(item, Mapping):
            raise ModelIntegrityVerificationError("Bundle inventory contains a non-object.")
        path = _safe_relative_path(item.get("path"))
        if path == cache_name or path.startswith(cache_name + "/"):
            raise ModelIntegrityVerificationError(
                "Approved model inventory conflicts with transfer cache directory."
            )
        if path in seen:
            raise ModelIntegrityVerificationError("Bundle inventory contains duplicate paths.")
        seen.add(path)
        size = item.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ModelIntegrityVerificationError("Bundle inventory has invalid file size.")
        provider = item.get("provider_lfs_sha256")
        if provider is not None:
            provider = _digest(provider, "provider LFS SHA-256")
        normalized.append({
            "path": path,
            "size_bytes": size,
            "provider_lfs_sha256": provider,
            "provider_git_blob_id": item.get(
                "git_blob_id", item.get("provider_git_blob_id")
            ),
            "provider_xet_hash": item.get(
                "xet_hash", item.get("provider_xet_hash")
            ),
            "is_weight_file": item.get("is_weight_file") is True,
        })
        total += size
    normalized.sort(key=lambda item: item["path"])
    contract = bundle.get("integrity_contract")
    if (
        not isinstance(contract, Mapping)
        or contract.get("expected_file_count") != len(normalized)
        or contract.get("expected_total_bytes") != total
    ):
        raise ModelIntegrityVerificationError("Bundle integrity totals do not match inventory.")
    return normalized


def _validate_policy(path: Path, backend_id: str) -> tuple[dict[str, Any], bytes]:
    policy, raw = _read_json(path, "model integrity verification policy")
    if (
        policy.get("schema_version") != "1.0"
        or policy.get("policy_version") != MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION
        or policy.get("backend_id") != backend_id
    ):
        raise ModelIntegrityVerificationError("Unsupported integrity verification policy.")
    if policy.get("ssh_host") != "roihu":
        raise ModelIntegrityVerificationError("Integrity verification must use Roihu-CPU alias.")
    for field in ("remote_python", "worker_python"):
        value = str(policy.get(field) or "")
        if not re.fullmatch(r"[A-Za-z0-9_./+-]+", value):
            raise ModelIntegrityVerificationError("Unsafe {}.".format(field))
    if policy["worker_python"] != "/usr/bin/python3":
        raise ModelIntegrityVerificationError("Worker Python must be explicit system Python.")
    control_root = _absolute_path(policy.get("control_root"), "control root")
    if not control_root.startswith("/scratch/project_"):
        raise ModelIntegrityVerificationError("Control root must be in project scratch.")
    cache = str(policy.get("download_cache_directory") or "")
    if cache != ".cache":
        raise ModelIntegrityVerificationError("Only the registered transfer cache is supported.")
    limits = policy.get("limits")
    expected_limit_types = {
        "inventory_timeout_seconds": (int, 60, 600),
        "inventory_freshness_seconds": (int, 300, 3600),
        "authorization_validity_seconds": (int, 300, 900),
        "maximum_inventory_entries": (int, 100, 10000),
        "maximum_approved_files": (int, 1, 5000),
        "maximum_command_output_characters": (int, 1000, 100000),
        "hash_chunk_bytes": (int, 1048576, 67108864),
        "status_log_tail_bytes": (int, 1000, 20000),
    }
    if not isinstance(limits, Mapping) or set(limits) != set(expected_limit_types):
        raise ModelIntegrityVerificationError("Integrity limit field set is invalid.")
    for name, (kind, low, high) in expected_limit_types.items():
        value = limits[name]
        if isinstance(value, bool) or not isinstance(value, kind) or not low <= value <= high:
            raise ModelIntegrityVerificationError("Invalid integrity limit {}.".format(name))
    slurm = policy.get("slurm")
    expected_slurm = {
        "account": "project_2001113",
        "partition": "small",
        "time_limit": "02:00:00",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 1,
        "memory": "4G",
        "job_name": "GAmodelverify",
    }
    if slurm != expected_slurm:
        raise ModelIntegrityVerificationError("Unsupported Slurm integrity contract.")
    if tuple(policy.get("allowed_remote_mutations") or ()) != ALLOWED_REMOTE_MUTATIONS:
        raise ModelIntegrityVerificationError("Integrity mutation allow-list is invalid.")
    _false_fields(policy.get("safety"), POLICY_FALSE_SAFETY_FIELDS, "Policy")
    return policy, raw


def _latest_download_completion(
    execution_root: Path,
    backend_id: str,
    bundle_id: str,
    execution_id: str,
) -> tuple[dict[str, Any], bytes, Path, dict[str, Any], bytes, Path]:
    execution_id = _digest(execution_id, "download execution ID")
    root = execution_root / backend_id / bundle_id / execution_id
    launch_path = root / "launch.json"
    launch, launch_raw = _read_json(launch_path, "model download launch evidence")
    if (
        launch.get("source_mode")
        != "explicit_authorized_remote_public_model_download_launch"
        or launch.get("backend_id") != backend_id
        or launch.get("bundle_id") != bundle_id
        or launch.get("execution_id") != execution_id
        or launch.get("publication") is not False
        or launch.get("model_hashing") is not False
    ):
        raise ModelIntegrityVerificationError("Download launch evidence is invalid.")
    status_paths = sorted((root / "status").glob("*.json"))
    if not status_paths:
        raise ModelIntegrityVerificationError("No model download status evidence exists.")
    status_path = status_paths[-1]
    status, status_raw = _read_json(status_path, "model download completion evidence")
    if (
        status.get("source_mode") != "bounded_read_only_download_status_observation"
        or status.get("backend_id") != backend_id
        or status.get("bundle_id") != bundle_id
        or status.get("execution_id") != execution_id
        or status.get("status") != "download_completed_unverified"
        or status.get("publication") is not False
        or status.get("large_file_hashing") is not False
    ):
        raise ModelIntegrityVerificationError(
            "Latest download status is not completed-unverified evidence."
        )
    return launch, launch_raw, launch_path, status, status_raw, status_path


def _inventory_program(
    backend_id: str,
    bundle_id: str,
    execution_id: str,
    bundle: Mapping[str, Any],
    policy: Mapping[str, Any],
    inventory: list[dict[str, Any]],
) -> str:
    config = {
        "backend_id": backend_id,
        "bundle_id": bundle_id,
        "download_execution_id": execution_id,
        "project_storage_root": bundle["target"]["project_storage_root"],
        "staging_path": bundle["target"]["staging_path"],
        "installation_path": bundle["target"]["installation_path"],
        "control_root": policy["control_root"],
        "worker_python": policy["worker_python"],
        "cache_name": policy["download_cache_directory"],
        "maximum_entries": policy["limits"]["maximum_inventory_entries"],
        "maximum_output": policy["limits"]["maximum_command_output_characters"],
        "expected_inventory": [
            {"path": item["path"], "size_bytes": item["size_bytes"]}
            for item in inventory
        ],
        "slurm": policy["slurm"],
    }
    encoded = repr(json.dumps(config, sort_keys=True))
    program = r'''import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_INVENTORY_CONFIG__)


def bounded_run(command, timeout=15):
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False)
        limit = int(CONFIG["maximum_output"])
        return {"returncode":completed.returncode,
            "stdout":completed.stdout[:limit], "stderr":completed.stderr[:limit],
            "timed_out":False}
    except subprocess.TimeoutExpired as exc:
        return {"returncode":None, "stdout":str(exc.stdout or "")[:2000],
            "stderr":str(exc.stderr or "")[:2000], "timed_out":True}


def state(path_text):
    path = Path(path_text)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"path":path_text, "lexists":False, "exists":False,
            "is_symlink":False, "is_directory":False, "is_file":False}
    try:
        resolved = str(path.resolve(strict=True))
    except OSError:
        resolved = None
    return {"path":path_text, "lexists":True, "exists":path.exists(),
        "is_symlink":stat.S_ISLNK(info.st_mode),
        "is_directory":stat.S_ISDIR(info.st_mode),
        "is_file":stat.S_ISREG(info.st_mode), "is_file_followed":path.is_file(),
        "executable_by_access_check":os.access(path, os.X_OK),
        "resolved_path":resolved, "device":info.st_dev,
        "mode":stat.S_IMODE(info.st_mode), "size_bytes":info.st_size}


def confined(path_text, root_text):
    try:
        relative = Path(path_text).relative_to(Path(root_text))
    except ValueError:
        return False
    return bool(relative.parts)


def chain_symlinks(path_text, root_text):
    root = Path(root_text)
    path = Path(path_text)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return ["outside_project_root"]
    found = []
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            found.append(str(current))
    return found


root = CONFIG["project_storage_root"]
staging = Path(CONFIG["staging_path"])
target = Path(CONFIG["installation_path"])
cache_name = CONFIG["cache_name"]
maximum = int(CONFIG["maximum_entries"])
entries = []
cache = {"present":False, "regular_files":0, "directories":0, "bytes":0}
symlinks = []
special = []
truncated = False
walk_error = None
if not all(confined(item, root) for item in (
    str(staging), str(target), CONFIG["control_root"]
)):
    walk_error = "configured_path_outside_project_storage"
elif staging.is_dir() and not staging.is_symlink():
    try:
        for current, directories, files in os.walk(staging, followlinks=False):
            current_path = Path(current)
            relative_current = current_path.relative_to(staging)
            in_cache = bool(relative_current.parts and relative_current.parts[0] == cache_name)
            if in_cache:
                cache["present"] = True
                cache["directories"] += 1
            for name in list(directories):
                path = current_path / name
                relative = path.relative_to(staging).as_posix()
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    symlinks.append(relative)
                    directories.remove(name)
                elif not stat.S_ISDIR(info.st_mode):
                    special.append(relative)
                    directories.remove(name)
            for name in files:
                path = current_path / name
                relative = path.relative_to(staging).as_posix()
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode):
                    symlinks.append(relative)
                elif not stat.S_ISREG(info.st_mode):
                    special.append(relative)
                elif relative == cache_name or relative.startswith(cache_name + "/"):
                    cache["present"] = True
                    cache["regular_files"] += 1
                    cache["bytes"] += info.st_size
                else:
                    entries.append({"path":relative, "size_bytes":info.st_size})
                if len(entries) + cache["regular_files"] + len(symlinks) + len(special) > maximum:
                    truncated = True
                    raise RuntimeError("inventory_limit_exceeded")
    except Exception as exc:
        walk_error = type(exc).__name__ + ":" + str(exc)[:1000]

entries.sort(key=lambda item:item["path"])
symlinks.sort()
special.sort()
sbatch = shutil.which("sbatch")
squeue = shutil.which("squeue")
sacct = shutil.which("sacct")
sinfo = shutil.which("sinfo")
partition = bounded_run([sinfo, "-h", "-p", CONFIG["slurm"]["partition"],
    "-o", "%P|%a|%l|%D"]) if sinfo else None
worker = state(CONFIG["worker_python"])
result = {"schema_version":"1.0",
    "observation_type":"staged_model_inventory_preflight",
    "backend_id":CONFIG["backend_id"], "bundle_id":CONFIG["bundle_id"],
    "download_execution_id":CONFIG["download_execution_id"],
    "identity":{"hostname":os.uname().nodename, "machine":os.uname().machine,
        "python_executable":sys.executable, "python_version":sys.version.split()[0]},
    "paths":{"project_storage":state(root), "staging":state(str(staging)),
        "installation":state(str(target)), "control_root":state(CONFIG["control_root"]),
        "worker_python":worker,
        "staging_chain_symlinks":chain_symlinks(str(staging), root),
        "installation_chain_symlinks":chain_symlinks(str(target), root),
        "control_chain_symlinks":chain_symlinks(CONFIG["control_root"], root)},
    "inventory":{"files":entries, "file_count":len(entries),
        "total_bytes":sum(item["size_bytes"] for item in entries),
        "symlinks":symlinks, "special_entries":special, "truncated":truncated,
        "walk_error":walk_error, "download_cache":cache},
    "scheduler":{"sbatch_path":sbatch, "squeue_path":squeue,
        "sacct_path":sacct, "sinfo_path":sinfo,
        "expected_partition":CONFIG["slurm"]["partition"],
        "partition_query":partition},
    "safety":{"remote_writes":False, "model_file_contents_read":False,
        "large_file_hashing":False, "job_submission":False,
        "staging_mutation":False, "publication":False, "gpu_allocation":False,
        "inference":False, "training":False}}
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
'''
    return program.replace("__GENOMEAGENT_INVENTORY_CONFIG__", encoded)


def _partition_is_ready(value: Any, expected: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("returncode") != 0 or value.get("timed_out") is not False:
        return False
    for line in str(value.get("stdout") or "").splitlines():
        fields = line.split("|")
        if len(fields) >= 2 and fields[0].rstrip("*") == expected and fields[1] == "up":
            return True
    return False


def _evaluate_inventory(
    observation: Mapping[str, Any],
    inventory: list[dict[str, Any]],
    policy: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> tuple[str, str, list[str], dict[str, Any]]:
    blockers = []
    if observation.get("observation_type") != "staged_model_inventory_preflight":
        raise ModelIntegrityVerificationError("Remote inventory observation type is invalid.")
    _false_fields(
        observation.get("safety"), OBSERVATION_FALSE_SAFETY_FIELDS, "Inventory observation"
    )
    paths = observation.get("paths")
    remote_inventory = observation.get("inventory")
    scheduler = observation.get("scheduler")
    if not isinstance(paths, Mapping) or not isinstance(remote_inventory, Mapping):
        raise ModelIntegrityVerificationError("Remote inventory observation is incomplete.")
    project = paths.get("project_storage")
    staging = paths.get("staging")
    installation = paths.get("installation")
    worker = paths.get("worker_python")
    if paths.get("staging_chain_symlinks") != []:
        blockers.append("staging_parent_symlink_detected")
    if paths.get("installation_chain_symlinks") != []:
        blockers.append("installation_parent_symlink_detected")
    if paths.get("control_chain_symlinks") != []:
        blockers.append("verification_control_parent_symlink_detected")
    if not isinstance(project, Mapping) or not (
        project.get("path") == bundle["target"]["project_storage_root"]
        and
        project.get("exists") is True
        and project.get("is_directory") is True
        and project.get("is_symlink") is False
    ):
        blockers.append("project_storage_unverified")
    if not isinstance(staging, Mapping) or not (
        staging.get("path") == bundle["target"]["staging_path"]
        and
        staging.get("exists") is True
        and staging.get("is_directory") is True
        and staging.get("is_symlink") is False
    ):
        blockers.append("staging_directory_unverified")
    if not isinstance(installation, Mapping) or not (
        installation.get("path") == bundle["target"]["installation_path"]
        and installation.get("lexists") is False
    ):
        blockers.append("installation_target_not_absent")
    if not isinstance(worker, Mapping) or not (
        worker.get("exists") is True
        and worker.get("is_file_followed") is True
        and worker.get("executable_by_access_check") is True
        and str(worker.get("resolved_path") or "").startswith("/usr/bin/python3")
    ):
        blockers.append("worker_python_unverified")
    if remote_inventory.get("truncated") is not False:
        blockers.append("inventory_truncated")
    if remote_inventory.get("walk_error") is not None:
        blockers.append("inventory_walk_failed")
    if remote_inventory.get("symlinks") != []:
        blockers.append("staging_symlinks_detected")
    if remote_inventory.get("special_entries") != []:
        blockers.append("staging_special_entries_detected")
    expected = [
        {"path": item["path"], "size_bytes": item["size_bytes"]}
        for item in inventory
    ]
    observed = remote_inventory.get("files")
    if observed != expected:
        blockers.append("staged_paths_or_sizes_mismatch")
    if remote_inventory.get("file_count") != len(expected):
        blockers.append("staged_file_count_mismatch")
    expected_bytes = sum(item["size_bytes"] for item in inventory)
    if remote_inventory.get("total_bytes") != expected_bytes:
        blockers.append("staged_total_bytes_mismatch")
    if not isinstance(scheduler, Mapping) or not all(
        str(scheduler.get(name) or "").startswith("/")
        for name in ("sbatch_path", "squeue_path", "sacct_path", "sinfo_path")
    ):
        blockers.append("slurm_commands_unverified")
    elif not _partition_is_ready(
        scheduler.get("partition_query"), policy["slurm"]["partition"]
    ):
        blockers.append("cpu_partition_unverified")
    identity = observation.get("identity")
    if not isinstance(identity, Mapping) or identity.get("machine") not in {
        "x86_64", "amd64"
    }:
        blockers.append("cpu_architecture_unverified")
    elif not re.fullmatch(
        r"roihu-cpu-login[0-9]+(?:\..*)?", str(identity.get("hostname") or "")
    ):
        blockers.append("cpu_login_identity_unverified")
    if observation.get("backend_id") != bundle["backend_id"] or observation.get(
        "bundle_id"
    ) != bundle["bundle_id"]:
        raise ModelIntegrityVerificationError("Remote inventory identity mismatch.")
    cache = remote_inventory.get("download_cache")
    cache_summary = dict(cache) if isinstance(cache, Mapping) else {}
    summary = {
        "expected_files": len(expected),
        "observed_files": remote_inventory.get("file_count"),
        "expected_bytes": expected_bytes,
        "observed_bytes": remote_inventory.get("total_bytes"),
        "download_cache": cache_summary,
    }
    blockers = list(dict.fromkeys(blockers))
    if blockers:
        return (
            "staging_inventory_attention_required",
            "review_staging_inventory_and_environment_before_hashing",
            blockers,
            summary,
        )
    return (
        "ready_for_hash_verification_authorization_review",
        "review_and_issue_fresh_hash_verification_authorization",
        ["fresh_hash_verification_authorization_missing"],
        summary,
    )


def _inventory_report(evidence: Mapping[str, Any]) -> str:
    summary = evidence["inventory_summary"]
    cache = summary.get("download_cache") or {}
    blockers = evidence["readiness"]["blockers"]
    lines = [
        "# GenomeAgent Staged Model Inventory Evidence",
        "",
        "- Backend: `{}`".format(evidence["backend_id"]),
        "- Bundle: `{}`".format(evidence["bundle_id"]),
        "- Download execution: `{}`".format(evidence["download_execution_id"]),
        "- Evidence: `{}`".format(evidence["evidence_id"]),
        "- Status: **{}**".format(evidence["readiness"]["status"]),
        "- Expected/observed model files: `{}/{}`".format(
            summary["expected_files"], summary["observed_files"]
        ),
        "- Expected/observed model bytes: `{}/{}`".format(
            summary["expected_bytes"], summary["observed_bytes"]
        ),
        "- Transfer-cache files/bytes: `{}/{}`".format(
            cache.get("regular_files", 0), cache.get("bytes", 0)
        ),
        "",
        "## Blockers",
        "",
    ]
    lines.extend("- `{}`".format(item) for item in blockers)
    lines.extend([
        "",
        "## Boundary",
        "",
        "This observation read filesystem metadata only. It did not read model-file "
        "contents, hash files, submit a job, mutate staging or publish the model.",
        "",
    ])
    return "\n".join(lines)


class ModelIntegrityInventoryCollector:
    """Collect one immutable, bounded, read-only staged inventory observation."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/integrity_verification"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
        download_execution_root: Path = Path("workspace/model_acquisition_downloads"),
        evidence_root: Path = Path("workspace/model_integrity_inventory_evidence"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.bundle_root = Path(bundle_root)
        self.plan_root = Path(plan_root)
        self.download_execution_root = Path(download_execution_root)
        self.evidence_root = Path(evidence_root)

    def _inputs(self, backend_id: str, bundle_id: str, execution_id: str):
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        execution_id = _digest(execution_id, "download execution ID")
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        policy_path = self.policy_root / (backend_id + ".json")
        policy, policy_raw = _validate_policy(policy_path, backend_id)
        bundle, _, _ = _load_bundle(
            backend_id, bundle_id, self.bundle_root, self.plan_root, backend
        )
        inventory = _normalized_inventory(bundle, policy["download_cache_directory"])
        if len(inventory) > policy["limits"]["maximum_approved_files"]:
            raise ModelIntegrityVerificationError("Approved inventory exceeds policy bound.")
        launch, launch_raw, launch_path, status, status_raw, status_path = (
            _latest_download_completion(
                self.download_execution_root, backend_id, bundle_id, execution_id
            )
        )
        remote_result = launch.get("remote_result")
        if not isinstance(remote_result, Mapping) or remote_result.get(
            "staging_path"
        ) != bundle["target"]["staging_path"]:
            raise ModelIntegrityVerificationError("Download launch staging path mismatch.")
        sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "model_integrity_verification_policy"),
            *_bundle_sources(self.bundle_root, backend_id, bundle_id),
            _source(launch_path, launch_raw, "model_download_launch_evidence"),
            _source(status_path, status_raw, "model_download_completion_evidence"),
        ]
        return backend, policy, bundle, inventory, sources

    def collect(
        self,
        backend_id: str,
        bundle_id: str,
        execution_id: str,
        runner: PythonRemoteRunner,
        stamp: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> InventoryCollectionResult:
        backend, policy, bundle, inventory, sources = self._inputs(
            backend_id, bundle_id, execution_id
        )
        if str(runner.host) != policy["ssh_host"]:
            raise ModelIntegrityVerificationError("Inventory runner host is not Roihu-CPU.")
        observed = observed_at or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            raise ModelIntegrityVerificationError("Observation timestamp must include timezone.")
        observed = observed.astimezone(timezone.utc)
        evidence_id = stamp or observed.strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", evidence_id):
            raise ModelIntegrityVerificationError("Invalid inventory evidence ID.")
        try:
            observation = runner.run_python(
                _inventory_program(
                    backend_id, bundle_id, execution_id, bundle, policy, inventory
                ),
                timeout_seconds=policy["limits"]["inventory_timeout_seconds"],
            )
        except Exception as exc:
            raise ModelIntegrityVerificationError(
                "Read-only staged inventory collection failed: {}".format(exc)
            ) from exc
        if not isinstance(observation, Mapping):
            raise ModelIntegrityVerificationError("Remote inventory result is not an object.")
        if observation.get("download_execution_id") != execution_id:
            raise ModelIntegrityVerificationError(
                "Remote inventory download-execution identity mismatch."
            )
        status, next_action, blockers, summary = _evaluate_inventory(
            observation, inventory, policy, bundle
        )
        expires = observed + timedelta(
            seconds=policy["limits"]["inventory_freshness_seconds"]
        )
        evidence = {
            "schema_version": "1.0",
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
            "source_mode": "bounded_read_only_staged_model_inventory_observation",
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "download_execution_id": execution_id,
            "evidence_id": evidence_id,
            "observed_at_utc": observed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at_utc": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_artifacts": sources,
            "source_artifact_sha256": {
                "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
                for item in sources
            },
            "inventory_summary": summary,
            "observation": dict(observation),
            "readiness": {
                "status": status,
                "next_safe_action": next_action,
                "blockers": blockers,
                "automatic_execution_allowed": False,
                "automatic_job_submission_allowed": False,
                "automatic_publication_allowed": False,
            },
            "safety": {field: False for field in OBSERVATION_FALSE_SAFETY_FIELDS},
        }
        evidence_path = self.evidence_root / backend_id / bundle_id / (
            evidence_id + ".json"
        )
        _write_immutable(evidence_path, _pretty_json(evidence))
        _write_immutable(evidence_path.with_suffix(".md"), _inventory_report(evidence).encode())
        return InventoryCollectionResult(
            backend_id,
            bundle_id,
            execution_id,
            evidence_id,
            evidence_path,
            status,
            tuple(blockers),
        )


def _authorization_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "policy_version": value.get("policy_version"),
        "backend_id": value.get("backend_id"),
        "bundle_id": value.get("bundle_id"),
        "download_execution_id": value.get("download_execution_id"),
        "inventory_evidence_id": value.get("inventory_evidence_id"),
        "inventory_evidence_sha256": value.get("inventory_evidence_sha256"),
        "source_artifact_sha256": value.get("source_artifact_sha256"),
        "reviewer": value.get("reviewer"),
        "authorization_scope": value.get("authorization_scope"),
        "remote_host": value.get("remote_host"),
        "slurm": value.get("slurm"),
        "allowed_remote_mutations": value.get("allowed_remote_mutations"),
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
        "safety": value.get("safety"),
    }


def _validate_authorization(
    value: Mapping[str, Any], authorization_id: str, backend_id: str, bundle_id: str
) -> None:
    if (
        value.get("schema_version") != "1.0"
        or value.get("policy_version") != MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION
        or value.get("source_mode")
        != "explicit_researcher_model_hash_verification_authorization"
        or value.get("authorization_id") != authorization_id
        or value.get("backend_id") != backend_id
        or value.get("bundle_id") != bundle_id
        or value.get("authorization_scope")
        != "submit_one_exact_cpu_hash_verification_without_publication"
        or tuple(value.get("allowed_remote_mutations") or ())
        != ALLOWED_REMOTE_MUTATIONS
    ):
        raise ModelIntegrityVerificationError("Hash authorization identity or scope is invalid.")
    _reviewer(value.get("reviewer"))
    _digest(value.get("download_execution_id"), "download execution ID")
    _digest(value.get("inventory_evidence_sha256"), "inventory evidence digest")
    source_digests = value.get("source_artifact_sha256")
    if not isinstance(source_digests, Mapping) or not source_digests:
        raise ModelIntegrityVerificationError("Authorization source digest set is invalid.")
    for digest in source_digests.values():
        _digest(digest, "authorization source digest")
    if _sha256_value(_authorization_identity(value)) != authorization_id:
        raise ModelIntegrityVerificationError("Hash authorization content digest mismatch.")
    if _sha256_value(_authorization_record_identity(value)) != value.get(
        "authorization_record_sha256"
    ):
        raise ModelIntegrityVerificationError("Hash authorization record digest mismatch.")
    _parse_utc(value.get("authorized_at_utc"), "authorization time")
    _parse_utc(value.get("expires_at_utc"), "authorization expiry")
    _false_fields(value.get("safety"), POLICY_FALSE_SAFETY_FIELDS, "Authorization")


def _verification_submission_program(
    backend_id: str,
    bundle_id: str,
    verification_id: str,
    bundle: Mapping[str, Any],
    inventory: list[dict[str, Any]],
    policy: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> str:
    control = str(Path(policy["control_root"]) / verification_id)
    config = {
        "backend_id": backend_id,
        "bundle_id": bundle_id,
        "authorization_id": authorization["authorization_id"],
        "verification_id": verification_id,
        "repository": bundle["repository"],
        "resolved_revision": bundle["resolved_revision"],
        "project_storage_root": bundle["target"]["project_storage_root"],
        "staging_path": bundle["target"]["staging_path"],
        "installation_path": bundle["target"]["installation_path"],
        "control_root": policy["control_root"],
        "control_directory": control,
        "cache_name": policy["download_cache_directory"],
        "maximum_entries": policy["limits"]["maximum_inventory_entries"],
        "hash_chunk_bytes": policy["limits"]["hash_chunk_bytes"],
        "worker_python": policy["worker_python"],
        "slurm": policy["slurm"],
        "expected_inventory": inventory,
    }
    encoded = repr(json.dumps(config, sort_keys=True))
    program = r"""import json
import os
import re
import stat
import subprocess
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_VERIFICATION_CONFIG__)


def fail(message):
    print(json.dumps({"schema_version":"1.0", "status":"submission_rejected",
        "verification_id":CONFIG["verification_id"], "error":str(message)[:2000]},
        sort_keys=True))
    raise SystemExit(2)


def confined(path_text, root_text):
    try:
        relative = Path(path_text).relative_to(Path(root_text))
    except ValueError:
        return False
    return bool(relative.parts)


def ensure_directory(path_text, root_text):
    root = Path(root_text)
    path = Path(path_text)
    try:
        relative = path.relative_to(root)
    except ValueError:
        fail("directory creation escaped project storage")
    if root.is_symlink() or not root.is_dir():
        fail("project root is absent, symlinked or not a directory")
    current = root
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)):
            if current.is_symlink() or not current.is_dir():
                fail("existing control path component is unsafe")
        else:
            current.mkdir(mode=0o770)


def existing_chain_is_safe(path_text, root_text):
    root = Path(root_text)
    path = Path(path_text)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    if current.is_symlink() or not current.is_dir():
        return False
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            return False
    return True


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


root = CONFIG["project_storage_root"]
staging = Path(CONFIG["staging_path"])
target = Path(CONFIG["installation_path"])
control_root = CONFIG["control_root"]
control = Path(CONFIG["control_directory"])
if not all(confined(item, root) for item in (
    str(staging), str(target), control_root, str(control)
)):
    fail("configured path escaped project storage")
if not staging.is_dir() or staging.is_symlink():
    fail("staging directory is absent or symlinked")
if not existing_chain_is_safe(str(staging), root) or not existing_chain_is_safe(
    str(target.parent), root
):
    fail("staging or installation parent path contains a symlink")
if os.path.lexists(str(target)):
    fail("installation target already exists")
ensure_directory(str(Path(control_root).parent), root)
ensure_directory(control_root, root)

execution_spec = {key:value for key,value in CONFIG.items()}
if os.path.lexists(str(control)):
    if control.is_symlink() or not control.is_dir():
        fail("verification control path is unsafe")
    try:
        existing = json.loads((control / "verification_spec.json").read_text())
    except Exception:
        fail("existing verification has ambiguous submission state")
    if existing != execution_spec:
        fail("existing verification belongs to different content")
    submission_path = control / "submission.json"
    if not submission_path.is_file() or submission_path.is_symlink():
        fail("existing verification has submission intent without receipt")
    submission = json.loads(submission_path.read_text())
    print(json.dumps({"schema_version":"1.0", "status":"already_submitted",
        "verification_id":CONFIG["verification_id"],
        "job_id":submission.get("job_id"), "control_directory":str(control),
        "publication":False, "gpu_allocation":False}, sort_keys=True))
    raise SystemExit(0)

control.mkdir(mode=0o770)
atomic_json(control / "verification_spec.json", execution_spec)

worker = r'''import hashlib
import json
import os
import stat
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

spec_path = Path(sys.argv[1])
spec = json.loads(spec_path.read_text(encoding="utf-8"))
control = spec_path.parent
staging = Path(spec["staging_path"])
target = Path(spec["installation_path"])


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def existing_chain_is_safe(path_text, root_text):
    root = Path(root_text)
    path = Path(path_text)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    if current.is_symlink() or not current.is_dir():
        return False
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)) and current.is_symlink():
            return False
    return True


def write_status(status, **extra):
    atomic_json(control / "status.json", {"schema_version":"1.0",
        "verification_id":spec["verification_id"], "status":status,
        "updated_at_utc":utc(), "staging_mutation":False,
        "publication":False, "gpu_allocation":False, **extra})


def scan_inventory():
    entries = []
    symlinks = []
    special = []
    cache = {"present":False, "regular_files":0, "directories":0, "bytes":0}
    maximum = int(spec["maximum_entries"])
    cache_name = spec["cache_name"]
    for current, directories, files in os.walk(staging, followlinks=False):
        current_path = Path(current)
        relative_current = current_path.relative_to(staging)
        if relative_current.parts and relative_current.parts[0] == cache_name:
            cache["present"] = True
            cache["directories"] += 1
        for name in list(directories):
            path = current_path / name
            relative = path.relative_to(staging).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                symlinks.append(relative)
                directories.remove(name)
            elif not stat.S_ISDIR(info.st_mode):
                special.append(relative)
                directories.remove(name)
        for name in files:
            path = current_path / name
            relative = path.relative_to(staging).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                symlinks.append(relative)
            elif not stat.S_ISREG(info.st_mode):
                special.append(relative)
            elif relative == cache_name or relative.startswith(cache_name + "/"):
                cache["present"] = True
                cache["regular_files"] += 1
                cache["bytes"] += info.st_size
            else:
                entries.append({"path":relative, "size_bytes":info.st_size})
            if len(entries) + cache["regular_files"] + len(symlinks) + len(special) > maximum:
                raise RuntimeError("inventory_limit_exceeded")
    entries.sort(key=lambda item:item["path"])
    return entries, sorted(symlinks), sorted(special), cache


try:
    write_status("hash_verification_starting", started_at_utc=utc())
    if target.exists() or target.is_symlink():
        raise RuntimeError("installation target appeared before verification")
    if not staging.is_dir() or staging.is_symlink():
        raise RuntimeError("staging directory is absent or symlinked")
    if not existing_chain_is_safe(str(staging), spec["project_storage_root"]) or not (
        existing_chain_is_safe(str(target.parent), spec["project_storage_root"])
    ):
        raise RuntimeError("staging or installation parent path contains a symlink")
    entries, symlinks, special, cache = scan_inventory()
    expected_sizes = [{"path":item["path"], "size_bytes":item["size_bytes"]}
        for item in spec["expected_inventory"]]
    integrity_errors = []
    if symlinks:
        integrity_errors.append("symlinks_detected")
    if special:
        integrity_errors.append("special_entries_detected")
    if entries != expected_sizes:
        integrity_errors.append("paths_or_sizes_mismatch")
    if integrity_errors:
        result = {"schema_version":"1.0", "verification_id":spec["verification_id"],
            "status":"verification_failed_integrity", "finished_at_utc":utc(),
            "integrity_errors":integrity_errors, "observed_file_count":len(entries),
            "expected_file_count":len(expected_sizes), "download_cache":cache,
            "staging_mutation":False, "publication":False, "gpu_allocation":False}
        atomic_json(control / "result.json", result)
        write_status("verification_failed_integrity", integrity_errors=integrity_errors)
        raise SystemExit(3)
    write_status("hashing_running", files_total=len(expected_sizes), files_completed=0)
    verified = []
    provider_mismatches = []
    for number, item in enumerate(spec["expected_inventory"], 1):
        path = staging / item["path"]
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise RuntimeError("approved path became non-regular: " + item["path"])
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        digest = hashlib.sha256()
        bytes_read = 0
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev, before.st_ino, before.st_size
            ):
                raise RuntimeError("approved file changed before read: " + item["path"])
            while True:
                block = os.read(descriptor, int(spec["hash_chunk_bytes"]))
                if not block:
                    break
                digest.update(block)
                bytes_read += len(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if bytes_read != item["size_bytes"] or (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise RuntimeError("approved file changed during hashing: " + item["path"])
        local_sha256 = digest.hexdigest()
        provider = item.get("provider_lfs_sha256")
        provider_match = None if provider is None else local_sha256 == provider
        if provider_match is False:
            provider_mismatches.append(item["path"])
        verified.append({"path":item["path"], "size_bytes":item["size_bytes"],
            "local_sha256":local_sha256, "provider_lfs_sha256":provider,
            "provider_lfs_sha256_match":provider_match,
            "provider_git_blob_id":item.get("provider_git_blob_id"),
            "provider_xet_hash":item.get("provider_xet_hash")})
        write_status("hashing_running", files_total=len(expected_sizes),
            files_completed=number, last_completed_path=item["path"])
    if provider_mismatches:
        result = {"schema_version":"1.0", "verification_id":spec["verification_id"],
            "status":"verification_failed_integrity", "finished_at_utc":utc(),
            "integrity_errors":["provider_lfs_sha256_mismatch"],
            "provider_mismatch_paths":provider_mismatches,
            "staging_mutation":False, "publication":False, "gpu_allocation":False}
        atomic_json(control / "result.json", result)
        write_status("verification_failed_integrity",
            integrity_errors=result["integrity_errors"])
        raise SystemExit(4)
    manifest = {"schema_version":"1.0", "manifest_type":"verified_model_candidate",
        "backend_id":spec["backend_id"], "bundle_id":spec["bundle_id"],
        "verification_id":spec["verification_id"], "repository":spec["repository"],
        "resolved_revision":spec["resolved_revision"], "staging_path":str(staging),
        "installation_path":str(target), "files":verified,
        "file_count":len(verified), "total_bytes":sum(i["size_bytes"] for i in verified),
        "download_cache":cache, "publication_performed":False}
    atomic_json(control / "manifest_candidate.json", manifest)
    result = {"schema_version":"1.0", "verification_id":spec["verification_id"],
        "status":"verified_ready_for_publication_review", "finished_at_utc":utc(),
        "verified_file_count":len(verified), "verified_total_bytes":manifest["total_bytes"],
        "provider_lfs_files_verified":sum(
            1 for item in verified if item["provider_lfs_sha256"] is not None),
        "manifest_candidate_path":str(control / "manifest_candidate.json"),
        "download_cache":cache, "staging_mutation":False, "publication":False,
        "gpu_allocation":False}
    atomic_json(control / "result.json", result)
    write_status("verified_ready_for_publication_review",
        files_total=len(verified), files_completed=len(verified))
except SystemExit:
    raise
except BaseException as exc:
    failure = {"schema_version":"1.0", "verification_id":spec.get("verification_id"),
        "status":"verification_failed_runtime", "failed_at_utc":utc(),
        "error_type":type(exc).__name__, "error":str(exc)[:2000],
        "staging_mutation":False, "publication":False, "gpu_allocation":False}
    try:
        atomic_json(control / "result.json", failure)
        write_status("verification_failed_runtime", error_type=type(exc).__name__,
            error=str(exc)[:2000])
    finally:
        traceback.print_exc()
    raise
'''

worker_path = control / "verify_worker.py"
with worker_path.open("x", encoding="utf-8") as handle:
    handle.write(worker)
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(worker_path, stat.S_IRUSR | stat.S_IWUSR)

job_path = control / "verify_job.sh"
job_text = "#!/bin/bash\nset -euo pipefail\nexec " + CONFIG["worker_python"] + \
    " " + str(worker_path) + " " + str(control / "verification_spec.json") + "\n"
with job_path.open("x", encoding="utf-8") as handle:
    handle.write(job_text)
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(job_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

intent = {"schema_version":"1.0", "verification_id":CONFIG["verification_id"],
    "status":"submission_intent_recorded", "job_script":str(job_path),
    "publication":False, "gpu_allocation":False}
atomic_json(control / "submission_intent.json", intent)
slurm = CONFIG["slurm"]
command = ["sbatch", "--parsable", "--job-name=" + slurm["job_name"],
    "--account=" + slurm["account"], "--partition=" + slurm["partition"],
    "--time=" + slurm["time_limit"], "--nodes=" + str(slurm["nodes"]),
    "--ntasks=" + str(slurm["ntasks"]),
    "--cpus-per-task=" + str(slurm["cpus_per_task"]), "--mem=" + slurm["memory"],
    "--output=" + str(control / "slurm-%j.out"),
    "--error=" + str(control / "slurm-%j.err"), str(job_path)]
completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
    text=True, timeout=30, check=False)
if completed.returncode != 0:
    atomic_json(control / "submission_failure.json", {"schema_version":"1.0",
        "status":"submission_failed", "returncode":completed.returncode,
        "stdout":completed.stdout[:4000], "stderr":completed.stderr[:4000]})
    fail("sbatch rejected verification: " + completed.stderr[:1000])
job_id = completed.stdout.strip().split(";")[0]
if not re.fullmatch(r"[0-9]+", job_id):
    fail("sbatch returned an invalid job ID")
submission = {"schema_version":"1.0", "status":"verification_submitted",
    "verification_id":CONFIG["verification_id"], "job_id":job_id,
    "control_directory":str(control), "partition":slurm["partition"],
    "cpus_per_task":slurm["cpus_per_task"], "memory":slurm["memory"],
    "time_limit":slurm["time_limit"], "staging_mutation":False,
    "publication":False, "gpu_allocation":False}
atomic_json(control / "submission.json", submission)
print(json.dumps(submission, sort_keys=True))
"""
    return program.replace("__GENOMEAGENT_VERIFICATION_CONFIG__", encoded)


def _verification_status_program(
    backend_id: str,
    bundle_id: str,
    verification_id: str,
    control_directory: str,
    log_tail_bytes: int,
) -> str:
    config = repr(json.dumps({
        "backend_id": backend_id,
        "bundle_id": bundle_id,
        "verification_id": verification_id,
        "control_directory": control_directory,
        "log_tail_bytes": log_tail_bytes,
    }, sort_keys=True))
    program = r'''import json
import os
import re
import subprocess
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_VERIFICATION_STATUS_CONFIG__)
control = Path(CONFIG["control_directory"])


def small_json(path, limit=1048576):
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def bounded_run(command):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=15, check=False)
        return {"returncode":result.returncode, "stdout":result.stdout[:8000],
            "stderr":result.stderr[:8000], "timed_out":False}
    except subprocess.TimeoutExpired:
        return {"returncode":None, "stdout":"", "stderr":"timeout", "timed_out":True}


def tail(path):
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - int(CONFIG["log_tail_bytes"])))
            return handle.read(int(CONFIG["log_tail_bytes"])).decode(
                "utf-8", errors="replace")
    except OSError:
        return ""


submission = small_json(control / "submission.json")
worker_status = small_json(control / "status.json")
result = small_json(control / "result.json")
job_id = str(submission.get("job_id") or "") if submission else ""
squeue = None
sacct = None
if re.fullmatch(r"[0-9]+", job_id):
    squeue = bounded_run(["squeue", "-h", "-j", job_id, "-o", "%i|%T|%M|%l|%R"])
    sacct = bounded_run(["sacct", "-X", "-j", job_id, "-n", "-P",
        "--format=JobIDRaw,State,Elapsed,Timelimit,ExitCode,MaxRSS,ReqMem"])
stdout_tail = tail(control / ("slurm-" + job_id + ".out")) if job_id else ""
stderr_tail = tail(control / ("slurm-" + job_id + ".err")) if job_id else ""
observation = {"schema_version":"1.0",
    "observation_type":"model_integrity_verification_status",
    "backend_id":CONFIG["backend_id"], "bundle_id":CONFIG["bundle_id"],
    "verification_id":CONFIG["verification_id"], "control_directory":str(control),
    "control_exists":control.is_dir(), "control_is_symlink":control.is_symlink(),
    "submission":submission, "worker_status":worker_status, "result":result,
    "scheduler":{"squeue":squeue, "sacct":sacct},
    "stdout_tail":stdout_tail, "stderr_tail":stderr_tail,
    "safety":{"remote_writes":False, "model_file_contents_read":False,
        "large_file_hashing":False, "job_submission":False,
        "staging_mutation":False, "publication":False, "gpu_allocation":False,
        "inference":False, "training":False}}
print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
'''
    return program.replace("__GENOMEAGENT_VERIFICATION_STATUS_CONFIG__", config)


class ModelIntegrityVerificationCore:
    """Replay inventory evidence, authorize hashing, submit it and observe status."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/integrity_verification"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
        download_execution_root: Path = Path("workspace/model_acquisition_downloads"),
        evidence_root: Path = Path("workspace/model_integrity_inventory_evidence"),
        state_root: Path = Path("workspace/model_integrity_inventory_state"),
        authorization_root: Path = Path("workspace/model_integrity_authorizations"),
        verification_root: Path = Path("workspace/model_integrity_verifications"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.bundle_root = Path(bundle_root)
        self.plan_root = Path(plan_root)
        self.download_execution_root = Path(download_execution_root)
        self.evidence_root = Path(evidence_root)
        self.state_root = Path(state_root)
        self.authorization_root = Path(authorization_root)
        self.verification_root = Path(verification_root)

    def policy(self, backend_id: str) -> tuple[Path, dict[str, Any], bytes]:
        backend_id = _identifier(backend_id, "backend ID")
        path = self.policy_root / (backend_id + ".json")
        policy, raw = _validate_policy(path, backend_id)
        return path, policy, raw

    def _current_inputs(self, backend_id: str, bundle_id: str, execution_id: str):
        collector = ModelIntegrityInventoryCollector(
            registry=self.registry,
            policy_root=self.policy_root,
            bundle_root=self.bundle_root,
            plan_root=self.plan_root,
            download_execution_root=self.download_execution_root,
            evidence_root=self.evidence_root,
        )
        return collector._inputs(backend_id, bundle_id, execution_id)

    def _validate_evidence(self, evidence: Mapping[str, Any]) -> None:
        if (
            evidence.get("schema_version") != "1.0"
            or evidence.get("policy_version")
            != MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION
            or evidence.get("source_mode")
            != "bounded_read_only_staged_model_inventory_observation"
        ):
            raise ModelIntegrityVerificationError("Inventory evidence identity is invalid.")
        _identifier(evidence.get("backend_id"), "evidence backend ID")
        _digest(evidence.get("bundle_id"), "evidence bundle ID")
        _digest(evidence.get("download_execution_id"), "download execution ID")
        _parse_utc(evidence.get("observed_at_utc"), "evidence observation time")
        _parse_utc(evidence.get("expires_at_utc"), "evidence expiry")
        _false_fields(
            evidence.get("safety"), OBSERVATION_FALSE_SAFETY_FIELDS, "Inventory evidence"
        )
        readiness = evidence.get("readiness")
        if not isinstance(readiness, Mapping) or any(
            readiness.get(field) is not False
            for field in (
                "automatic_execution_allowed",
                "automatic_job_submission_allowed",
                "automatic_publication_allowed",
            )
        ):
            raise ModelIntegrityVerificationError("Inventory readiness grants authority.")

    def ingest(
        self,
        backend_id: str,
        bundle_id: str,
        now: Optional[datetime] = None,
    ) -> InventoryStateResult:
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        evidence_dir = self.evidence_root / backend_id / bundle_id
        paths = sorted(evidence_dir.glob("*.json"))
        if not paths:
            raise ModelIntegrityVerificationError("No inventory evidence snapshots exist.")
        snapshots = []
        for path in paths:
            value, raw = _read_json(path, "inventory evidence")
            self._validate_evidence(value)
            if value["backend_id"] != backend_id or value["bundle_id"] != bundle_id:
                raise ModelIntegrityVerificationError("Inventory evidence path identity mismatch.")
            snapshots.append((path, value, raw))
        latest_path, latest, latest_raw = snapshots[-1]
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ModelIntegrityVerificationError("Ingest time must include timezone.")
        current = current.astimezone(timezone.utc)
        inputs_current = True
        stale = []
        for item in latest.get("source_artifacts") or []:
            if not isinstance(item, Mapping):
                inputs_current = False
                stale.append("invalid_source_record")
                continue
            path = Path(str(item.get("path") or ""))
            try:
                raw = path.read_bytes() if not path.is_symlink() else b""
            except OSError:
                raw = b""
            if _sha256_bytes(raw) != item.get("sha256"):
                inputs_current = False
                stale.append(str(path))
        evidence_fresh = current <= _parse_utc(latest["expires_at_utc"], "evidence expiry")
        original = latest["readiness"]
        if not inputs_current:
            status = "stale_inventory_evidence"
            blockers = ["inventory_source_artifacts_changed"]
            next_action = "collect_and_ingest_fresh_staged_inventory"
        elif not evidence_fresh:
            status = "stale_inventory_evidence"
            blockers = ["inventory_evidence_expired"]
            next_action = "collect_and_ingest_fresh_staged_inventory"
        else:
            status = str(original["status"])
            blockers = list(original["blockers"])
            next_action = str(original["next_safe_action"])
        state = {
            "schema_version": "1.0",
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "download_execution_id": latest["download_execution_id"],
            "latest_evidence_id": latest["evidence_id"],
            "latest_source_sha256": _sha256_bytes(latest_raw),
            "latest_observed_at_utc": latest["observed_at_utc"],
            "latest_expires_at_utc": latest["expires_at_utc"],
            "source_snapshots": len(snapshots),
            "inputs_current": inputs_current,
            "evidence_fresh": evidence_fresh,
            "stale_inputs": stale,
            "status": status,
            "next_safe_action": next_action,
            "blockers": blockers,
            "inventory_summary": latest["inventory_summary"],
            "automatic_execution_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_publication_allowed": False,
        }
        readiness = {
            "schema_version": "1.0",
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "status": status,
            "next_safe_action": next_action,
            "blockers": blockers,
            "inputs_current": inputs_current,
            "evidence_fresh": evidence_fresh,
            "automatic_execution_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_publication_allowed": False,
        }
        provenance = {
            "schema_version": "1.0",
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
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
            "derivation": "deterministic_local_inventory_evidence_replay",
            "remote_access": False,
            "job_submission": False,
            "large_file_hashing": False,
            "publication": False,
        }
        state_dir = self.state_root / backend_id / bundle_id
        artifacts = {
            "current_evidence.json": _pretty_json(state),
            "readiness.json": _pretty_json(readiness),
            "provenance.json": _pretty_json(provenance),
        }
        for name, payload in artifacts.items():
            path = state_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(payload)
            os.replace(temporary, path)
        report = "\n".join([
            "# GenomeAgent Staged Model Inventory State",
            "",
            "- Backend: `{}`".format(backend_id),
            "- Bundle: `{}`".format(bundle_id),
            "- Latest evidence: `{}`".format(latest["evidence_id"]),
            "- Status: **{}**".format(status),
            "- Next safe action: `{}`".format(next_action),
            "- Automatic execution: **no**",
            "- Publication: **no**",
            "",
        ])
        (state_dir / "report.md").write_text(report)
        return InventoryStateResult(
            backend_id,
            bundle_id,
            len(snapshots),
            latest["evidence_id"],
            status,
            next_action,
            tuple(blockers),
            state_dir,
        )

    def _authorization_inputs(
        self,
        backend_id: str,
        bundle_id: str,
        execution_id: str,
        evidence_id: str,
        now: datetime,
    ):
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        execution_id = _digest(execution_id, "download execution ID")
        state_dir = self.state_root / backend_id / bundle_id
        state_path = state_dir / "current_evidence.json"
        readiness_path = state_dir / "readiness.json"
        provenance_path = state_dir / "provenance.json"
        state, state_raw = _read_json(state_path, "inventory current state")
        readiness, readiness_raw = _read_json(readiness_path, "inventory readiness")
        provenance, provenance_raw = _read_json(provenance_path, "inventory provenance")
        if (
            state.get("backend_id") != backend_id
            or state.get("bundle_id") != bundle_id
            or state.get("download_execution_id") != execution_id
            or state.get("latest_evidence_id") != evidence_id
            or state.get("status")
            != "ready_for_hash_verification_authorization_review"
            or state.get("blockers") != ["fresh_hash_verification_authorization_missing"]
            or state.get("inputs_current") is not True
            or state.get("evidence_fresh") is not True
            or readiness.get("status")
            != "ready_for_hash_verification_authorization_review"
            or readiness.get("blockers")
            != ["fresh_hash_verification_authorization_missing"]
            or readiness.get("automatic_execution_allowed") is not False
            or readiness.get("automatic_job_submission_allowed") is not False
            or readiness.get("automatic_publication_allowed") is not False
        ):
            raise ModelIntegrityVerificationError(
                "Inventory state is not hash-authorization-ready."
            )
        state_expiry = _parse_utc(state.get("latest_expires_at_utc"), "state expiry")
        if now > state_expiry:
            raise ModelIntegrityVerificationError(
                "Inventory evidence expired; collect and ingest again."
            )
        evidence_path = self.evidence_root / backend_id / bundle_id / (
            evidence_id + ".json"
        )
        evidence, evidence_raw = _read_json(evidence_path, "inventory evidence")
        self._validate_evidence(evidence)
        direct_readiness = evidence.get("readiness")
        if (
            evidence.get("download_execution_id") != execution_id
            or evidence.get("expires_at_utc") != state.get("latest_expires_at_utc")
            or _sha256_bytes(evidence_raw) != state.get("latest_source_sha256")
            or now > _parse_utc(evidence.get("expires_at_utc"), "evidence expiry")
            or not isinstance(direct_readiness, Mapping)
            or direct_readiness.get("status")
            != "ready_for_hash_verification_authorization_review"
            or direct_readiness.get("blockers")
            != ["fresh_hash_verification_authorization_missing"]
        ):
            raise ModelIntegrityVerificationError(
                "Direct inventory evidence is expired or not authorization-ready."
            )
        records = provenance.get("evidence_snapshots")
        if not isinstance(records, list) or not any(
            isinstance(item, Mapping)
            and item.get("evidence_id") == evidence_id
            and item.get("sha256") == _sha256_bytes(evidence_raw)
            for item in records
        ):
            raise ModelIntegrityVerificationError(
                "Inventory provenance does not bind direct evidence."
            )
        backend, policy, bundle, inventory, current_sources = self._current_inputs(
            backend_id, bundle_id, execution_id
        )
        current_map = {
            "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
            for item in current_sources
        }
        if current_map != evidence.get("source_artifact_sha256"):
            raise ModelIntegrityVerificationError(
                "Inventory source artifacts changed after observation."
            )
        sources = [
            *current_sources,
            _source(state_path, state_raw, "model_integrity_inventory_state"),
            _source(readiness_path, readiness_raw, "model_integrity_inventory_readiness"),
            _source(provenance_path, provenance_raw, "model_integrity_inventory_provenance"),
            _source(evidence_path, evidence_raw, "model_integrity_inventory_evidence"),
        ]
        return backend, policy, bundle, inventory, state, evidence_raw, sources

    def authorize(
        self,
        backend_id: str,
        bundle_id: str,
        execution_id: str,
        evidence_id: str,
        reviewer: str,
        confirmed: bool,
        now: Optional[datetime] = None,
    ) -> VerificationAuthorizationResult:
        if not confirmed:
            raise ModelIntegrityVerificationError(
                "Explicit --confirm-staging-hash-verification is required."
            )
        reviewer = _reviewer(reviewer)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ModelIntegrityVerificationError("Authorization time must include timezone.")
        current = current.astimezone(timezone.utc)
        backend, policy, bundle, _, state, evidence_raw, sources = (
            self._authorization_inputs(
                backend_id, bundle_id, execution_id, evidence_id, current
            )
        )
        identity = {
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "download_execution_id": execution_id,
            "inventory_evidence_id": evidence_id,
            "inventory_evidence_sha256": _sha256_bytes(evidence_raw),
            "source_artifact_sha256": {
                "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
                for item in sources
            },
            "reviewer": reviewer,
            "authorization_scope": (
                "submit_one_exact_cpu_hash_verification_without_publication"
            ),
            "remote_host": policy["ssh_host"],
            "slurm": policy["slurm"],
            "allowed_remote_mutations": list(ALLOWED_REMOTE_MUTATIONS),
        }
        authorization_id = _sha256_value(identity)
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        if path.exists():
            existing, _ = _read_json(path, "hash verification authorization")
            _validate_authorization(existing, authorization_id, backend_id, bundle_id)
            return VerificationAuthorizationResult(
                backend_id,
                bundle_id,
                authorization_id,
                path,
                False,
                "hash_verification_already_authorized",
                str(existing["expires_at_utc"]),
            )
        expiry = min(
            _parse_utc(state["latest_expires_at_utc"], "inventory expiry"),
            current
            + timedelta(seconds=policy["limits"]["authorization_validity_seconds"]),
        )
        authorization = {
            "schema_version": "1.0",
            "source_mode": "explicit_researcher_model_hash_verification_authorization",
            "authorization_id": authorization_id,
            **identity,
            "authorized_at_utc": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at_utc": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_artifacts": sources,
            "safety": {field: False for field in POLICY_FALSE_SAFETY_FIELDS},
        }
        authorization["authorization_record_sha256"] = _sha256_value(
            _authorization_record_identity(authorization)
        )
        _validate_authorization(authorization, authorization_id, backend_id, bundle_id)
        created = _write_immutable(path, _pretty_json(authorization))
        report = "\n".join([
            "# GenomeAgent Model Hash Verification Authorization",
            "",
            "- Backend: `{}`".format(backend_id),
            "- Bundle: `{}`".format(bundle_id),
            "- Authorization: `{}`".format(authorization_id),
            "- Reviewer: `{}`".format(reviewer),
            "- Expires: `{}`".format(authorization["expires_at_utc"]),
            "- Slurm: `small`, 1 CPU, 4G, 02:00:00",
            "- Scope: **read and SHA-256 staged model files only**",
            "- Staging mutation/publication/GPU/inference: **forbidden**",
            "",
        ])
        _write_immutable(path.with_suffix(".md"), report.encode())
        return VerificationAuthorizationResult(
            backend_id,
            bundle_id,
            authorization_id,
            path,
            created,
            "hash_verification_authorized",
            authorization["expires_at_utc"],
        )

    def _load_current_authorization(
        self,
        backend_id: str,
        bundle_id: str,
        authorization_id: str,
        now: datetime,
    ):
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        authorization_id = _digest(authorization_id, "authorization ID")
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        authorization, raw = _read_json(path, "hash verification authorization")
        _validate_authorization(authorization, authorization_id, backend_id, bundle_id)
        if now > _parse_utc(authorization["expires_at_utc"], "authorization expiry"):
            raise ModelIntegrityVerificationError(
                "Hash verification authorization expired; collect fresh inventory evidence."
            )
        inputs = self._authorization_inputs(
            backend_id,
            bundle_id,
            authorization["download_execution_id"],
            authorization["inventory_evidence_id"],
            now,
        )
        backend, policy, bundle, inventory, _, evidence_raw, sources = inputs
        source_map = {
            "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
            for item in sources
        }
        if (
            source_map != authorization["source_artifact_sha256"]
            or _sha256_bytes(evidence_raw) != authorization["inventory_evidence_sha256"]
            or policy["slurm"] != authorization["slurm"]
        ):
            raise ModelIntegrityVerificationError("Authorized verification inputs changed.")
        return backend, policy, bundle, inventory, authorization, raw

    def launch(
        self,
        backend_id: str,
        bundle_id: str,
        authorization_id: str,
        runner: PythonRemoteRunner,
        confirmed: bool,
        now: Optional[datetime] = None,
    ) -> VerificationLaunchResult:
        if not confirmed:
            raise ModelIntegrityVerificationError(
                "Explicit --confirm-submit-hash-verification is required."
            )
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ModelIntegrityVerificationError("Launch time must include timezone.")
        current = current.astimezone(timezone.utc)
        backend, policy, bundle, inventory, authorization, authorization_raw = (
            self._load_current_authorization(
                backend_id, bundle_id, authorization_id, current
            )
        )
        if str(runner.host) != authorization["remote_host"]:
            raise ModelIntegrityVerificationError("Verification runner host mismatch.")
        verification_id = _sha256_value({
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(authorization_raw),
            "backend_id": backend_id,
            "bundle_id": bundle_id,
        })
        local_dir = self.verification_root / backend_id / bundle_id / verification_id
        launch_path = local_dir / "launch.json"
        if launch_path.exists():
            existing, _ = _read_json(launch_path, "verification launch evidence")
            return VerificationLaunchResult(
                backend_id,
                bundle_id,
                authorization_id,
                verification_id,
                str(existing.get("job_id") or ""),
                launch_path,
                False,
                str(existing.get("remote_result", {}).get("status") or "unknown"),
            )
        try:
            result = runner.run_python(
                _verification_submission_program(
                    backend_id,
                    bundle_id,
                    verification_id,
                    bundle,
                    inventory,
                    policy,
                    authorization,
                ),
                timeout_seconds=120,
            )
        except Exception as exc:
            raise ModelIntegrityVerificationError(
                "Approved hash-verification submission failed: {}".format(exc)
            ) from exc
        if (
            not isinstance(result, Mapping)
            or result.get("verification_id") != verification_id
            or result.get("status")
            not in {"verification_submitted", "already_submitted"}
            or not re.fullmatch(r"[0-9]+", str(result.get("job_id") or ""))
            or result.get("publication") is not False
            or result.get("gpu_allocation") is not False
        ):
            raise ModelIntegrityVerificationError("Remote submission result is invalid.")
        expected_control = str(Path(policy["control_root"]) / verification_id)
        if result.get("control_directory") != expected_control:
            raise ModelIntegrityVerificationError("Verification control path mismatch.")
        evidence = {
            "schema_version": "1.0",
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
            "source_mode": "explicit_authorized_cpu_model_hash_verification_submission",
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "authorization_id": authorization_id,
            "verification_id": verification_id,
            "job_id": str(result["job_id"]),
            "submitted_at_utc": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "control_directory": expected_control,
            "expected_file_count": len(inventory),
            "expected_total_bytes": sum(item["size_bytes"] for item in inventory),
            "remote_result": dict(result),
            "staging_mutation": False,
            "publication": False,
            "gpu_allocation": False,
            "inference": False,
            "training": False,
        }
        created = _write_immutable(launch_path, _pretty_json(evidence))
        _write_immutable(local_dir / "report.md", "\n".join([
            "# GenomeAgent Model Hash Verification Submission",
            "",
            "- Verification: `{}`".format(verification_id),
            "- Job ID: `{}`".format(result["job_id"]),
            "- Partition: `small`",
            "- Publication: **no**",
            "- GPU use: **no**",
            "",
        ]).encode())
        return VerificationLaunchResult(
            backend_id,
            bundle_id,
            authorization_id,
            verification_id,
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
    ) -> VerificationStatusResult:
        backend_id = _identifier(backend_id, "backend ID")
        bundle_id = _digest(bundle_id, "bundle ID")
        authorization_id = _digest(authorization_id, "authorization ID")
        authorization_path = self.authorization_root / backend_id / (
            authorization_id + ".json"
        )
        authorization, authorization_raw = _read_json(
            authorization_path, "hash verification authorization"
        )
        _validate_authorization(authorization, authorization_id, backend_id, bundle_id)
        if str(runner.host) != authorization["remote_host"]:
            raise ModelIntegrityVerificationError("Verification status host mismatch.")
        verification_id = _sha256_value({
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(authorization_raw),
            "backend_id": backend_id,
            "bundle_id": bundle_id,
        })
        launch_path = self.verification_root / backend_id / bundle_id / verification_id / (
            "launch.json"
        )
        launch, _ = _read_json(launch_path, "verification launch evidence")
        control = str(launch.get("control_directory") or "")
        if not control or not _within(control, "/scratch/project_2001113"):
            raise ModelIntegrityVerificationError("Saved verification control path is unsafe.")
        try:
            result = runner.run_python(
                _verification_status_program(
                    backend_id, bundle_id, verification_id, control, 8000
                ),
                timeout_seconds=60,
            )
        except Exception as exc:
            raise ModelIntegrityVerificationError(
                "Read-only verification status failed: {}".format(exc)
            ) from exc
        if (
            not isinstance(result, Mapping)
            or result.get("verification_id") != verification_id
            or result.get("backend_id") != backend_id
            or result.get("bundle_id") != bundle_id
        ):
            raise ModelIntegrityVerificationError("Invalid verification status result.")
        _false_fields(
            result.get("safety"), OBSERVATION_FALSE_SAFETY_FIELDS, "Status observation"
        )
        worker = result.get("worker_status")
        remote_result = result.get("result")
        status = None
        if isinstance(remote_result, Mapping):
            status = str(remote_result.get("status") or "")
            if status == "verified_ready_for_publication_review" and (
                remote_result.get("verified_file_count")
                != launch.get("expected_file_count")
                or remote_result.get("verified_total_bytes")
                != launch.get("expected_total_bytes")
                or remote_result.get("publication") is not False
                or remote_result.get("gpu_allocation") is not False
            ):
                status = "verification_result_contract_mismatch"
        if status not in TERMINAL_VERIFICATION_STATES and isinstance(worker, Mapping):
            status = str(worker.get("status") or "")
        if not status:
            squeue = result.get("scheduler", {}).get("squeue") if isinstance(
                result.get("scheduler"), Mapping
            ) else None
            queue_text = str(squeue.get("stdout") or "") if isinstance(squeue, Mapping) else ""
            if "|RUNNING|" in queue_text:
                status = "hashing_running"
            elif "|PENDING|" in queue_text:
                status = "verification_queued"
            else:
                scheduler = result.get("scheduler")
                sacct = scheduler.get("sacct") if isinstance(scheduler, Mapping) else None
                accounting = str(sacct.get("stdout") or "") if isinstance(
                    sacct, Mapping
                ) else ""
                if re.search(
                    r"\|(FAILED|CANCELLED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL)(?:\||\s)",
                    accounting,
                ):
                    status = "verification_job_failed"
                elif "|COMPLETED|" in accounting:
                    status = "scheduler_completed_result_missing"
                else:
                    status = "verification_state_unresolved"
        now = datetime.now(timezone.utc)
        observation_id = stamp or now.strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", observation_id):
            raise ModelIntegrityVerificationError("Invalid status observation ID.")
        path = self.verification_root / backend_id / bundle_id / verification_id / (
            "status"
        ) / (observation_id + ".json")
        evidence = {
            "schema_version": "1.0",
            "policy_version": MODEL_INTEGRITY_VERIFICATION_POLICY_VERSION,
            "source_mode": "bounded_read_only_model_integrity_status_observation",
            "backend_id": backend_id,
            "bundle_id": bundle_id,
            "authorization_id": authorization_id,
            "verification_id": verification_id,
            "observation_id": observation_id,
            "observed_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "job_id": launch.get("job_id"),
            "status": status,
            "remote_observation": dict(result),
            "remote_writes": False,
            "model_file_contents_read_by_observation": False,
            "large_file_hashing_started_by_observation": False,
            "job_submission": False,
            "staging_mutation": False,
            "publication": False,
            "gpu_allocation": False,
            "inference": False,
            "training": False,
        }
        _write_immutable(path, _pretty_json(evidence))
        return VerificationStatusResult(
            backend_id,
            bundle_id,
            verification_id,
            observation_id,
            path,
            status,
            str(launch.get("job_id")) if launch.get("job_id") else None,
        )
