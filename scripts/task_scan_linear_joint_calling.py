#!/usr/bin/env python3
"""
GenomeAgent Task Scanner v1: linear_joint_calling

Purpose:
  Read-only scanner for the known GATK linear-reference joint-calling workflow.

Runs locally, connects to Puhti/Roihu with SSH, and writes:
  workspace/task_scans/linear_joint_calling/<timestamp>/
    task_scan.json
    report.md
    sample_status.tsv
    missing_outputs.tsv
    running_jobs.tsv
    recent_errors.txt

This script does NOT delete files, submit jobs, or modify the cluster.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REMOTE_SCANNER = r'''
import json
import os
import re
import subprocess
import time
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
        "is_symlink": False,
        "size_bytes": None,
        "mtime": None,
    }
    try:
        info["exists"] = p.exists()
        info["is_file"] = p.is_file()
        info["is_dir"] = p.is_dir()
        info["is_symlink"] = p.is_symlink()
        if info["exists"]:
            st = p.stat()
            info["size_bytes"] = st.st_size
            info["mtime"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(st.st_mtime))
    except Exception as e:
        info["error"] = str(e)
    return info


def file_exists_nonzero(path):
    try:
        p = Path(path)
        return p.exists() and p.is_file() and p.stat().st_size > 0
    except Exception:
        return False


def dir_exists_nonempty(path):
    try:
        p = Path(path)
        if not p.exists() or not p.is_dir():
            return False
        next(p.iterdir())
        return True
    except StopIteration:
        return False
    except Exception:
        return False


def run_shell(cmd, timeout=30):
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "cmd": cmd,
            "returncode": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }
    except Exception as e:
        return {
            "cmd": cmd,
            "returncode": None,
            "stdout": "",
            "stderr": str(e),
        }


def read_sample_map(path):
    p = Path(path)
    out = {
        "path": str(p),
        "exists": p.exists(),
        "line_count": 0,
        "valid_records": 0,
        "unique_samples": 0,
        "duplicate_samples": [],
        "malformed_lines": [],
        "records": [],
    }

    if not p.exists():
        return out

    seen = {}
    records = []

    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                out["line_count"] += 1
                parts = stripped.split()

                if len(parts) < 2:
                    out["malformed_lines"].append({
                        "line_no": line_no,
                        "line": stripped,
                    })
                    continue

                sample = parts[0]
                gvcf = parts[1]

                rec = {
                    "line_no": line_no,
                    "sample": sample,
                    "gvcf": gvcf,
                }
                records.append(rec)
                seen.setdefault(sample, []).append(gvcf)

        out["records"] = records
        out["valid_records"] = len(records)
        out["unique_samples"] = len(seen)
        out["duplicate_samples"] = [
            {
                "sample": sample,
                "count": len(paths),
                "paths": paths,
            }
            for sample, paths in sorted(seen.items())
            if len(paths) > 1
        ]

    except Exception as e:
        out["error"] = str(e)

    return out


def discover_samples(sample_map):
    records = sample_map.get("records", [])
    if records:
        ordered = []
        seen = set()
        sample_to_gvcf = {}
        for rec in records:
            sample = rec["sample"]
            if sample not in seen:
                ordered.append(sample)
                seen.add(sample)
                sample_to_gvcf[sample] = rec["gvcf"]
        return ordered, sample_to_gvcf

    # Fallback: infer samples from GVCF directory
    gvcf_dir = Path(cfg["gvcf_dir"])
    ordered = []
    sample_to_gvcf = {}
    if gvcf_dir.exists():
        for p in sorted(gvcf_dir.glob("*.g.vcf.gz")):
            sample = p.name.replace(".g.vcf.gz", "")
            ordered.append(sample)
            sample_to_gvcf[sample] = str(p)

    return ordered, sample_to_gvcf


def check_samples(samples, sample_to_gvcf):
    bam_dir = Path(cfg["bam_dir"])
    gvcf_dir = Path(cfg["gvcf_dir"])

    rows = []

    for sample in samples:
        bam = bam_dir / f"{sample}.bam"
        bai_short = bam_dir / f"{sample}.bai"
        bai_long = bam_dir / f"{sample}.bam.bai"

        gvcf = Path(sample_to_gvcf.get(sample, str(gvcf_dir / f"{sample}.g.vcf.gz")))
        gvcf_tbi = Path(str(gvcf) + ".tbi")
        gvcf_csi = Path(str(gvcf) + ".csi")

        bai_short_exists = file_exists_nonzero(bai_short)
        bai_long_exists = file_exists_nonzero(bai_long)

        if bai_short_exists and bai_long_exists:
            bam_index_style = "both"
        elif bai_short_exists:
            bam_index_style = "sample.bai"
        elif bai_long_exists:
            bam_index_style = "sample.bam.bai"
        else:
            bam_index_style = "missing"

        gvcf_tbi_exists = file_exists_nonzero(gvcf_tbi)
        gvcf_csi_exists = file_exists_nonzero(gvcf_csi)

        rows.append({
            "sample": sample,
            "bam": str(bam),
            "bam_exists": file_exists_nonzero(bam),
            "bam_index_style": bam_index_style,
            "bai_short": str(bai_short),
            "bai_short_exists": bai_short_exists,
            "bai_long": str(bai_long),
            "bai_long_exists": bai_long_exists,
            "gvcf": str(gvcf),
            "gvcf_exists": file_exists_nonzero(gvcf),
            "gvcf_index_exists": gvcf_tbi_exists or gvcf_csi_exists,
            "gvcf_tbi": str(gvcf_tbi),
            "gvcf_tbi_exists": gvcf_tbi_exists,
            "gvcf_csi": str(gvcf_csi),
            "gvcf_csi_exists": gvcf_csi_exists,
        })

    return rows


def reference_checks():
    ref = Path(cfg["reference"])
    fai = Path(str(ref) + ".fai")
    dict1 = ref.with_suffix(".dict")
    dict2 = Path(str(ref) + ".dict")

    return {
        "reference": path_info(ref),
        "fai": path_info(fai),
        "dict_candidates": [
            path_info(dict1),
            path_info(dict2),
        ],
        "dict_exists": dict1.exists() or dict2.exists(),
    }


def detect_genomicsdb_workspaces():
    joint_dir = Path(cfg["joint_dir"])
    configured_chromosomes = cfg["chromosomes"]

    detected_all = []
    if joint_dir.exists():
        try:
            detected_all = [
                p for p in joint_dir.iterdir()
                if p.is_dir() and "genomicsdb" in p.name.lower()
            ]
        except Exception:
            detected_all = []

    workspaces = []
    configured_existing = 0

    for chrom in configured_chromosomes:
        candidates = [
            joint_dir / f"GenomicsDB_{chrom}",
            joint_dir / f"genomicsdb_{chrom}",
            joint_dir / f"{chrom}_GenomicsDB",
            joint_dir / f"{chrom}_genomicsdb",
            joint_dir / f"{chrom}.genomicsdb",
        ]

        chosen = candidates[0]
        for c in candidates:
            if c.exists():
                chosen = c
                break

        if chosen.exists():
            configured_existing += 1

        workspaces.append(workspace_info(chrom, chosen))

    # If configured names do not match but GenomicsDB directories exist,
    # switch to discovered names. This makes the scanner tolerant to naming.
    if configured_existing == 0 and detected_all:
        workspaces = []
        for p in sorted(detected_all):
            label = p.name
            label = re.sub(r"(?i)^genomicsdb[_\-.]?", "", label)
            label = re.sub(r"(?i)[_\-.]?genomicsdb$", "", label)
            if not label:
                label = p.name
            workspaces.append(workspace_info(label, p))

    detected_paths = [str(p) for p in sorted(detected_all)]

    return {
        "joint_dir": str(joint_dir),
        "detected_genomicsdb_dirs": detected_paths,
        "workspaces": workspaces,
    }


def workspace_info(label, path):
    p = Path(path)
    children = []
    if p.exists() and p.is_dir():
        try:
            children = sorted([x.name for x in p.iterdir()])[:30]
        except Exception:
            children = []

    return {
        "label": label,
        "path": str(p),
        "exists": p.exists(),
        "nonempty": dir_exists_nonempty(p),
        "has_callset_json": (p / "callset.json").exists(),
        "has_vidmap_json": (p / "vidmap.json").exists(),
        "has_tiledb_marker": (p / "__tiledb_workspace.tdb").exists(),
        "sample_children": children,
    }


def choose_best_vcf(candidates):
    def rank(p):
        name = p.name.lower()
        score = 0
        if "joint" in name:
            score += 10
        if "genotyped" in name:
            score += 8
        if "genotypegvcfs" in name:
            score += 8
        if "final" in name or "merged" in name or "cohort" in name or "all" in name:
            score += 4
        return -score, len(name), name

    candidates = sorted(candidates, key=rank)
    return candidates[0] if candidates else None


def detect_genotype_vcfs(workspaces):
    joint_dir = Path(cfg["joint_dir"])
    labels = [w["label"] for w in workspaces]
    by_chrom = []

    all_vcfs = []
    if joint_dir.exists():
        all_vcfs = [
            p for p in sorted(joint_dir.glob("*.vcf.gz"))
            if not p.name.endswith(".g.vcf.gz")
        ]

    for label in labels:
        candidates = [
            p for p in all_vcfs
            if label in p.name
        ]
        chosen = choose_best_vcf(candidates)
        if chosen is None:
            by_chrom.append({
                "label": label,
                "vcf": None,
                "exists": False,
                "index_exists": False,
                "candidate_count": 0,
                "candidates": [],
            })
        else:
            by_chrom.append({
                "label": label,
                "vcf": str(chosen),
                "exists": file_exists_nonzero(chosen),
                "index_exists": file_exists_nonzero(str(chosen) + ".tbi") or file_exists_nonzero(str(chosen) + ".csi"),
                "candidate_count": len(candidates),
                "candidates": [str(p) for p in candidates],
            })

    final_candidates = []
    for p in all_vcfs:
        lower = p.name.lower()
        has_label = any(label in p.name for label in labels)
        if not has_label and any(k in lower for k in ["final", "merged", "cohort", "all_samples", "allsamples"]):
            final_candidates.append(p)

    return {
        "all_vcfs_in_joint_dir": [str(p) for p in all_vcfs],
        "by_chromosome": by_chrom,
        "final_candidates": [
            {
                "vcf": str(p),
                "exists": file_exists_nonzero(p),
                "index_exists": file_exists_nonzero(str(p) + ".tbi") or file_exists_nonzero(str(p) + ".csi"),
            }
            for p in final_candidates
        ],
    }


def parse_squeue():
    cmd = "squeue -u \"$USER\" -h -o '%i|%P|%j|%T|%M|%l|%D|%R'"
    raw = run_shell(cmd, timeout=20)

    jobs = []
    for line in raw["stdout"].splitlines():
        parts = line.split("|", 7)
        if len(parts) != 8:
            continue
        job_id, partition, name, state, elapsed, timelimit, nodes, reason = parts
        jobs.append({
            "job_id": job_id.strip(),
            "partition": partition.strip(),
            "name": name.strip(),
            "state": state.strip(),
            "elapsed": elapsed.strip(),
            "time_limit": timelimit.strip(),
            "nodes": nodes.strip(),
            "reason": reason.strip(),
        })

    keywords = [
        "gatk",
        "genotype",
        "genotypegvcfs",
        "genomicsdb",
        "joint",
        "gvcf",
        "haplotype",
    ]

    relevant = [
        j for j in jobs
        if any(k in j["name"].lower() for k in keywords)
    ]

    return {
        "command": cmd,
        "raw_returncode": raw["returncode"],
        "raw_stderr": raw["stderr"],
        "all_jobs": jobs,
        "relevant_jobs": relevant,
    }


def parse_sacct_today():
    cmd = "sacct -X -n -P -S $(date +%Y-%m-%d) --format=JobID,JobName%50,State,ExitCode,Elapsed,MaxRSS"
    raw = run_shell(cmd, timeout=30)

    rows = []
    for line in raw["stdout"].splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        job_id, name, state, exit_code, elapsed, maxrss = parts[:6]
        rows.append({
            "job_id": job_id.strip(),
            "name": name.strip(),
            "state": state.strip(),
            "exit_code": exit_code.strip(),
            "elapsed": elapsed.strip(),
            "max_rss": maxrss.strip(),
        })

    keywords = [
        "gatk",
        "genotype",
        "genotypegvcfs",
        "genomicsdb",
        "joint",
        "gvcf",
        "haplotype",
    ]

    relevant = [
        r for r in rows
        if any(k in r["name"].lower() for k in keywords)
    ]

    return {
        "command": cmd,
        "raw_returncode": raw["returncode"],
        "raw_stderr": raw["stderr"],
        "relevant_today": relevant,
    }


def tail_text(path, max_bytes=65536):
    p = Path(path)
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except Exception as e:
        return f"[Could not read {path}: {e}]"


def recent_log_errors():
    joint_dir = Path(cfg["joint_dir"])
    if not joint_dir.exists():
        return {
            "logs_checked": [],
            "error_hits": [],
        }

    suffixes = {".out", ".err", ".log", ".txt"}
    logs = []

    try:
        for root, dirs, files in os.walk(joint_dir):
            root_path = Path(root)

            # Avoid walking huge GenomicsDB internal directories.
            dirs[:] = [
                d for d in dirs
                if "genomicsdb" not in d.lower()
            ]

            try:
                depth = len(root_path.relative_to(joint_dir).parts)
            except Exception:
                depth = 0

            if depth > 3:
                dirs[:] = []
                continue

            for fn in files:
                p = root_path / fn
                lower = fn.lower()
                if (
                    p.suffix.lower() in suffixes
                    or lower.startswith("slurm-")
                    or lower.endswith(".e")
                    or lower.endswith(".o")
                ):
                    try:
                        st = p.stat()
                        logs.append((st.st_mtime, st.st_size, p))
                    except Exception:
                        pass
    except Exception:
        pass

    logs = sorted(logs, reverse=True)[:30]

    error_patterns = [
        "a user error has occurred",
        "user error",
        "error",
        "exception",
        "traceback",
        "killed",
        "outofmemory",
        "out of memory",
        "unsupportedclassversionerror",
        "segmentation fault",
        "cannot allocate memory",
        "no space left",
        "failed",
    ]

    hits = []
    checked = []

    for mtime, size, p in logs:
        checked.append({
            "path": str(p),
            "size_bytes": size,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(mtime)),
        })

        text = tail_text(p)
        lines = text.splitlines()
        matched_lines = []
        for line in lines:
            low = line.lower()
            if any(pattern in low for pattern in error_patterns):
                matched_lines.append(line[-500:])

        if matched_lines:
            hits.append({
                "path": str(p),
                "mtime": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(mtime)),
                "matched_lines": matched_lines[-30:],
            })

    return {
        "logs_checked": checked,
        "error_hits": hits,
    }


def storage_context():
    scratch_path = cfg.get("project_root", "/scratch/project_2001113")
    df = run_shell(f"df -h {scratch_path} 2>/dev/null || true", timeout=15)
    return {
        "project_root": scratch_path,
        "df_h": df["stdout"],
        "note": "Trimmed FASTQ files may be archived to Allas and removed from scratch; missing trimmed FASTQs are not treated as failure if downstream BAM/GVCF outputs exist.",
    }


sample_map = read_sample_map(cfg["sample_map"])
samples, sample_to_gvcf = discover_samples(sample_map)
sample_rows = check_samples(samples, sample_to_gvcf)
ref = reference_checks()
genomicsdb = detect_genomicsdb_workspaces()
genotype_vcfs = detect_genotype_vcfs(genomicsdb["workspaces"])
squeue = parse_squeue()
sacct = parse_sacct_today()
logs = recent_log_errors()
storage = storage_context()

result = {
    "scanner": "GenomeAgent Task Scanner v1",
    "task": "linear_joint_calling",
    "cluster_host_context": run_shell("hostname && whoami", timeout=10)["stdout"],
    "scanned_at_cluster_time": now_local_iso(),
    "config": cfg,
    "reference": ref,
    "sample_map": sample_map,
    "samples": sample_rows,
    "genomicsdb": genomicsdb,
    "genotype_vcfs": genotype_vcfs,
    "squeue": squeue,
    "sacct_today": sacct,
    "recent_logs": logs,
    "storage": storage,
}

print(json.dumps(result, indent=2))
'''


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_remote_scan(host: str, cfg: dict[str, Any]) -> dict[str, Any]:
    remote_code = (
        "import json\n"
        f"CONFIG = json.loads({json.dumps(json.dumps(cfg))})\n"
        + REMOTE_SCANNER
    )

    proc = subprocess.run(
        ["ssh", host, "python3", "-"],
        input=remote_code,
        text=True,
        capture_output=True,
    )

    if proc.returncode != 0:
        print("ERROR: remote scan failed.", file=sys.stderr)
        print("STDERR:", proc.stderr, file=sys.stderr)
        print("STDOUT:", proc.stdout[:2000], file=sys.stderr)
        sys.exit(proc.returncode)

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("ERROR: remote scan did not return valid JSON.", file=sys.stderr)
        print("STDERR:", proc.stderr, file=sys.stderr)
        print("STDOUT first 4000 characters:", proc.stdout[:4000], file=sys.stderr)
        sys.exit(1)


def count_true(rows: list[dict[str, Any]], key: str) -> int:
    return sum(1 for r in rows if r.get(key) is True)


def infer_status(data: dict[str, Any]) -> dict[str, Any]:
    cfg = data["config"]
    samples = data["samples"]
    expected_samples = int(cfg.get("expected_samples") or 0)

    if expected_samples <= 0:
        expected_samples = len(samples)

    bam_count = sum(
        1
        for r in samples
        if r.get("bam_exists") and r.get("bam_index_style") != "missing"
    )

    gvcf_count = sum(
        1
        for r in samples
        if r.get("gvcf_exists") and r.get("gvcf_index_exists")
    )

    workspaces = data["genomicsdb"]["workspaces"]
    db_count = sum(
        1
        for w in workspaces
        if w.get("exists") and w.get("nonempty")
    )

    chrom_vcfs = data["genotype_vcfs"]["by_chromosome"]
    genotype_vcf_count = sum(
        1
        for v in chrom_vcfs
        if v.get("exists") and v.get("index_exists")
    )

    final_candidates = data["genotype_vcfs"]["final_candidates"]
    final_exists = any(
        v.get("exists") and v.get("index_exists")
        for v in final_candidates
    )

    relevant_jobs = data["squeue"]["relevant_jobs"]
    running_job_names = " ".join(j["name"].lower() for j in relevant_jobs)

    error_hits = data["recent_logs"]["error_hits"]

    def stage_status(done: int, expected: int) -> str:
        if expected <= 0:
            return "unknown"
        if done >= expected:
            return "validated_completed"
        if done > 0:
            return "partial"
        return "not_started"

    mapping_status = stage_status(bam_count, expected_samples)
    haplotypecaller_status = stage_status(gvcf_count, expected_samples)
    genomicsdb_status = stage_status(db_count, len(workspaces))
    genotype_status = stage_status(genotype_vcf_count, len(chrom_vcfs))

    if genotype_status != "validated_completed":
        if any("genotype" in j["name"].lower() or "joint" in j["name"].lower() for j in relevant_jobs):
            genotype_status = "running"
    if genomicsdb_status != "validated_completed":
        if any("genomicsdb" in j["name"].lower() for j in relevant_jobs):
            genomicsdb_status = "running"

    if final_exists:
        current_stage = "final_validation_or_completed"
        overall = "final_vcf_detected"
        next_safe_action = "validate_final_vcf"
    elif genotype_status == "validated_completed":
        current_stage = "gather_final_vcf"
        overall = "ready_for_final_merge"
        next_safe_action = "run_gather_or_merge_vcfs_after_manual_review"
    elif genotype_status == "running":
        current_stage = "genotypegvcfs"
        overall = "running"
        next_safe_action = "wait_for_genotypegvcfs_to_finish"
    elif genomicsdb_status == "validated_completed":
        current_stage = "genotypegvcfs"
        overall = "ready_for_genotypegvcfs_or_running_unrecognized_job_name"
        next_safe_action = "check_squeue_logs_then_run_or_continue_genotypegvcfs"
    elif genomicsdb_status == "running":
        current_stage = "genomicsdbimport"
        overall = "running"
        next_safe_action = "wait_for_genomicsdbimport_to_finish"
    elif haplotypecaller_status == "validated_completed":
        current_stage = "genomicsdbimport"
        overall = "ready_for_genomicsdbimport"
        next_safe_action = "run_genomicsdbimport_after_manual_review"
    else:
        current_stage = "pre_joint_calling"
        overall = "incomplete_inputs"
        next_safe_action = "inspect_missing_bams_or_gvcfs"

    bam_index_styles = {}
    for r in samples:
        style = r.get("bam_index_style", "unknown")
        bam_index_styles[style] = bam_index_styles.get(style, 0) + 1

    warnings = []

    duplicate_samples = data["sample_map"].get("duplicate_samples", [])
    if duplicate_samples:
        warnings.append(f"Sample map has duplicate sample names: {len(duplicate_samples)} duplicated sample IDs.")

    if bam_index_styles.get("sample.bai", 0) > 0 and bam_index_styles.get("sample.bam.bai", 0) > 0:
        warnings.append("Mixed BAM index naming detected: both sample.bai and sample.bam.bai are present. Scanner accepts both.")

    if bam_index_styles.get("missing", 0) > 0:
        warnings.append(f"Missing BAM index for {bam_index_styles['missing']} samples.")

    if gvcf_count < expected_samples:
        warnings.append(f"Only {gvcf_count}/{expected_samples} indexed GVCFs validated.")

    if error_hits:
        warnings.append(f"Recent log files contain {len(error_hits)} error-like hit groups. Inspect recent_errors.txt.")

    return {
        "overall_status": overall,
        "current_stage": current_stage,
        "next_safe_action": next_safe_action,
        "expected_samples": expected_samples,
        "counts": {
            "samples_seen": len(samples),
            "validated_bams_with_index": bam_count,
            "validated_gvcfs_with_index": gvcf_count,
            "validated_genomicsdb_workspaces": db_count,
            "expected_genomicsdb_workspaces": len(workspaces),
            "validated_genotype_vcfs_with_index": genotype_vcf_count,
            "expected_genotype_vcfs": len(chrom_vcfs),
            "final_vcf_candidates": len(final_candidates),
            "running_relevant_jobs": len(relevant_jobs),
            "recent_error_hit_groups": len(error_hits),
        },
        "stages": {
            "bam_markduplicates": {
                "status": mapping_status,
                "validated_outputs": bam_count,
                "expected_outputs": expected_samples,
            },
            "haplotypecaller_gvcfs": {
                "status": haplotypecaller_status,
                "validated_outputs": gvcf_count,
                "expected_outputs": expected_samples,
            },
            "genomicsdbimport": {
                "status": genomicsdb_status,
                "validated_outputs": db_count,
                "expected_outputs": len(workspaces),
            },
            "genotypegvcfs": {
                "status": genotype_status,
                "validated_outputs": genotype_vcf_count,
                "expected_outputs": len(chrom_vcfs),
            },
            "final_merge": {
                "status": "detected" if final_exists else "not_started",
                "final_vcf_candidates": len(final_candidates),
            },
        },
        "bam_index_styles": bam_index_styles,
        "warnings": warnings,
    }


def write_tsv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8") as out:
        out.write("\t".join(columns) + "\n")
        for row in rows:
            vals = []
            for col in columns:
                val = row.get(col, "")
                if val is None:
                    val = ""
                vals.append(str(val).replace("\t", " "))
            out.write("\t".join(vals) + "\n")


def write_outputs(scan_dir: Path, data: dict[str, Any], status: dict[str, Any]) -> None:
    scan_dir.mkdir(parents=True, exist_ok=True)

    data_with_status = dict(data)
    data_with_status["status_summary"] = status

    (scan_dir / "task_scan.json").write_text(
        json.dumps(data_with_status, indent=2),
        encoding="utf-8",
    )

    sample_cols = [
        "sample",
        "bam",
        "bam_exists",
        "bam_index_style",
        "bai_short_exists",
        "bai_long_exists",
        "gvcf",
        "gvcf_exists",
        "gvcf_index_exists",
    ]
    write_tsv(scan_dir / "sample_status.tsv", data["samples"], sample_cols)

    missing = []
    for r in data["samples"]:
        if not r["bam_exists"]:
            missing.append({"sample": r["sample"], "missing_type": "bam", "path": r["bam"]})
        if r["bam_index_style"] == "missing":
            missing.append({"sample": r["sample"], "missing_type": "bam_index", "path": r["bai_short"] + " OR " + r["bai_long"]})
        if not r["gvcf_exists"]:
            missing.append({"sample": r["sample"], "missing_type": "gvcf", "path": r["gvcf"]})
        if not r["gvcf_index_exists"]:
            missing.append({"sample": r["sample"], "missing_type": "gvcf_index", "path": r["gvcf_tbi"] + " OR " + r["gvcf_csi"]})

    write_tsv(scan_dir / "missing_outputs.tsv", missing, ["sample", "missing_type", "path"])

    jobs = data["squeue"]["relevant_jobs"]
    write_tsv(
        scan_dir / "running_jobs.tsv",
        jobs,
        ["job_id", "partition", "name", "state", "elapsed", "time_limit", "nodes", "reason"],
    )

    error_lines = []
    for hit in data["recent_logs"]["error_hits"]:
        error_lines.append("=" * 80)
        error_lines.append(hit["path"])
        error_lines.append(hit.get("mtime", ""))
        for line in hit["matched_lines"]:
            error_lines.append(line)
        error_lines.append("")
    (scan_dir / "recent_errors.txt").write_text("\n".join(error_lines), encoding="utf-8")

    report = make_report(data, status, scan_dir)
    (scan_dir / "report.md").write_text(report, encoding="utf-8")


def make_report(data: dict[str, Any], status: dict[str, Any], scan_dir: Path) -> str:
    lines = []
    lines.append("# GenomeAgent Task Scan: linear_joint_calling")
    lines.append("")
    lines.append(f"Scan directory: `{scan_dir}`")
    lines.append(f"Cluster time: `{data.get('scanned_at_cluster_time', '')}`")
    lines.append(f"Overall status: **{status['overall_status']}**")
    lines.append(f"Current stage: **{status['current_stage']}**")
    lines.append(f"Next safe action: **{status['next_safe_action']}**")
    lines.append("")

    lines.append("## Stage summary")
    lines.append("")
    lines.append("| Stage | Status | Validated | Expected |")
    lines.append("|---|---:|---:|---:|")
    for stage, info in status["stages"].items():
        lines.append(
            f"| {stage} | {info.get('status', '')} | "
            f"{info.get('validated_outputs', info.get('final_vcf_candidates', ''))} | "
            f"{info.get('expected_outputs', '')} |"
        )
    lines.append("")

    lines.append("## Counts")
    lines.append("")
    for key, val in status["counts"].items():
        lines.append(f"- {key}: {val}")
    lines.append("")

    lines.append("## BAM index styles")
    lines.append("")
    for key, val in sorted(status["bam_index_styles"].items()):
        lines.append(f"- {key}: {val}")
    lines.append("")

    if status["warnings"]:
        lines.append("## Warnings")
        lines.append("")
        for w in status["warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("## Storage note")
    lines.append("")
    lines.append(data["storage"].get("note", ""))
    lines.append("")
    if data["storage"].get("df_h"):
        lines.append("```text")
        lines.append(data["storage"]["df_h"].rstrip())
        lines.append("```")
        lines.append("")

    lines.append("## Output files")
    lines.append("")
    lines.append("- `task_scan.json`")
    lines.append("- `sample_status.tsv`")
    lines.append("- `missing_outputs.tsv`")
    lines.append("- `running_jobs.tsv`")
    lines.append("- `recent_errors.txt`")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GenomeAgent read-only Task Scanner v1 for linear joint calling."
    )
    parser.add_argument("--host", default="puhti", help="SSH host alias, e.g. puhti or roihu.")
    parser.add_argument("--out-root", default="workspace/task_scans/linear_joint_calling")
    parser.add_argument("--project-root", default="/scratch/project_2001113")
    parser.add_argument("--reference", default="/scratch/project_2001113/pangenome/Fragaria_vesca_v6_genome.fasta")
    parser.add_argument("--bam-dir", default="/scratch/project_2001113/GATK/bam")
    parser.add_argument("--gvcf-dir", default="/scratch/project_2001113/GATK/gvcf")
    parser.add_argument("--joint-dir", default="/scratch/project_2001113/GATK/jointcalling")
    parser.add_argument("--sample-map", default="/scratch/project_2001113/GATK/jointcalling/sample_map.txt")
    parser.add_argument("--expected-samples", type=int, default=455)
    parser.add_argument(
        "--chromosomes",
        default="chr1,chr2,chr3,chr4,chr5,chr6,chr7",
        help="Comma-separated chromosome labels used in GenomicsDB/output names. "
             "If these do not match, scanner will auto-detect GenomicsDB directories.",
    )

    args = parser.parse_args()

    cfg = {
        "project_root": args.project_root,
        "reference": args.reference,
        "bam_dir": args.bam_dir,
        "gvcf_dir": args.gvcf_dir,
        "joint_dir": args.joint_dir,
        "sample_map": args.sample_map,
        "expected_samples": args.expected_samples,
        "chromosomes": [x.strip() for x in args.chromosomes.split(",") if x.strip()],
    }

    stamp = utc_stamp()
    scan_dir = Path(args.out_root) / stamp

    print("=" * 80)
    print("GenomeAgent Task Scanner v1: linear_joint_calling")
    print("=" * 80)
    print(f"SSH host      : {args.host}")
    print(f"Output folder : {scan_dir}")
    print("Mode          : read-only")
    print("")

    data = run_remote_scan(args.host, cfg)
    status = infer_status(data)
    write_outputs(scan_dir, data, status)

    print("Scan complete.")
    print("")
    print(f"Overall status   : {status['overall_status']}")
    print(f"Current stage    : {status['current_stage']}")
    print(f"Next safe action : {status['next_safe_action']}")
    print("")
    print(f"Wrote: {scan_dir / 'report.md'}")
    print(f"Wrote: {scan_dir / 'task_scan.json'}")
    print(f"Wrote: {scan_dir / 'sample_status.tsv'}")
    print(f"Wrote: {scan_dir / 'missing_outputs.tsv'}")
    print(f"Wrote: {scan_dir / 'running_jobs.tsv'}")
    print(f"Wrote: {scan_dir / 'recent_errors.txt'}")

    if status["warnings"]:
        print("")
        print("Warnings:")
        for w in status["warnings"]:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
