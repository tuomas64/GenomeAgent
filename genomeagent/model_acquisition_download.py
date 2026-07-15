#!/usr/bin/env python3
"""Explicit authorization and confined launch of a public model download."""

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
from genomeagent.model_acquisition_preflight import (
    BUNDLE_ARTIFACT_NAMES,
    _load_bundle,
)


MODEL_ACQUISITION_DOWNLOAD_POLICY_VERSION = "1.0"
DOWNLOAD_FALSE_SAFETY_FIELDS = (
    "automatic_authorization_allowed",
    "automatic_remote_execution_allowed",
    "automatic_model_download_allowed",
    "credential_use_allowed",
    "project_data_egress_allowed",
    "remote_deletion_allowed",
    "model_hashing_allowed",
    "publication_allowed",
    "job_submission_allowed",
    "gpu_allocation_allowed",
    "inference_allowed",
    "training_allowed",
    "registry_update_allowed",
    "backend_activation_allowed",
)
ALLOWED_REMOTE_MUTATIONS = (
    "create_confined_control_directory",
    "write_confined_execution_metadata_and_logs",
    "create_confined_model_parent_directories",
    "create_exact_bundle_staging_directory",
    "download_exact_public_repository_revision_into_staging",
)


class ModelAcquisitionDownloadError(RuntimeError):
    """Raised when download authorization or execution is unsafe."""


class PythonRemoteRunner(Protocol):
    host: str

    def run_python(self, program: str, timeout_seconds: int = 240) -> dict[str, Any]:
        """Run one remote Python action and return a JSON object."""


@dataclass(frozen=True)
class DownloadAuthorizationResult:
    backend_id: str
    bundle_id: str
    evidence_id: str
    authorization_id: str
    authorization_path: Path
    created: bool
    status: str
    expires_at_utc: str


@dataclass(frozen=True)
class DownloadLaunchResult:
    backend_id: str
    bundle_id: str
    authorization_id: str
    execution_id: str
    launch_path: Path
    created: bool
    remote_status: str
    control_directory: str


@dataclass(frozen=True)
class DownloadStatusResult:
    backend_id: str
    bundle_id: str
    execution_id: str
    observation_id: str
    observation_path: Path
    status: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


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
            raise ModelAcquisitionDownloadError(
                "Immutable download artifact exists with different content: {}".format(path)
            )
        return False
    _atomic_write(path, text)
    return True


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink():
        raise ModelAcquisitionDownloadError("Refusing symlinked {}: {}".format(label, path))
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ModelAcquisitionDownloadError("Missing {}: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelAcquisitionDownloadError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise ModelAcquisitionDownloadError("{} must be a JSON object.".format(label))
    return value, raw


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise ModelAcquisitionDownloadError("Unsafe or empty {}.".format(label))
    return normalized


def _digest(value: Any, label: str, length: int = 64) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{{{}}}".format(length), normalized):
        raise ModelAcquisitionDownloadError("Invalid {}.".format(label))
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
        raise ModelAcquisitionDownloadError("Unsafe {}.".format(label))
    return text


def _within(path_text: str, root_text: str) -> bool:
    try:
        Path(path_text).relative_to(Path(root_text))
    except ValueError:
        return False
    return Path(path_text) != Path(root_text)


def _reviewer(value: Any) -> str:
    reviewer = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,127}", reviewer):
        raise ModelAcquisitionDownloadError("Unsafe reviewer identifier.")
    return reviewer


