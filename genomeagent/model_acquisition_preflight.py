#!/usr/bin/env python3
"""Bounded read-only runtime preflight for an approved model acquisition bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from genomeagent.ai_evaluation import AIRegistry
from genomeagent.model_acquisition import _project_quota_evidence


MODEL_ACQUISITION_PREFLIGHT_POLICY_VERSION = "1.0"
PREFLIGHT_FALSE_SAFETY_FIELDS = (
    "remote_writes_allowed",
    "provider_requests_allowed",
    "model_download_allowed",
    "job_submission_allowed",
    "gpu_allocation_allowed",
    "recursive_model_scan_allowed",
    "model_file_reading_allowed",
    "large_file_hashing_allowed",
    "publication_allowed",
    "registry_update_allowed",
    "backend_activation_allowed",
)
REMOTE_FALSE_SAFETY_FIELDS = (
    "remote_writes",
    "provider_requests",
    "model_download",
    "job_submission",
    "gpu_allocation",
    "recursive_model_scan",
    "model_file_reading",
    "large_file_hashing",
    "publication",
)
BUNDLE_ARTIFACT_NAMES = (
    "acquisition_bundle.json",
    "execution_readiness.json",
    "report.md",
    "provenance.json",
)


class ModelAcquisitionPreflightError(RuntimeError):
    """Raised when runtime preflight evidence is unsafe or inconsistent."""


class PythonRemoteRunner(Protocol):
    host: str

    def run_python(self, program: str, timeout_seconds: int = 240) -> dict[str, Any]:
        """Run one read-only observation and return its JSON object."""


@dataclass(frozen=True)
class AcquisitionPreflightCollectionResult:
    backend_id: str
    bundle_id: str
    host: str
    evidence_id: str
    evidence_path: Path
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class AcquisitionPreflightIngestResult:
    backend_id: str
    bundle_id: str
    state_dir: Path
    snapshots: int
    status: str
    next_safe_action: str
    blockers: tuple[str, ...]
    artifact_paths: tuple[Path, ...]


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
        if path.is_symlink() or path.read_text(encoding="utf-8") != text:
            raise ModelAcquisitionPreflightError(
                "Immutable preflight artifact exists with different content: {}".format(
                    path
                )
            )
        return False
    _atomic_write(path, text)
    return True


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise ModelAcquisitionPreflightError("Refusing symlinked {}: {}".format(label, path))
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ModelAcquisitionPreflightError("Missing {}: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelAcquisitionPreflightError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise ModelAcquisitionPreflightError("{} must be a JSON object.".format(label))
    return value, raw


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise ModelAcquisitionPreflightError("Unsafe or empty {}.".format(label))
    return normalized


def _digest(value: Any, label: str, length: int = 64) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{{{}}}".format(length), normalized):
        raise ModelAcquisitionPreflightError("Invalid {}.".format(label))
    return normalized


def _absolute_path(value: Any, label: str) -> str:
    text = str(value or "").strip()
    path = Path(text)
    if (
        not text
        or not path.is_absolute()
        or "\x00" in text
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ModelAcquisitionPreflightError("Unsafe {}.".format(label))
    return text


def _is_within(path_text: str, root_text: str) -> bool:
    path = Path(path_text)
    root = Path(root_text)
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _false_safety(value: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(value, Mapping):
        raise ModelAcquisitionPreflightError("{} has no safety object.".format(label))
    for field in fields:
        if value.get(field) is not False:
            raise ModelAcquisitionPreflightError(
                "{} must explicitly disable {}.".format(label, field)
            )


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "path": str(path),
        "sha256": _sha256_bytes(raw),
    }


def _validate_policy(
    path: Path, expected_backend_id: Optional[str] = None
) -> tuple[dict[str, Any], bytes]:
    policy, raw = _read_json(path, "acquisition runtime policy")
    backend_id = _identifier(policy.get("backend_id"), "backend_id")
    if path.stem != backend_id or (
        expected_backend_id is not None and backend_id != expected_backend_id
    ):
        raise ModelAcquisitionPreflightError("Runtime policy backend identity mismatch.")
    if policy.get("schema_version") != "1.0" or policy.get(
        "policy_version"
    ) != MODEL_ACQUISITION_PREFLIGHT_POLICY_VERSION:
        raise ModelAcquisitionPreflightError("Unsupported runtime policy version.")
    remote_python = str(policy.get("remote_python") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_./+-]+", remote_python):
        raise ModelAcquisitionPreflightError("Unsafe remote Python executable.")
    for field, maximum in (
        ("module_initialization_candidates", 5),
        ("module_use_paths", 10),
        ("workspace_quota_command_candidates", 5),
    ):
        values = policy.get(field)
        if not isinstance(values, list) or not values or len(values) > maximum:
            raise ModelAcquisitionPreflightError("Invalid {}.".format(field))
        for value in values:
            validated = _absolute_path(value, field)
            if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", validated):
                raise ModelAcquisitionPreflightError(
                    "{} contains unsupported shell characters.".format(field)
                )
    for candidate in policy["workspace_quota_command_candidates"]:
        if not candidate.startswith("/appl/soft/") or Path(candidate).name != "csc-workspaces":
            raise ModelAcquisitionPreflightError(
                "Quota command must be an explicit CSC csc-workspaces path."
            )
    transfer = policy.get("transfer_context")
    expected_transfer = {
        "method": "huggingface_hub.snapshot_download",
        "provider": "huggingface_hub",
        "host_role": "login_node_data_transfer",
        "network_direction": "inbound_public_model_only",
        "credentials_mode": "public_repository_no_token",
        "project_data_egress_allowed": False,
    }
    if transfer != expected_transfer:
        raise ModelAcquisitionPreflightError(
            "Transfer context must register public inbound-only snapshot_download."
        )
    checks = policy.get("checks")
    if not isinstance(checks, Mapping) or not checks or not all(
        value is True for value in checks.values()
    ):
        raise ModelAcquisitionPreflightError("All bounded preflight checks are required.")
    limits = policy.get("limits")
    ranges = {
        "timeout_seconds": (5, 240),
        "maximum_command_output_characters": (1000, 50000),
        "maximum_path_components": (4, 128),
        "freshness_seconds": (300, 3600),
    }
    if not isinstance(limits, Mapping):
        raise ModelAcquisitionPreflightError("Runtime policy requires limits.")
    for field, (minimum, maximum) in ranges.items():
        value = limits.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ModelAcquisitionPreflightError("Invalid preflight limit {}.".format(field))
    _false_safety(policy.get("safety"), PREFLIGHT_FALSE_SAFETY_FIELDS, "Runtime policy")
    return policy, raw


def _load_bundle(
    backend_id: str,
    bundle_id: str,
    bundle_root: Path,
    plan_root: Path,
    backend: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    bundle_dir = bundle_root / backend_id / bundle_id
    sources = []
    values: dict[str, Any] = {}
    for name in BUNDLE_ARTIFACT_NAMES:
        path = bundle_dir / name
        if name.endswith(".json"):
            value, raw = _read_json(path, "bundle artifact")
            values[name] = value
        else:
            if path.is_symlink():
                raise ModelAcquisitionPreflightError("Refusing symlinked bundle report.")
            try:
                raw = path.read_bytes()
            except FileNotFoundError as exc:
                raise ModelAcquisitionPreflightError("Missing bundle report.") from exc
        sources.append(_source(path, raw, "model_acquisition_bundle_artifact"))
    bundle = values["acquisition_bundle.json"]
    readiness = values["execution_readiness.json"]
    if bundle.get("schema_version") != "1.0" or bundle.get("source_mode") != (
        "deterministic_local_execution_bundle_preparation"
    ):
        raise ModelAcquisitionPreflightError("Unsupported acquisition bundle.")
    if bundle.get("backend_id") != backend_id or bundle.get("bundle_id") != bundle_id:
        raise ModelAcquisitionPreflightError("Acquisition bundle identity mismatch.")
    identity = {
        "policy_version": bundle.get("policy_version"),
        "backend_id": bundle.get("backend_id"),
        "plan_id": bundle.get("plan_id"),
        "approval_id": bundle.get("approval_id"),
        "approval_sha256": bundle.get("approval_sha256"),
        "source_evidence_id": bundle.get("source_evidence_id"),
        "source_evidence_sha256": bundle.get("source_evidence_sha256"),
    }
    if _sha256_value(identity) != bundle_id:
        raise ModelAcquisitionPreflightError("Acquisition bundle content digest mismatch.")
    for field in (
        "automatic_execution_allowed",
        "automatic_remote_access_allowed",
        "automatic_model_download_allowed",
        "automatic_job_submission_allowed",
        "automatic_publication_allowed",
        "automatic_registry_update_allowed",
        "automatic_backend_activation_allowed",
    ):
        if bundle.get(field) is not False:
            raise ModelAcquisitionPreflightError("Bundle grants unsafe authority: {}.".format(field))
    expected_blockers = {
        "acquisition_runtime_unverified",
        "transfer_execution_context_unregistered",
        "remote_target_state_unverified",
        "fresh_execution_authorization_missing",
    }
    if (
        readiness.get("bundle_id") != bundle_id
        or readiness.get("status") != "environment_execution_preflight_required"
        or set(readiness.get("blockers") or []) != expected_blockers
        or readiness.get("automatic_execution_allowed") is not False
    ):
        raise ModelAcquisitionPreflightError("Bundle readiness contract is invalid.")
    target = bundle.get("target")
    if not isinstance(target, Mapping):
        raise ModelAcquisitionPreflightError("Bundle target is missing.")
    project_root = _absolute_path(target.get("project_storage_root"), "project root")
    staging = _absolute_path(target.get("staging_path"), "staging path")
    installation = _absolute_path(target.get("installation_path"), "installation path")
    manifest = _absolute_path(target.get("manifest_path"), "manifest path")
    if (
        not _is_within(staging, project_root)
        or not _is_within(installation, project_root)
        or Path(staging).parent != Path(installation).parent
        or staging == installation
        or Path(manifest).parent != Path(installation)
        or installation != str(backend.get("installation", {}).get("path") or "")
    ):
        raise ModelAcquisitionPreflightError("Bundle target layout is unsafe or inconsistent.")
    contract = bundle.get("integrity_contract")
    inventory = bundle.get("approved_source_inventory")
    if not isinstance(contract, Mapping) or not isinstance(inventory, list) or not inventory:
        raise ModelAcquisitionPreflightError("Bundle integrity inventory is missing.")
    total = 0
    seen = set()
    for item in inventory:
        if not isinstance(item, Mapping):
            raise ModelAcquisitionPreflightError("Bundle inventory contains a non-object.")
        path = str(item.get("path") or "")
        if not path or Path(path).is_absolute() or ".." in Path(path).parts or path in seen:
            raise ModelAcquisitionPreflightError("Bundle inventory contains an unsafe path.")
        seen.add(path)
        size = item.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ModelAcquisitionPreflightError("Bundle inventory contains an invalid size.")
        total += size
    if contract.get("expected_file_count") != len(inventory) or contract.get(
        "expected_total_bytes"
    ) != total:
        raise ModelAcquisitionPreflightError("Bundle inventory totals are inconsistent.")
    plan_id = _digest(bundle.get("plan_id"), "plan ID")
    plan_path = plan_root / backend_id / plan_id / "model_acquisition_plan.json"
    plan, plan_raw = _read_json(plan_path, "acquisition plan")
    if (
        plan.get("backend_id") != backend_id
        or plan.get("plan_id") != plan_id
        or plan.get("status") != "ready_for_researcher_acquisition_review"
        or plan.get("automatic_execution_allowed") is not False
    ):
        raise ModelAcquisitionPreflightError("Acquisition plan is not current and review-ready.")
    required = plan.get("storage", {}).get("required_working_free_bytes")
    if isinstance(required, bool) or not isinstance(required, int) or required <= 0:
        raise ModelAcquisitionPreflightError("Acquisition plan lacks required working storage.")
    sources.append(_source(plan_path, plan_raw, "model_acquisition_plan"))
    bundle["_preflight_required_working_free_bytes"] = required
    return bundle, readiness, sources


def _probe_program(
    backend: Mapping[str, Any], policy: Mapping[str, Any], bundle: Mapping[str, Any]
) -> str:
    target = bundle["target"]
    resources = backend["resource_request"]
    runtime = backend["runtime"]
    config = {
        "backend_id": backend["backend_id"],
        "bundle_id": bundle["bundle_id"],
        "expected_architecture": runtime.get("architecture"),
        "expected_module": runtime.get("module"),
        "expected_vllm_version": runtime.get("version"),
        "expected_partition": resources.get("partition"),
        "expected_gpu_type": resources.get("gpu_type"),
        "project_storage_root": target["project_storage_root"],
        "staging_path": target["staging_path"],
        "installation_path": target["installation_path"],
        "required_working_free_bytes": bundle["_preflight_required_working_free_bytes"],
        "module_initialization_candidates": policy["module_initialization_candidates"],
        "module_use_paths": policy["module_use_paths"],
        "workspace_quota_command_candidates": policy[
            "workspace_quota_command_candidates"
        ],
        "transfer_context": policy["transfer_context"],
        "limits": policy["limits"],
    }
    encoded = repr(json.dumps(config, sort_keys=True))
    program = r'''import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_PREFLIGHT_CONFIG__)
MAX_OUTPUT = int(CONFIG["limits"]["maximum_command_output_characters"])
MAX_COMPONENTS = int(CONFIG["limits"]["maximum_path_components"])


def bounded(value):
    text = str(value or "")
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n[truncated]"


def run(args, timeout=20, env=None):
    try:
        completed = subprocess.run(
            list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout, env=env, check=False,
        )
        return {"returncode": completed.returncode, "stdout": bounded(completed.stdout),
                "stderr": bounded(completed.stderr), "timed_out": False}
    except subprocess.TimeoutExpired as exc:
        return {"returncode": None, "stdout": bounded(exc.stdout),
                "stderr": bounded(exc.stderr), "timed_out": True}
    except OSError as exc:
        return {"returncode": None, "stdout": "", "stderr": bounded(exc),
                "timed_out": False}


def path_state(path_text):
    path = Path(path_text)
    result = {"path": path_text, "exists": path.exists(), "lexists": os.path.lexists(path_text),
              "is_symlink": path.is_symlink(), "is_directory": path.is_dir(),
              "is_file": path.is_file()}
    if os.path.lexists(path_text):
        try:
            info = os.lstat(path_text)
            result.update({"device": info.st_dev, "mode": stat.S_IMODE(info.st_mode)})
        except OSError as exc:
            result["lstat_error"] = bounded(exc)
    return result


def nearest_existing(path_text):
    current = Path(path_text)
    steps = 0
    while not os.path.lexists(str(current)) and current != current.parent:
        current = current.parent
        steps += 1
        if steps > MAX_COMPONENTS:
            return {"error": "component_limit_exceeded"}
    result = path_state(str(current))
    result["missing_components"] = steps
    return result


def existing_chain(root_text, destination_text):
    root = Path(root_text)
    destination = Path(destination_text)
    try:
        relative = destination.relative_to(root)
    except ValueError:
        return {"within_root": False, "entries": [], "symlink_detected": True}
    entries = [path_state(str(root))]
    current = root
    for index, part in enumerate(relative.parts):
        if index >= MAX_COMPONENTS:
            return {"within_root": True, "entries": entries,
                    "symlink_detected": True, "truncated": True}
        current = current / part
        if not os.path.lexists(str(current)):
            break
        entries.append(path_state(str(current)))
    return {"within_root": True, "entries": entries,
            "symlink_detected": any(item.get("is_symlink") for item in entries),
            "truncated": False}


observation = {
    "schema_version": "1.0",
    "observation_type": "model_acquisition_runtime_preflight",
    "backend_id": CONFIG["backend_id"],
    "bundle_id": CONFIG["bundle_id"],
    "identity": {"hostname": socket.gethostname(), "machine": platform.machine(),
                 "python_executable": sys.executable, "python_version": platform.python_version()},
    "runtime": {}, "scheduler": {}, "storage": {}, "target": {},
    "transfer_context": CONFIG["transfer_context"],
    "safety": {"remote_writes": False, "provider_requests": False,
               "model_download": False, "job_submission": False,
               "gpu_allocation": False, "recursive_model_scan": False,
               "model_file_reading": False, "large_file_hashing": False,
               "publication": False},
}

module_init = next((item for item in CONFIG["module_initialization_candidates"]
                    if Path(item).is_file()), None)
module_env = dict(os.environ)
module_env.update({"MODULE_INIT": module_init or "",
                   "MODULE_NAME": CONFIG["expected_module"],
                   "MODULE_USE_PATHS": ":".join(CONFIG["module_use_paths"])})
prefix = ('source "$MODULE_INIT" >/dev/null 2>&1 || exit; '
          'old_ifs=$IFS; IFS=:; for p in $MODULE_USE_PATHS; do '
          'module use "$p" >/dev/null 2>&1 || exit; done; IFS=$old_ifs; ')
runtime = {"selected_initialization": module_init,
           "expected_module": CONFIG["expected_module"]}
if module_init:
    runtime["availability"] = run(["bash", "-c", prefix +
        'module is-avail "$MODULE_NAME"'], env=module_env)
    loaded = run(["bash", "-c", prefix +
        'module load "$MODULE_NAME" >/dev/null 2>&1 && command -v python3'],
        env=module_env)
    runtime["loaded_python_query"] = loaded
    python_path = loaded.get("stdout", "").strip().splitlines()
    python_path = python_path[-1] if python_path else None
    runtime["loaded_python_executable"] = python_path
    if python_path and Path(python_path).is_file():
        code = ('import importlib.metadata as m, json, platform; '
                'from huggingface_hub import snapshot_download; '
                'print(json.dumps({"vllm_version":m.version("vllm"),'
                '"huggingface_hub_version":m.version("huggingface_hub"),'
                '"snapshot_download_callable":callable(snapshot_download),'
                '"machine":platform.machine()}))')
        metadata_env = dict(module_env)
        metadata_env["GENOMEAGENT_METADATA_CODE"] = code
        runtime["package_metadata"] = run([
            "bash", "-c", prefix +
            'module load "$MODULE_NAME" >/dev/null 2>&1 && '
            'python3 -c "$GENOMEAGENT_METADATA_CODE"'
        ], env=metadata_env)
observation["runtime"] = runtime

sbatch = shutil.which("sbatch")
sinfo = shutil.which("sinfo")
scheduler = {"sbatch_path": sbatch, "sinfo_path": sinfo,
             "expected_partition": CONFIG["expected_partition"],
             "expected_gpu_type": CONFIG["expected_gpu_type"]}
if sinfo:
    scheduler["partition_query"] = run([
        sinfo, "-h", "-p", CONFIG["expected_partition"],
        "-o", "%P|%a|%l|%D|%G|%c|%m",
    ])
observation["scheduler"] = scheduler

root = CONFIG["project_storage_root"]
storage = path_state(root)
storage["writable_by_access_check"] = os.access(root, os.W_OK | os.X_OK)
try:
    values = os.statvfs(root)
    storage["filesystem_available_bytes"] = values.f_frsize * values.f_bavail
except OSError as exc:
    storage["statvfs_error"] = bounded(exc)
command_path = None
for item in CONFIG["workspace_quota_command_candidates"]:
    if Path(item).is_file() and os.access(item, os.X_OK):
        command_path = item
        break
storage["workspace_command_path"] = command_path
if command_path:
    storage["workspace_command"] = run([command_path])
observation["storage"] = storage

staging = CONFIG["staging_path"]
installation = CONFIG["installation_path"]
observation["target"] = {
    "staging": path_state(staging),
    "installation": path_state(installation),
    "staging_nearest_existing": nearest_existing(str(Path(staging).parent)),
    "installation_nearest_existing": nearest_existing(str(Path(installation).parent)),
    "staging_chain": existing_chain(root, str(Path(staging).parent)),
    "installation_chain": existing_chain(root, str(Path(installation).parent)),
}

print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
'''
    return program.replace("__GENOMEAGENT_PREFLIGHT_CONFIG__", encoded)


def _command_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("returncode") != 0:
        return {}
    for line in reversed(str(value.get("stdout") or "").splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _partition_rows(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Mapping) or value.get("returncode") != 0:
        return []
    fields = ("partition", "availability", "time_limit", "nodes", "gres", "cpus", "memory")
    rows = []
    for line in str(value.get("stdout") or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) == len(fields):
            row = dict(zip(fields, parts))
            row["partition"] = row["partition"].rstrip("*")
            rows.append(row)
    return rows


def _interpret(
    backend: Mapping[str, Any],
    policy: Mapping[str, Any],
    bundle: Mapping[str, Any],
    observation: Mapping[str, Any],
    inputs_current: bool,
    evidence_fresh: bool,
) -> dict[str, Any]:
    checks = []

    def add(group: str, name: str, passed: bool, observed: Any, expected: Any) -> None:
        checks.append({"group": group, "check": name,
                       "status": "verified" if passed else "unknown_or_mismatch",
                       "observed": observed, "expected": expected})

    identity = observation.get("identity", {})
    runtime = observation.get("runtime", {})
    scheduler = observation.get("scheduler", {})
    storage = observation.get("storage", {})
    target = observation.get("target", {})
    metadata = _command_json(runtime.get("package_metadata", {})) if isinstance(runtime, Mapping) else {}
    resources = backend.get("resource_request", {})
    expected_partition = str(resources.get("partition") or "")
    expected_gpu = str(resources.get("gpu_type") or "").lower()
    rows = _partition_rows(scheduler.get("partition_query", {})) if isinstance(scheduler, Mapping) else []
    matching = [row for row in rows if row["partition"] == expected_partition]
    add("runtime", "remote_identity", bool(identity.get("hostname")), identity.get("hostname"), "non-empty")
    add("runtime", "architecture", identity.get("machine") == backend.get("runtime", {}).get("architecture"),
        identity.get("machine"), backend.get("runtime", {}).get("architecture"))
    availability = runtime.get("availability", {}) if isinstance(runtime, Mapping) else {}
    add("runtime", "runtime_module", isinstance(availability, Mapping) and availability.get("returncode") == 0,
        runtime.get("expected_module") if isinstance(runtime, Mapping) else None,
        backend.get("runtime", {}).get("module"))
    add("runtime", "vllm_version", metadata.get("vllm_version") == backend.get("runtime", {}).get("version"),
        metadata.get("vllm_version"), backend.get("runtime", {}).get("version"))
    add("runtime", "huggingface_hub", bool(metadata.get("huggingface_hub_version")),
        metadata.get("huggingface_hub_version"), "installed in acquisition runtime")
    add("runtime", "snapshot_download_import", metadata.get("snapshot_download_callable") is True,
        metadata.get("snapshot_download_callable"), True)
    add("runtime", "slurm_commands", bool(scheduler.get("sbatch_path") and scheduler.get("sinfo_path")),
        {"sbatch": scheduler.get("sbatch_path"), "sinfo": scheduler.get("sinfo_path")}, "available")
    add("runtime", "scheduler_partition", bool(matching), [row["partition"] for row in rows], expected_partition)
    observed_gres = [row["gres"] for row in matching]
    add("runtime", "gpu_type", bool(expected_gpu and any(expected_gpu in value.lower() for value in observed_gres)),
        observed_gres, expected_gpu)

    transfer = observation.get("transfer_context")
    host = str(identity.get("hostname") or "")
    add("transfer", "registered_transfer_context", transfer == policy.get("transfer_context"),
        transfer, policy.get("transfer_context"))
    add("transfer", "login_data_transfer_host", bool(re.search(r"-login[0-9]*$", host)), host,
        "Roihu login node used only for data movement")
    add("transfer", "public_pinned_repository", bool(bundle.get("repository") and re.fullmatch(
        r"[0-9a-f]{40}", str(bundle.get("resolved_revision") or ""))),
        {"repository": bundle.get("repository"), "revision": bundle.get("resolved_revision")},
        "public repository at immutable revision")

    root = bundle["target"]["project_storage_root"]
    add("target", "project_storage", storage.get("exists") is True and storage.get("is_directory") is True
        and storage.get("is_symlink") is False, storage.get("path"), root)
    add("target", "project_storage_access", storage.get("writable_by_access_check") is True,
        storage.get("writable_by_access_check"), True)
    workspace = storage.get("workspace_command", {})
    quota = _project_quota_evidence({"observation": {"storage": storage}}, root)
    add("target", "project_quota", quota.get("status") == "verified",
        {"command": storage.get("workspace_command_path"), "status": quota.get("status")},
        "verified csc-workspaces project row")
    required = bundle["_preflight_required_working_free_bytes"]
    add("target", "fresh_storage_capacity", isinstance(quota.get("available_bytes"), int)
        and quota.get("available_bytes") >= required,
        quota.get("available_bytes"), required)
    staging = target.get("staging", {}) if isinstance(target, Mapping) else {}
    installation = target.get("installation", {}) if isinstance(target, Mapping) else {}
    add("target", "staging_absent", staging.get("lexists") is False,
        staging, "path absent including symlinks")
    add("target", "installation_absent", installation.get("lexists") is False,
        installation, "path absent including symlinks")
    staging_chain = target.get("staging_chain", {}) if isinstance(target, Mapping) else {}
    installation_chain = target.get("installation_chain", {}) if isinstance(target, Mapping) else {}
    add("target", "path_chain_no_symlinks", staging_chain.get("within_root") is True
        and installation_chain.get("within_root") is True
        and staging_chain.get("symlink_detected") is False
        and installation_chain.get("symlink_detected") is False,
        {"staging": staging_chain, "installation": installation_chain}, "within project root without symlinks")
    staging_ancestor = target.get("staging_nearest_existing", {}) if isinstance(target, Mapping) else {}
    installation_ancestor = target.get("installation_nearest_existing", {}) if isinstance(target, Mapping) else {}
    add("target", "same_filesystem_publication", staging_ancestor.get("device") is not None
        and staging_ancestor.get("device") == installation_ancestor.get("device")
        and staging_ancestor.get("is_symlink") is False and installation_ancestor.get("is_symlink") is False,
        {"staging": staging_ancestor, "installation": installation_ancestor}, "same device")

    runtime_failed = any(item["status"] != "verified" and item["group"] == "runtime" for item in checks)
    transfer_failed = any(item["status"] != "verified" and item["group"] == "transfer" for item in checks)
    target_failed = any(item["status"] != "verified" and item["group"] == "target" for item in checks)
    blockers = []
    if runtime_failed:
        blockers.append("acquisition_runtime_unverified")
    if transfer_failed:
        blockers.append("transfer_execution_context_unregistered")
    if target_failed:
        blockers.append("remote_target_state_unverified")
    if not inputs_current:
        blockers.append("preflight_inputs_changed")
    if not evidence_fresh:
        blockers.append("preflight_evidence_expired")
    blockers.append("fresh_execution_authorization_missing")
    if not inputs_current or not evidence_fresh:
        status = "preflight_evidence_stale"
        next_action = "collect_fresh_acquisition_runtime_preflight"
    elif target_failed:
        status = "remote_target_review_required"
        next_action = "review_remote_target_or_capacity_conflict"
    elif runtime_failed or transfer_failed:
        status = "acquisition_runtime_evidence_incomplete"
        next_action = "review_unverified_acquisition_runtime_checks"
    else:
        status = "ready_for_execution_authorization_review"
        next_action = "review_and_issue_fresh_acquisition_execution_authorization"
    return {
        "schema_version": "1.0",
        "policy_version": MODEL_ACQUISITION_PREFLIGHT_POLICY_VERSION,
        "backend_id": backend["backend_id"],
        "bundle_id": bundle["bundle_id"],
        "status": status,
        "next_safe_action": next_action,
        "checks": checks,
        "quota_evidence": quota,
        "inputs_current": inputs_current,
        "evidence_fresh": evidence_fresh,
        "blockers": list(dict.fromkeys(blockers)),
        "automatic_execution_allowed": False,
        "automatic_remote_write_allowed": False,
        "automatic_model_download_allowed": False,
        "automatic_job_submission_allowed": False,
        "automatic_gpu_allocation_allowed": False,
        "automatic_publication_allowed": False,
    }


def _validate_observation(observation: Mapping[str, Any], backend_id: str, bundle_id: str) -> None:
    if observation.get("schema_version") != "1.0" or observation.get(
        "observation_type"
    ) != "model_acquisition_runtime_preflight":
        raise ModelAcquisitionPreflightError("Remote preflight observation type is invalid.")
    if observation.get("backend_id") != backend_id or observation.get("bundle_id") != bundle_id:
        raise ModelAcquisitionPreflightError("Remote preflight identity mismatch.")
    _false_safety(observation.get("safety"), REMOTE_FALSE_SAFETY_FIELDS, "Remote observation")


def _snapshot_report(snapshot: Mapping[str, Any]) -> str:
    readiness = snapshot["readiness"]
    lines = [
        "# GenomeAgent Model Acquisition Runtime Preflight",
        "",
        "- Backend: `{}`".format(snapshot["backend_id"]),
        "- Bundle: `{}`".format(snapshot["bundle_id"]),
        "- Evidence: `{}`".format(snapshot["evidence_id"]),
        "- Status: **{}**".format(readiness["status"]),
        "- Expires: `{}`".format(snapshot["expires_at_utc"]),
        "- Remote writes: **no**",
        "- Model download: **no**",
        "- Slurm/GPU execution: **no**",
        "",
        "## Remaining blockers",
        "",
    ]
    lines.extend("- `{}`".format(item) for item in readiness["blockers"])
    lines.extend(["", "This observation imported transfer tooling but did not contact the model provider.", ""])
    return "\n".join(lines)


def _state_report(state: Mapping[str, Any]) -> str:
    readiness = state["readiness"]
    return "\n".join([
        "# GenomeAgent Model Acquisition Preflight State", "",
        "- Backend: `{}`".format(state["backend_id"]),
        "- Bundle: `{}`".format(state["bundle_id"]),
        "- Source snapshots: {}".format(state["source_snapshots"]),
        "- Status: **{}**".format(readiness["status"]),
        "- Next safe action: `{}`".format(readiness["next_safe_action"]),
        "- Automatic execution: **no**", "", "## Blockers", "",
        *("- `{}`".format(item) for item in readiness["blockers"]), "",
    ])


class ModelAcquisitionPreflightCollector:
    """Collect one bounded, read-only runtime and target observation over SSH."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/acquisition_runtime"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
        evidence_root: Path = Path("workspace/model_acquisition_preflight_evidence"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.bundle_root = Path(bundle_root)
        self.plan_root = Path(plan_root)
        self.evidence_root = Path(evidence_root)

    def policy(self, backend_id: str) -> tuple[Path, dict[str, Any], bytes]:
        backend_id = _identifier(backend_id, "backend_id")
        path = self.policy_root / (backend_id + ".json")
        policy, raw = _validate_policy(path, backend_id)
        return path, policy, raw

    def collect(
        self,
        backend_id: str,
        bundle_id: str,
        runner: PythonRemoteRunner,
        stamp: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        observed_at: Optional[datetime] = None,
    ) -> AcquisitionPreflightCollectionResult:
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        backend_id = _identifier(backend.get("backend_id"), "backend_id")
        bundle_id = _digest(bundle_id, "bundle ID")
        policy_path, policy, policy_raw = self.policy(backend_id)
        bundle, _, bundle_sources = _load_bundle(
            backend_id, bundle_id, self.bundle_root, self.plan_root, backend
        )
        host = str(runner.host or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
            raise ModelAcquisitionPreflightError("Unsafe SSH host.")
        timeout = int(timeout_seconds) if timeout_seconds is not None else int(
            policy["limits"]["timeout_seconds"]
        )
        if not 5 <= timeout <= 240:
            raise ModelAcquisitionPreflightError("Preflight timeout must be 5 to 240 seconds.")
        now = observed_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ModelAcquisitionPreflightError("Observation time must include timezone.")
        now = now.astimezone(timezone.utc)
        evidence_id = stamp or now.strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", evidence_id):
            raise ModelAcquisitionPreflightError("Invalid evidence ID.")
        evidence_dir = self.evidence_root / backend_id / bundle_id
        evidence_path = evidence_dir / (evidence_id + ".json")
        report_path = evidence_dir / (evidence_id + ".md")
        if evidence_path.exists() or report_path.exists():
            raise ModelAcquisitionPreflightError("Refusing to overwrite immutable preflight evidence.")
        try:
            observation = runner.run_python(
                _probe_program(backend, policy, bundle), timeout_seconds=timeout
            )
        except Exception as exc:
            if isinstance(exc, ModelAcquisitionPreflightError):
                raise
            raise ModelAcquisitionPreflightError(
                "Read-only acquisition preflight failed on {}: {}".format(host, exc)
            ) from exc
        if not isinstance(observation, Mapping):
            raise ModelAcquisitionPreflightError("Remote preflight result must be an object.")
        observation = dict(observation)
        _validate_observation(observation, backend_id, bundle_id)
        sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "acquisition_runtime_policy"),
            *bundle_sources,
        ]
        readiness = _interpret(backend, policy, bundle, observation, True, True)
        observed_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        expires = now + timedelta(seconds=int(policy["limits"]["freshness_seconds"]))
        snapshot = {
            "schema_version": "1.0", "policy_version": MODEL_ACQUISITION_PREFLIGHT_POLICY_VERSION,
            "source_mode": "bounded_read_only_acquisition_runtime_observation",
            "backend_id": backend_id, "bundle_id": bundle_id,
            "host": host, "evidence_id": evidence_id,
            "observed_at_utc": observed_text,
            "expires_at_utc": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "query": {"single_ssh_session": True, "timeout_seconds": timeout,
                      "provider_requests": 0, "path_scan": "metadata_only"},
            "source_artifacts": sources, "observation": observation,
            "readiness": readiness,
            "safety": {"bounded_remote_read": True, **{
                field.replace("_allowed", ""): False
                for field in PREFLIGHT_FALSE_SAFETY_FIELDS
            }},
        }
        _write_immutable(evidence_path, _pretty_json(snapshot))
        _write_immutable(report_path, _snapshot_report(snapshot))
        return AcquisitionPreflightCollectionResult(
            backend_id, bundle_id, host, evidence_id, evidence_path,
            str(readiness["status"]), tuple(readiness["blockers"]),
        )


