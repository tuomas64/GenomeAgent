#!/usr/bin/env python3
"""Read-only task profile for interval-scattered GATK GenotypeGVCFs."""

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
import stat as stat_module
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path


cfg = CONFIG


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
        stat = p.stat()
        info["exists"] = True
        info["is_file"] = stat_module.S_ISREG(stat.st_mode)
        info["is_dir"] = stat_module.S_ISDIR(stat.st_mode)
        info["size_bytes"] = stat.st_size
        info["mtime"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)
        )
    except FileNotFoundError:
        pass
    except Exception as exc:
        info["error"] = str(exc)
    return info


def bulk_path_info(paths):
    """Inspect requested filenames with one directory scan per parent directory."""
    normalized = sorted({str(Path(path)) for path in paths})
    results = {
        path: {
            "path": path,
            "exists": False,
            "is_file": False,
            "is_dir": False,
            "size_bytes": None,
            "mtime": None,
        }
        for path in normalized
    }
    grouped = {}
    for path in normalized:
        source = Path(path)
        grouped.setdefault(str(source.parent), {}).setdefault(source.name, []).append(path)

    errors = []
    for parent, names in grouped.items():
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    requested = names.get(entry.name)
                    if not requested:
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=True)
                        info = {
                            "path": str(Path(parent) / entry.name),
                            "exists": True,
                            "is_file": stat_module.S_ISREG(stat.st_mode),
                            "is_dir": stat_module.S_ISDIR(stat.st_mode),
                            "size_bytes": stat.st_size,
                            "mtime": time.strftime(
                                "%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)
                            ),
                        }
                        for path in requested:
                            results[path] = {**info, "path": path}
                    except Exception as exc:
                        for path in requested:
                            results[path]["error"] = str(exc)
        except FileNotFoundError:
            continue
        except Exception as exc:
            errors.append({"directory": parent, "error": str(exc)})
    return results, errors


def directory_nonempty(path):
    try:
        p = Path(path)
        return p.is_dir() and next(p.iterdir(), None) is not None
    except Exception:
        return False


def select_candidate(paths, purpose, expected_kind, scanned_paths):
    selected = None
    for path in paths:
        info = path_info(path)
        info.update({"purpose": purpose, "selected": False})
        valid = info["is_dir"] if expected_kind == "dir" else info["is_file"]
        if selected is None and valid:
            selected = str(path)
            info["selected"] = True
        scanned_paths.append(info)
    if selected is None and paths:
        selected = str(paths[0])
    return selected


def format_value(value, context):
    try:
        return str(value).format(**context)
    except (KeyError, ValueError):
        return str(value)


def read_sample_map(path):
    result = {
        "path": str(path or ""),
        "exists": False,
        "valid_records": 0,
        "unique_samples": 0,
        "duplicate_samples": [],
        "malformed_lines": [],
    }
    if not path:
        return result
    source = Path(path)
    result["exists"] = source.is_file()
    if not result["exists"]:
        return result

    seen = {}
    try:
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                fields = stripped.split()
                if len(fields) < 2:
                    result["malformed_lines"].append({
                        "line_number": line_number,
                        "line": stripped,
                    })
                    continue
                sample = fields[0]
                seen.setdefault(sample, []).append(fields[1])
                result["valid_records"] += 1
    except Exception as exc:
        result["error"] = str(exc)
        return result

    result["unique_samples"] = len(seen)
    result["duplicate_samples"] = [
        {"sample": sample, "paths": paths}
        for sample, paths in sorted(seen.items())
        if len(paths) > 1
    ]
    return result


def resolve_output_path(value, outbase):
    candidate = Path(value)
    if not candidate.is_absolute() and outbase:
        candidate = Path(outbase) / candidate
    return candidate


def inspect_outputs_bulk(vcfs, index_suffixes):
    normalized_vcfs = sorted({str(Path(vcf)) for vcf in vcfs})
    suffixes = list(index_suffixes) or [".tbi"]
    requested = list(normalized_vcfs)
    for vcf in normalized_vcfs:
        requested.extend(vcf + suffix for suffix in suffixes)
    infos, errors = bulk_path_info(requested)

    outputs = {}
    for vcf in normalized_vcfs:
        vcf_info = infos[vcf]
        index_candidates = [vcf + suffix for suffix in suffixes]
        selected_index = index_candidates[0]
        for candidate in index_candidates:
            info = infos[candidate]
            if info["is_file"] and (info["size_bytes"] or 0) > 0:
                selected_index = candidate
                break
        index_info = infos[selected_index]
        vcf_nonzero = bool(vcf_info["is_file"] and (vcf_info["size_bytes"] or 0) > 0)
        index_nonzero = bool(index_info["is_file"] and (index_info["size_bytes"] or 0) > 0)

        if vcf_nonzero and index_nonzero:
            state = "completed_atomic_publish_contract"
        elif vcf_nonzero:
            state = "vcf_present_index_missing"
        elif index_nonzero:
            state = "index_present_vcf_missing"
        else:
            state = "output_absent"

        outputs[vcf] = {
            "state": state,
            "vcf": vcf,
            "vcf_exists_nonzero": vcf_nonzero,
            "vcf_size_bytes": vcf_info.get("size_bytes"),
            "vcf_mtime": vcf_info.get("mtime"),
            "index": selected_index,
            "index_exists_nonzero": index_nonzero,
            "index_size_bytes": index_info.get("size_bytes"),
            "index_mtime": index_info.get("mtime"),
        }
    return outputs, errors