def _false_safety(value: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(value, Mapping):
        raise ModelAcquisitionDownloadError("{} has no safety object.".format(label))
    for field in fields:
        if value.get(field) is not False:
            raise ModelAcquisitionDownloadError("{} must disable {}.".format(label, field))


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {"artifact_type": artifact_type, "path": str(path), "sha256": _sha256_bytes(raw)}


def _parse_utc(value: Any, label: str) -> datetime:
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ModelAcquisitionDownloadError("Invalid {}.".format(label)) from exc


def _validate_policy(
    path: Path, expected_backend_id: Optional[str] = None
) -> tuple[dict[str, Any], bytes]:
    policy, raw = _read_json(path, "download execution policy")
    backend_id = _identifier(policy.get("backend_id"), "backend_id")
    if path.stem != backend_id or (
        expected_backend_id is not None and backend_id != expected_backend_id
    ):
        raise ModelAcquisitionDownloadError("Download policy backend identity mismatch.")
    if policy.get("schema_version") != "1.0" or policy.get(
        "policy_version"
    ) != MODEL_ACQUISITION_DOWNLOAD_POLICY_VERSION:
        raise ModelAcquisitionDownloadError("Unsupported download policy version.")
    remote_python = str(policy.get("remote_python") or "")
    if not re.fullmatch(r"[A-Za-z0-9_./+-]+", remote_python):
        raise ModelAcquisitionDownloadError("Unsafe remote Python executable.")
    validity = policy.get("authorization_validity_seconds")
    if isinstance(validity, bool) or not isinstance(validity, int) or not 300 <= validity <= 900:
        raise ModelAcquisitionDownloadError("Download authorization validity must be 300 to 900 seconds.")
    control_root = _absolute_path(policy.get("control_root"), "control root")
    if not control_root.startswith("/scratch/project_"):
        raise ModelAcquisitionDownloadError("Control root must be under CSC project scratch.")
    _absolute_path(policy.get("module_initialization"), "module initialization")
    module_paths = policy.get("module_use_paths")
    if not isinstance(module_paths, list) or not module_paths or len(module_paths) > 10:
        raise ModelAcquisitionDownloadError("Invalid module_use_paths.")
    for item in module_paths:
        if not re.fullmatch(r"/[A-Za-z0-9_./+-]+", _absolute_path(item, "module path")):
            raise ModelAcquisitionDownloadError("Unsafe module path.")
    download = policy.get("download")
    expected = {
        "method": "huggingface_hub.snapshot_download",
        "public_repository_only": True,
        "token_mode": "explicitly_disabled",
        "max_workers": 2,
        "background_process": True,
        "staging_only": True,
        "hash_during_download": False,
        "publish_after_download": False,
    }
    if download != expected:
        raise ModelAcquisitionDownloadError("Unsupported or unsafe download contract.")
    if tuple(policy.get("allowed_remote_mutations") or ()) != ALLOWED_REMOTE_MUTATIONS:
        raise ModelAcquisitionDownloadError("Remote mutation allow-list is invalid.")
    _false_safety(policy.get("safety"), DOWNLOAD_FALSE_SAFETY_FIELDS, "Download policy")
    return policy, raw


def _bundle_sources(
    bundle_root: Path, backend_id: str, bundle_id: str
) -> list[dict[str, Any]]:
    sources = []
    for name in BUNDLE_ARTIFACT_NAMES:
        path = bundle_root / backend_id / bundle_id / name
        if path.is_symlink():
            raise ModelAcquisitionDownloadError("Refusing symlinked bundle artifact.")
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise ModelAcquisitionDownloadError("Missing bundle artifact {}.".format(name)) from exc
        sources.append(_source(path, raw, "model_acquisition_bundle_artifact"))
    return sources


def _preflight_evidence(
    backend_id: str,
    bundle_id: str,
    evidence_id: str,
    preflight_evidence_root: Path,
    preflight_state_root: Path,
    now: datetime,
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]]:
    state_dir = preflight_state_root / backend_id / bundle_id
    state_path = state_dir / "current_evidence.json"
    readiness_path = state_dir / "readiness.json"
    provenance_path = state_dir / "provenance.json"
    state, state_raw = _read_json(state_path, "preflight current state")
    readiness, readiness_raw = _read_json(readiness_path, "preflight readiness")
    provenance, provenance_raw = _read_json(provenance_path, "preflight provenance")
    if (
        state.get("backend_id") != backend_id
        or state.get("bundle_id") != bundle_id
        or state.get("latest_evidence_id") != evidence_id
        or readiness.get("status") != "ready_for_execution_authorization_review"
        or readiness.get("blockers") != ["fresh_execution_authorization_missing"]
        or readiness.get("automatic_execution_allowed") is not False
        or readiness.get("evidence_fresh") is not True
        or readiness.get("inputs_current") is not True
    ):
        raise ModelAcquisitionDownloadError("Preflight state is not authorization-ready.")
    expires = _parse_utc(state.get("latest_expires_at_utc"), "preflight expiry")
    if now > expires:
        raise ModelAcquisitionDownloadError("Preflight evidence has expired; collect and ingest again.")
    evidence_path = preflight_evidence_root / backend_id / bundle_id / (evidence_id + ".json")
    evidence, evidence_raw = _read_json(evidence_path, "preflight evidence")
    if (
        evidence.get("source_mode") != "bounded_read_only_acquisition_runtime_observation"
        or evidence.get("backend_id") != backend_id
        or evidence.get("bundle_id") != bundle_id
        or evidence.get("evidence_id") != evidence_id
        or _sha256_bytes(evidence_raw) != state.get("latest_source_sha256")
    ):
        raise ModelAcquisitionDownloadError("Preflight evidence digest or identity mismatch.")
    evidence_expiry = _parse_utc(evidence.get("expires_at_utc"), "preflight evidence expiry")
    evidence_readiness = evidence.get("readiness")
    if (
        evidence.get("expires_at_utc") != state.get("latest_expires_at_utc")
        or now > evidence_expiry
        or not isinstance(evidence_readiness, Mapping)
        or evidence_readiness.get("status") != "ready_for_execution_authorization_review"
        or evidence_readiness.get("blockers") != ["fresh_execution_authorization_missing"]
    ):
        raise ModelAcquisitionDownloadError("Preflight evidence is expired or not authorization-ready.")
    if provenance.get("backend_id") != backend_id or provenance.get("bundle_id") != bundle_id:
        raise ModelAcquisitionDownloadError("Preflight provenance identity mismatch.")
    provenance_records = provenance.get("evidence_snapshots")
    if not isinstance(provenance_records, list) or not any(
        isinstance(item, Mapping)
        and item.get("evidence_id") == evidence_id
        and item.get("sha256") == _sha256_bytes(evidence_raw)
        for item in provenance_records
    ):
        raise ModelAcquisitionDownloadError("Preflight provenance does not bind current evidence.")
    sources = [
        _source(state_path, state_raw, "model_acquisition_preflight_state"),
        _source(readiness_path, readiness_raw, "model_acquisition_preflight_readiness"),
        _source(provenance_path, provenance_raw, "model_acquisition_preflight_provenance"),
        _source(evidence_path, evidence_raw, "model_acquisition_preflight_evidence"),
    ]
    return state, evidence_raw, sources


