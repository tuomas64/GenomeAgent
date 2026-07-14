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
            state = "pending"

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


def job_relevant(name, patterns):
    lowered = str(name).lower()
    return any(str(pattern).lower() in lowered for pattern in patterns)


def scheduler_observation(user, account, patterns, recent_days):
    queue_result = run_command([
        "squeue", "-u", user, "-h", "-o",
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
        "--format=JobIDRaw,Account,JobName%60,State,ExitCode,Elapsed",
    ], timeout=45)
    recent = []
    for line in history_result["stdout"].splitlines():
        fields = line.split("|")
        if len(fields) < 6:
            continue
        job_id, job_account, name, state, exit_code, elapsed = fields[:6]
        if account and job_account.strip() != account:
            continue
        if not job_relevant(name, patterns):
            continue
        recent.append({
            "job_id": job_id.strip(),
            "parent_job_id": parent_job_id(job_id),
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


def inspect_logs(patterns, max_files, max_bytes):
    if max_files <= 0:
        return {
            "candidate_count": 0,
            "checked": [],
            "error_hits": [],
            "completion_hits": [],
            "selection_policy": "disabled_by_max_recent_log_files",
        }

    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern))
    unique_paths = set(paths)
    candidates = sorted(unique_paths, key=log_candidate_rank, reverse=True)[:max_files]

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
        "candidate_count": len(unique_paths),
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
logs = inspect_logs(
    log_patterns,
    int(cfg.get("max_recent_log_files", 48)),
    int(cfg.get("max_log_tail_bytes", 65536)),
)
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


def build_remote_program(config: Mapping[str, Any]) -> str:
    validate_config(config)
    encoded = json.dumps(dict(config))
    return "import json\nCONFIG = json.loads(" + repr(encoded) + ")\n" + REMOTE_OBSERVER