def read_interval_table(path, outbase, index_suffixes):
    result = {
        "path": str(path or ""),
        "exists": False,
        "records": [],
        "malformed_lines": [],
        "task_line_mismatches": [],
        "duplicate_task_ids": [],
        "duplicate_output_paths": [],
        "output_scan_errors": [],
    }
    if not path:
        return result
    source = Path(path)
    result["exists"] = source.is_file()
    if not result["exists"]:
        return result

    seen_tasks = {}
    seen_outputs = {}
    try:
        with source.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.rstrip("\r\n")
                if not stripped:
                    result["malformed_lines"].append({
                        "line_number": line_number,
                        "reason": "blank_physical_line",
                        "line": "",
                    })
                    continue
                fields = stripped.split("\t")
                if len(fields) < 6:
                    result["malformed_lines"].append({
                        "line_number": line_number,
                        "reason": "expected_at_least_six_tab_fields",
                        "line": stripped,
                    })
                    continue
                task_raw, chromosome, contig, start_raw, end_raw, vcf_raw = fields[:6]
                try:
                    task = int(task_raw)
                    start = int(start_raw)
                    end = int(end_raw)
                except ValueError:
                    result["malformed_lines"].append({
                        "line_number": line_number,
                        "reason": "task_start_or_end_not_integer",
                        "line": stripped,
                    })
                    continue

                if task != line_number:
                    result["task_line_mismatches"].append({
                        "line_number": line_number,
                        "task": task,
                    })

                vcf = resolve_output_path(vcf_raw, outbase)
                interval = "%s:%s-%s" % (contig, start, end)
                record = {
                    "line_number": line_number,
                    "task": task,
                    "chromosome": chromosome,
                    "contig": contig,
                    "start": start,
                    "end": end,
                    "interval": interval,
                    "vcf": str(vcf),
                }
                result["records"].append(record)
                seen_tasks.setdefault(task, []).append(line_number)
                seen_outputs.setdefault(str(vcf), []).append(task)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    result["duplicate_task_ids"] = [
        {"task": task, "line_numbers": lines}
        for task, lines in sorted(seen_tasks.items())
        if len(lines) > 1
    ]
    result["duplicate_output_paths"] = [
        {"vcf": vcf, "tasks": tasks}
        for vcf, tasks in sorted(seen_outputs.items())
        if len(tasks) > 1
    ]
    outputs, output_errors = inspect_outputs_bulk(
        [record["vcf"] for record in result["records"]], index_suffixes
    )
    for record in result["records"]:
        record.update(outputs[record["vcf"]])
    result["output_scan_errors"] = output_errors
    return result


def inspect_workspaces(records, joint_dir, template):
    rows = []
    chromosomes = []
    seen = set()
    for record in records:
        chromosome = str(record["chromosome"])
        if chromosome not in seen:
            seen.add(chromosome)
            chromosomes.append(chromosome)

    context = {"joint_dir": joint_dir or ""}
    for chromosome in chromosomes:
        workspace = Path(format_value(template, {**context, "chromosome": chromosome}))
        info = path_info(workspace)
        rows.append({
            "chromosome": chromosome,
            "workspace": str(workspace),
            "exists": info["is_dir"],
            "nonempty": directory_nonempty(workspace),
            "has_callset_json": (workspace / "callset.json").is_file(),
            "has_vidmap_json": (workspace / "vidmap.json").is_file(),
            "has_tiledb_marker": (workspace / "__tiledb_workspace.tdb").exists(),
        })
    return rows


def run_command(argv, timeout=30):
    try:
        completed = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except Exception as exc:
        return {"returncode": None, "stdout": "", "stderr": str(exc)}


def parent_job_id(job_id):
    match = re.match(r"^(\d+)", str(job_id))
    return match.group(1) if match else ""


def array_task_id(job_id):
    match = re.match(r"^\d+_(\d+)$", str(job_id).strip())
    return int(match.group(1)) if match else None


def job_relevant(name, patterns):
    lowered = str(name).lower()
    return any(str(pattern).lower() in lowered for pattern in patterns)


def scheduler_observation(user, account, patterns, recent_days):
    queue_result = run_command([
        "squeue", "-r", "-u", user, "-h", "-o",
        "%i|%a|%j|%T|%M|%l|%R",
    ], timeout=30)
    running = []
    for line in queue_result["stdout"].splitlines():
        fields = line.split("|", 6)
        if len(fields) != 7:
            continue
        job_id, job_account, name, state, elapsed, time_limit, reason = fields
        if account and job_account.strip() != account:
            continue
        if not job_relevant(name, patterns):
            continue
        running.append({
            "job_id": job_id.strip(),
            "parent_job_id": parent_job_id(job_id),
            "array_task_id": array_task_id(job_id),
            "account": job_account.strip(),
            "name": name.strip(),
            "state": state.strip(),
            "elapsed": elapsed.strip(),
            "time_limit": time_limit.strip(),
            "reason": reason.strip(),
        })

    start_date = (datetime.now() - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    history_result = run_command([
        "sacct", "-X", "-n", "-P", "-S", start_date,
        "--format=JobID%40,JobIDRaw,Account,JobName%60,State,ExitCode,Elapsed",
    ], timeout=45)
    recent = []
    for line in history_result["stdout"].splitlines():
        fields = line.split("|")
        if len(fields) < 7:
            continue
        job_id, job_id_raw, job_account, name, state, exit_code, elapsed = fields[:7]
        if account and job_account.strip() != account:
            continue
        if not job_relevant(name, patterns):
            continue
        recent.append({
            "job_id": job_id.strip(),
            "job_id_raw": job_id_raw.strip(),
            "parent_job_id": parent_job_id(job_id),
            "array_task_id": array_task_id(job_id),
            "account": job_account.strip(),
            "name": name.strip(),
            "state": state.strip(),
            "exit_code": exit_code.strip(),
            "elapsed": elapsed.strip(),
        })

    return {
        "running": running,
        "recent": recent,
        "squeue_returncode": queue_result["returncode"],
        "squeue_stderr": queue_result["stderr"],
        "sacct_returncode": history_result["returncode"],
        "sacct_stderr": history_result["stderr"],
    }


def extract_job_id(path):
    matches = re.findall(r"(?<!\d)(\d{6,})(?:_(\d+))?(?!\d)", Path(path).name)
    if not matches:
        return "", ""
    parent, task = matches[-1]
    job_id = parent + ("_" + task if task else "")
    return job_id, parent


def read_log_tail(path, max_bytes):
    try:
        with Path(path).open("rb") as handle:
            stat = os.fstat(handle.fileno())
            size = stat.st_size
            if size > max_bytes:
                handle.seek(size - max_bytes)
            text = handle.read().decode("utf-8", errors="replace")
        return {
            "text": text,
            "size_bytes": size,
            "mtime": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)
            ),
        }
    except Exception as exc:
        return {
            "text": "[Could not read %s: %s]" % (path, exc),
            "size_bytes": None,
            "mtime": None,
            "error": str(exc),
        }