def _authorization_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "policy_version": value.get("policy_version"),
        "backend_id": value.get("backend_id"),
        "bundle_id": value.get("bundle_id"),
        "bundle_artifact_sha256": value.get("bundle_artifact_sha256"),
        "source_artifact_sha256": value.get("source_artifact_sha256"),
        "preflight_evidence_id": value.get("preflight_evidence_id"),
        "preflight_evidence_sha256": value.get("preflight_evidence_sha256"),
        "reviewer": value.get("reviewer"),
        "authorization_scope": value.get("authorization_scope"),
        "remote_host": value.get("remote_host"),
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
        or value.get("policy_version") != MODEL_ACQUISITION_DOWNLOAD_POLICY_VERSION
        or value.get("source_mode") != "explicit_researcher_public_model_download_authorization"
        or value.get("authorization_id") != authorization_id
        or value.get("backend_id") != backend_id
        or value.get("bundle_id") != bundle_id
        or value.get("authorization_scope") != "launch_exact_public_revision_into_bundle_staging_only"
        or tuple(value.get("allowed_remote_mutations") or ()) != ALLOWED_REMOTE_MUTATIONS
    ):
        raise ModelAcquisitionDownloadError("Download authorization identity or scope is invalid.")
    _reviewer(value.get("reviewer"))
    _digest(value.get("preflight_evidence_sha256"), "preflight evidence digest")
    artifacts = value.get("bundle_artifact_sha256")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(BUNDLE_ARTIFACT_NAMES):
        raise ModelAcquisitionDownloadError("Authorization bundle digest set is invalid.")
    for digest in artifacts.values():
        _digest(digest, "bundle artifact digest")
    source_digests = value.get("source_artifact_sha256")
    if not isinstance(source_digests, Mapping) or not source_digests:
        raise ModelAcquisitionDownloadError("Authorization source digest set is invalid.")
    for digest in source_digests.values():
        _digest(digest, "source artifact digest")
    if _sha256_value(_authorization_identity(value)) != authorization_id:
        raise ModelAcquisitionDownloadError("Download authorization content digest mismatch.")
    if _sha256_value(_authorization_record_identity(value)) != value.get(
        "authorization_record_sha256"
    ):
        raise ModelAcquisitionDownloadError("Download authorization record digest mismatch.")
    _parse_utc(value.get("authorized_at_utc"), "authorization time")
    _parse_utc(value.get("expires_at_utc"), "authorization expiry")
    _false_safety(value.get("safety"), DOWNLOAD_FALSE_SAFETY_FIELDS, "Authorization")