def _failed_state(value: Any) -> bool:
    state = str(value or "").upper()
    return any(term in state for term in ("FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"))


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
        logs = data.get("logs", {})

        sample_count = int(sample_map.get("unique_samples") or 0)
        fallback_samples = int(config.get("expected_samples_fallback") or 0)
        expected_samples = sample_count or fallback_samples

        state_counts: dict[str, int] = {}
        for record in records:
            state = str(record.get("state", "unknown"))
            state_counts[state] = state_counts.get(state, 0) + 1

        expected_intervals = len(records)
        completed = state_counts.get("completed_atomic_publish_contract", 0)
        vcf_without_index = state_counts.get("vcf_present_index_missing", 0)
        index_without_vcf = state_counts.get("index_present_vcf_missing", 0)
        pending = state_counts.get("pending", 0)
        inconsistent_pairs = vcf_without_index + index_without_vcf

        ready_workspaces = sum(
            1 for item in workspaces if item.get("exists") and item.get("nonempty")
        )
        missing_workspaces = len(workspaces) - ready_workspaces

        running_jobs = list(jobs.get("running", []))
        recent_failed = [item for item in jobs.get("recent", []) if _failed_state(item.get("state"))]
        error_hits = list(logs.get("error_hits", []))
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
        if recent_failed:
            warnings.append(f"Scheduler history contains {len(recent_failed)} failed job records.")
        if error_hits:
            warnings.append(f"Bounded recent log tails contain {len(error_hits)} error-like groups.")

        table_invalid = bool(
            interval_table.get("malformed_lines")
            or interval_table.get("task_line_mismatches")
            or interval_table.get("duplicate_task_ids")
            or interval_table.get("duplicate_output_paths")
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
        elif completed == expected_intervals and not inconsistent_pairs:
            overall = "scattered_genotyping_complete"
            current_stage = "gather_or_merge"
            next_action = "review_interval_completeness_then_prepare_gather"
        elif running_jobs and (error_hits or recent_failed or inconsistent_pairs):
            overall = "running_with_warnings"
            current_stage = "scattered_genotypegvcfs"
            next_action = "inspect_current_errors_while_other_shards_continue"
        elif running_jobs:
            overall = "running"
            current_stage = "scattered_genotypegvcfs"
            next_action = "wait_and_rescan"
        elif inconsistent_pairs or error_hits or recent_failed:
            overall = "attention_required"
            current_stage = "scattered_genotypegvcfs"
            next_action = "inspect_failed_or_partial_shards_before_resubmission"
        elif completed:
            overall = "paused_incomplete"
            current_stage = "scattered_genotypegvcfs"
            next_action = "review_and_submit_remaining_batches"
        elif pending and not jobs.get("recent") and not logs.get("completion_hits"):
            overall = "ready_to_start"
            current_stage = "scattered_genotypegvcfs"
            next_action = "review_and_submit_first_batch"
        elif pending:
            overall = "paused_incomplete"
            current_stage = "scattered_genotypegvcfs"
            next_action = "review_and_submit_remaining_batches"
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
                "pending_intervals": pending,
                "vcf_present_index_missing": vcf_without_index,
                "index_present_vcf_missing": index_without_vcf,
                "ready_genomicsdb_workspaces": ready_workspaces,
                "expected_genomicsdb_workspaces": len(workspaces),
                "running_relevant_jobs": len(running_jobs),
                "recent_failed_job_records": len(recent_failed),
                "recent_error_hit_groups": len(error_hits),
                "final_vcf_pairs_detected": len(final_pairs),
            },
            "warnings": warnings,
            "validation_boundary": (
                "A non-empty final interval VCF/index pair is accepted as completed under the "
                "configured atomic publication contract: the worker validates the temporary VCF "
                "header and sample count before moving both files to their final paths. The scanner "
                "does not independently rerun bcftools across every interval."
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
        records = observation.get("interval_table", {}).get("records", [])

        interval_status_path = scan_dir / "interval_status.tsv"
        write_tsv(
            interval_status_path,
            records,
            [
                "line_number", "task", "chromosome", "contig", "start", "end",
                "interval", "state", "vcf", "vcf_exists_nonzero", "vcf_size_bytes",
                "vcf_mtime", "index", "index_exists_nonzero", "index_size_bytes",
                "index_mtime",
            ],
        )

        incomplete_path = scan_dir / "incomplete_intervals.tsv"
        write_tsv(
            incomplete_path,
            [row for row in records if row.get("state") != "completed_atomic_publish_contract"],
            ["task", "chromosome", "interval", "state", "vcf", "index"],
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
                "job_id", "parent_job_id", "account", "name", "state", "elapsed",
                "time_limit", "reason",
            ],
        )

        recent_jobs_path = scan_dir / "recent_jobs.tsv"
        write_tsv(
            recent_jobs_path,
            observation.get("jobs", {}).get("recent", []),
            ["job_id", "parent_job_id", "account", "name", "state", "exit_code", "elapsed"],
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
        error_lines = []
        for hit in observation.get("logs", {}).get("error_hits", []):
            error_lines.extend(["=" * 80, hit.get("path", "")])
            error_lines.extend(hit.get("matched_lines", []))
            error_lines.append("")
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
            "## Counts",
            "",
        ]
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
            "The scan reads the small interval and sample manifests, filesystem metadata, "
            "bounded log tails and scheduler state. It does not read complete VCFs, run "
            "`bcftools` or GATK, submit jobs, remove partial files or modify Puhti.",
            "",
            "## Output files",
            "",
            "- `task_scan.json`",
            "- `report.md`",
            "- `scatter_summary.tsv`",
            "- `scan_timings.tsv`",
            "- `interval_status.tsv`",
            "- `incomplete_intervals.tsv`",
            "- `workspace_status.tsv`",
            "- `running_jobs.tsv`",
            "- `recent_jobs.tsv`",
            "- `scanned_paths.tsv`",
            "- `recent_errors.txt`",
            "",
        ])
        return "\n".join(lines)