def log_candidate_rank(path):
    job_id, parent = extract_job_id(path)
    try:
        parent_number = int(parent)
    except ValueError:
        parent_number = 0
    task_number = 0
    if "_" in job_id:
        try:
            task_number = int(job_id.rsplit("_", 1)[1])
        except ValueError:
            task_number = 0
    suffix = Path(path).suffix.lower()
    error_priority = 1 if suffix in {".err", ".e"} else 0
    return parent_number, error_priority, task_number, Path(path).name


def inspect_logs(patterns, max_files, max_bytes, allowed_parent_ids=None):
    if max_files <= 0:
        return {
            "discovered_candidate_count": 0,
            "candidate_count": 0,
            "checked": [],
            "error_hits": [],
            "completion_hits": [],
            "selection_policy": "disabled_by_max_recent_log_files",
        }

    allowed = None if allowed_parent_ids is None else set(allowed_parent_ids)
    if allowed is not None and not allowed:
        return {
            "discovered_candidate_count": 0,
            "candidate_count": 0,
            "checked": [],
            "error_hits": [],
            "completion_hits": [],
            "selection_policy": "failed_jobs_only_no_failed_scheduler_ids",
        }

    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern))
    unique_paths = set(paths)
    if allowed is None:
        eligible_paths = unique_paths
    else:
        eligible_paths = {
            path for path in unique_paths
            if extract_job_id(path)[1] in allowed
        }
    candidates = sorted(eligible_paths, key=log_candidate_rank, reverse=True)[:max_files]

    error_patterns = [
        "error", "exception", "traceback", "out of memory", "outofmemory",
        "killed", "cancelled", "failed", "no space left", "segmentation fault",
    ]
    error_hits = []
    completion_hits = []
    checked = []
    for path in candidates:
        tail = read_log_tail(path, max_bytes)
        checked.append({
            "path": path,
            "size_bytes": tail.get("size_bytes"),
            "mtime": tail.get("mtime"),
            "error": tail.get("error", ""),
        })
        text = tail["text"]
        lines = text.splitlines()
        matched = [line[-500:] for line in lines if any(p in line.lower() for p in error_patterns)]
        completed = [line[-500:] for line in lines if "Window completed successfully" in line]
        job_id, parent = extract_job_id(path)
        if matched:
            error_hits.append({
                "path": path,
                "job_id": job_id,
                "parent_job_id": parent,
                "matched_lines": matched[-30:],
            })
        if completed:
            completion_hits.append({
                "path": path,
                "job_id": job_id,
                "parent_job_id": parent,
                "matched_lines": completed[-10:],
            })
    return {
        "discovered_candidate_count": len(unique_paths),
        "candidate_count": len(eligible_paths),
        "checked": checked,
        "error_hits": error_hits,
        "completion_hits": completion_hits,
        "selection_policy": (
            "newest_slurm_parent_ids_first_then_error_logs_before_stdout; "
            "metadata_and tails limited before file access"
        ),
    }


def inspect_final_candidates(patterns, index_suffixes):
    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern))
    outputs, _ = inspect_outputs_bulk(paths, index_suffixes)
    return [outputs[path] for path in sorted(outputs)]


scan_started = time.monotonic()
timings = {}

phase_started = time.monotonic()
scanned_paths = []
joint_dir = select_candidate(
    cfg.get("joint_dir_candidates", []), "joint_dir", "dir", scanned_paths
)
context = {"joint_dir": joint_dir or ""}
outbase_candidates = [format_value(x, context) for x in cfg.get("outbase_candidates", [])]
outbase = select_candidate(outbase_candidates, "outbase", "dir", scanned_paths)
context["outbase"] = outbase or ""

sample_map_candidates = [format_value(x, context) for x in cfg.get("sample_map_candidates", [])]
sample_map_path = select_candidate(sample_map_candidates, "sample_map", "file", scanned_paths)
interval_candidates = [format_value(x, context) for x in cfg.get("interval_table_candidates", [])]
interval_path = select_candidate(interval_candidates, "interval_table", "file", scanned_paths)
reference_candidates = [format_value(x, context) for x in cfg.get("reference_candidates", [])]
reference_path = select_candidate(reference_candidates, "reference", "file", scanned_paths)
timings["path_selection"] = round(time.monotonic() - phase_started, 3)

phase_started = time.monotonic()
sample_map = read_sample_map(sample_map_path)
timings["sample_map"] = round(time.monotonic() - phase_started, 3)

phase_started = time.monotonic()
index_suffixes = cfg.get("index_suffixes", [".tbi", ".csi"])
interval_table = read_interval_table(interval_path, outbase, index_suffixes)
timings["interval_table_and_outputs"] = round(time.monotonic() - phase_started, 3)

phase_started = time.monotonic()
workspaces = inspect_workspaces(
    interval_table["records"],
    joint_dir,
    cfg.get("workspace_template", "{joint_dir}/GenomicsDB_chr{chromosome}"),
)
timings["genomicsdb_workspaces"] = round(time.monotonic() - phase_started, 3)

log_patterns = [format_value(x, context) for x in cfg.get("log_globs", [])]
final_patterns = [format_value(x, context) for x in cfg.get("final_vcf_globs", [])]

phase_started = time.monotonic()
final_candidates = inspect_final_candidates(final_patterns, index_suffixes)
timings["final_candidates"] = round(time.monotonic() - phase_started, 3)