def _launcher_program(
    backend: Mapping[str, Any],
    policy: Mapping[str, Any],
    bundle: Mapping[str, Any],
    authorization: Mapping[str, Any],
    execution_id: str,
) -> str:
    control_dir = str(Path(policy["control_root"]) / execution_id)
    config = {
        "backend_id": backend["backend_id"],
        "bundle_id": bundle["bundle_id"],
        "authorization_id": authorization["authorization_id"],
        "execution_id": execution_id,
        "repository": bundle["repository"],
        "resolved_revision": bundle["resolved_revision"],
        "project_storage_root": bundle["target"]["project_storage_root"],
        "staging_path": bundle["target"]["staging_path"],
        "installation_path": bundle["target"]["installation_path"],
        "control_root": policy["control_root"],
        "control_directory": control_dir,
        "runtime_module": backend["runtime"]["module"],
        "module_initialization": policy["module_initialization"],
        "module_use_paths": policy["module_use_paths"],
        "max_workers": policy["download"]["max_workers"],
        "expected_file_count": bundle["integrity_contract"]["expected_file_count"],
        "expected_total_bytes": bundle["integrity_contract"]["expected_total_bytes"],
    }
    encoded = repr(json.dumps(config, sort_keys=True))
    program = r"""import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_DOWNLOAD_CONFIG__)


def fail(message):
    print(json.dumps({"schema_version":"1.0", "status":"launch_rejected",
        "execution_id":CONFIG["execution_id"], "error":str(message)[:2000]}, sort_keys=True))
    raise SystemExit(2)


def confined(path_text, root_text):
    try:
        Path(path_text).relative_to(Path(root_text))
    except ValueError:
        return False
    return Path(path_text) != Path(root_text)


def ensure_directory(path_text, root_text):
    root_path = Path(root_text)
    path = Path(path_text)
    try:
        relative = path.relative_to(root_path)
    except ValueError:
        fail("directory creation escaped project storage")
    if root_path.is_symlink() or not root_path.is_dir():
        fail("project storage root is absent, symlinked or not a directory")
    current = root_path
    for part in relative.parts:
        current = current / part
        if os.path.lexists(str(current)):
            if current.is_symlink() or not current.is_dir():
                fail("existing path component is symlinked or not a directory")
        else:
            current.mkdir(mode=0o770)


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


root = CONFIG["project_storage_root"]
staging = CONFIG["staging_path"]
target = CONFIG["installation_path"]
control_root = CONFIG["control_root"]
control = Path(CONFIG["control_directory"])
if not all(confined(item, root) for item in (staging, target, control_root, str(control))):
    fail("configured paths are not confined to project storage")
if Path(staging).parent != Path(target).parent:
    fail("staging and target do not share a parent")
if os.path.lexists(target):
    fail("installation target already exists")

execution_spec = {key:value for key,value in CONFIG.items() if key not in {
    "module_initialization", "module_use_paths", "runtime_module"
}}
ensure_directory(str(Path(control_root).parent), root)
if os.path.lexists(control_root):
    if Path(control_root).is_symlink() or not Path(control_root).is_dir():
        fail("control root is symlinked or not a directory")
else:
    Path(control_root).mkdir(mode=0o770)
if os.path.lexists(str(control)):
    if control.is_symlink() or not control.is_dir():
        fail("existing control path is symlinked or not a directory")
    existing_path = control / "execution.json"
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except Exception:
        fail("existing control directory is not a valid matching execution")
    if existing != execution_spec:
        fail("existing control directory belongs to different execution content")
    status_path = control / "status.json"
    status = json.loads(status_path.read_text()) if status_path.is_file() else {"status":"launch_recorded"}
    print(json.dumps({"schema_version":"1.0", "status":"already_launched",
        "execution_id":CONFIG["execution_id"], "control_directory":str(control),
        "worker_status":status.get("status")}, sort_keys=True))
    raise SystemExit(0)

control.mkdir(mode=0o770)
if os.path.lexists(staging):
    fail("staging path already exists")
ensure_directory(str(Path(staging).parent), root)
Path(staging).mkdir(mode=0o770)
atomic_json(control / "execution.json", execution_spec)

worker = r'''import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import snapshot_download

spec_path = Path(sys.argv[1])
spec = json.loads(spec_path.read_text(encoding="utf-8"))
control = spec_path.parent
staging = Path(spec["staging_path"])
target = Path(spec["installation_path"])


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_status(value):
    path = control / "status.json"
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


try:
    if target.exists() or target.is_symlink():
        raise RuntimeError("installation target appeared before download")
    if not staging.is_dir() or staging.is_symlink():
        raise RuntimeError("staging directory is absent or symlinked")
    write_status({"schema_version":"1.0", "status":"download_running",
        "execution_id":spec["execution_id"], "started_at_utc":utc(),
        "model_hashing":False, "publication":False})
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    result = snapshot_download(
        repo_id=spec["repository"], revision=spec["resolved_revision"],
        local_dir=str(staging), token=False, max_workers=int(spec["max_workers"]),
    )
    write_status({"schema_version":"1.0", "status":"download_completed_unverified",
        "execution_id":spec["execution_id"], "finished_at_utc":utc(),
        "provider_result_path":str(result), "model_hashing":False,
        "publication":False})
except BaseException as exc:
    write_status({"schema_version":"1.0", "status":"download_failed",
        "execution_id":spec.get("execution_id"), "failed_at_utc":utc(),
        "error_type":type(exc).__name__, "error":str(exc)[:2000],
        "model_hashing":False, "publication":False})
    traceback.print_exc()
    raise
'''
worker_path = control / "download_worker.py"
with worker_path.open("x", encoding="utf-8") as handle:
    handle.write(worker)
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(worker_path, stat.S_IRUSR | stat.S_IWUSR)

module_env = dict(os.environ)
module_env.update({
    "MODULE_INIT":CONFIG["module_initialization"],
    "MODULE_NAME":CONFIG["runtime_module"],
    "MODULE_USE_PATHS":":".join(CONFIG["module_use_paths"]),
    "WORKER_PATH":str(worker_path),
    "SPEC_PATH":str(control / "execution.json"),
})
module_env.pop("HF_TOKEN", None)
module_env.pop("HUGGING_FACE_HUB_TOKEN", None)
shell = ('source "$MODULE_INIT" >/dev/null 2>&1 || exit 90; '
         'old_ifs=$IFS; IFS=:; for p in $MODULE_USE_PATHS; do '
         'module use "$p" >/dev/null 2>&1 || exit 91; done; IFS=$old_ifs; '
         'module load "$MODULE_NAME" >/dev/null 2>&1 || exit 92; '
         'exec python3 "$WORKER_PATH" "$SPEC_PATH"')
log = (control / "download.log").open("ab", buffering=0)
process = subprocess.Popen(["bash", "-c", shell], stdin=subprocess.DEVNULL,
    stdout=log, stderr=subprocess.STDOUT, cwd=str(control), env=module_env,
    start_new_session=True, close_fds=True)
launch = {"schema_version":"1.0", "status":"download_launched",
    "execution_id":CONFIG["execution_id"], "pid":process.pid,
    "control_directory":str(control), "staging_path":staging,
    "installation_path":target, "model_hashing":False, "publication":False,
    "job_submission":False, "gpu_allocation":False}
atomic_json(control / "launch.json", launch)
print(json.dumps(launch, sort_keys=True))
"""
    return program.replace("__GENOMEAGENT_DOWNLOAD_CONFIG__", encoded)


