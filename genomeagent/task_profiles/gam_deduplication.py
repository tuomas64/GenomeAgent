#!/usr/bin/env python3
"""Read-only task profile for per-sample GAM duplicate removal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from genomeagent.task_scanner import RemoteRunner, TaskScanError, write_tsv


REMOTE_OBSERVER = r'''
import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path


def now_local_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def path_info(path):
    p = Path(path)
    info = {
        "path": str(p),
        "exists": False,
        "is_file": False,
        "is_dir": False,
        "size_bytes": None,
        "mtime": None,
    }
    try:
        info["exists"] = p.exists()
        info["is_file"] = p.is_file()
        info["is_dir"] = p.is_dir()
        if info["exists"]:
            stat = p.stat()
            info["size_bytes"] = stat.st_size
            info["mtime"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)
            )
    except Exception as exc:
        info["error"] = str(exc)
    return info


def select_candidate(paths, expected_kind, dataset, purpose, scanned_paths):
    selected = None
    for path in paths:
        info = path_info(path)
        info.update({"dataset": dataset, "purpose": purpose, "selected": False})
        valid = info["is_dir"] if expected_kind == "dir" else info["is_file"]
        if selected is None and valid:
            selected = path
            info["selected"] = True
        scanned_paths.append(info)
    if selected is None and paths:
        selected = paths[0]
    return selected


def normalize_sample(value):
    name = Path(str(value).strip()).name
    suffixes = [
        ".duplicate_names.txt",
        ".duplicates.txt",
        ".rg.markdup.bam",
        ".markdup.bam",
        ".dedup.gam",
        ".gam",
        ".bam",
        ".txt",
    ]
    lowered = name.lower()
    for suffix in suffixes:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def list_gams(directory):
    result = {"records": {}, "duplicates": [], "errors": []}
    if not directory:
        return result
    root = Path(directory)
    if not root.exists() or not root.is_dir():
        return result
    grouped = {}
    try:
        for path in root.iterdir():
            if not path.is_file() or not path.name.lower().endswith(".gam"):
                continue
            sample = normalize_sample(path.name)
            stat = path.stat()
            grouped.setdefault(sample, []).append({
                "path": str(path),
                "size_bytes": stat.st_size,
                "mtime": time.strftime(
                    "%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)
                ),
                "nonzero": stat.st_size > 0,
            })
    except Exception as exc:
        result["errors"].append(str(exc))

    for sample, records in sorted(grouped.items()):
        records.sort(key=lambda row: row["path"])
        result["records"][sample] = records[0]
        if len(records) > 1:
            result["duplicates"].append({
                "sample": sample,
                "paths": [record["path"] for record in records],
            })
    return result


def choose_column(header, names):
    lowered = [item.strip().lower() for item in header]
    for name in names:
        if name in lowered:
            return lowered.index(name)
    return None


def parse_manifest(path):
    result = {
        "path": str(path) if path else "",
        "exists": False,
        "records": [],
        "duplicate_samples": [],
        "malformed_lines": [],
        "has_header": False,
    }
    if not path:
        return result
    manifest = Path(path)
    result["exists"] = manifest.exists() and manifest.is_file()
    if not result["exists"]:
        return result

    rows = []
    try:
        with manifest.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                fields = stripped.split("\t") if "\t" in stripped else stripped.split()
                rows.append((line_no, fields, stripped))
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if not rows:
        return result

    header_terms = {
        "sample", "sample_id", "id", "input", "input_gam", "gam",
        "output", "output_gam", "dedup_gam", "duplicate_names",
    }
    first_lower = {field.strip().lower() for field in rows[0][1]}
    has_header = bool(first_lower & header_terms)
    result["has_header"] = has_header
    header = rows[0][1] if has_header else []
    data_rows = rows[1:] if has_header else rows

    sample_col = choose_column(header, ["sample", "sample_id", "id"]) if header else None
    input_col = choose_column(header, ["input_gam", "input", "gam"]) if header else None
    output_col = choose_column(header, ["output_gam", "dedup_gam", "output"]) if header else None

    seen = {}
    for line_no, fields, original in data_rows:
        if not fields:
            continue

        input_gam = ""
        output_gam = ""
        if input_col is not None and input_col < len(fields):
            input_gam = fields[input_col]
        if output_col is not None and output_col < len(fields):
            output_gam = fields[output_col]

        gam_fields = [field for field in fields if field.lower().endswith(".gam")]
        if not input_gam and gam_fields:
            non_dedup = [field for field in gam_fields if "dedup" not in Path(field).name.lower()]
            input_gam = (non_dedup or gam_fields)[0]
        if not output_gam:
            dedup_fields = [field for field in gam_fields if "dedup" in Path(field).name.lower()]
            if dedup_fields:
                output_gam = dedup_fields[-1]

        if sample_col is not None and sample_col < len(fields):
            sample = normalize_sample(fields[sample_col])
        elif fields[0].lower().endswith((".gam", ".bam", ".txt")) or "/" in fields[0]:
            sample = normalize_sample(fields[0])
        else:
            sample = fields[0].strip()

        if not sample:
            result["malformed_lines"].append({"line_no": line_no, "line": original})
            continue

        record = {
            "line_no": line_no,
            "sample": sample,
            "input_gam": input_gam,
            "output_gam": output_gam,
        }
        result["records"].append(record)
        seen.setdefault(sample, []).append(line_no)

    result["duplicate_samples"] = [
        {"sample": sample, "line_numbers": lines}
        for sample, lines in sorted(seen.items())
        if len(lines) > 1
    ]
    return result


def expand_patterns(patterns, values):
    expanded = []
    for pattern in patterns:
        try:
            expanded.append(pattern.format(**values))
        except (KeyError, ValueError):
            expanded.append(pattern)
    return expanded


def discover_worker_assignments(patterns, dataset, scanned_paths):
    matches = []
    for pattern in patterns:
        matches.extend(glob.glob(pattern))
    matches = sorted(set(matches))
    assignments = {}
    parsed = []
    for path in matches:
        info = path_info(path)
        info.update({"dataset": dataset, "purpose": "worker_manifest", "selected": True})
        scanned_paths.append(info)
        manifest = parse_manifest(path)
        parsed.append({
            "path": path,
            "record_count": len(manifest["records"]),
            "duplicate_samples": manifest["duplicate_samples"],
            "malformed_lines": manifest["malformed_lines"],
        })
        for record in manifest["records"]:
            assignments.setdefault(record["sample"], []).append(path)
    return assignments, parsed


def tail_text(path, max_bytes):
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes), os.SEEK_SET)
            return handle.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


ERROR_PATTERN = re.compile(
    r"(?:\bERROR\b|\bFATAL\b|Traceback|OutOfMemory|out of memory|"
    r"oom-kill|slurmstepd: error|CANCELLED|TIMEOUT|No such file|"
    r"missing or empty|validation failed|MISMATCH)",
    re.IGNORECASE,
)
IGNORE_ERROR_PATTERN = re.compile(
    r"(?:\b0 errors?\b|\bno errors?\b|error rate)", re.IGNORECASE
)
COMPLETION_PATTERN = re.compile(
    r"(?:EXACT_TEMPLATE_PAIR_MATCH|VALIDATION(?: STATUS)?:?\s*PASS|"
    r"completed successfully|deduplication complete)",
    re.IGNORECASE,
)


def scan_logs(patterns, max_files, max_bytes, scanned_paths):
    matches = []
    for pattern in patterns:
        matches.extend(glob.glob(pattern))
    candidates = []
    for path in sorted(set(matches)):
        try:
            p = Path(path)
            if p.is_file():
                candidates.append((p.stat().st_mtime, path))
        except Exception:
            continue
    candidates.sort(reverse=True)
    selected = candidates[:max_files]

    errors = []
    completions = []
    files = []
    for _, path in selected:
        info = path_info(path)
        info.update({"dataset": "all", "purpose": "recent_log", "selected": True})
        scanned_paths.append(info)
        files.append(info)
        text = tail_text(path, max_bytes)
        error_lines = []
        completion_lines = []
        for line in text.splitlines():
            if ERROR_PATTERN.search(line) and not IGNORE_ERROR_PATTERN.search(line):
                error_lines.append(line[-1000:])
            if COMPLETION_PATTERN.search(line):
                completion_lines.append(line[-1000:])
        if error_lines:
            errors.append({"path": path, "matched_lines": error_lines[-30:]})
        if completion_lines:
            completions.append({"path": path, "matched_lines": completion_lines[-30:]})

    return {
        "files_considered": len(candidates),
        "files_scanned": files,
        "error_hits": errors,
        "completion_hits": completions,
        "truncated_to_recent_files": len(candidates) > len(selected),
    }


def run_command(argv, timeout=30):
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except Exception as exc:
        return {"argv": argv, "returncode": None, "stdout": "", "stderr": str(exc)}


def relevant_name(name, patterns):
    lowered = name.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def scan_jobs(user, account, patterns):
    squeue = run_command([
        "squeue", "-h", "-u", user,
        "-o", "%i|%a|%j|%T|%M|%l|%R",
    ])
    running = []
    if squeue["returncode"] == 0:
        for line in squeue["stdout"].splitlines():
            fields = line.split("|", 6)
            if len(fields) != 7:
                continue
            job_id, job_account, name, state, elapsed, time_limit, reason = fields
            if relevant_name(name, patterns) and (not account or job_account == account):
                running.append({
                    "job_id": job_id,
                    "account": job_account,
                    "name": name,
                    "state": state,
                    "elapsed": elapsed,
                    "time_limit": time_limit,
                    "reason": reason,
                })

    sacct = run_command([
        "sacct", "-X", "-S", "today", "-n", "-P",
        "-o", "JobIDRaw,Account,JobName,State,Elapsed,ExitCode",
    ])
    recent = []
    if sacct["returncode"] == 0:
        for line in sacct["stdout"].splitlines():
            fields = line.split("|", 5)
            if len(fields) != 6:
                continue
            job_id, job_account, name, state, elapsed, exit_code = fields
            if relevant_name(name, patterns) and (not account or job_account == account):
                recent.append({
                    "job_id": job_id,
                    "account": job_account,
                    "name": name,
                    "state": state,
                    "elapsed": elapsed,
                    "exit_code": exit_code,
                })

    return {
        "running": running,
        "recent": recent[-200:],
        "squeue_returncode": squeue["returncode"],
        "squeue_stderr": squeue["stderr"][-2000:],
        "sacct_returncode": sacct["returncode"],
        "sacct_stderr": sacct["stderr"][-2000:],
    }


def scan_dataset(spec, project_root, scanned_paths):
    name = spec["name"]
    expected = int(spec["expected_samples"])
    input_dir = select_candidate(
        spec.get("input_dir_candidates", []), "dir", name, "input_dir", scanned_paths
    )
    output_dir = select_candidate(
        spec.get("output_dir_candidates", []), "dir", name, "output_dir", scanned_paths
    )
    manifest_path = select_candidate(
        spec.get("manifest_candidates", []), "file", name, "master_manifest", scanned_paths
    )

    values = {
        "project_root": project_root,
        "input_dir": input_dir or "",
        "output_dir": output_dir or "",
        "dataset": name,
    }
    worker_patterns = expand_patterns(spec.get("worker_manifest_globs", []), values)
    assignments, worker_manifests = discover_worker_assignments(
        worker_patterns, name, scanned_paths
    )

    manifest = parse_manifest(manifest_path)
    input_gams = list_gams(input_dir)
    output_gams = list_gams(output_dir)

    manifest_by_sample = {}
    for record in manifest["records"]:
        manifest_by_sample.setdefault(record["sample"], record)

    universe = set(manifest_by_sample)
    universe.update(input_gams["records"])
    universe.update(output_gams["records"])
    universe.update(assignments)

    samples = []
    for sample in sorted(universe):
        manifest_record = manifest_by_sample.get(sample, {})
        input_record = input_gams["records"].get(sample)
        output_record = output_gams["records"].get(sample)

        input_path = (input_record or {}).get("path") or manifest_record.get("input_gam", "")
        output_path = (output_record or {}).get("path") or manifest_record.get("output_gam", "")

        input_exists = bool(input_record and input_record.get("nonzero"))
        if not input_exists and input_path:
            info = path_info(input_path)
            input_exists = bool(info["is_file"] and (info["size_bytes"] or 0) > 0)

        output_exists = bool(output_record and output_record.get("nonzero"))
        if not output_exists and output_path:
            info = path_info(output_path)
            output_exists = bool(info["is_file"] and (info["size_bytes"] or 0) > 0)

        if output_exists:
            state = "output_present_unvalidated"
        elif not input_exists:
            state = "missing_input"
        elif sample in assignments:
            state = "assigned_pending_or_running"
        else:
            state = "pending_unassigned"

        samples.append({
            "sample": sample,
            "state": state,
            "input_gam": input_path,
            "input_exists_nonzero": input_exists,
            "input_size_bytes": (input_record or {}).get("size_bytes"),
            "output_gam": output_path,
            "output_exists_nonzero": output_exists,
            "output_size_bytes": (output_record or {}).get("size_bytes"),
            "worker_manifests": assignments.get(sample, []),
            "in_master_manifest": sample in manifest_by_sample,
        })

    counts = {
        "expected_samples": expected,
        "samples_observed": len(samples),
        "input_gams_nonzero": sum(row["input_exists_nonzero"] for row in samples),
        "outputs_present_unvalidated": sum(row["output_exists_nonzero"] for row in samples),
        "missing_inputs": sum(row["state"] == "missing_input" for row in samples),
        "assigned_without_output": sum(
            row["state"] == "assigned_pending_or_running" for row in samples
        ),
        "pending_unassigned": sum(row["state"] == "pending_unassigned" for row in samples),
        "master_manifest_records": len(manifest["records"]),
        "worker_manifests": len(worker_manifests),
    }

    return {
        "name": name,
        "expected_samples": expected,
        "selected_paths": {
            "input_dir": input_dir or "",
            "output_dir": output_dir or "",
            "manifest": manifest_path or "",
        },
        "counts": counts,
        "manifest": {
            "path": manifest["path"],
            "exists": manifest["exists"],
            "record_count": len(manifest["records"]),
            "duplicate_samples": manifest["duplicate_samples"],
            "malformed_lines": manifest["malformed_lines"],
        },
        "worker_manifests": worker_manifests,
        "input_duplicate_ids": input_gams["duplicates"],
        "output_duplicate_ids": output_gams["duplicates"],
        "directory_errors": input_gams["errors"] + output_gams["errors"],
        "samples": samples,
    }


def main():
    scanned_paths = []
    project_root = CONFIG.get("project_root", "/scratch/project_2001113")
    datasets = [
        scan_dataset(spec, project_root, scanned_paths)
        for spec in CONFIG.get("datasets", [])
    ]

    log_patterns = []
    for spec, dataset in zip(CONFIG.get("datasets", []), datasets):
        selected = dataset["selected_paths"]
        values = {
            "project_root": project_root,
            "input_dir": selected["input_dir"],
            "output_dir": selected["output_dir"],
            "dataset": dataset["name"],
        }
        log_patterns.extend(expand_patterns(spec.get("log_globs", []), values))

    logs = scan_logs(
        log_patterns,
        int(CONFIG.get("max_recent_log_files", 40)),
        int(CONFIG.get("max_log_tail_bytes", 65536)),
        scanned_paths,
    )
    jobs = scan_jobs(
        CONFIG.get("user", "tuomas64"),
        CONFIG.get("account", ""),
        CONFIG.get("job_name_patterns", ["dedup"]),
    )

    observation = {
        "scanned_at_cluster_time": now_local_iso(),
        "project_root": project_root,
        "datasets": datasets,
        "jobs": jobs,
        "logs": logs,
        "scanned_paths": scanned_paths,
        "io_policy": {
            "gam_content_read": False,
            "checksums_computed": False,
            "vg_stats_run": False,
            "log_tail_bytes_per_file": int(CONFIG.get("max_log_tail_bytes", 65536)),
            "note": "Only directory metadata, manifests, scheduler state and bounded log tails were read.",
        },
    }
    print(json.dumps(observation, separators=(",", ":")))


main()
'''


def build_remote_program(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(config), separators=(",", ":"))
    return "import json\nCONFIG = json.loads(" + repr(encoded) + ")\n" + REMOTE_OBSERVER


def validate_config(config: Mapping[str, Any]) -> None:
    datasets = config.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise TaskScanError("GAM deduplication configuration requires a non-empty datasets list.")

    seen = set()
    for index, dataset in enumerate(datasets, start=1):
        if not isinstance(dataset, Mapping):
            raise TaskScanError(f"Dataset entry {index} must be a JSON object.")
        name = str(dataset.get("name", "")).strip()
        if not name:
            raise TaskScanError(f"Dataset entry {index} has no name.")
        if name in seen:
            raise TaskScanError(f"Dataset name occurs more than once: {name}")
        seen.add(name)

        expected = dataset.get("expected_samples")
        if not isinstance(expected, int) or expected <= 0:
            raise TaskScanError(
                f"Dataset {name} requires a positive integer expected_samples value."
            )
        for field in (
            "input_dir_candidates",
            "output_dir_candidates",
            "manifest_candidates",
            "worker_manifest_globs",
            "log_globs",
        ):
            value = dataset.get(field)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise TaskScanError(f"Dataset {name} requires a list of strings in {field}.")


class GamDeduplicationProfile:
    name = "gam_deduplication"
    title = "GAM duplicate removal"

    def collect(
        self,
        runner: RemoteRunner,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        validate_config(config)
        timeout = int(config.get("remote_timeout_seconds", 240))
        return runner.run_python(build_remote_program(config), timeout_seconds=timeout)

    def interpret(
        self,
        data: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        datasets = list(data.get("datasets", []))
        running_jobs = list(data.get("jobs", {}).get("running", []))
        recent_jobs = list(data.get("jobs", {}).get("recent", []))
        error_hits = list(data.get("logs", {}).get("error_hits", []))

        expected_total = sum(int(item.get("expected_samples", 0)) for item in datasets)
        observed_total = sum(int(item.get("counts", {}).get("samples_observed", 0)) for item in datasets)
        output_total = sum(
            int(item.get("counts", {}).get("outputs_present_unvalidated", 0))
            for item in datasets
        )
        missing_total = sum(
            int(item.get("counts", {}).get("missing_inputs", 0)) for item in datasets
        )

        failed_states = ("FAILED", "OUT_OF_MEMORY", "TIMEOUT", "CANCELLED", "NODE_FAIL")
        failed_jobs = [
            job for job in recent_jobs
            if any(str(job.get("state", "")).upper().startswith(state) for state in failed_states)
        ]

        dataset_statuses = []
        warnings = []
        for dataset in datasets:
            counts = dataset.get("counts", {})
            expected = int(dataset.get("expected_samples", 0))
            outputs = int(counts.get("outputs_present_unvalidated", 0))
            missing = int(counts.get("missing_inputs", 0))
            inputs = int(counts.get("input_gams_nonzero", 0))
            observed = int(counts.get("samples_observed", 0))

            if outputs == expected and expected > 0:
                state = "ready_for_content_validation"
            elif missing:
                state = "blocked_missing_inputs"
            elif observed or inputs or outputs:
                state = "running" if running_jobs else "incomplete_no_active_job"
            else:
                state = "not_started_or_not_found"

            dataset_statuses.append({
                "dataset": dataset.get("name", ""),
                "status": state,
                "expected_samples": expected,
                "input_gams_nonzero": inputs,
                "outputs_present_unvalidated": outputs,
                "missing_inputs": missing,
            })

            if outputs > expected:
                warnings.append(
                    f"{dataset.get('name')}: found {outputs} output GAM IDs, "
                    f"which exceeds the expected {expected}."
                )
            if observed and observed != expected:
                warnings.append(
                    f"{dataset.get('name')}: observed {observed} sample IDs across manifests "
                    f"and GAM directories; expected {expected}."
                )
            if dataset.get("manifest", {}).get("duplicate_samples"):
                warnings.append(
                    f"{dataset.get('name')}: duplicate sample IDs occur in the selected manifest."
                )
            if dataset.get("manifest", {}).get("malformed_lines"):
                warnings.append(
                    f"{dataset.get('name')}: malformed manifest lines were detected."
                )
            if dataset.get("input_duplicate_ids"):
                warnings.append(
                    f"{dataset.get('name')}: multiple input GAM files normalize to the same sample ID."
                )
            if dataset.get("output_duplicate_ids"):
                warnings.append(
                    f"{dataset.get('name')}: multiple output GAM files normalize to the same sample ID."
                )

        all_ready = bool(dataset_statuses) and all(
            item["status"] == "ready_for_content_validation"
            for item in dataset_statuses
        )

        if all_ready:
            overall = "ready_for_content_validation"
            next_action = "validate_deduplicated_gams_before_vg_pack"
        elif running_jobs and (error_hits or failed_jobs):
            overall = "running_with_warnings"
            next_action = "inspect_recent_errors_while_other_workers_continue"
        elif running_jobs:
            overall = "running"
            next_action = "wait_and_rescan"
        elif error_hits or failed_jobs:
            overall = "attention_required"
            next_action = "inspect_errors_and_prepare_only_failed_samples_for_rerun"
        elif missing_total:
            overall = "incomplete_inputs"
            next_action = "review_missing_inputs_and_manifest_paths"
        elif output_total:
            overall = "incomplete_no_active_job"
            next_action = "review_pending_samples_before_any_rerun"
        else:
            overall = "not_started_or_paths_not_found"
            next_action = "verify_configured_paths"

        if error_hits:
            warnings.append(
                f"Bounded tails of recent logs contain {len(error_hits)} error-like hit groups."
            )
        if failed_jobs:
            warnings.append(f"Scheduler history contains {len(failed_jobs)} failed job records.")
        if output_total:
            warnings.append(
                "Output counts are metadata-only completion candidates; GAM content is not yet validated."
            )

        return {
            "overall_status": overall,
            "current_stage": "gam_deduplication",
            "next_safe_action": next_action,
            "counts": {
                "expected_samples": expected_total,
                "samples_observed": observed_total,
                "outputs_present_unvalidated": output_total,
                "missing_inputs": missing_total,
                "running_relevant_jobs": len(running_jobs),
                "recent_failed_job_records": len(failed_jobs),
                "recent_error_hit_groups": len(error_hits),
            },
            "datasets": dataset_statuses,
            "warnings": warnings,
            "validation_boundary": (
                "A non-empty output GAM is counted only as present. Content-level validation, "
                "duplicate-template accounting and residual-duplication QC remain separate steps."
            ),
        }

    def write_artifacts(
        self,
        payload: Mapping[str, Any],
        scan_dir: Path,
    ) -> Sequence[Path]:
        observation = payload["observation"]
        status = payload["status_summary"]

        dataset_summary_path = scan_dir / "dataset_summary.tsv"
        write_tsv(
            dataset_summary_path,
            status["datasets"],
            [
                "dataset", "status", "expected_samples", "input_gams_nonzero",
                "outputs_present_unvalidated", "missing_inputs",
            ],
        )

        sample_rows = []
        for dataset in observation.get("datasets", []):
            for row in dataset.get("samples", []):
                sample_rows.append({"dataset": dataset.get("name", ""), **row})
        sample_status_path = scan_dir / "sample_status.tsv"
        write_tsv(
            sample_status_path,
            sample_rows,
            [
                "dataset", "sample", "state", "input_gam", "input_exists_nonzero",
                "input_size_bytes", "output_gam", "output_exists_nonzero",
                "output_size_bytes", "worker_manifests", "in_master_manifest",
            ],
        )

        missing_rows = [
            row for row in sample_rows if row.get("state") == "missing_input"
        ]
        missing_inputs_path = scan_dir / "missing_inputs.tsv"
        write_tsv(
            missing_inputs_path,
            missing_rows,
            ["dataset", "sample", "input_gam", "worker_manifests", "in_master_manifest"],
        )

        running_jobs_path = scan_dir / "running_jobs.tsv"
        write_tsv(
            running_jobs_path,
            observation.get("jobs", {}).get("running", []),
            ["job_id", "account", "name", "state", "elapsed", "time_limit", "reason"],
        )

        recent_jobs_path = scan_dir / "recent_jobs.tsv"
        write_tsv(
            recent_jobs_path,
            observation.get("jobs", {}).get("recent", []),
            ["job_id", "account", "name", "state", "elapsed", "exit_code"],
        )

        scanned_paths_path = scan_dir / "scanned_paths.tsv"
        write_tsv(
            scanned_paths_path,
            observation.get("scanned_paths", []),
            [
                "dataset", "purpose", "selected", "path", "exists", "is_file",
                "is_dir", "size_bytes", "mtime", "error",
            ],
        )

        errors_path = scan_dir / "recent_errors.txt"
        error_lines = []
        for hit in observation.get("logs", {}).get("error_hits", []):
            error_lines.extend(["=" * 80, hit.get("path", "")])
            error_lines.extend(hit.get("matched_lines", []))
            error_lines.append("")
        errors_path.write_text("\n".join(error_lines), encoding="utf-8")

        return (
            dataset_summary_path,
            sample_status_path,
            missing_inputs_path,
            running_jobs_path,
            recent_jobs_path,
            scanned_paths_path,
            errors_path,
        )

    def render_report(
        self,
        payload: Mapping[str, Any],
        scan_dir: Path,
    ) -> str:
        observation = payload["observation"]
        status = payload["status_summary"]
        lines = [
            "# GenomeAgent Task Scan: GAM duplicate removal",
            "",
            f"Scan directory: `{scan_dir}`",
            f"Cluster time: `{observation.get('scanned_at_cluster_time', '')}`",
            f"Overall status: **{status['overall_status']}**",
            f"Current stage: **{status['current_stage']}**",
            f"Next safe action: **{status['next_safe_action']}**",
            "",
            "## Dataset progress",
            "",
            "| Dataset | Status | Inputs | Output candidates | Expected | Missing inputs |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for item in status["datasets"]:
            lines.append(
                f"| {item['dataset']} | {item['status']} | "
                f"{item['input_gams_nonzero']} | {item['outputs_present_unvalidated']} | "
                f"{item['expected_samples']} | {item['missing_inputs']} |"
            )

        lines.extend(["", "## Combined counts", ""])
        for key, value in status["counts"].items():
            lines.append(f"- {key}: {value}")

        if status["warnings"]:
            lines.extend(["", "## Warnings", ""])
            lines.extend(f"- {warning}" for warning in status["warnings"])

        lines.extend([
            "",
            "## Validation boundary",
            "",
            status["validation_boundary"],
            "",
            "This scan does not run `vg stats`, compute checksums, read complete GAM files, "
            "submit jobs or modify Puhti. The running workflow therefore receives no additional "
            "high-volume storage I/O from GenomeAgent.",
            "",
            "## Output files",
            "",
            "- `task_scan.json`",
            "- `report.md`",
            "- `dataset_summary.tsv`",
            "- `sample_status.tsv`",
            "- `missing_inputs.tsv`",
            "- `running_jobs.tsv`",
            "- `recent_jobs.tsv`",
            "- `scanned_paths.tsv`",
            "- `recent_errors.txt`",
            "",
        ])
        return "\n".join(lines)