phase_started = time.monotonic()
jobs = scheduler_observation(
    cfg.get("user", ""),
    cfg.get("account", ""),
    cfg.get("job_name_patterns", []),
    int(cfg.get("recent_job_days", 7)),
)
timings["scheduler"] = round(time.monotonic() - phase_started, 3)

phase_started = time.monotonic()
log_scan_policy = cfg.get("log_scan_policy", "bounded_recent")
failed_parent_ids = {
    item.get("parent_job_id", "")
    for item in jobs.get("recent", [])
    if any(
        term in str(item.get("state", "")).upper()
        for term in ("FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY")
    )
}
failed_parent_ids.discard("")
allowed_log_parent_ids = failed_parent_ids if log_scan_policy == "failed_jobs_only" else None
logs = inspect_logs(
    log_patterns,
    int(cfg.get("max_recent_log_files", 48)),
    int(cfg.get("max_log_tail_bytes", 65536)),
    allowed_parent_ids=allowed_log_parent_ids,
)
logs["configured_policy"] = log_scan_policy
logs["failed_scheduler_parent_ids"] = sorted(failed_parent_ids)
timings["logs"] = round(time.monotonic() - phase_started, 3)
timings["total"] = round(time.monotonic() - scan_started, 3)

result = {
    "scanned_at_cluster_time": now_local_iso(),
    "selected_paths": {
        "joint_dir": joint_dir,
        "outbase": outbase,
        "sample_map": sample_map_path,
        "interval_table": interval_path,
        "reference": reference_path,
    },
    "scanned_paths": scanned_paths,
    "reference": {
        "fasta": path_info(reference_path) if reference_path else {},
        "fai": path_info(str(reference_path) + ".fai") if reference_path else {},
        "dict_candidates": (
            [path_info(str(Path(reference_path).with_suffix(".dict"))), path_info(str(reference_path) + ".dict")]
            if reference_path else []
        ),
    },
    "sample_map": sample_map,
    "interval_table": interval_table,
    "workspaces": workspaces,
    "final_vcf_candidates": final_candidates,
    "jobs": jobs,
    "logs": logs,
    "timings_seconds": timings,
    "publication_contract": cfg.get("publication_contract", {}),
    "io_policy": {
        "remote_mutation": False,
        "vcf_content_read": False,
        "bcftools_run": False,
        "checksums_computed": False,
        "scheduler_observation_only": True,
    },
}