def _status_program(backend_id: str, bundle_id: str, execution_id: str, control_dir: str) -> str:
    config = repr(json.dumps({"backend_id": backend_id, "bundle_id": bundle_id,
        "execution_id": execution_id, "control_directory": control_dir}, sort_keys=True))
    program = r'''import json
import os
from pathlib import Path

CONFIG = json.loads(__GENOMEAGENT_STATUS_CONFIG__)
control = Path(CONFIG["control_directory"])


def small_json(path):
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1048576:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


status = small_json(control / "status.json")
launch = small_json(control / "launch.json")
execution = small_json(control / "execution.json")
log_tail = ""
log_path = control / "download.log"
try:
    if log_path.is_file() and not log_path.is_symlink():
        with log_path.open("rb") as handle:
            handle.seek(max(0, log_path.stat().st_size - 4000))
            log_tail = handle.read(4000).decode("utf-8", errors="replace")
except OSError:
    pass
result = {"schema_version":"1.0", "observation_type":"model_download_status",
    "backend_id":CONFIG["backend_id"], "bundle_id":CONFIG["bundle_id"],
    "execution_id":CONFIG["execution_id"], "control_directory":str(control),
    "control_exists":control.is_dir(), "control_is_symlink":control.is_symlink(),
    "execution":execution, "launch":launch, "worker_status":status,
    "log_tail":log_tail,
    "safety":{"remote_writes":False, "provider_requests":False,
        "model_download_started":False, "job_submission":False,
        "gpu_allocation":False, "model_file_reads":False,
        "large_file_hashing":False, "publication":False}}
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
'''
    return program.replace("__GENOMEAGENT_STATUS_CONFIG__", config)


