#!/usr/bin/env python3
"""GenomeAgent Allas Tools v0.3 — Mac-side controller.

Permanent control plane: the GenomeAgent Git repository on the user's Mac.
Execution plane: timestamped scripts and logs under Puhti scratch.

Only the Python standard library is required on the Mac and on Puhti.
No Allas credentials are stored in scripts, manifests, logs, or Git.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any, Iterable, Sequence

REPOSITORY_IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_IMPORT_ROOT))
from genomeagent.dataset_registry import (  # noqa: E402
    DatasetRegistryError,
    datasets_for_transfer,
    register_transfer as registry_register_transfer,
    sync_transfer_manifest,
    transfer_by_identifier,
)

VERSION = "0.3.0-mac"
SCHEMA_VERSION = "0.1"

REMOTE_INVENTORY_PROGRAM = r'''
import fnmatch, json, os, pathlib, sys, time
cfg = json.loads(sys.argv[1])
root = pathlib.Path(cfg["source_root"])
paths = [pathlib.Path(x) for x in cfg["paths"]]
include = cfg.get("include_globs") or ["*"]
records = []
missing_roots = []
seen = set()

def included(path):
    return any(fnmatch.fnmatch(path.name, pat) or fnmatch.fnmatch(path.as_posix(), pat) for pat in include)

for rel in paths:
    target = root / rel
    if not target.exists():
        missing_roots.append(rel.as_posix())
        continue
    candidates = [target] if target.is_file() else sorted(target.rglob("*"))
    for path in candidates:
        if not path.is_file() or path.is_symlink() or not included(path):
            continue
        relpath = path.relative_to(root).as_posix()
        if relpath in seen:
            continue
        seen.add(relpath)
        st = path.stat()
        records.append({"path": relpath, "bytes": st.st_size, "mtime_ns": st.st_mtime_ns})
print(json.dumps({
    "source_root": str(root),
    "roots": [x.as_posix() for x in paths],
    "include_globs": include,
    "missing_roots": missing_roots,
    "file_count": len(records),
    "total_bytes": sum(x["bytes"] for x in records),
    "files": records,
}, separators=(",", ":")))
'''


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def repo_root() -> Path:
    env = os.environ.get("GENOMEAGENT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def registry_dir(root: Path) -> Path:
    return root / "data_registry" / "archives"


def work_root(root: Path) -> Path:
    return root / "workspace" / "allas_tasks"


def adhoc_registry_dir(root: Path) -> Path:
    return work_root(root) / "_adhoc_manifests"


def safe_identifier(value: str, label: str = "identifier") -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise SystemExit(f"Invalid {label}: {value!r}")
    return value


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return cleaned[:40] or "folder"


def stable_adhoc_task_id(source_path: str, host: str, bucket: str) -> str:
    source = str(source_path).rstrip("/")
    digest = hashlib.sha256(f"{host}\0{bucket}\0{source}".encode("utf-8")).hexdigest()[:10]
    return f"folder_{slug(Path(source).name)}_{digest}"


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def manifest_path(root: Path, dataset_id: str) -> Path:
    return registry_dir(root) / f"{dataset_id}.json"


def validate_manifest(data: dict[str, Any], path: Path | None = None) -> None:
    label = str(path) if path else "manifest"
    required = ["schema_version", "dataset_id", "project", "remote", "local", "archive", "slurm"]
    missing = [x for x in required if x not in data]
    if missing:
        raise SystemExit(f"{label}: missing fields: {', '.join(missing)}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise SystemExit(f"{label}: unsupported schema_version {data['schema_version']!r}")
    remote = data["remote"]
    local = data["local"]
    if not remote.get("host") or not remote.get("runtime_root"):
        raise SystemExit(f"{label}: remote.host and remote.runtime_root are required")
    if not local.get("source_root") or not local.get("paths"):
        raise SystemExit(f"{label}: local.source_root and local.paths are required")
    if not data["archive"].get("bucket"):
        raise SystemExit(f"{label}: archive.bucket is required")
    for rel in local["paths"]:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            raise SystemExit(f"{label}: local.paths must contain safe relative paths: {rel!r}")
    for value in [remote["runtime_root"], local["source_root"]]:
        if not str(value).startswith("/"):
            raise SystemExit(f"{label}: Puhti paths must be absolute: {value!r}")


def load_manifest(root: Path, dataset_id: str) -> tuple[Path, dict[str, Any]]:
    path = manifest_path(root, dataset_id)
    data = load_json(path)
    validate_manifest(data, path)
    return path, data


def adhoc_manifest_path(root: Path, task_id: str) -> Path:
    return adhoc_registry_dir(root) / f"{safe_identifier(task_id, 'ad-hoc task ID')}.json"


def load_adhoc_manifest(root: Path, task_id_or_source: str) -> tuple[Path, dict[str, Any]]:
    try:
        path, data = transfer_by_identifier(root, task_id_or_source)
    except DatasetRegistryError as exc:
        raise SystemExit(str(exc)) from exc
    validate_manifest(data, path)
    if data.get("task_mode") != "ad_hoc_folder":
        raise SystemExit(f"Not an ad-hoc folder task: {task_id_or_source}")
    return path, data


def list_json_manifests(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows: list[tuple[Path, dict[str, Any]]] = []
    if not directory.exists():
        return rows
    for path in sorted(directory.glob("*.json")):
        try:
            data = load_json(path)
            validate_manifest(data, path)
        except SystemExit:
            continue
        rows.append((path, data))
    return rows


def create_adhoc_manifest(
    root: Path,
    source_path: str,
    *,
    host: str,
    project: str,
    bucket: str,
    runtime_root: str | None = None,
    include_globs: Sequence[str] | None = None,
    time_limit: str = "72:00:00",
    mem: str = "4G",
) -> tuple[Path, dict[str, Any]]:
    source = source_path.rstrip("/")
    if not source.startswith("/") or source == "/":
        raise SystemExit("Ad-hoc Puhti source must be an absolute folder path other than /")
    source_obj = Path(source)
    parent = str(source_obj.parent)
    name = source_obj.name
    task_id = stable_adhoc_task_id(source, host, bucket)
    path = adhoc_manifest_path(root, task_id)
    existing_status = "not_verified"
    if path.exists():
        old = load_json(path)
        existing_status = old.get("archive", {}).get("status", existing_status)
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": task_id,
        "task_mode": "ad_hoc_folder",
        "description": f"Ad-hoc Allas archive of {source}",
        "project": project,
        "remote": {
            "host": host,
            "runtime_root": runtime_root or f"/scratch/{project}/GenomeAgent_runtime/allas_tasks",
            "allas_conf": "/appl/opt/csc-cli-utils/allas-cli-utils/allas_conf",
        },
        "local": {
            "source_root": parent,
            "paths": [name],
            "original_path": source,
            "status": "available_local",
            "removal_allowed": False,
        },
        "archive": {
            "provider": "allas",
            "backend": "a-commands",
            "bucket": bucket,
            "status": existing_status,
            "visibility": "private",
        },
        "transfer": {
            "layout": "preserve_relative_folder_path",
            "include_globs": list(include_globs or ["*"]),
            "batch_size": 50,
            "verification": "a-check-object-names",
            "automatic_delete_local": False,
        },
        "restore": {"free_space_multiplier": 1.10, "overwrite_policy": "deny"},
        "slurm": {
            "account": project, "partition": "small", "time": time_limit, "cpus": 1, "mem": mem
        },
        "validation": {"primary_suffix": "", "expected_primary_files": None, "require_index": False},
        "created_at": utc_now(),
        "notes": [
            "Created from the generic ad-hoc folder interface.",
            "Automatic local deletion is disabled.",
            "Promote with register-folder-upload if this becomes a durable project dataset.",
        ],
    }
    write_json(path, data)
    return path, data


def q(value: Any) -> str:
    return shlex.quote(str(value))


def human_bytes(value: int) -> str:
    n = float(value)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]:
        if n < 1024 or unit == "PiB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return str(value)


def run(cmd: Sequence[str], *, input_text: str | None = None, check: bool = True, tty: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(list(cmd), input=input_text, text=True, capture_output=not tty)
    if check and proc.returncode != 0:
        out = "" if tty else (proc.stdout or "")
        err = "" if tty else (proc.stderr or "")
        raise SystemExit(f"Command failed ({proc.returncode}): {shlex.join(cmd)}\n{out}{err}".rstrip())
    return proc


def ssh_command(host: str, command: str, *, tty: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    args = ["ssh"]
    if tty:
        args.append("-tt")
    args.extend([host, command])
    return run(args, tty=tty, check=check)


def scp_to(host: str, local: Path, remote: str) -> None:
    run(["scp", str(local), f"{host}:{remote}"])


def remote_exec(host: str, argv: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return ssh_command(host, shlex.join([str(x) for x in argv]), check=check)


def remote_inventory(manifest: dict[str, Any]) -> dict[str, Any]:
    cfg = {
        "source_root": manifest["local"]["source_root"],
        "paths": manifest["local"]["paths"],
        "include_globs": manifest.get("transfer", {}).get("include_globs", ["*"]),
    }
    host = manifest["remote"]["host"]
    proc = remote_exec(host, ["python3", "-c", REMOTE_INVENTORY_PROGRAM, json.dumps(cfg, separators=(",", ":"))])
    try:
        inv = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse remote inventory from {host}:\n{proc.stdout}\n{proc.stderr}") from exc
    inv.update({"dataset_id": manifest["dataset_id"], "host": host, "created_at": utc_now()})
    return inv


def validate_inventory(manifest: dict[str, Any], inv: dict[str, Any]) -> dict[str, Any]:
    rules = manifest.get("validation", {})
    suffix = rules.get("primary_suffix", ".bam")
    expected = rules.get("expected_primary_files")
    require_index = bool(rules.get("require_index", False))
    files = {x["path"] for x in inv["files"]}
    primary = sorted(x for x in files if x.endswith(suffix))
    missing_indexes: list[str] = []
    if require_index and suffix == ".bam":
        for bam in primary:
            if bam + ".bai" not in files and bam[:-4] + ".bai" not in files:
                missing_indexes.append(bam)
    errors: list[str] = []
    if inv.get("missing_roots"):
        errors.append("missing source roots: " + ", ".join(inv["missing_roots"]))
    if expected is not None and len(primary) != int(expected):
        errors.append(f"expected {int(expected)} {suffix} files, found {len(primary)}")
    if missing_indexes:
        errors.append(f"{len(missing_indexes)} BAM files lack matching indexes")
    if inv["file_count"] == 0:
        errors.append("no matching regular files found")
    return {
        "passed": not errors,
        "primary_suffix": suffix,
        "expected_primary_files": expected,
        "observed_primary_files": len(primary),
        "missing_indexes": missing_indexes,
        "errors": errors,
    }


def save_inventory(root: Path, manifest: dict[str, Any], inv: dict[str, Any], label: str) -> Path:
    out = work_root(root) / manifest["dataset_id"] / "inventories" / f"{label}_{timestamp()}.json"
    write_json(out, inv)
    return out


def latest_run(root: Path, dataset_id: str, operation: str | None = None) -> Path | None:
    base = work_root(root) / dataset_id / "runs"
    if not base.exists():
        return None
    runs = sorted(x for x in base.iterdir() if x.is_dir())
    if operation:
        runs = [x for x in runs if (x / f"{operation}.slurm").exists()]
    return runs[-1] if runs else None


def make_run(root: Path, manifest: dict[str, Any], operation: str) -> tuple[Path, str]:
    stamp = timestamp()
    local = work_root(root) / manifest["dataset_id"] / "runs" / stamp
    local.mkdir(parents=True, exist_ok=False)
    remote = f"{manifest['remote']['runtime_root'].rstrip('/')}/{manifest['dataset_id']}/runs/{stamp}"
    (local / "remote_run_dir.txt").write_text(remote + "\n", encoding="utf-8")
    (local / "operation.txt").write_text(operation + "\n", encoding="utf-8")
    return local, remote


def remote_run_dir(local_run: Path) -> str:
    return (local_run / "remote_run_dir.txt").read_text(encoding="utf-8").strip()


def slurm_header(manifest: dict[str, Any], job_name: str, remote_run: str, operation: str) -> str:
    s = manifest["slurm"]
    return f'''#!/usr/bin/env bash
#SBATCH --job-name={job_name}
#SBATCH --account={s.get("account", manifest["project"])}
#SBATCH --partition={s.get("partition", "small")}
#SBATCH --time={s.get("time", "72:00:00")}
#SBATCH --cpus-per-task={int(s.get("cpus", 1))}
#SBATCH --mem={s.get("mem", "4G")}
#SBATCH --output={remote_run}/logs/{operation}_%j.out
#SBATCH --error={remote_run}/logs/{operation}_%j.err
'''


def batch_preamble(manifest: dict[str, Any], remote_run: str) -> str:
    return f'''
set -euo pipefail
source /appl/profile/zz-csc-env.sh
module load allas

RUN_DIR={q(remote_run)}
mkdir -p "$RUN_DIR/logs" "$RUN_DIR/verification"

if [[ -z "${{OS_PASSWORD:-}}" ]]; then
    echo "ERROR: OS_PASSWORD is absent. Submit through the Mac controller so allas-conf -k runs before sbatch." >&2
    exit 42
fi

for cmd in a-put a-get a-check a-list; do
    command -v "$cmd" >/dev/null 2>&1 || {{ echo "ERROR: missing $cmd" >&2; exit 43; }}
done
'''


def prepare_upload(root: Path, manifest: dict[str, Any]) -> Path:
    inv = remote_inventory(manifest)
    validation = validate_inventory(manifest, inv)
    inv["validation"] = validation
    if not validation["passed"]:
        raise SystemExit("Upload preparation blocked: " + "; ".join(validation["errors"]))
    local_run, remote_run = make_run(root, manifest, "upload")
    write_json(local_run / "local_inventory.before_upload.json", inv)
    inputs = local_run / "input_files.txt"
    inputs.write_text("".join(x["path"] + "\n" for x in inv["files"]), encoding="utf-8")
    bucket = manifest["archive"]["bucket"]
    batch_size = int(manifest.get("transfer", {}).get("batch_size", 50))
    source_root = manifest["local"]["source_root"]
    prefixes = "\n".join(manifest["local"]["paths"])
    script = local_run / "upload.slurm"
    text = slurm_header(manifest, f"GA_AUP_{manifest['dataset_id'][:16]}", remote_run, "upload")
    text += batch_preamble(manifest, remote_run)
    text += f'''
SOURCE_ROOT={q(source_root)}
INPUT_LIST="$RUN_DIR/input_files.txt"
BUCKET={q(bucket)}
BATCH_SIZE={batch_size}

cd "$SOURCE_ROOT"
mapfile -t FILES < "$INPUT_LIST"
TOTAL=${{#FILES[@]}}
[[ "$TOTAL" -gt 0 ]] || {{ echo "ERROR: empty input list" >&2; exit 44; }}

echo "Dataset: {manifest['dataset_id']}"
echo "Source root: $SOURCE_ROOT"
echo "Bucket: $BUCKET"
echo "Files: $TOTAL"

for ((i=0; i<TOTAL; i+=BATCH_SIZE)); do
    batch=("${{FILES[@]:i:BATCH_SIZE}}")
    echo "Uploading $((i+1))-$((i+${{#batch[@]}})) of $TOTAL"
    a-put -b "$BUCKET" "${{batch[@]}}"
done

touch "$RUN_DIR/upload.completed"
touch "$RUN_DIR/verification/check.started"

CHECK_COMMAND_FAILED=0
for ((i=0; i<TOTAL; i+=BATCH_SIZE)); do
    batch=("${{FILES[@]:i:BATCH_SIZE}}")
    a-check -b "$BUCKET" "${{batch[@]}}" || CHECK_COMMAND_FAILED=1
done

find "$SOURCE_ROOT" -maxdepth 1 -type f -name "missing_${{BUCKET}}_*" -newer "$RUN_DIR/verification/check.started" \
    -exec mv -f {{}} "$RUN_DIR/verification/" \\;

shopt -s nullglob
missing=("$RUN_DIR"/verification/missing_*)
if (( ${{#missing[@]}} > 0 || CHECK_COMMAND_FAILED != 0 )); then
    touch "$RUN_DIR/upload.namecheck.failed"
    echo "ERROR: a-check reported missing objects or returned a failure" >&2
    exit 45
fi

touch "$RUN_DIR/upload.namecheck.ok"
while IFS= read -r prefix; do
    [[ -n "$prefix" ]] || continue
    safe=$(printf '%s' "$prefix" | tr '/' '_')
    a-list -l "$BUCKET/$prefix" > "$RUN_DIR/verification/remote_${{safe}}.txt"
done <<'PREFIXES'
{prefixes}
PREFIXES

echo "Upload and object-name verification completed."
'''
    script.write_text(text, encoding="utf-8")
    script.chmod(0o750)
    stage_run(manifest, local_run, remote_run, [script, inputs, local_run / "local_inventory.before_upload.json"])
    print(f"Prepared upload run: {local_run}")
    print(f"Remote runtime: {remote_run}")
    print(f"Files: {inv['file_count']} ({human_bytes(inv['total_bytes'])})")
    return local_run


def prepare_restore(root: Path, manifest: dict[str, Any]) -> Path:
    if not str(manifest.get("archive", {}).get("status", "")).startswith("verified"):
        raise SystemExit("Restore preparation blocked: archive is not recorded as verified")
    upload_run = latest_run(root, manifest["dataset_id"], "upload")
    if not upload_run or not (upload_run / "local_inventory.before_upload.json").exists():
        raise SystemExit("Restore preparation blocked: upload inventory is unavailable on the Mac")
    expected = load_json(upload_run / "local_inventory.before_upload.json")
    local_run, remote_run = make_run(root, manifest, "restore")
    expected_path = local_run / "expected_inventory.json"
    write_json(expected_path, expected)
    bucket = manifest["archive"]["bucket"]
    source_root = manifest["local"]["source_root"]
    paths = manifest["local"]["paths"]
    prefixes = "\n".join(paths)
    expected_bytes = int(expected["total_bytes"])
    reserve = float(manifest.get("restore", {}).get("free_space_multiplier", 1.10))
    required_bytes = int(expected_bytes * reserve)
    conflict_checks = "\n".join(f'[[ ! -e {q(str(Path(source_root) / rel))} ]] || {{ echo "ERROR: restore target exists: {str(Path(source_root) / rel)}" >&2; exit 50; }}' for rel in paths)
    script = local_run / "restore.slurm"
    text = slurm_header(manifest, f"GA_ARG_{manifest['dataset_id'][:16]}", remote_run, "restore")
    text += batch_preamble(manifest, remote_run)
    text += f'''
SOURCE_ROOT={q(source_root)}
BUCKET={q(bucket)}
OBJECT_LIST="$RUN_DIR/objects_to_restore.txt"
REQUIRED_BYTES={required_bytes}

{conflict_checks}

mkdir -p "$SOURCE_ROOT"
FREE_KB=$(df -Pk "$SOURCE_ROOT" | awk 'NR==2 {{print $4}}')
FREE_BYTES=$((FREE_KB * 1024))
if (( FREE_BYTES < REQUIRED_BYTES )); then
    echo "ERROR: insufficient free space. Required with reserve: $REQUIRED_BYTES; available: $FREE_BYTES" >&2
    exit 51
fi

: > "$OBJECT_LIST"
while IFS= read -r prefix; do
    [[ -n "$prefix" ]] || continue
    a-list "$BUCKET/$prefix" >> "$OBJECT_LIST"
done <<'PREFIXES'
{prefixes}
PREFIXES
sort -u "$OBJECT_LIST" -o "$OBJECT_LIST"
COUNT=$(wc -l < "$OBJECT_LIST" | tr -d ' ')
[[ "$COUNT" -gt 0 ]] || {{ echo "ERROR: no matching Allas objects" >&2; exit 52; }}

echo "Restoring $COUNT objects to original locations"
while IFS= read -r object; do
    [[ -n "$object" ]] || continue
    a-get -l "$object"
done < "$OBJECT_LIST"

touch "$RUN_DIR/restore.completed"
echo "Restore transfer completed; run verify-restore from the Mac."
'''
    script.write_text(text, encoding="utf-8")
    script.chmod(0o750)
    stage_run(manifest, local_run, remote_run, [script, expected_path])
    print(f"Prepared restore run: {local_run}")
    print(f"Remote runtime: {remote_run}")
    print(f"Expected data: {expected['file_count']} files ({human_bytes(expected_bytes)})")
    return local_run


def stage_run(manifest: dict[str, Any], local_run: Path, remote_run: str, files: Iterable[Path]) -> None:
    host = manifest["remote"]["host"]
    remote_exec(host, ["mkdir", "-p", remote_run, f"{remote_run}/logs", f"{remote_run}/verification"])
    for file in files:
        scp_to(host, file, f"{remote_run}/{file.name}")


def auth_and_submit(manifest: dict[str, Any], local_run: Path, operation: str) -> str:
    host = manifest["remote"]["host"]
    remote_run = remote_run_dir(local_run)
    remote_script = f"{remote_run}/{operation}.slurm"
    project = manifest["project"]
    allas_conf = manifest["remote"].get("allas_conf", "/appl/opt/csc-cli-utils/allas-cli-utils/allas_conf")
    remote_job_file = f"{remote_run}/{operation}.job_id"
    inner = (
        f"set +u; source /appl/profile/zz-csc-env.sh; module load allas; "
        f"source {q(allas_conf)} -k {q(project)}; set -u; "
        f"jid=$(sbatch --parsable {q(remote_script)}); "
        f"printf '%s\n' \"$jid\" > {q(remote_job_file)}; "
        f"echo Submitted Slurm job \"$jid\""
    )
    command = f"bash -lc {q(inner)}"
    print("Puhti will now ask you to activate Allas. Your password is not stored by GenomeAgent.")
    proc = subprocess.run(["ssh", "-tt", host, command])
    if proc.returncode != 0:
        raise SystemExit(f"Remote authentication/submission failed with exit code {proc.returncode}")
    fetched = remote_exec(host, ["cat", remote_job_file])
    job_id = fetched.stdout.strip().split(";")[0]
    if not re.fullmatch(r"[0-9]+", job_id):
        raise SystemExit(f"Submission completed, but invalid Slurm job ID was read from {remote_job_file}: {job_id!r}")
    (local_run / f"{operation}.job_id").write_text(job_id + "\n", encoding="utf-8")
    print(f"Recorded Slurm job ID {job_id} in {local_run}")
    return job_id


def remote_file_state(host: str, path: str) -> bool:
    proc = remote_exec(host, ["test", "-e", path], check=False)
    return proc.returncode == 0


def scan_run(root: Path, manifest: dict[str, Any], operation: str | None = None) -> dict[str, Any]:
    local_run = latest_run(root, manifest["dataset_id"], operation)
    if not local_run:
        raise SystemExit("No matching prepared run exists")
    operation = (local_run / "operation.txt").read_text(encoding="utf-8").strip()
    remote_run = remote_run_dir(local_run)
    host = manifest["remote"]["host"]
    job_file = local_run / f"{operation}.job_id"
    job_id = job_file.read_text(encoding="utf-8").strip() if job_file.exists() else None
    scheduler = {"state": "not_submitted", "exit_code": None}
    if job_id:
        cmd = f"sacct -n -X -j {q(job_id)} --format=State,ExitCode -P 2>/dev/null | head -n 1"
        proc = ssh_command(host, cmd, check=False)
        line = (proc.stdout or "").strip()
        if line:
            fields = line.split("|")
            scheduler = {"state": fields[0].strip(), "exit_code": fields[1].strip() if len(fields) > 1 else None}
        else:
            proc2 = ssh_command(host, f"squeue -h -j {q(job_id)} -o '%T'", check=False)
            state = (proc2.stdout or "").strip()
            scheduler = {"state": state or "unknown", "exit_code": None}
    markers = {
        "upload_completed": remote_file_state(host, f"{remote_run}/upload.completed"),
        "upload_namecheck_ok": remote_file_state(host, f"{remote_run}/upload.namecheck.ok"),
        "upload_namecheck_failed": remote_file_state(host, f"{remote_run}/upload.namecheck.failed"),
        "restore_completed": remote_file_state(host, f"{remote_run}/restore.completed"),
    }
    result = {
        "dataset_id": manifest["dataset_id"], "operation": operation, "observed_at": utc_now(),
        "host": host, "local_run": str(local_run), "remote_run": remote_run,
        "job_id": job_id, "scheduler": scheduler, "markers": markers,
    }
    write_json(local_run / f"scan_{timestamp()}.json", result)
    print(json.dumps(result, indent=2))
    return result


def verify_upload(root: Path, manifest_path_: Path, manifest: dict[str, Any]) -> None:
    result = scan_run(root, manifest, "upload")
    ok = result["markers"]["upload_completed"] and result["markers"]["upload_namecheck_ok"] and not result["markers"]["upload_namecheck_failed"]
    if not ok:
        raise SystemExit("Upload is incomplete or object-name verification failed")
    manifest["archive"].update({
        "status": "verified_object_names",
        "verified_at": utc_now(),
        "verification_run": result["remote_run"],
    })
    manifest["local"]["removal_allowed"] = False
    manifest["local"]["removal_note"] = "v0.1 does not authorize automatic deletion; a-check verifies names, not object contents."
    write_json(manifest_path_, manifest)
    synced = sync_transfer_manifest(root, manifest)
    print("Upload verification passed at the a-check object-name level.")
    if synced:
        print(f"Dataset registry records synchronized: {len(synced)}")
    print("Automatic local deletion remains disabled.")


def compare_inventories(expected: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    exp = {x["path"]: x for x in expected["files"]}
    obs = {x["path"]: x for x in observed["files"]}
    missing = sorted(set(exp) - set(obs))
    extra = sorted(set(obs) - set(exp))
    size_mismatch = sorted(x for x in set(exp) & set(obs) if exp[x]["bytes"] != obs[x]["bytes"])
    return {
        "expected_file_count": expected["file_count"], "observed_file_count": observed["file_count"],
        "expected_total_bytes": expected["total_bytes"], "observed_total_bytes": observed["total_bytes"],
        "missing": missing, "extra": extra, "size_mismatch": size_mismatch,
        "passed": not missing and not extra and not size_mismatch,
    }


def verify_restore(root: Path, manifest_path_: Path, manifest: dict[str, Any]) -> None:
    restore_run = latest_run(root, manifest["dataset_id"], "restore")
    if not restore_run:
        raise SystemExit("No restore run exists")
    result = scan_run(root, manifest, "restore")
    if not result["markers"]["restore_completed"]:
        raise SystemExit("Restore transfer is not marked complete")
    upload_run = latest_run(root, manifest["dataset_id"], "upload")
    if not upload_run:
        raise SystemExit("Expected upload inventory unavailable")
    expected = load_json(upload_run / "local_inventory.before_upload.json")
    observed = remote_inventory(manifest)
    comparison = compare_inventories(expected, observed)
    comparison.update({"dataset_id": manifest["dataset_id"], "checked_at": utc_now()})
    write_json(restore_run / "restore_verification.json", comparison)
    if not comparison["passed"]:
        raise SystemExit(f"Restore verification failed: missing={len(comparison['missing'])}, extra={len(comparison['extra'])}, size_mismatch={len(comparison['size_mismatch'])}")
    manifest["local"].update({"status": "available_local", "restored_verified_at": comparison["checked_at"]})
    write_json(manifest_path_, manifest)
    synced = sync_transfer_manifest(root, manifest)
    print(f"Restore verification passed: {observed['file_count']} files, {human_bytes(observed['total_bytes'])}")
    if synced:
        print(f"Dataset registry records synchronized: {len(synced)}")


def doctor(manifest: dict[str, Any] | None = None) -> None:
    print(f"GenomeAgent Allas Tools {VERSION}")
    for cmd in ["python3", "ssh", "scp"]:
        print(f"Mac {cmd:<7}: {'found' if shutil.which(cmd) else 'MISSING'}")
    if not manifest:
        return
    host = manifest["remote"]["host"]
    print(f"Puhti host: {host}")
    probe = "source /appl/profile/zz-csc-env.sh >/dev/null 2>&1; module load allas >/dev/null 2>&1; for c in python3 sbatch sacct a-put a-get a-check a-list; do command -v $c >/dev/null && echo $c=found || echo $c=missing; done"
    proc = ssh_command(host, f"bash -lc {q(probe)}", check=False)
    print((proc.stdout or proc.stderr or "SSH probe failed").strip())


def plan_upload(root: Path, manifest: dict[str, Any]) -> None:
    inv = remote_inventory(manifest)
    validation = validate_inventory(manifest, inv)
    path = save_inventory(root, manifest, inv, "remote")
    print(f"Dataset: {manifest['dataset_id']}")
    print(f"Puhti source: {inv['source_root']}")
    print(f"Matching files: {inv['file_count']} ({human_bytes(inv['total_bytes'])})")
    print(f"BAM files: {validation['observed_primary_files']} (expected {validation['expected_primary_files']})")
    print(f"Missing BAM indexes: {len(validation['missing_indexes'])}")
    print(f"Allas bucket: {manifest['archive']['bucket']}")
    print(f"Validation: {'PASSED' if validation['passed'] else 'FAILED'}")
    for error in validation["errors"]:
        print(f"  ERROR: {error}")
    print(f"Inventory saved: {path}")
    print("Automatic local deletion: disabled")


def plan_restore(root: Path, manifest: dict[str, Any]) -> None:
    print(f"Dataset: {manifest['dataset_id']}")
    print(f"Archive state: {manifest['archive'].get('status', 'unknown')}")
    print(f"Allas bucket: {manifest['archive']['bucket']}")
    print(f"Original Puhti root: {manifest['local']['source_root']}")
    upload_run = latest_run(root, manifest["dataset_id"], "upload")
    if upload_run and (upload_run / "local_inventory.before_upload.json").exists():
        inv = load_json(upload_run / "local_inventory.before_upload.json")
        multiplier = float(manifest.get("restore", {}).get("free_space_multiplier", 1.10))
        print(f"Expected restore: {inv['file_count']} files ({human_bytes(inv['total_bytes'])})")
        print(f"Required free space with reserve: {human_bytes(int(inv['total_bytes'] * multiplier))}")
    else:
        print("Expected upload inventory: unavailable")
    print("Restore will refuse existing target directories and insufficient free space.")


def show(path: Path, manifest: dict[str, Any]) -> None:
    print(f"Manifest: {path}")
    print(json.dumps(manifest, indent=2))
    root = repo_root()
    transfer_id = str(manifest.get("registered_from_adhoc_task") or manifest.get("dataset_id") or "")
    assignments = datasets_for_transfer(root, transfer_id) if transfer_id else []
    print()
    print("Scientific dataset assignments")
    print("=" * 80)
    if not assignments:
        print("Unassigned. Use bin/genomeagent-datasets assign-transfer ...")
    else:
        for dataset in assignments:
            print(f"{dataset['dataset_id']}: {len(dataset.get('scientific_links', []))} scientific links")


def list_datasets(root: Path) -> None:
    rows = list_json_manifests(registry_dir(root))
    print("Managed Allas datasets")
    print("=" * 80)
    if not rows:
        print("No managed datasets registered.")
        return
    for _, data in rows:
        print(f"{data['dataset_id']:<38} {data.get('archive', {}).get('status', 'unknown'):<24} {data.get('description', '')}")


def list_folder_uploads(root: Path) -> None:
    rows = list_json_manifests(adhoc_registry_dir(root))
    print("Ad-hoc Allas folder tasks")
    print("=" * 80)
    if not rows:
        print("No ad-hoc folder tasks recorded.")
        return
    for _, data in rows:
        source = data.get("local", {}).get("original_path", data.get("local", {}).get("source_root", ""))
        assigned = datasets_for_transfer(root, str(data.get("dataset_id", "")))
        assignment = ",".join(x["dataset_id"] for x in assigned) if assigned else "unassigned"
        print(f"{data['dataset_id']:<38} {data.get('archive', {}).get('status', 'unknown'):<24} {assignment:<34} {source}")


def folder_manifest_from_args(root: Path, args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    return create_adhoc_manifest(
        root,
        args.source,
        host=args.host,
        project=args.project,
        bucket=args.bucket,
        runtime_root=args.runtime_root,
        include_globs=args.include,
        time_limit=args.time,
        mem=args.mem,
    )


def plan_folder_upload(root: Path, args: argparse.Namespace) -> None:
    path, manifest = folder_manifest_from_args(root, args)
    print(f"Ad-hoc task ID: {manifest['dataset_id']}")
    print(f"Manifest: {path}")
    plan_upload(root, manifest)


def prepare_folder_upload(root: Path, args: argparse.Namespace) -> None:
    path, manifest = folder_manifest_from_args(root, args)
    print(f"Ad-hoc task ID: {manifest['dataset_id']}")
    print(f"Manifest: {path}")
    prepare_upload(root, manifest)


def submit_folder_upload(root: Path, args: argparse.Namespace) -> None:
    path, manifest = folder_manifest_from_args(root, args)
    print(f"Ad-hoc task ID: {manifest['dataset_id']}")
    print(f"Manifest: {path}")
    local_run = latest_run(root, manifest["dataset_id"], "upload") or prepare_upload(root, manifest)
    auth_and_submit(manifest, local_run, "upload")


def register_folder_upload(root: Path, task_id: str, dataset_id: str, description: str | None) -> Path:
    source_path, manifest = load_adhoc_manifest(root, task_id)
    if not str(manifest.get("archive", {}).get("status", "")).startswith("verified"):
        raise SystemExit("Registration blocked: verify the folder upload first")
    dataset_id = safe_identifier(dataset_id, "dataset ID")
    target = manifest_path(root, dataset_id)
    if target.exists():
        raise SystemExit(f"Managed dataset already exists: {target}")
    old_id = manifest["dataset_id"]
    manifest["dataset_id"] = dataset_id
    manifest["task_mode"] = "managed_dataset"
    manifest["registered_from_adhoc_task"] = old_id
    manifest["registered_at"] = utc_now()
    if description:
        manifest["description"] = description
    write_json(target, manifest)
    source_work = work_root(root) / old_id
    target_work = work_root(root) / dataset_id
    if source_work.exists() and not target_work.exists():
        shutil.copytree(source_work, target_work)
    try:
        lifecycle_path = registry_register_transfer(
            root,
            identifier=old_id,
            dataset_id=dataset_id,
            title=dataset_id,
            description=manifest.get("description", ""),
            scientific_links=None,
        )
    except DatasetRegistryError as exc:
        raise SystemExit(f"Managed archive created, but lifecycle registration failed: {exc}") from exc
    print(f"Registered managed dataset: {dataset_id}")
    print(f"Manifest: {target}")
    print(f"Lifecycle registry: {lifecycle_path}")
    print(f"Source ad-hoc manifest retained: {source_path}")
    return target


def add_folder_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("source", help="Absolute folder path on Puhti")
    parser.add_argument("--host", default="puhti", help="SSH host or alias (default: puhti)")
    parser.add_argument("--project", default="project_2001113", help="CSC project ID")
    parser.add_argument("--bucket", default="tuomas64-genomics", help="Allas bucket")
    parser.add_argument("--runtime-root", default=None, help="Remote GenomeAgent runtime root")
    parser.add_argument("--include", action="append", default=None, metavar="GLOB", help="Include glob; repeatable (default: all files)")
    parser.add_argument("--time", default="72:00:00", help="Slurm time limit")
    parser.add_argument("--mem", default="4G", help="Slurm memory")


def build_parser() -> argparse.ArgumentParser:
    description = "GenomeAgent Allas Tools v0.3 Mac controller"
    epilog = """Command styles are equivalent:
  bin/genomeagent-allas plan-upload graph_markdup_bams_458
  python3 scripts/allas.py plan-upload graph_markdup_bams_458