print(json.dumps(result, separators=(",", ":")))
'''


def validate_config(config: Mapping[str, Any]) -> None:
    required_lists = [
        "joint_dir_candidates",
        "outbase_candidates",
        "interval_table_candidates",
        "sample_map_candidates",
        "reference_candidates",
        "job_name_patterns",
        "log_globs",
    ]
    for key in required_lists:
        value = config.get(key)
        if not isinstance(value, list) or not value:
            raise TaskScanError(f"scattered_joint_calling requires a non-empty {key} list")

    contract = config.get("publication_contract")
    if not isinstance(contract, Mapping):
        raise TaskScanError("scattered_joint_calling requires publication_contract")
    if not contract.get("atomic_final_vcf_and_index_after_validation"):
        raise TaskScanError(
            "publication_contract must confirm atomic final VCF/index publication"
        )

    fallback = config.get("expected_samples_fallback")
    if fallback is not None and (not isinstance(fallback, int) or fallback <= 0):
        raise TaskScanError("expected_samples_fallback must be a positive integer")

    batches = config.get("scatter_batches")
    if not isinstance(batches, list) or not batches:
        raise TaskScanError("scattered_joint_calling requires scatter_batches")

    names = set()
    job_names = set()
    covered_tasks = set()
    for batch in batches:
        if not isinstance(batch, Mapping):
            raise TaskScanError("each scatter_batches entry must be an object")
        name = str(batch.get("name", "")).strip()
        job_name = str(batch.get("job_name", "")).strip()
        if not name or not job_name:
            raise TaskScanError("each scatter batch requires name and job_name")
        if name in names or job_name.lower() in job_names:
            raise TaskScanError("scatter batch names and job names must be unique")
        names.add(name)
        job_names.add(job_name.lower())

        try:
            array_start = int(batch["array_start"])
            array_end = int(batch["array_end"])
            offset = int(batch["offset"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskScanError(
                f"scatter batch {name} requires integer array_start, array_end and offset"
            ) from exc
        if array_start < 0 or array_end < array_start or offset < 0:
            raise TaskScanError(f"scatter batch {name} has an invalid array range or offset")
        for array_task in range(array_start, array_end + 1):
            interval_task = array_task + offset
            if interval_task in covered_tasks:
                raise TaskScanError(
                    f"scatter batch coverage overlaps at interval task {interval_task}"
                )
            covered_tasks.add(interval_task)

    expected_batches = config.get("expected_batches")
    if expected_batches is not None and int(expected_batches) != len(batches):
        raise TaskScanError(
            f"expected_batches is {expected_batches}, but {len(batches)} batches are configured"
        )


def build_remote_program(config: Mapping[str, Any]) -> str:
    validate_config(config)
    encoded = json.dumps(dict(config))
    return "import json\nCONFIG = json.loads(" + repr(encoded) + ")\n" + REMOTE_OBSERVER


def _failed_state(value: Any) -> bool:
    state = str(value or "").upper()
    return any(
        term in state
        for term in (
            "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
            "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED",
        )
    )


def _scheduler_state_category(value: Any) -> str:
    state = str(value or "").upper()
    if state.startswith(("PENDING", "CONFIGURING")):
        return "queued"
    if state.startswith(("RUNNING", "COMPLETING", "SUSPENDED", "RESIZING", "STAGE_OUT")):
        return "running"
    if _failed_state(state):
        return "failed_needs_review"
    if state.startswith("COMPLETED"):
        return "scheduler_completed_output_missing"
    return "submitted_unresolved"


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _classify_intervals(
    data: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    records = list(data.get("interval_table", {}).get("records", []))
    batches = [dict(item) for item in config.get("scatter_batches", [])]
    batch_by_job_name = {
        str(batch["job_name"]).lower(): batch for batch in batches
    }
    coverage: dict[int, dict[str, Any]] = {}
    for batch in batches:
        for array_task in range(int(batch["array_start"]), int(batch["array_end"]) + 1):
            coverage[array_task + int(batch["offset"])] = batch

    task_records = {
        int(record["task"]): record
        for record in records
        if _integer_or_none(record.get("task")) is not None
    }
    observed_batch_names = set()
    for task, record in task_records.items():
        batch = coverage.get(task)
        if batch and record.get("state") != "output_absent":
            observed_batch_names.add(str(batch["name"]))

    latest_attempt: dict[int, dict[str, Any]] = {}
    invalid_scheduler_mappings = []
    job_sources = (
        ("sacct", data.get("jobs", {}).get("recent", []), 0),
        ("squeue", data.get("jobs", {}).get("running", []), 1),
    )
    for source, rows, source_priority in job_sources:
        for row in rows:
            batch = batch_by_job_name.get(str(row.get("name", "")).lower())
            if batch is None:
                continue
            observed_batch_names.add(str(batch["name"]))
            array_task = _integer_or_none(row.get("array_task_id"))
            if array_task is None:
                continue
            if not int(batch["array_start"]) <= array_task <= int(batch["array_end"]):
                invalid_scheduler_mappings.append({
                    "job_id": row.get("job_id", ""),
                    "job_name": row.get("name", ""),
                    "array_task_id": array_task,
                    "reason": "array_task_outside_configured_batch_range",
                })
                continue
            interval_task = array_task + int(batch["offset"])
            if interval_task not in task_records:
                invalid_scheduler_mappings.append({
                    "job_id": row.get("job_id", ""),
                    "job_name": row.get("name", ""),
                    "array_task_id": array_task,
                    "interval_task": interval_task,
                    "reason": "mapped_interval_task_not_in_manifest",
                })
                continue
            parent = str(row.get("parent_job_id", ""))
            parent_number = _integer_or_none(parent) or 0
            candidate = {
                **dict(row),
                "source": source,
                "batch": batch["name"],
                "interval_task": interval_task,
                "array_task_id": array_task,
                "rank": (parent_number, source_priority),
            }
            previous = latest_attempt.get(interval_task)
            if previous is None or candidate["rank"] > previous["rank"]:
                latest_attempt[interval_task] = candidate

    outbase = str(data.get("selected_paths", {}).get("outbase") or "")
    classified = []
    lifecycle_counts: dict[str, int] = {}
    for task in sorted(task_records):
        record = task_records[task]
        batch = coverage.get(task)
        attempt = latest_attempt.get(task)
        output_state = str(record.get("state", "output_absent"))

        if output_state == "completed_atomic_publish_contract":
            lifecycle_state = "completed"
        elif output_state in {"vcf_present_index_missing", "index_present_vcf_missing"}:
            lifecycle_state = "partial_output"
        elif attempt is not None:
            lifecycle_state = _scheduler_state_category(attempt.get("state"))
        elif batch and str(batch["name"]) in observed_batch_names:
            lifecycle_state = "submitted_unresolved"
        elif batch:
            lifecycle_state = "not_submitted"
        else:
            lifecycle_state = "unmapped_interval"

        parent = str(attempt.get("parent_job_id", "")) if attempt else ""
        array_task = attempt.get("array_task_id", "") if attempt else ""
        log_prefix = str(batch.get("log_prefix", "")) if batch else ""
        stdout_log = ""
        stderr_log = ""
        if outbase and log_prefix and parent and array_task != "":
            stdout_log = f"{outbase}/logs/{log_prefix}_{parent}_{array_task}.out"
            stderr_log = f"{outbase}/logs/{log_prefix}_{parent}_{array_task}.err"

        row = {
            **dict(record),
            "output_state": output_state,
            "lifecycle_state": lifecycle_state,
            "batch": batch.get("name", "") if batch else "",
            "batch_job_name": batch.get("job_name", "") if batch else "",
            "scheduler_job_id": attempt.get("job_id", "") if attempt else "",
            "scheduler_parent_job_id": parent,
            "scheduler_array_task_id": array_task,
            "scheduler_state": attempt.get("state", "") if attempt else "",
            "scheduler_source": attempt.get("source", "") if attempt else "",
            "stdout_log": stdout_log,
            "stderr_log": stderr_log,
        }
        classified.append(row)
        lifecycle_counts[lifecycle_state] = lifecycle_counts.get(lifecycle_state, 0) + 1

    batch_summaries = []
    for batch in batches:
        batch_rows = [row for row in classified if row["batch"] == batch["name"]]
        counts = {}
        for row in batch_rows:
            state = row["lifecycle_state"]
            counts[state] = counts.get(state, 0) + 1
        expected = int(batch["array_end"]) - int(batch["array_start"]) + 1
        active = counts.get("running", 0) + counts.get("queued", 0)
        attention = sum(
            counts.get(state, 0)
            for state in (
                "failed_needs_review", "partial_output",
                "scheduler_completed_output_missing", "unmapped_interval",
            )
        )
        if counts.get("completed", 0) == expected:
            batch_status = "completed"
        elif active and attention:
            batch_status = "active_with_warnings"
        elif active:
            batch_status = "active"
        elif attention:
            batch_status = "attention_required"
        elif counts.get("not_submitted", 0) == expected:
            batch_status = "not_submitted"
        else:
            batch_status = "submitted_incomplete"
        batch_summaries.append({
            "batch": batch["name"],
            "job_name": batch["job_name"],
            "status": batch_status,
            "expected_intervals": expected,
            "completed": counts.get("completed", 0),
            "running": counts.get("running", 0),
            "queued": counts.get("queued", 0),
            "failed_needs_review": counts.get("failed_needs_review", 0),
            "submitted_unresolved": counts.get("submitted_unresolved", 0),
            "not_submitted": counts.get("not_submitted", 0),
            "partial_output": counts.get("partial_output", 0),
            "scheduler_completed_output_missing": counts.get(
                "scheduler_completed_output_missing", 0
            ),
        })

    manifest_tasks = set(task_records)
    configured_tasks = set(coverage)
    return {
        "intervals": classified,
        "batches": batch_summaries,
        "lifecycle_counts": lifecycle_counts,
        "invalid_scheduler_mappings": invalid_scheduler_mappings,
        "manifest_tasks_without_batch": sorted(manifest_tasks - configured_tasks),
        "configured_tasks_without_manifest": sorted(configured_tasks - manifest_tasks),
    }


class ScatteredJointCallingProfile:
    """Monitor manifest-defined GenotypeGVCFs windows without touching VCF contents."""

    name = "scattered_joint_calling"
    title = "GATK scattered joint calling"

    def collect(
        self,
        runner: RemoteRunner,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        program = build_remote_program(config)
        return runner.run_python(
            program,
            timeout_seconds=int(config.get("remote_timeout_seconds", 240)),
        )

    def interpret(
        self,
        data: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        sample_map = data.get("sample_map", {})
        interval_table = data.get("interval_table", {})
        records = interval_table.get("records", [])
        workspaces = data.get("workspaces", [])
        jobs = data.get("jobs", {})
        classification = _classify_intervals(data, config)
        lifecycle = classification["lifecycle_counts"]

        sample_count = int(sample_map.get("unique_samples") or 0)
        fallback_samples = int(config.get("expected_samples_fallback") or 0)
        expected_samples = sample_count or fallback_samples

        output_state_counts: dict[str, int] = {}
        for record in records:
            state = str(record.get("state", "unknown"))
            output_state_counts[state] = output_state_counts.get(state, 0) + 1

        expected_intervals = len(records)
        completed = lifecycle.get("completed", 0)
        vcf_without_index = output_state_counts.get("vcf_present_index_missing", 0)
        index_without_vcf = output_state_counts.get("index_present_vcf_missing", 0)
        without_output = output_state_counts.get("output_absent", 0)
        inconsistent_pairs = vcf_without_index + index_without_vcf
        running = lifecycle.get("running", 0)
        queued = lifecycle.get("queued", 0)
        failed = lifecycle.get("failed_needs_review", 0)
        submitted_unresolved = lifecycle.get("submitted_unresolved", 0)
        not_submitted = lifecycle.get("not_submitted", 0)
        scheduler_completed_missing = lifecycle.get(
            "scheduler_completed_output_missing", 0
        )
        unmapped_intervals = lifecycle.get("unmapped_interval", 0)
        active_intervals = running + queued
        attention_intervals = (
            failed + inconsistent_pairs + scheduler_completed_missing + unmapped_intervals
        )

        ready_workspaces = sum(
            1 for item in workspaces if item.get("exists") and item.get("nonempty")
        )
        missing_workspaces = len(workspaces) - ready_workspaces

        running_jobs = list(jobs.get("running", []))
        scheduler_failed_records = [
            item for item in jobs.get("recent", []) if _failed_state(item.get("state"))
        ]
        final_pairs = [
            item for item in data.get("final_vcf_candidates", [])
            if item.get("state") == "completed_atomic_publish_contract"
        ]

        warnings = []
        if sample_count and fallback_samples and sample_count != fallback_samples:
            warnings.append(
                f"Sample map contains {sample_count} unique samples; fallback expectation is "
                f"{fallback_samples}. The sample map count is authoritative for this scan."
            )
        if sample_map.get("duplicate_samples"):
            warnings.append("The selected sample map contains duplicate sample identifiers.")
        if sample_map.get("malformed_lines"):
            warnings.append("Malformed sample-map lines were detected.")
        if interval_table.get("malformed_lines"):
            warnings.append(
                f"Interval table contains {len(interval_table['malformed_lines'])} malformed physical lines."
            )
        if interval_table.get("task_line_mismatches"):
            warnings.append(
                f"Interval table contains {len(interval_table['task_line_mismatches'])} TASK/line mismatches."
            )
        if interval_table.get("duplicate_task_ids"):
            warnings.append("Duplicate task identifiers were detected in the interval table.")
        if interval_table.get("duplicate_output_paths"):
            warnings.append("Multiple interval records resolve to the same output VCF path.")
        if interval_table.get("output_scan_errors"):
            warnings.append(
                f"Could not scan {len(interval_table['output_scan_errors'])} output directories."
            )
        if inconsistent_pairs:
            warnings.append(
                f"{inconsistent_pairs} interval outputs have only one member of the final VCF/index pair."
            )
        if failed:
            warnings.append(f"{failed} intervals have a latest scheduler attempt that failed.")
        if scheduler_completed_missing:
            warnings.append(
                f"{scheduler_completed_missing} scheduler-completed intervals have no published output pair."
            )
        if unmapped_intervals:
            warnings.append(f"{unmapped_intervals} interval rows are not covered by a configured batch.")
        if classification["invalid_scheduler_mappings"]:
            warnings.append(
                f"{len(classification['invalid_scheduler_mappings'])} scheduler records could not be mapped to interval rows."
            )
        if not_submitted:
            warnings.append(
                f"{not_submitted} intervals belong to batches with no submission evidence yet."
            )

        table_invalid = bool(
            interval_table.get("malformed_lines")
            or interval_table.get("task_line_mismatches")
            or interval_table.get("duplicate_task_ids")
            or interval_table.get("duplicate_output_paths")
            or classification["manifest_tasks_without_batch"]
            or classification["configured_tasks_without_manifest"]
        )

        if final_pairs:
            overall = "final_joint_vcf_detected"
            current_stage = "final_validation"
            next_action = "independently_validate_final_joint_vcf"
        elif not interval_table.get("exists") or expected_intervals == 0:
            overall = "configuration_or_interval_table_error"
            current_stage = "pre_scatter_validation"
            next_action = "verify_interval_table_path_and_contents"
        elif table_invalid:
            overall = "invalid_interval_table"
            current_stage = "pre_scatter_validation"
            next_action = "repair_interval_table_before_any_submission"
        elif missing_workspaces:
            overall = "blocked_missing_genomicsdb_workspaces"
            current_stage = "pre_scatter_validation"
            next_action = "inspect_missing_genomicsdb_workspaces"
        elif completed == expected_intervals and not attention_intervals:
            overall = "scattered_genotyping_complete"
            current_stage = "gather_or_merge"
            next_action = "review_interval_completeness_then_prepare_gather"
        elif active_intervals and attention_intervals:
            overall = "running_with_warnings"
            current_stage = "scattered_genotypegvcfs"
            next_action = "inspect_failed_intervals_while_other_shards_continue"
        elif active_intervals:
            overall = "running"
            current_stage = "scattered_genotypegvcfs"
            next_action = "wait_and_rescan"
        elif attention_intervals:
            overall = "attention_required"
            current_stage = "scattered_genotypegvcfs"
            next_action = "inspect_failed_or_partial_shards_before_resubmission"
        elif not_submitted:
            overall = "awaiting_submission"
            current_stage = "scattered_genotypegvcfs"
            next_action = "review_and_submit_next_unsubmitted_batch"
        elif submitted_unresolved:
            overall = "paused_incomplete"
            current_stage = "scattered_genotypegvcfs"
            next_action = "reconcile_submitted_intervals_without_recent_scheduler_evidence"
        else:
            overall = "ready_to_start"
            current_stage = "scattered_genotypegvcfs"
            next_action = "review_and_submit_first_batch"

        return {
            "overall_status": overall,
            "current_stage": current_stage,
            "next_safe_action": next_action,
            "counts": {
                "sample_map_unique_samples": sample_count,
                "expected_samples_per_interval": expected_samples,
                "expected_intervals": expected_intervals,
                "completed_atomic_publish_contract": completed,
                "without_published_output": without_output,
                "running_intervals": running,
                "queued_intervals": queued,
                "failed_needs_review": failed,
                "submitted_unresolved": submitted_unresolved,
                "not_submitted": not_submitted,
                "scheduler_completed_output_missing": scheduler_completed_missing,
                "unmapped_intervals": unmapped_intervals,
                "vcf_present_index_missing": vcf_without_index,
                "index_present_vcf_missing": index_without_vcf,
                "ready_genomicsdb_workspaces": ready_workspaces,
                "expected_genomicsdb_workspaces": len(workspaces),
                "active_scheduler_records": len(running_jobs),
                "scheduler_failed_records_observed": len(scheduler_failed_records),
                "final_vcf_pairs_detected": len(final_pairs),
            },
            "intervals": classification["intervals"],
            "batches": classification["batches"],
            "invalid_scheduler_mappings": classification["invalid_scheduler_mappings"],
            "warnings": warnings,
            "validation_boundary": (
                "A non-empty final interval VCF/index pair is accepted as completed under the "
                "configured atomic publication contract: the worker validates the temporary VCF "
                "header and sample count before moving both files to their final paths. The scanner "
                "does not independently rerun bcftools across every interval. Scheduler state is "
                "mapped with batch-specific array offsets; routine scans do not open log files."
            ),
            "expected_batches": config.get("expected_batches"),
        }

    def write_artifacts(
        self,
        payload: Mapping[str, Any],
        scan_dir: Path,
    ) -> Sequence[Path]:
        observation = payload["observation"]
        status = payload["status_summary"]
        records = status.get("intervals", [])

        interval_status_path = scan_dir / "interval_status.tsv"
        write_tsv(
            interval_status_path,
            records,
            [
                "line_number", "task", "batch", "batch_job_name", "chromosome",
                "contig", "start", "end", "interval", "lifecycle_state",
                "output_state", "scheduler_job_id", "scheduler_parent_job_id",
                "scheduler_array_task_id", "scheduler_state", "scheduler_source",
                "vcf", "vcf_exists_nonzero", "vcf_size_bytes", "vcf_mtime",
                "index", "index_exists_nonzero", "index_size_bytes", "index_mtime",
                "stdout_log", "stderr_log",
            ],
        )

        incomplete_path = scan_dir / "incomplete_intervals.tsv"
        write_tsv(
            incomplete_path,
            [row for row in records if row.get("lifecycle_state") != "completed"],
            [
                "task", "batch", "chromosome", "interval", "lifecycle_state",
                "output_state", "scheduler_job_id", "scheduler_array_task_id",
                "scheduler_state", "vcf", "index", "stdout_log", "stderr_log",
            ],
        )

        failed_states = {
            "failed_needs_review", "partial_output",
            "scheduler_completed_output_missing", "unmapped_interval",
        }
        failed_path = scan_dir / "failed_intervals.tsv"
        write_tsv(
            failed_path,
            [row for row in records if row.get("lifecycle_state") in failed_states],
            [
                "task", "batch", "chromosome", "interval", "lifecycle_state",
                "scheduler_job_id", "scheduler_parent_job_id",
                "scheduler_array_task_id", "scheduler_state", "stdout_log",
                "stderr_log", "vcf", "index",
            ],
        )

        batch_path = scan_dir / "batch_status.tsv"
        write_tsv(
            batch_path,
            status.get("batches", []),
            [
                "batch", "job_name", "status", "expected_intervals", "completed",
                "running", "queued", "failed_needs_review", "submitted_unresolved",
                "not_submitted", "partial_output",
                "scheduler_completed_output_missing",
            ],
        )

        workspace_path = scan_dir / "workspace_status.tsv"
        write_tsv(
            workspace_path,
            observation.get("workspaces", []),
            [
                "chromosome", "workspace", "exists", "nonempty", "has_callset_json",
                "has_vidmap_json", "has_tiledb_marker",
            ],
        )

        running_jobs_path = scan_dir / "running_jobs.tsv"
        write_tsv(
            running_jobs_path,
            observation.get("jobs", {}).get("running", []),
            [
                "job_id", "parent_job_id", "array_task_id", "account", "name",
                "state", "elapsed", "time_limit", "reason",
            ],
        )

        recent_jobs_path = scan_dir / "recent_jobs.tsv"
        write_tsv(
            recent_jobs_path,
            observation.get("jobs", {}).get("recent", []),
            [
                "job_id", "job_id_raw", "parent_job_id", "array_task_id", "account",
                "name", "state", "exit_code", "elapsed",
            ],
        )

        scanned_paths_path = scan_dir / "scanned_paths.tsv"
        write_tsv(
            scanned_paths_path,
            observation.get("scanned_paths", []),
            [
                "purpose", "selected", "path", "exists", "is_file", "is_dir",
                "size_bytes", "mtime", "error",
            ],
        )

        errors_path = scan_dir / "recent_errors.txt"
        error_lines = [
            "Routine log reading is disabled for this profile.",
            "Use failed_intervals.tsv to inspect only scheduler-confirmed failed task logs.",
        ]
        errors_path.write_text("\n".join(error_lines), encoding="utf-8")

        summary_path = scan_dir / "scatter_summary.tsv"
        write_tsv(summary_path, [status["counts"]], list(status["counts"].keys()))

        timings_path = scan_dir / "scan_timings.tsv"
        write_tsv(
            timings_path,
            [
                {"phase": phase, "seconds": seconds}
                for phase, seconds in observation.get("timings_seconds", {}).items()
            ],
            ["phase", "seconds"],
        )

        return (
            summary_path,
            timings_path,
            interval_status_path,
            incomplete_path,
            failed_path,
            batch_path,
            workspace_path,
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
        counts = status["counts"]
        lines = [
            "# GenomeAgent Task Scan: scattered joint calling",
            "",
            f"Scan directory: `{scan_dir}`",
            f"Cluster time: `{observation.get('scanned_at_cluster_time', '')}`",
            f"Overall status: **{status['overall_status']}**",
            f"Current stage: **{status['current_stage']}**",
            f"Next safe action: **{status['next_safe_action']}**",
            "",
            "## Workflow progress",
            "",
            "| Unit | Completed | Expected | Remaining |",
            "|---|---:|---:|---:|",
            f"| Samples represented per interval | {counts['sample_map_unique_samples']} | "
            f"{counts['expected_samples_per_interval']} | "
            f"{max(0, counts['expected_samples_per_interval'] - counts['sample_map_unique_samples'])} |",
            f"| GenomicsDB workspaces | {counts['ready_genomicsdb_workspaces']} | "
            f"{counts['expected_genomicsdb_workspaces']} | "
            f"{max(0, counts['expected_genomicsdb_workspaces'] - counts['ready_genomicsdb_workspaces'])} |",
            f"| Published interval VCF/index pairs | "
            f"{counts['completed_atomic_publish_contract']} | {counts['expected_intervals']} | "
            f"{max(0, counts['expected_intervals'] - counts['completed_atomic_publish_contract'])} |",
            "",
            "## Interval lifecycle",
            "",
            "| State | Intervals |",
            "|---|---:|",
            f"| Completed | {counts['completed_atomic_publish_contract']} |",
            f"| Running | {counts['running_intervals']} |",
            f"| Queued in Slurm | {counts['queued_intervals']} |",
            f"| Failed, needs review | {counts['failed_needs_review']} |",
            f"| Submitted, scheduler state unresolved | {counts['submitted_unresolved']} |",
            f"| Not submitted | {counts['not_submitted']} |",
            f"| Scheduler completed, output missing | {counts['scheduler_completed_output_missing']} |",
            f"| VCF/index pair incomplete | "
            f"{counts['vcf_present_index_missing'] + counts['index_present_vcf_missing']} |",
            "",
            "## Batch progress",
            "",
            "| Batch | Job name | Status | Complete | Running | Queued | Failed | Unresolved | Not submitted |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for batch in status.get("batches", []):
            lines.append(
                f"| {batch['batch']} | {batch['job_name']} | {batch['status']} | "
                f"{batch['completed']}/{batch['expected_intervals']} | "
                f"{batch['running']} | {batch['queued']} | "
                f"{batch['failed_needs_review']} | {batch['submitted_unresolved']} | "
                f"{batch['not_submitted']} |"
            )

        lines.extend([
            "",
            "## Counts",
            "",
        ])
        for key, value in counts.items():
            lines.append(f"- {key}: {value}")
        if status.get("expected_batches"):
            lines.append(f"- configured_submission_batches: {status['expected_batches']}")

        if status["warnings"]:
            lines.extend(["", "## Warnings", ""])
            lines.extend(f"- {warning}" for warning in status["warnings"])

        timings = observation.get("timings_seconds", {})
        if timings:
            lines.extend(["", "## Scan timings", ""])
            for phase, seconds in timings.items():
                lines.append(f"- {phase}: {seconds} seconds")

        lines.extend([
            "",
            "## Validation boundary",
            "",
            status["validation_boundary"],
            "",
            "The scan reads the small interval and sample manifests, filesystem metadata and "
            "scheduler state. Routine log opening is disabled. It does not read complete VCFs, "
            "run `bcftools` or GATK, submit jobs, remove partial files or modify Puhti.",
            "",
            "## Output files",
            "",
            "- `task_scan.json`",
            "- `report.md`",
            "- `scatter_summary.tsv`",
            "- `scan_timings.tsv`",
            "- `interval_status.tsv`",
            "- `incomplete_intervals.tsv`",
            "- `failed_intervals.tsv`",
            "- `batch_status.tsv`",
            "- `workspace_status.tsv`",
            "- `running_jobs.tsv`",
            "- `recent_jobs.tsv`",
            "- `scanned_paths.tsv`",
            "- `recent_errors.txt`",
            "",
        ])
        return "\n".join(lines)