class ControlledModelDownloadCore:
    """Authorize, launch and observe only the staging download phase."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        policy_root: Path = Path("config/ai/acquisition_execution"),
        bundle_root: Path = Path("workspace/model_acquisition_bundles"),
        plan_root: Path = Path("workspace/model_acquisition_plans"),
        preflight_evidence_root: Path = Path("workspace/model_acquisition_preflight_evidence"),
        preflight_state_root: Path = Path("workspace/model_acquisition_preflight_state"),
        authorization_root: Path = Path("workspace/model_acquisition_download_authorizations"),
        execution_root: Path = Path("workspace/model_acquisition_downloads"),
    ):
        self.registry = registry or AIRegistry()
        self.policy_root = Path(policy_root)
        self.bundle_root = Path(bundle_root)
        self.plan_root = Path(plan_root)
        self.preflight_evidence_root = Path(preflight_evidence_root)
        self.preflight_state_root = Path(preflight_state_root)
        self.authorization_root = Path(authorization_root)
        self.execution_root = Path(execution_root)

    def policy(self, backend_id: str) -> tuple[Path, dict[str, Any], bytes]:
        backend_id = _identifier(backend_id, "backend_id")
        path = self.policy_root / (backend_id + ".json")
        policy, raw = _validate_policy(path, backend_id)
        return path, policy, raw

    def _inputs(self, backend_id: str, bundle_id: str, evidence_id: str, now: datetime):
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        backend_id = _identifier(backend.get("backend_id"), "backend_id")
        bundle_id = _digest(bundle_id, "bundle ID")
        policy_path, policy, policy_raw = self.policy(backend_id)
        bundle, _, _ = _load_bundle(
            backend_id, bundle_id, self.bundle_root, self.plan_root, backend
        )
        sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(policy_path, policy_raw, "model_download_execution_policy"),
            *_bundle_sources(self.bundle_root, backend_id, bundle_id),
        ]
        state, evidence_raw, preflight_sources = _preflight_evidence(
            backend_id, bundle_id, evidence_id, self.preflight_evidence_root,
            self.preflight_state_root, now,
        )
        sources.extend(preflight_sources)
        project_root = bundle["target"]["project_storage_root"]
        control_root = policy["control_root"]
        if not _within(control_root, project_root):
            raise ModelAcquisitionDownloadError("Control root is outside bundle project storage.")
        if str(backend.get("ssh_host") or "") != "roihu-gpu":
            raise ModelAcquisitionDownloadError("Download policy is bound to roihu-gpu.")
        return backend_id, backend, policy, bundle, state, evidence_raw, sources

    def authorize(
        self,
        backend_id: str,
        bundle_id: str,
        evidence_id: str,
        reviewer: str,
        confirmed: bool,
        now: Optional[datetime] = None,
    ) -> DownloadAuthorizationResult:
        if not confirmed:
            raise ModelAcquisitionDownloadError("Explicit --confirm-public-model-download is required.")
        reviewer = _reviewer(reviewer)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ModelAcquisitionDownloadError("Authorization time must include timezone.")
        current = current.astimezone(timezone.utc)
        backend_id, backend, policy, bundle, state, evidence_raw, sources = self._inputs(
            backend_id, bundle_id, evidence_id, current
        )
        bundle_digests = {
            Path(item["path"]).name: item["sha256"]
            for item in sources
            if item["artifact_type"] == "model_acquisition_bundle_artifact"
        }
        identity = {
            "policy_version": MODEL_ACQUISITION_DOWNLOAD_POLICY_VERSION,
            "backend_id": backend_id, "bundle_id": bundle["bundle_id"],
            "bundle_artifact_sha256": bundle_digests,
            "source_artifact_sha256": {
                "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
                for item in sources
            },
            "preflight_evidence_id": evidence_id,
            "preflight_evidence_sha256": _sha256_bytes(evidence_raw),
            "reviewer": reviewer,
            "authorization_scope": "launch_exact_public_revision_into_bundle_staging_only",
            "remote_host": backend["ssh_host"],
            "allowed_remote_mutations": list(ALLOWED_REMOTE_MUTATIONS),
        }
        authorization_id = _sha256_value(identity)
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        report_path = path.with_suffix(".md")
        if path.exists():
            existing, _ = _read_json(path, "download authorization")
            _validate_authorization(existing, authorization_id, backend_id, bundle["bundle_id"])
            return DownloadAuthorizationResult(
                backend_id, bundle["bundle_id"], evidence_id, authorization_id, path,
                False, "public_model_download_already_authorized",
                str(existing["expires_at_utc"]),
            )
        preflight_expiry = _parse_utc(state["latest_expires_at_utc"], "preflight expiry")
        expiry = min(
            preflight_expiry,
            current + timedelta(seconds=int(policy["authorization_validity_seconds"])),
        )
        authorization = {
            "schema_version": "1.0",
            "source_mode": "explicit_researcher_public_model_download_authorization",
            "authorization_id": authorization_id,
            **identity,
            "authorized_at_utc": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at_utc": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_artifacts": sources,
            "safety": {field: False for field in DOWNLOAD_FALSE_SAFETY_FIELDS},
        }
        authorization["authorization_record_sha256"] = _sha256_value(
            _authorization_record_identity(authorization)
        )
        _validate_authorization(authorization, authorization_id, backend_id, bundle["bundle_id"])
        created = _write_immutable(path, _pretty_json(authorization))
        report = "\n".join([
            "# GenomeAgent Public Model Download Authorization", "",
            "- Backend: `{}`".format(backend_id),
            "- Bundle: `{}`".format(bundle["bundle_id"]),
            "- Authorization: `{}`".format(authorization_id),
            "- Reviewer: `{}`".format(reviewer),
            "- Expires: `{}`".format(authorization["expires_at_utc"]),
            "- Scope: **public pinned revision into staging only**", "",
            "Hashing, publication, Slurm, GPU use, inference and training remain forbidden.", "",
        ])
        _write_immutable(report_path, report)
        return DownloadAuthorizationResult(
            backend_id, bundle["bundle_id"], evidence_id, authorization_id, path,
            created, "public_model_download_authorized", authorization["expires_at_utc"],
        )

    def _load_current_authorization(
        self, backend_id: str, bundle_id: str, authorization_id: str, now: datetime
    ):
        backend_id = _identifier(backend_id, "backend_id")
        bundle_id = _digest(bundle_id, "bundle ID")
        authorization_id = _digest(authorization_id, "authorization ID")
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        authorization, raw = _read_json(path, "download authorization")
        _validate_authorization(authorization, authorization_id, backend_id, bundle_id)
        if now > _parse_utc(authorization["expires_at_utc"], "authorization expiry"):
            raise ModelAcquisitionDownloadError("Download authorization expired; collect fresh preflight evidence.")
        evidence_id = str(authorization["preflight_evidence_id"])
        inputs = self._inputs(backend_id, bundle_id, evidence_id, now)
        _, backend, policy, bundle, _, evidence_raw, sources = inputs
        current_bundle = {
            Path(item["path"]).name: item["sha256"]
            for item in sources
            if item["artifact_type"] == "model_acquisition_bundle_artifact"
        }
        current_sources = {
            "{}|{}".format(item["artifact_type"], item["path"]): item["sha256"]
            for item in sources
        }
        if (
            current_bundle != authorization["bundle_artifact_sha256"]
            or current_sources != authorization["source_artifact_sha256"]
            or _sha256_bytes(evidence_raw) != authorization["preflight_evidence_sha256"]
        ):
            raise ModelAcquisitionDownloadError("Authorized inputs changed after approval.")
        return backend, policy, bundle, authorization, raw

    def launch(
        self,
        backend_id: str,
        bundle_id: str,
        authorization_id: str,
        runner: PythonRemoteRunner,
        confirmed: bool,
        now: Optional[datetime] = None,
    ) -> DownloadLaunchResult:
        if not confirmed:
            raise ModelAcquisitionDownloadError("Explicit --confirm-execute-approved-download is required.")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ModelAcquisitionDownloadError("Launch time must include timezone.")
        current = current.astimezone(timezone.utc)
        backend, policy, bundle, authorization, authorization_raw = self._load_current_authorization(
            backend_id, bundle_id, authorization_id, current
        )
        if str(runner.host) != authorization["remote_host"]:
            raise ModelAcquisitionDownloadError("Runner host does not match authorization.")
        execution_id = _sha256_value({
            "policy_version": MODEL_ACQUISITION_DOWNLOAD_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(authorization_raw),
            "backend_id": backend_id,
            "bundle_id": bundle_id,
        })
        launch_dir = self.execution_root / backend_id / bundle_id / execution_id
        launch_path = launch_dir / "launch.json"
        if launch_path.exists():
            existing, _ = _read_json(launch_path, "download launch evidence")
            return DownloadLaunchResult(
                backend_id, bundle_id, authorization_id, execution_id, launch_path,
                False, str(existing.get("remote_result", {}).get("status") or "unknown"),
                str(existing.get("control_directory") or ""),
            )
        try:
            result = runner.run_python(
                _launcher_program(backend, policy, bundle, authorization, execution_id),
                timeout_seconds=120,
            )
        except Exception as exc:
            raise ModelAcquisitionDownloadError("Approved remote download launch failed: {}".format(exc)) from exc
        if not isinstance(result, Mapping) or result.get("execution_id") != execution_id or result.get(
            "status"
        ) not in {"download_launched", "already_launched"}:
            raise ModelAcquisitionDownloadError("Remote launcher returned an invalid result.")
        control = str(result.get("control_directory") or "")
        expected_control = str(Path(policy["control_root"]) / execution_id)
        if control != expected_control:
            raise ModelAcquisitionDownloadError("Remote launcher control path mismatch.")
        evidence = {
            "schema_version": "1.0", "policy_version": MODEL_ACQUISITION_DOWNLOAD_POLICY_VERSION,
            "source_mode": "explicit_authorized_remote_public_model_download_launch",
            "backend_id": backend_id, "bundle_id": bundle_id,
            "authorization_id": authorization_id, "execution_id": execution_id,
            "launched_at_utc": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "control_directory": control, "remote_result": dict(result),
            "execution_scope": "download_into_staging_only",
            "model_hashing": False, "publication": False, "job_submission": False,
            "gpu_allocation": False, "inference": False, "training": False,
        }
        created = _write_immutable(launch_path, _pretty_json(evidence))
        _write_immutable(launch_dir / "report.md", "\n".join([
            "# GenomeAgent Model Download Launch", "",
            "- Execution: `{}`".format(execution_id),
            "- Remote status: **{}**".format(result["status"]),
            "- Control directory: `{}`".format(control),
            "- Publication: **no**", "- GPU execution: **no**", "",
        ]))
        return DownloadLaunchResult(
            backend_id, bundle_id, authorization_id, execution_id, launch_path,
            created, str(result["status"]), control,
        )

    def status(
        self,
        backend_id: str,
        bundle_id: str,
        authorization_id: str,
        runner: PythonRemoteRunner,
        stamp: Optional[str] = None,
    ) -> DownloadStatusResult:
        backend_id = _identifier(backend_id, "backend_id")
        bundle_id = _digest(bundle_id, "bundle ID")
        authorization_id = _digest(authorization_id, "authorization ID")
        path = self.authorization_root / backend_id / (authorization_id + ".json")
        authorization, raw = _read_json(path, "download authorization")
        _validate_authorization(authorization, authorization_id, backend_id, bundle_id)
        if str(runner.host) != authorization["remote_host"]:
            raise ModelAcquisitionDownloadError("Runner host does not match authorization.")
        execution_id = _sha256_value({
            "policy_version": MODEL_ACQUISITION_DOWNLOAD_POLICY_VERSION,
            "authorization_id": authorization_id,
            "authorization_sha256": _sha256_bytes(raw),
            "backend_id": backend_id, "bundle_id": bundle_id,
        })
        _, policy, _ = self.policy(backend_id)
        control = str(Path(policy["control_root"]) / execution_id)
        try:
            result = runner.run_python(
                _status_program(backend_id, bundle_id, execution_id, control),
                timeout_seconds=60,
            )
        except Exception as exc:
            raise ModelAcquisitionDownloadError("Read-only download status failed: {}".format(exc)) from exc
        if not isinstance(result, Mapping) or result.get("execution_id") != execution_id:
            raise ModelAcquisitionDownloadError("Invalid remote download status result.")
        safety = result.get("safety")
        if not isinstance(safety, Mapping) or any(value is not False for value in safety.values()):
            raise ModelAcquisitionDownloadError("Download status observation claims mutation.")
        worker = result.get("worker_status")
        status = str(worker.get("status")) if isinstance(worker, Mapping) else (
            "launch_recorded_without_worker_status" if result.get("control_exists") else "not_launched"
        )
        now = datetime.now(timezone.utc)
        observation_id = stamp or now.strftime("%Y%m%dT%H%M%S%fZ")
        if not re.fullmatch(r"[0-9]{8}T[0-9]{6}(?:[0-9]{6})?Z", observation_id):
            raise ModelAcquisitionDownloadError("Invalid status observation ID.")
        observation_path = self.execution_root / backend_id / bundle_id / execution_id / "status" / (
            observation_id + ".json"
        )
        evidence = {
            "schema_version": "1.0", "policy_version": MODEL_ACQUISITION_DOWNLOAD_POLICY_VERSION,
            "source_mode": "bounded_read_only_download_status_observation",
            "backend_id": backend_id, "bundle_id": bundle_id, "execution_id": execution_id,
            "observation_id": observation_id, "observed_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status, "remote_observation": dict(result),
            "remote_writes": False, "provider_requests": False,
            "model_download_started_by_observation": False, "job_submission": False,
            "gpu_allocation": False, "model_file_reads": False,
            "large_file_hashing": False, "publication": False,
        }
        _write_immutable(observation_path, _pretty_json(evidence))
        return DownloadStatusResult(
            backend_id, bundle_id, execution_id, observation_id, observation_path, status
        )
