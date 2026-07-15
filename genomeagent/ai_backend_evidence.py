#!/usr/bin/env python3
"""Bounded read-only evidence collection for registered AI backends."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from genomeagent.ai_evaluation import AIRegistry


AI_BACKEND_EVIDENCE_POLICY_VERSION = "1.2"
SUPPORTED_AI_BACKEND_EVIDENCE_SNAPSHOT_POLICY_VERSIONS = frozenset(
    {"1.0", "1.1", "1.2"}
)
REQUIRED_FALSE_SAFETY_FIELDS = (
    "remote_writes_allowed",
    "job_submission_allowed",
    "gpu_allocation_allowed",
    "model_download_allowed",
    "model_import_allowed",
    "recursive_model_scan_allowed",
    "large_model_file_hashing_allowed",
)
REMOTE_FALSE_SAFETY_FIELDS = (
    "remote_writes",
    "job_submission",
    "gpu_allocation",
    "model_download",
    "model_import",
    "recursive_model_scan",
    "large_model_file_hashing",
)
SNAPSHOT_FALSE_SAFETY_FIELDS = REMOTE_FALSE_SAFETY_FIELDS + (
    "automatic_registry_update",
)


class AIBackendEvidenceError(RuntimeError):
    """Raised when backend evidence is unsafe, incomplete or inconsistent."""


class PythonRemoteRunner(Protocol):
    host: str

    def run_python(self, program: str, timeout_seconds: int = 240) -> dict[str, Any]:
        """Run one read-only observation and return its JSON object."""


@dataclass(frozen=True)
class AIBackendCollectionResult:
    backend_id: str
    host: str
    evidence_path: Path
    evidence_id: str
    environment_status: str


@dataclass(frozen=True)
class AIBackendIngestResult:
    backend_id: str
    state_dir: Path
    snapshots: int
    status: str
    next_safe_action: str
    blockers: tuple[str, ...]
    artifact_paths: tuple[Path, ...]


def _sha256_bytes(value: bytes) -> str:
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


def _write_immutable(path: Path, text: str) -> bool:
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise AIBackendEvidenceError(
                "Immutable backend evidence exists with different content: {}".format(
                    path
                )
            )
        return False
    _atomic_write(path, text)
    return True


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise AIBackendEvidenceError("Unsafe or empty {}: {!r}".format(label, value))
    return normalized


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise AIBackendEvidenceError("{} does not exist: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIBackendEvidenceError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise AIBackendEvidenceError("{} must be a JSON object: {}".format(label, path))
    return value, raw


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "path": str(path),
        "sha256": _sha256_bytes(raw),
    }


def _validate_absolute_path(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or not Path(text).is_absolute() or "\x00" in text:
        raise AIBackendEvidenceError("{} must be a safe absolute path.".format(label))
    if any(part in {"", ".", ".."} for part in Path(text).parts[1:]):
        raise AIBackendEvidenceError("{} contains an unsafe path segment.".format(label))
    return text


def _validate_policy(
    path: Path,
    expected_backend_id: Optional[str] = None,
) -> tuple[dict[str, Any], bytes]:
    policy, raw = _read_json(path, "AI backend evidence policy")
    backend_id = _identifier(policy.get("backend_id"), "backend_id")
    if path.stem != backend_id:
        raise AIBackendEvidenceError(
            "Evidence policy filename {} must match backend_id {}.".format(
                path.name, backend_id
            )
        )
    if expected_backend_id is not None and backend_id != expected_backend_id:
        raise AIBackendEvidenceError("Evidence policy backend does not match registry backend.")
    if policy.get("policy_version") != AI_BACKEND_EVIDENCE_POLICY_VERSION:
        raise AIBackendEvidenceError("Unsupported AI backend evidence policy version.")
    remote_python = str(policy.get("remote_python") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_./+-]+", remote_python):
        raise AIBackendEvidenceError("Unsafe remote Python executable.")
    _validate_absolute_path(policy.get("project_storage_root"), "project_storage_root")
    workspace_candidates = policy.get("workspace_quota_command_candidates")
    if (
        not isinstance(workspace_candidates, list)
        or not workspace_candidates
        or len(workspace_candidates) > 5
    ):
        raise AIBackendEvidenceError(
            "workspace_quota_command_candidates must contain between 1 and 5 paths."
        )
    if not all(isinstance(candidate, str) for candidate in workspace_candidates):
        raise AIBackendEvidenceError(
            "workspace_quota_command_candidates must contain only path strings."
        )
    if len(set(workspace_candidates)) != len(workspace_candidates):
        raise AIBackendEvidenceError(
            "workspace_quota_command_candidates must not contain duplicates."
        )
    for candidate in workspace_candidates:
        validated = _validate_absolute_path(
            candidate, "workspace quota command candidate"
        )
        if (
            not validated.startswith("/appl/soft/")
            or Path(validated).name != "csc-workspaces"
            or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", validated)
        ):
            raise AIBackendEvidenceError(
                "Workspace quota commands must be explicit CSC software paths "
                "ending in csc-workspaces."
            )
    candidates = policy.get("module_initialization_candidates")
    if not isinstance(candidates, list) or not candidates or len(candidates) > 5:
        raise AIBackendEvidenceError(
            "module_initialization_candidates must contain between 1 and 5 paths."
        )
    for candidate in candidates:
        _validate_absolute_path(candidate, "module initialization candidate")
    module_use_paths = policy.get("module_use_paths")
    if not isinstance(module_use_paths, list) or len(module_use_paths) > 10:
        raise AIBackendEvidenceError(
            "module_use_paths must contain at most 10 paths."
        )
    for module_path in module_use_paths:
        validated = _validate_absolute_path(module_path, "module use path")
        if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", validated):
            raise AIBackendEvidenceError(
                "module use path contains unsupported shell characters."
            )
    manifest_name = str(policy.get("model_inventory_manifest") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", manifest_name):
        raise AIBackendEvidenceError("Unsafe model inventory manifest filename.")
    checks = policy.get("checks")
    if not isinstance(checks, Mapping) or not checks:
        raise AIBackendEvidenceError("Evidence policy requires explicit checks.")
    if not all(isinstance(value, bool) for value in checks.values()):
        raise AIBackendEvidenceError("Every evidence check switch must be boolean.")
    limits = policy.get("limits")
    if not isinstance(limits, Mapping):
        raise AIBackendEvidenceError("Evidence policy requires limits.")
    limit_ranges = {
        "timeout_seconds": (5, 240),
        "maximum_command_output_characters": (1000, 50000),
        "maximum_model_entries": (1, 1000),
        "maximum_small_metadata_file_bytes": (1024, 4 * 1024 * 1024),
    }
    for field, (minimum, maximum) in limit_ranges.items():
        value = limits.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise AIBackendEvidenceError("Evidence limit {} must be an integer.".format(field))
        if not minimum <= value <= maximum:
            raise AIBackendEvidenceError(
                "Evidence limit {} must be between {} and {}.".format(
                    field, minimum, maximum
                )
            )
    safety = policy.get("safety")
    if not isinstance(safety, Mapping):
        raise AIBackendEvidenceError("Evidence policy requires a safety object.")
    for field in REQUIRED_FALSE_SAFETY_FIELDS:
        if safety.get(field) is not False:
            raise AIBackendEvidenceError(
                "Evidence policy must explicitly set {} to false.".format(field)
            )
    return policy, raw


def _validate_backend_fields(backend: Mapping[str, Any]) -> None:
    runtime = backend.get("runtime", {})
    installation = backend.get("installation", {})
    resources = backend.get("resource_request", {})
    if not isinstance(runtime, Mapping) or not isinstance(installation, Mapping):
        raise AIBackendEvidenceError("Backend runtime and installation must be objects.")
    if not isinstance(resources, Mapping):
        raise AIBackendEvidenceError("Backend resource_request must be an object.")
    module = str(runtime.get("module") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._/+:-]+", module):
        raise AIBackendEvidenceError("Unsafe or empty runtime module name.")
    _validate_absolute_path(installation.get("path"), "model installation path")
    partition = str(resources.get("partition") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", partition):
        raise AIBackendEvidenceError("Unsafe or empty Slurm partition.")
    gpu_type = str(resources.get("gpu_type") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", gpu_type):
        raise AIBackendEvidenceError("Unsafe or empty GPU type.")


def _probe_program(backend: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    runtime = backend["runtime"]
    installation = backend["installation"]
    resources = backend["resource_request"]
    probe_config = {
        "backend_id": backend["backend_id"],
        "expected_architecture": runtime.get("architecture"),
        "expected_module": runtime["module"],
        "expected_runtime_version": runtime.get("version"),
        "expected_partition": resources["partition"],
        "expected_gpu_type": resources["gpu_type"],
        "project_storage_root": policy["project_storage_root"],
        "workspace_quota_command_candidates": policy[
            "workspace_quota_command_candidates"
        ],
        "model_path": installation["path"],
        "model_inventory_manifest": policy["model_inventory_manifest"],
        "module_initialization_candidates": policy["module_initialization_candidates"],
        "module_use_paths": policy["module_use_paths"],
        "checks": policy["checks"],
        "limits": policy["limits"],
    }
    encoded = repr(json.dumps(probe_config, sort_keys=True))
    program = r'''import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_PROBE_CONFIG__)
MAX_OUTPUT = int(CONFIG["limits"]["maximum_command_output_characters"])
MAX_ENTRIES = int(CONFIG["limits"]["maximum_model_entries"])
MAX_SMALL = int(CONFIG["limits"]["maximum_small_metadata_file_bytes"])


def bounded(value):
    text = str(value or "")
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + "\n[output truncated by GenomeAgent]"


def run(args, timeout=20, env=None):
    try:
        completed = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": bounded(completed.stdout),
            "stderr": bounded(completed.stderr),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": bounded(exc.stdout),
            "stderr": bounded(exc.stderr),
            "timed_out": True,
        }
    except OSError as exc:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": bounded(exc),
            "timed_out": False,
        }


def storage(path_text):
    path = Path(path_text)
    result = {
        "path": path_text,
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "is_symlink": path.is_symlink(),
    }
    if path.exists():
        try:
            values = os.statvfs(path_text)
            result.update({
                "filesystem_capacity_bytes": values.f_frsize * values.f_blocks,
                "filesystem_available_bytes": values.f_frsize * values.f_bavail,
                "filesystem_files": values.f_files,
                "filesystem_files_available": values.f_favail,
            })
        except OSError as exc:
            result["statvfs_error"] = bounded(exc)
    return result


def read_small_json(path):
    try:
        size = path.stat().st_size
        if size > MAX_SMALL:
            return {"status": "too_large", "size_bytes": size}
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            return {"status": "not_object", "size_bytes": size}
        allowed = ("_name_or_path", "model_type", "architectures", "torch_dtype")
        return {
            "status": "parsed",
            "size_bytes": size,
            "selected_fields": {key: value.get(key) for key in allowed if key in value},
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    except Exception as exc:
        return {"status": "unreadable", "error": bounded(exc)}


def git_revision(model_path):
    git_root = model_path / ".git"
    head = git_root / "HEAD"
    try:
        if not head.is_file() or head.stat().st_size > 4096:
            return None
        value = head.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", value):
            return value.lower()
        match = re.fullmatch(r"ref: (refs/[A-Za-z0-9._/-]+)", value)
        if not match or ".." in match.group(1).split("/"):
            return None
        reference = git_root / match.group(1)
        if not reference.is_file() or reference.stat().st_size > 4096:
            return None
        revision = reference.read_text(encoding="utf-8").strip()
        return revision.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", revision) else None
    except (OSError, UnicodeError):
        return None


def model_inventory(path_text):
    path = Path(path_text)
    result = {
        "path": path_text,
        "exists": path.exists(),
        "is_directory": path.is_dir(),
        "is_symlink": path.is_symlink(),
        "scan_depth": 1,
        "large_files_hashed": False,
    }
    if not path.is_dir():
        return result
    entries = []
    truncated = False
    try:
        with os.scandir(path_text) as iterator:
            for index, entry in enumerate(iterator):
                if index >= MAX_ENTRIES:
                    truncated = True
                    break
                record = {
                    "name": entry.name,
                    "is_file": entry.is_file(follow_symlinks=False),
                    "is_directory": entry.is_dir(follow_symlinks=False),
                    "is_symlink": entry.is_symlink(),
                }
                if record["is_file"]:
                    try:
                        record["size_bytes"] = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        record["size_bytes"] = None
                entries.append(record)
    except OSError as exc:
        result["scan_error"] = bounded(exc)
    result["entries"] = sorted(entries, key=lambda item: item["name"])
    result["entries_observed"] = len(entries)
    result["entries_truncated"] = truncated
    config_path = path / "config.json"
    if config_path.is_file():
        result["config_metadata"] = read_small_json(config_path)
    result["observed_git_revision"] = git_revision(path)
    manifest = path / CONFIG["model_inventory_manifest"]
    result["inventory_manifest"] = {
        "path": str(manifest),
        "exists": manifest.is_file(),
        "verified_against_model_files": False,
    }
    if manifest.is_file():
        result["inventory_manifest"]["metadata"] = read_small_json(manifest)
    return result


checks = CONFIG["checks"]
observation = {
    "schema_version": "1.0",
    "observation_type": "ai_backend_environment",
    "backend_id": CONFIG["backend_id"],
    "identity": {},
    "module": {},
    "scheduler": {},
    "storage": {},
    "model": {},
    "safety": {
        "remote_writes": False,
        "job_submission": False,
        "gpu_allocation": False,
        "model_download": False,
        "model_import": False,
        "recursive_model_scan": False,
        "large_model_file_hashing": False,
    },
}

if checks.get("identity"):
    observation["identity"] = {
        "hostname": socket.gethostname(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "remote_python_executable": sys.executable,
        "remote_python_version": platform.python_version(),
    }

module_init = next(
    (value for value in CONFIG["module_initialization_candidates"] if Path(value).is_file()),
    None,
)
module_env = dict(os.environ)
module_env.update({
    "MODULE_INIT": module_init or "",
    "MODULE_NAME": CONFIG["expected_module"],
    "MODULE_USE_PATHS": ":".join(CONFIG["module_use_paths"]),
})
module = {
    "expected_module": CONFIG["expected_module"],
    "expected_version": CONFIG["expected_runtime_version"],
    "initialization_candidates": CONFIG["module_initialization_candidates"],
    "selected_initialization": module_init,
    "module_use_paths": CONFIG["module_use_paths"],
}
module_prefix = (
    'source "$MODULE_INIT" >/dev/null 2>&1 || exit; '
    'if [ -n "$MODULE_USE_PATHS" ]; then '
    'old_ifs=$IFS; IFS=:; '
    'for module_path in $MODULE_USE_PATHS; do '
    'module use "$module_path" >/dev/null 2>&1 || exit; '
    'done; IFS=$old_ifs; fi; '
)
if checks.get("module_metadata") and module_init:
    module["availability"] = run([
        "bash", "-c", module_prefix + 'module is-avail "$MODULE_NAME"',
    ], env=module_env)
    module["show"] = run([
        "bash", "-c", module_prefix + 'module show "$MODULE_NAME"',
    ], env=module_env)
    try:
        loaded = subprocess.run(
            [
                "bash", "-c", module_prefix +
                'module load "$MODULE_NAME" >/dev/null 2>&1 && env -0',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            env=module_env,
            check=False,
        )
        module["load_environment_returncode"] = loaded.returncode
        module["load_environment_stderr"] = bounded(
            loaded.stderr.decode("utf-8", errors="replace")
        )
        if loaded.returncode == 0:
            loaded_env = {}
            for item in loaded.stdout.split(b"\0"):
                if b"=" not in item:
                    continue
                key, value = item.split(b"=", 1)
                loaded_env[key.decode("utf-8", errors="replace")] = value.decode(
                    "utf-8", errors="replace"
                )
            python_path = shutil.which("python3", path=loaded_env.get("PATH"))
            module["loaded_python_executable"] = python_path
            if python_path:
                metadata_code = (
                    'import importlib.metadata as m, json, platform, sys; '
                    'print(json.dumps({"package_version": m.version("vllm"), '
                    '"python_version": platform.python_version(), '
                    '"machine": platform.machine(), "python_executable": sys.executable}))'
                )
                module["package_metadata"] = run(
                    [python_path, "-c", metadata_code], env=loaded_env
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        module["load_environment_error"] = bounded(exc)
observation["module"] = module

if checks.get("scheduler_partition"):
    scheduler = {
        "sbatch_path": shutil.which("sbatch"),
        "sinfo_path": shutil.which("sinfo"),
        "expected_partition": CONFIG["expected_partition"],
        "expected_gpu_type": CONFIG["expected_gpu_type"],
    }
    if scheduler["sinfo_path"]:
        scheduler["partition_query"] = run([
            scheduler["sinfo_path"], "-h", "-p", CONFIG["expected_partition"],
            "-o", "%P|%a|%l|%D|%G|%c|%m",
        ])
    observation["scheduler"] = scheduler

if checks.get("project_storage"):
    observation["storage"] = storage(CONFIG["project_storage_root"])
if checks.get("workspace_quota_command"):
    path_search_result = shutil.which("csc-workspaces")
    approved_candidates = list(CONFIG["workspace_quota_command_candidates"])
    observation["storage"]["workspace_command_path_search_result"] = path_search_result
    command_path = None
    command_source = None
    if path_search_result in approved_candidates:
        candidate = Path(path_search_result)
        if candidate.is_file() and os.access(path_search_result, os.X_OK):
            command_path = path_search_result
            command_source = "approved_remote_python_path"
    if command_path is None:
        for candidate_text in approved_candidates:
            candidate = Path(candidate_text)
            if candidate.is_file() and os.access(candidate_text, os.X_OK):
                command_path = candidate_text
                command_source = "approved_explicit_candidate"
                break
    observation["storage"]["workspace_command_path"] = command_path
    observation["storage"]["workspace_command_path_source"] = command_source
    if command_path:
        observation["storage"]["workspace_command"] = run([command_path])
if checks.get("model_top_level_inventory"):
    observation["model"] = model_inventory(CONFIG["model_path"])

print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
'''
    return program.replace("__GENOMEAGENT_PROBE_CONFIG__", encoded)


def _parse_package_metadata(module: Mapping[str, Any]) -> dict[str, Any]:
    result = module.get("package_metadata", {})
    if not isinstance(result, Mapping) or result.get("returncode") != 0:
        return {}
    stdout = str(result.get("stdout") or "")
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _parse_partition_rows(scheduler: Mapping[str, Any]) -> list[dict[str, str]]:
    query = scheduler.get("partition_query", {})
    if not isinstance(query, Mapping) or query.get("returncode") != 0:
        return []
    fields = ("partition", "availability", "time_limit", "nodes", "gres", "cpus", "memory")
    rows = []
    for line in str(query.get("stdout") or "").splitlines():
        values = line.strip().split("|")
        if len(values) != len(fields):
            continue
        row = dict(zip(fields, values))
        row["partition"] = row["partition"].rstrip("*")
        rows.append(row)
    return rows


def _interpret(
    backend: Mapping[str, Any],
    policy: Mapping[str, Any],
    observation: Mapping[str, Any],
    current_config: bool,
) -> dict[str, Any]:
    checks = []
    identity = observation.get("identity", {})
    module = observation.get("module", {})
    scheduler = observation.get("scheduler", {})
    storage = observation.get("storage", {})
    model_observation = observation.get("model", {})
    if not isinstance(identity, Mapping):
        identity = {}
    if not isinstance(module, Mapping):
        module = {}
    if not isinstance(scheduler, Mapping):
        scheduler = {}
    if not isinstance(storage, Mapping):
        storage = {}
    if not isinstance(model_observation, Mapping):
        model_observation = {}
    runtime = backend.get("runtime", {})
    model = backend.get("model", {})
    installation = backend.get("installation", {})
    resources = backend.get("resource_request", {})
    package_metadata = _parse_package_metadata(module)
    partition_rows = _parse_partition_rows(scheduler)
    expected_partition = str(resources.get("partition") or "")
    expected_gpu = str(resources.get("gpu_type") or "").lower()
    matching_partitions = [
        row for row in partition_rows if row["partition"] == expected_partition
    ]

    def add(name: str, passed: bool, observed: Any, expected: Any) -> None:
        checks.append({
            "check": name,
            "status": "verified" if passed else "unknown_or_mismatch",
            "expected": expected,
            "observed": observed,
        })

    add("remote_identity", bool(identity.get("hostname")), identity.get("hostname"), "non-empty")
    add(
        "architecture",
        identity.get("machine") == runtime.get("architecture"),
        identity.get("machine"),
        runtime.get("architecture"),
    )
    availability = module.get("availability", {})
    module_available = isinstance(availability, Mapping) and availability.get("returncode") == 0
    add("runtime_module", module_available, module.get("expected_module"), runtime.get("module"))
    add(
        "runtime_version",
        package_metadata.get("package_version") == runtime.get("version"),
        package_metadata.get("package_version"),
        runtime.get("version"),
    )
    add("slurm_commands", bool(scheduler.get("sbatch_path") and scheduler.get("sinfo_path")), {
        "sbatch": scheduler.get("sbatch_path"), "sinfo": scheduler.get("sinfo_path")
    }, "sbatch and sinfo")
    add("scheduler_partition", bool(matching_partitions), [row["partition"] for row in partition_rows], expected_partition)
    observed_gres = [row["gres"] for row in matching_partitions]
    add(
        "gpu_type",
        bool(expected_gpu and any(expected_gpu in value.lower() for value in observed_gres)),
        observed_gres,
        expected_gpu,
    )
    add(
        "project_storage",
        storage.get("exists") is True and storage.get("is_directory") is True,
        storage.get("path"),
        policy.get("project_storage_root"),
    )
    workspace_result = storage.get("workspace_command", {})
    workspace_path = storage.get("workspace_command_path")
    workspace_verified = (
        workspace_path in policy.get("workspace_quota_command_candidates", [])
        and isinstance(workspace_result, Mapping)
        and workspace_result.get("returncode") == 0
        and workspace_result.get("timed_out") is False
    )
    add(
        "workspace_quota_command",
        workspace_verified,
        {
            "path": workspace_path,
            "source": storage.get("workspace_command_path_source"),
            "returncode": (
                workspace_result.get("returncode")
                if isinstance(workspace_result, Mapping)
                else None
            ),
        },
        policy.get("workspace_quota_command_candidates", []),
    )
    add(
        "model_path",
        model_observation.get("exists") is True and model_observation.get("is_directory") is True,
        model_observation.get("path"),
        installation.get("path"),
    )
    manifest = model_observation.get("inventory_manifest", {})
    manifest_metadata = manifest.get("metadata", {}) if isinstance(manifest, Mapping) else {}
    registered_manifest_sha256 = str(
        installation.get("manifest_sha256") or ""
    ).strip().lower()
    registered_inventory_sha256 = str(
        installation.get("verified_inventory_sha256") or ""
    ).strip().lower()
    registered_manifest_verified = (
        installation.get("status") == "verified_present"
        and isinstance(manifest, Mapping)
        and manifest.get("exists") is True
        and isinstance(manifest_metadata, Mapping)
        and manifest_metadata.get("status") == "parsed"
        and re.fullmatch(r"[0-9a-f]{64}", registered_manifest_sha256) is not None
        and manifest_metadata.get("sha256") == registered_manifest_sha256
        and model.get("weights_digest_type")
        == "verified_model_candidate_manifest_sha256"
        and re.fullmatch(r"[0-9a-f]{64}", registered_inventory_sha256) is not None
        and str(model.get("weights_sha256") or "").strip().lower()
        == registered_inventory_sha256
    )
    add(
        "registered_installed_manifest",
        registered_manifest_verified,
        {
            "observed_manifest_sha256": (
                manifest_metadata.get("sha256")
                if isinstance(manifest_metadata, Mapping)
                else None
            ),
            "installation_status": installation.get("status"),
        },
        {
            "registered_manifest_sha256": registered_manifest_sha256 or None,
            "registered_inventory_sha256": registered_inventory_sha256 or None,
        },
    )

    environment_names = {
        "remote_identity", "architecture", "runtime_module", "runtime_version",
        "slurm_commands", "scheduler_partition", "gpu_type", "project_storage",
        "workspace_quota_command",
    }
    environment_blockers = [
        item["check"] + "_unverified"
        for item in checks
        if item["check"] in environment_names and item["status"] != "verified"
    ]
    model_blockers = []
    if not str(model.get("revision") or "").strip():
        model_blockers.append("model_revision_unpinned")
    digest = str(model.get("weights_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        model_blockers.append("model_weights_digest_missing")
    if not any(item["check"] == "model_path" and item["status"] == "verified" for item in checks):
        model_blockers.append("model_path_absent")
    if not registered_manifest_verified:
        model_blockers.append("verified_model_inventory_missing")
    benchmark_blockers = []
    if backend.get("benchmark_status") != "passed_reviewed_suite":
        benchmark_blockers.append("inference_not_benchmarked")
    freshness_blockers = [] if current_config else ["configuration_changed_since_observation"]
    blockers = environment_blockers + freshness_blockers + model_blockers + benchmark_blockers

    if environment_blockers:
        status = "environment_evidence_incomplete"
        next_action = "review_unverified_environment_checks"
    elif freshness_blockers:
        status = "environment_evidence_stale"
        next_action = "collect_fresh_backend_evidence"
    elif any(item in model_blockers for item in (
        "model_revision_unpinned", "model_path_absent"
    )):
        status = "model_identity_and_acquisition_required"
        next_action = "review_pinned_model_acquisition_plan"
    elif model_blockers:
        status = "model_integrity_evidence_required"
        next_action = "verify_model_inventory_and_digest"
    elif benchmark_blockers:
        status = "ready_for_bounded_benchmark_review"
        next_action = "review_bounded_gpu_benchmark_plan"
    else:
        status = "benchmark_evidence_available_for_review"
        next_action = "review_backend_candidate"
    return {
        "schema_version": "1.0",
        "policy_version": AI_BACKEND_EVIDENCE_POLICY_VERSION,
        "backend_id": backend["backend_id"],
        "status": status,
        "next_safe_action": next_action,
        "environment_checks": checks,
        "environment_blockers": environment_blockers,
        "model_blockers": model_blockers,
        "benchmark_blockers": benchmark_blockers,
        "freshness_blockers": freshness_blockers,
        "blockers": blockers,
        "automatic_execution_allowed": False,
        "automatic_model_download_allowed": False,
        "automatic_job_submission_allowed": False,
        "automatic_registry_update_allowed": False,
    }


def _validate_remote_observation(
    observation: Mapping[str, Any],
    backend_id: str,
) -> None:
    if observation.get("schema_version") != "1.0":
        raise AIBackendEvidenceError("Remote backend observation schema is unsupported.")
    if observation.get("observation_type") != "ai_backend_environment":
        raise AIBackendEvidenceError("Remote result is not AI backend environment evidence.")
    if observation.get("backend_id") != backend_id:
        raise AIBackendEvidenceError("Remote observation backend ID mismatch.")
    safety = observation.get("safety")
    if not isinstance(safety, Mapping):
        raise AIBackendEvidenceError("Remote observation has no safety record.")
    for field in REMOTE_FALSE_SAFETY_FIELDS:
        if safety.get(field) is not False:
            raise AIBackendEvidenceError(
                "Remote observation must explicitly disable {}.".format(field)
            )


def _validate_snapshot(payload: Mapping[str, Any], path: Path, backend_id: str) -> None:
    if payload.get("schema_version") != "1.0":
        raise AIBackendEvidenceError("Backend evidence snapshot schema is unsupported.")
    if (
        payload.get("policy_version")
        not in SUPPORTED_AI_BACKEND_EVIDENCE_SNAPSHOT_POLICY_VERSIONS
    ):
        raise AIBackendEvidenceError("Backend evidence snapshot policy is unsupported.")
    if payload.get("source_mode") != "bounded_read_only_backend_observation":
        raise AIBackendEvidenceError(
            "Refusing backend evidence without read-only source mode: {}".format(path)
        )
    if payload.get("backend_id") != backend_id:
        raise AIBackendEvidenceError("Backend evidence snapshot ID mismatch.")
    evidence_id = str(payload.get("evidence_id") or "")
    if path.stem != evidence_id or not re.fullmatch(
        r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", evidence_id
    ):
        raise AIBackendEvidenceError("Backend evidence filename and ID do not match.")
    safety = payload.get("safety")
    if not isinstance(safety, Mapping):
        raise AIBackendEvidenceError("Backend evidence snapshot has no safety record.")
    for field in SNAPSHOT_FALSE_SAFETY_FIELDS:
        if safety.get(field) is not False:
            raise AIBackendEvidenceError(
                "Backend evidence snapshot must explicitly disable {}.".format(field)
            )


def _snapshot_report(snapshot: Mapping[str, Any]) -> str:
    readiness = snapshot["readiness"]
    observation = snapshot["observation"]
    lines = [
        "# GenomeAgent AI Backend Evidence",
        "",
        "- Backend: `{}`".format(snapshot["backend_id"]),
        "- Host: `{}`".format(snapshot["host"]),
        "- Evidence ID: `{}`".format(snapshot["evidence_id"]),
        "- Status: **{}**".format(readiness["status"]),
        "- Next safe action: `{}`".format(readiness["next_safe_action"]),
        "- Automatic execution: **no**",
        "",
        "## Environment checks",
        "",
        "| Check | Status | Expected | Observed |",
        "|---|---|---|---|",
    ]
    for item in readiness["environment_checks"]:
        lines.append("| {} | {} | `{}` | `{}` |".format(
            item["check"], item["status"],
            json.dumps(item["expected"], sort_keys=True),
            json.dumps(item["observed"], sort_keys=True),
        ))
    lines.extend(["", "## Blockers", ""])
    if readiness["blockers"]:
        lines.extend("- `{}`".format(item) for item in readiness["blockers"])
    else:
        lines.append("No evidence blockers are present; researcher review is still required.")
    model = observation.get("model", {})
    if isinstance(model, Mapping):
        lines.extend([
            "",
            "## Model observation boundary",
            "",
            "- Model path exists: **{}**".format("yes" if model.get("exists") else "no"),
            "- Inventory depth: {}".format(model.get("scan_depth", 0)),
            "- Large model files hashed: **no**",
        ])
    lines.extend([
        "",
        "## Safety boundary",
        "",
        "This observation did not write remotely, submit or allocate a job, download or "
        "import a model, recursively scan model storage, or hash large weight files.",
        "",
    ])
    return "\n".join(lines)


def _state_report(state: Mapping[str, Any]) -> str:
    lines = [
        "# GenomeAgent AI Backend State",
        "",
        "- Backend: `{}`".format(state["backend_id"]),
        "- Source snapshots: {}".format(state["source_snapshots"]),
        "- Latest evidence: `{}`".format(state["latest_evidence_id"]),
        "- Status: **{}**".format(state["readiness"]["status"]),
        "- Next safe action: `{}`".format(state["readiness"]["next_safe_action"]),
        "- Automatic execution: **no**",
        "",
        "## Current blockers",
        "",
    ]
    blockers = state["readiness"]["blockers"]
    if blockers:
        lines.extend("- `{}`".format(item) for item in blockers)
    else:
        lines.append("No evidence blockers are present; researcher review is still required.")
    lines.extend([
        "",
        "## Interpretation boundary",
        "",
        "The current state is deterministically reconstructed from immutable read-only "
        "snapshots. It does not edit the backend registry or grant model-download, GPU, "
        "scheduler or execution authority.",
        "",
    ])
    return "\n".join(lines)


class AIBackendEvidenceCollector:
    """Collect one bounded immutable AI backend observation over SSH."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/evidence"),
        evidence_root: Path = Path("workspace/ai_backend_evidence"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.evidence_root = Path(evidence_root)

    def policy(self, backend_id: str) -> tuple[Path, dict[str, Any], bytes]:
        backend_id = _identifier(backend_id, "backend_id")
        path = self.policy_root / (backend_id + ".json")
        policy, raw = _validate_policy(path, expected_backend_id=backend_id)
        return path, policy, raw

    def collect(
        self,
        backend_id: str,
        runner: PythonRemoteRunner,
        stamp: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> AIBackendCollectionResult:
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        _validate_backend_fields(backend)
        policy_path, policy, policy_raw = self.policy(str(backend["backend_id"]))
        configured_host = str(backend.get("ssh_host") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", configured_host):
            raise AIBackendEvidenceError("Unsafe configured SSH host.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(runner.host)):
            raise AIBackendEvidenceError("Unsafe runner SSH host.")
        timeout = (
            int(timeout_seconds)
            if timeout_seconds is not None
            else int(policy["limits"]["timeout_seconds"])
        )
        if not 5 <= timeout <= 240:
            raise AIBackendEvidenceError("Collection timeout must be between 5 and 240 seconds.")
        now = datetime.now(timezone.utc)
        observed_at = now.isoformat(timespec="seconds").replace("+00:00", "Z")
        evidence_id = stamp or now.strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", evidence_id):
            raise AIBackendEvidenceError("Unsafe or invalid evidence timestamp.")
        backend_dir = self.evidence_root / str(backend["backend_id"])
        evidence_path = backend_dir / (evidence_id + ".json")
        report_path = backend_dir / (evidence_id + ".md")
        if evidence_path.exists() or report_path.exists():
            raise AIBackendEvidenceError(
                "Refusing to overwrite immutable backend evidence: {}".format(
                    evidence_id
                )
            )
        try:
            observation = runner.run_python(
                _probe_program(backend, policy), timeout_seconds=timeout
            )
        except Exception as exc:
            if isinstance(exc, AIBackendEvidenceError):
                raise
            raise AIBackendEvidenceError(
                "Read-only backend observation failed on {}: {}".format(
                    runner.host, exc
                )
            ) from exc
        if not isinstance(observation, Mapping):
            raise AIBackendEvidenceError("Remote backend observation must be an object.")
        observation = dict(observation)
        _validate_remote_observation(observation, str(backend["backend_id"]))
        sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "ai_backend_evidence_policy"),
        ]
        readiness = _interpret(backend, policy, observation, current_config=True)
        snapshot = {
            "schema_version": "1.0",
            "policy_version": AI_BACKEND_EVIDENCE_POLICY_VERSION,
            "source_mode": "bounded_read_only_backend_observation",
            "backend_id": backend["backend_id"],
            "environment_id": backend["environment_id"],
            "execution_domain": backend["execution_domain"],
            "host": str(runner.host),
            "configured_host": configured_host,
            "evidence_id": evidence_id,
            "observed_at_utc": observed_at,
            "query": {
                "single_ssh_session": True,
                "timeout_seconds": timeout,
                "model_inventory_depth": 1,
                "maximum_model_entries": policy["limits"]["maximum_model_entries"],
            },
            "source_artifacts": sources,
            "observation": observation,
            "readiness": readiness,
            "safety": {
                "remote_writes": False,
                "job_submission": False,
                "gpu_allocation": False,
                "model_download": False,
                "model_import": False,
                "recursive_model_scan": False,
                "large_model_file_hashing": False,
                "automatic_registry_update": False,
            },
        }
        _write_immutable(evidence_path, _pretty_json(snapshot))
        _write_immutable(report_path, _snapshot_report(snapshot))
        return AIBackendCollectionResult(
            backend_id=str(backend["backend_id"]),
            host=str(runner.host),
            evidence_path=evidence_path,
            evidence_id=evidence_id,
            environment_status=str(readiness["status"]),
        )


class AIBackendEvidenceCore:
    """Replay immutable backend observations into deterministic current state."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/evidence"),
        evidence_root: Path = Path("workspace/ai_backend_evidence"),
        state_root: Path = Path("workspace/ai_backend_state"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.evidence_root = Path(evidence_root)
        self.state_root = Path(state_root)

    def ingest(self, backend_id: str) -> AIBackendIngestResult:
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        _validate_backend_fields(backend)
        policy_path = self.policy_root / (str(backend["backend_id"]) + ".json")
        policy, policy_raw = _validate_policy(
            policy_path, expected_backend_id=str(backend["backend_id"])
        )
        current_sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "ai_backend_evidence_policy"),
        ]
        paths = sorted(
            (self.evidence_root / str(backend["backend_id"])).glob("*.json")
        )
        if not paths:
            raise AIBackendEvidenceError(
                "No backend evidence snapshots found for {}.".format(backend["backend_id"])
            )
        records = []
        for path in paths:
            payload, raw = _read_json(path, "AI backend evidence snapshot")
            _validate_snapshot(payload, path, str(backend["backend_id"]))
            observation = payload.get("observation")
            if not isinstance(observation, Mapping):
                raise AIBackendEvidenceError("Backend evidence contains no observation object.")
            _validate_remote_observation(observation, str(backend["backend_id"]))
            source_artifacts = payload.get("source_artifacts")
            current_config = source_artifacts == current_sources
            readiness = _interpret(backend, policy, observation, current_config)
            records.append({
                "evidence_id": str(payload.get("evidence_id") or path.stem),
                "observed_at_utc": str(payload.get("observed_at_utc") or ""),
                "host": str(payload.get("host") or ""),
                "source_path": str(path),
                "source_sha256": _sha256_bytes(raw),
                "configuration_current": current_config,
                "readiness": readiness,
                "observation": dict(observation),
            })
        records.sort(key=lambda item: (item["observed_at_utc"], item["evidence_id"]))
        latest = records[-1]
        state = {
            "schema_version": "1.0",
            "policy_version": AI_BACKEND_EVIDENCE_POLICY_VERSION,
            "backend_id": backend["backend_id"],
            "source_snapshots": len(records),
            "latest_evidence_id": latest["evidence_id"],
            "latest_observed_at_utc": latest["observed_at_utc"],
            "latest_source_sha256": latest["source_sha256"],
            "host": latest["host"],
            "readiness": latest["readiness"],
            "observation": latest["observation"],
            "automatic_execution_allowed": False,
            "automatic_registry_update_allowed": False,
        }
        history = [
            {
                key: value for key, value in record.items() if key != "observation"
            }
            for record in records
        ]
        provenance = {
            "schema_version": "1.0",
            "policy_version": AI_BACKEND_EVIDENCE_POLICY_VERSION,
            "backend_id": backend["backend_id"],
            "source_artifacts": current_sources,
            "evidence_snapshots": [
                {
                    "evidence_id": record["evidence_id"],
                    "path": record["source_path"],
                    "sha256": record["source_sha256"],
                }
                for record in records
            ],
            "derivation": "deterministic_local_backend_evidence_replay",
            "remote_access_during_ingest": False,
            "job_submission": False,
            "model_download": False,
            "automatic_registry_update": False,
        }
        state_dir = self.state_root / str(backend["backend_id"])
        artifact_paths = (
            state_dir / "current_evidence.json",
            state_dir / "evidence_history.jsonl",
            state_dir / "readiness.json",
            state_dir / "report.md",
            state_dir / "provenance.json",
        )
        contents = (
            _pretty_json(state),
            "".join(
                json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n"
                for item in history
            ),
            _pretty_json(latest["readiness"]),
            _state_report(state),
            _pretty_json(provenance),
        )
        for path, text in zip(artifact_paths, contents):
            _atomic_write(path, text)
        readiness = latest["readiness"]
        return AIBackendIngestResult(
            backend_id=str(backend["backend_id"]),
            state_dir=state_dir,
            snapshots=len(records),
            status=str(readiness["status"]),
            next_safe_action=str(readiness["next_safe_action"]),
            blockers=tuple(str(item) for item in readiness["blockers"]),
            artifact_paths=artifact_paths,
        )