Discover commands and tasks:
  bin/genomeagent-allas --help
  bin/genomeagent-allas list-datasets
  bin/genomeagent-allas list-folder-uploads

Generic folder example:
  bin/genomeagent-allas plan-folder-upload /scratch/project_2001113/results
  bin/genomeagent-allas submit-folder-upload /scratch/project_2001113/results
"""
    p = argparse.ArgumentParser(
        prog="genomeagent-allas",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("list-datasets", help="List managed dataset manifests")
    sub.add_parser("list-folder-uploads", help="List recorded ad-hoc folder tasks")

    d = sub.add_parser("doctor", help="Check Mac, SSH, Slurm, and Allas command availability")
    d.add_argument("dataset", nargs="?", help="Optional managed dataset ID")

    managed = {
        "show": "Show a managed dataset manifest",
        "inventory-remote": "Inventory a managed dataset on Puhti",
        "plan-upload": "Validate and plan a managed upload",
        "prepare-upload": "Generate and stage a managed upload job",
        "submit-upload": "Prepare if needed, authenticate, and submit upload",
        "verify-upload": "Verify a managed upload at object-name level",
        "plan-restore": "Show managed restore requirements",
        "prepare-restore": "Generate and stage a managed restore job",
        "submit-restore": "Prepare if needed, authenticate, and submit restore",
        "verify-restore": "Verify restored paths and byte sizes",
        "scan": "Scan latest managed upload or restore run",
    }
    for name, help_text in managed.items():
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("dataset", help="Managed dataset ID; see list-datasets")
    sub.choices["scan"].add_argument("--operation", choices=["upload", "restore"])

    for name, help_text in [
        ("plan-folder-upload", "Inventory and plan an arbitrary Puhti folder upload"),
        ("prepare-folder-upload", "Generate and stage an arbitrary folder upload"),
        ("submit-folder-upload", "Prepare if needed and submit an arbitrary folder upload"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        add_folder_source_arguments(sp)

    show_folder = sub.add_parser("show-folder-upload", help="Show a recorded ad-hoc folder task")
    show_folder.add_argument("task_id", help="Ad-hoc task ID or original source path; see list-folder-uploads")
    scan_folder = sub.add_parser("scan-folder-upload", help="Scan the latest ad-hoc upload run")
    scan_folder.add_argument("task_id")
    verify_folder = sub.add_parser("verify-folder-upload", help="Verify an ad-hoc folder upload")
    verify_folder.add_argument("task_id")

    for name, help_text in [
        ("plan-folder-restore", "Show restore requirements for an ad-hoc folder"),
        ("prepare-folder-restore", "Generate and stage an ad-hoc folder restore"),
        ("submit-folder-restore", "Prepare if needed and submit an ad-hoc folder restore"),
        ("verify-folder-restore", "Verify an ad-hoc folder restore"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("task_id", help="Ad-hoc task ID or original source path; see list-folder-uploads")

    register = sub.add_parser("register-folder-upload", help="Promote a verified ad-hoc upload into the managed Git registry")
    register.add_argument("task_id")
    register.add_argument("--dataset-id", required=True)
    register.add_argument("--description")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = repo_root()
    if args.command == "list-datasets":
        list_datasets(root)
        return 0
    if args.command == "list-folder-uploads":
        list_folder_uploads(root)
        return 0
    if args.command == "doctor":
        manifest = load_manifest(root, args.dataset)[1] if args.dataset else None
        doctor(manifest)
        return 0
    if args.command == "plan-folder-upload":
        plan_folder_upload(root, args)
        return 0
    if args.command == "prepare-folder-upload":
        prepare_folder_upload(root, args)
        return 0
    if args.command == "submit-folder-upload":
        submit_folder_upload(root, args)
        return 0
    if args.command in {"show-folder-upload", "scan-folder-upload", "verify-folder-upload", "plan-folder-restore", "prepare-folder-restore", "submit-folder-restore", "verify-folder-restore"}:
        path, manifest = load_adhoc_manifest(root, args.task_id)
        if args.command == "show-folder-upload":
            show(path, manifest)
        elif args.command == "scan-folder-upload":
            scan_run(root, manifest, "upload")
        elif args.command == "verify-folder-upload":
            verify_upload(root, path, manifest)
        elif args.command == "plan-folder-restore":
            plan_restore(root, manifest)
        elif args.command == "prepare-folder-restore":
            prepare_restore(root, manifest)
        elif args.command == "submit-folder-restore":
            local_run = latest_run(root, manifest["dataset_id"], "restore") or prepare_restore(root, manifest)
            auth_and_submit(manifest, local_run, "restore")
        elif args.command == "verify-folder-restore":
            verify_restore(root, path, manifest)
        return 0
    if args.command == "register-folder-upload":
        register_folder_upload(root, args.task_id, args.dataset_id, args.description)
        return 0

    path, manifest = load_manifest(root, args.dataset)
    if args.command == "show":
        show(path, manifest)
    elif args.command == "inventory-remote":
        inv = remote_inventory(manifest)
        out = save_inventory(root, manifest, inv, "remote")
        print(f"Inventory: {out}\nFiles: {inv['file_count']}\nSize: {human_bytes(inv['total_bytes'])}")
    elif args.command == "plan-upload":
        plan_upload(root, manifest)
    elif args.command == "prepare-upload":
        prepare_upload(root, manifest)
    elif args.command == "submit-upload":
        local_run = latest_run(root, manifest["dataset_id"], "upload") or prepare_upload(root, manifest)
        auth_and_submit(manifest, local_run, "upload")
    elif args.command == "verify-upload":
        verify_upload(root, path, manifest)
    elif args.command == "plan-restore":
        plan_restore(root, manifest)
    elif args.command == "prepare-restore":
        prepare_restore(root, manifest)
    elif args.command == "submit-restore":
        local_run = latest_run(root, manifest["dataset_id"], "restore") or prepare_restore(root, manifest)
        auth_and_submit(manifest, local_run, "restore")
    elif args.command == "verify-restore":
        verify_restore(root, path, manifest)
    elif args.command == "scan":
        scan_run(root, manifest, args.operation)
    else:
        raise AssertionError(args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