class ModelAcquisitionPreflightCore:
    """Rebuild current preflight state from immutable local evidence."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/acquisition_runtime"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
        evidence_root: Path = Path("workspace/model_acquisition_preflight_evidence"),
        state_root: Path = Path("workspace/model_acquisition_preflight_state"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.bundle_root = Path(bundle_root)
        self.plan_root = Path(plan_root)
        self.evidence_root = Path(evidence_root)
        self.state_root = Path(state_root)

    def ingest(
        self, backend_id: str, bundle_id: str, now: Optional[datetime] = None
    ) -> AcquisitionPreflightIngestResult:
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        backend_id = _identifier(backend.get("backend_id"), "backend_id")
        bundle_id = _digest(bundle_id, "bundle ID")
        policy_path = self.policy_root / (backend_id + ".json")
        policy, policy_raw = _validate_policy(policy_path, backend_id)
        bundle, _, bundle_sources = _load_bundle(
            backend_id, bundle_id, self.bundle_root, self.plan_root, backend
        )
        current_sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "acquisition_runtime_policy"),
            *bundle_sources,
        ]
        paths = sorted((self.evidence_root / backend_id / bundle_id).glob("*.json"))
        if not paths:
            raise ModelAcquisitionPreflightError("No acquisition preflight evidence found.")
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ModelAcquisitionPreflightError("Ingest time must include timezone.")
        current_time = current_time.astimezone(timezone.utc)
        records = []
        for path in paths:
            snapshot, raw = _read_json(path, "preflight evidence snapshot")
            if (
                snapshot.get("schema_version") != "1.0"
                or snapshot.get("policy_version") != MODEL_ACQUISITION_PREFLIGHT_POLICY_VERSION
                or snapshot.get("source_mode") != "bounded_read_only_acquisition_runtime_observation"
                or snapshot.get("backend_id") != backend_id
                or snapshot.get("bundle_id") != bundle_id
                or snapshot.get("evidence_id") != path.stem
            ):
                raise ModelAcquisitionPreflightError("Invalid preflight evidence snapshot.")
            snapshot_safety = snapshot.get("safety")
            if not isinstance(snapshot_safety, Mapping) or snapshot_safety.get(
                "bounded_remote_read"
            ) is not True:
                raise ModelAcquisitionPreflightError("Invalid preflight snapshot safety record.")
            for field in PREFLIGHT_FALSE_SAFETY_FIELDS:
                snapshot_field = field.replace("_allowed", "")
                if snapshot_safety.get(snapshot_field) is not False:
                    raise ModelAcquisitionPreflightError(
                        "Preflight snapshot must disable {}.".format(snapshot_field)
                    )
            observation = snapshot.get("observation")
            if not isinstance(observation, Mapping):
                raise ModelAcquisitionPreflightError("Preflight snapshot has no observation.")
            _validate_observation(observation, backend_id, bundle_id)
            try:
                expires = datetime.strptime(
                    str(snapshot.get("expires_at_utc") or ""), "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ModelAcquisitionPreflightError("Invalid preflight expiry time.") from exc
            inputs_current = snapshot.get("source_artifacts") == current_sources
            freshness = current_time <= expires
            readiness = _interpret(
                backend, policy, bundle, observation, inputs_current, freshness
            )
            records.append({
                "evidence_id": path.stem,
                "observed_at_utc": snapshot.get("observed_at_utc"),
                "expires_at_utc": snapshot.get("expires_at_utc"),
                "source_path": str(path), "source_sha256": _sha256_bytes(raw),
                "inputs_current": inputs_current, "evidence_fresh": freshness,
                "readiness": readiness, "observation": dict(observation),
            })
        records.sort(key=lambda item: (str(item["observed_at_utc"]), item["evidence_id"]))
        latest = records[-1]
        state = {
            "schema_version": "1.0", "policy_version": MODEL_ACQUISITION_PREFLIGHT_POLICY_VERSION,
            "backend_id": backend_id, "bundle_id": bundle_id,
            "source_snapshots": len(records), "latest_evidence_id": latest["evidence_id"],
            "latest_source_sha256": latest["source_sha256"],
            "latest_observed_at_utc": latest["observed_at_utc"],
            "latest_expires_at_utc": latest["expires_at_utc"],
            "readiness": latest["readiness"], "observation": latest["observation"],
            "automatic_execution_allowed": False, "automatic_model_download_allowed": False,
        }
        history = [{key: value for key, value in item.items() if key != "observation"}
                   for item in records]
        provenance = {
            "schema_version": "1.0", "policy_version": MODEL_ACQUISITION_PREFLIGHT_POLICY_VERSION,
            "backend_id": backend_id, "bundle_id": bundle_id,
            "source_artifacts": current_sources,
            "evidence_snapshots": [{"evidence_id": item["evidence_id"],
                "path": item["source_path"], "sha256": item["source_sha256"]}
                for item in records],
            "derivation": "deterministic_local_preflight_evidence_replay",
            "remote_access_during_ingest": False, "remote_writes": False,
            "model_download": False, "job_submission": False, "gpu_allocation": False,
        }
        state_dir = self.state_root / backend_id / bundle_id
        artifacts = (
            state_dir / "current_evidence.json", state_dir / "evidence_history.jsonl",
            state_dir / "readiness.json", state_dir / "report.md", state_dir / "provenance.json",
        )
        payloads = (
            _pretty_json(state),
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in history),
            _pretty_json(latest["readiness"]), _state_report(state), _pretty_json(provenance),
        )
        for path, payload in zip(artifacts, payloads):
            _atomic_write(path, payload)
        readiness = latest["readiness"]
        return AcquisitionPreflightIngestResult(
            backend_id, bundle_id, state_dir, len(records), str(readiness["status"]),
            str(readiness["next_safe_action"]), tuple(readiness["blockers"]), artifacts,
        )
