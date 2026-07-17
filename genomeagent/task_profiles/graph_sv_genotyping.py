#!/usr/bin/env python3
"""Read-only task profile for graph-based population SV genotyping."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from genomeagent.task_scanner import RemoteRunner, TaskScanError, write_tsv


REMOTE_OBSERVER = r'''
import glob
import gzip
import json
import os
import re
import subprocess
import time
from pathlib import Path


def now_local_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def nonzero(path):
    try:
        p = Path(path)
        return p.is_file() and p.stat().st_size > 0
    except Exception:
        return False


def file_info(path):
    p = Path(path) if path else Path("/path/not/found")
    out = {
        "path": str(path or ""),
        "exists": False,
        "size_bytes": None,
        "mtime": None,
    }
    try:
        out["exists"] = p.is_file()
        if out["exists"]:
            stat = p.stat()
            out["size_bytes"] = stat.st_size
            out["mtime"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)
            )
    except Exception as exc:
        out["error"] = str(exc)
    return out


def expand(patterns, values):
    result = []
    for pattern in patterns:
        try:
            result.append(pattern.format(**values))
        except Exception:
            result.append(pattern)
    return result


def matches(patterns):
    out = []
    for pattern in patterns:
        out.extend(glob.glob(pattern, recursive=True))
    return sorted(set(out))


def first_match(patterns):
    found = matches(patterns)
    return found[0] if found else ""


def normalize_sample(path):
    name = Path(path).name
    for suffix in (
        ".snarls.all.vcf.gz.tbi", ".snarls.all.vcf.gz.csi",
        ".snarls.all.vcf.gz", ".snarls.all.vcf",
        ".vcf.gz.tbi", ".vcf.gz.csi", ".vcf.gz", ".vcf",
        ".dedup.gam", ".gam", ".pack", ".vgpack"
    ):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


def read_manifest(path):
    out = {
        "path": path,
        "exists": False,
        "records": [],
        "duplicate_samples": [],
        "malformed_lines": [],
    }
    if not path or not Path(path).is_file():
        return out
    out["exists"] = True
    seen = {}
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split("\t") if "\t" in stripped else stripped.split()
            if line_no == 1 and fields and fields[0].lower() in {"sample", "sample_id", "id"}:
                continue
            if not fields:
                out["malformed_lines"].append({"line_no": line_no, "line": stripped})
                continue
            sample = normalize_sample(fields[0])
            out["records"].append({"sample": sample, "line_no": line_no})
            seen.setdefault(sample, []).append(line_no)
    out["duplicate_samples"] = [
        {"sample": sample, "line_numbers": lines}
        for sample, lines in sorted(seen.items()) if len(lines) > 1
    ]
    return out


def index_stage(patterns):
    grouped = {}
    errors = []
    for path in matches(patterns):
        try:
            p = Path(path)
            if not p.is_file():
                continue
            sample = normalize_sample(path)
            stat = p.stat()
            grouped.setdefault(sample, []).append({
                "path": path,
                "nonzero": stat.st_size > 0,
                "size_bytes": stat.st_size,
            })
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    records = {}
    duplicates = []
    for sample, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: row["path"])
        records[sample] = rows[0]
        if len(rows) > 1:
            duplicates.append({
                "sample": sample,
                "paths": [row["path"] for row in rows],
            })
    return {"records": records, "duplicates": duplicates, "errors": errors}


def bounded_text(path, max_bytes=1048576):
    try:
        with Path(path).open("rb") as handle:
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except Exception:
        return ""


def call_evidence(patterns):
    with_a = []
    without_a = []
    command_re = re.compile(r"\bvg\s+call\b[^\n\r]*", re.IGNORECASE)
    a_re = re.compile(r"(?:^|\s)-a(?:\s|$)")
    scanned = matches(patterns)
    for path in scanned:
        for match in command_re.finditer(bounded_text(path)):
            command = match.group(0).strip()
            row = {"path": path, "command": command}
            (with_a if a_re.search(command) else without_a).append(row)
    return {
        "files_scanned": scanned,
        "vg_call_a_hits": with_a,
        "vg_call_without_a_hits": without_a,
        "vg_call_a_confirmed": bool(with_a),
    }


def count_noncomment_lines(path):
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for line in handle if line.strip() and not line.startswith("#"))
    except Exception:
        return None


def run_bcftools(arguments):
    quoted = " ".join(subprocess.list2cmdline([arg]) for arg in arguments)
    attempts = [
        ["bcftools", *arguments],
        ["bash", "-lc", "module load biokit >/dev/null 2>&1 && bcftools " + quoted],
    ]
    for argv in attempts:
        try:
            result = subprocess.run(
                argv, text=True, capture_output=True, timeout=180, check=False
            )
            if result.returncode == 0:
                return result.stdout
        except Exception:
            pass
    return None


def product_summary(path):
    out = {
        "path": path,
        "exists_nonzero": False,
        "sample_count": None,
        "record_count": None,
        "product_type": None,
    }
    if not path or not nonzero(path):
        return out

    out["exists_nonzero"] = True
    lower = path.lower()

    if lower.endswith((".bcf", ".vcf.gz", ".vcf")):
        out["product_type"] = "variant_file"
        samples = run_bcftools(["query", "-l", path])
        if samples is not None:
            out["sample_count"] = len([x for x in samples.splitlines() if x.strip()])
        records = run_bcftools(["index", "-n", path])
        if records is not None:
            try:
                out["record_count"] = int(records.strip())
            except ValueError:
                pass
        return out

    if lower.endswith(".pgen"):
        out["product_type"] = "plink2_pgen"
        stem = path[:-5]
        pvar = stem + ".pvar"
        psam = stem + ".psam"
        out["record_count"] = count_noncomment_lines(pvar)
        out["sample_count"] = count_noncomment_lines(psam)
        out["pvar_path"] = pvar
        out["psam_path"] = psam
        return out

    if lower.endswith(".prune.in"):
        out["product_type"] = "plink_prune_in"
        out["record_count"] = count_noncomment_lines(path)
        return out

    out["product_type"] = "other"
    return out


def table_shape(path):
    out = {"path": path, "exists_nonzero": False, "rows": None}
    if not path or not nonzero(path):
        return out
    out["exists_nonzero"] = True
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            handle.readline()
            out["rows"] = sum(1 for _ in handle)
    except Exception as exc:
        out["error"] = str(exc)
    return out


def run(argv):
    try:
        result = subprocess.run(
            argv, text=True, capture_output=True, timeout=30, check=False
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:
        return None, "", str(exc)


def scheduler(config):
    user = config.get("user", os.environ.get("USER", ""))
    account = config.get("account", "")
    patterns = [x.lower() for x in config.get("job_name_patterns", [])]

    rc, stdout, stderr = run([
        "squeue", "-h", "-u", user, "-o", "%i|%a|%j|%T|%M|%l|%R"
    ])
    running = []
    if rc == 0:
        for line in stdout.splitlines():
            fields = line.split("|", 6)
            if len(fields) != 7:
                continue
            job_id, job_account, name, state, elapsed, limit, reason = fields
            if any(x in name.lower() for x in patterns) and (
                not account or job_account == account
            ):
                running.append({
                    "job_id": job_id, "name": name, "state": state,
                    "elapsed": elapsed, "reason": reason,
                })

    start = time.strftime(
        "%Y-%m-%d",
        time.localtime(time.time() - int(config.get("recent_job_days", 3)) * 86400)
    )
    arc, aout, aerr = run([
        "sacct", "-X", "-S", start, "-n", "-P",
        "-o", "JobID%40,Account,JobName%60,State,Elapsed,ExitCode"
    ])
    recent = []
    if arc == 0:
        for line in aout.splitlines():
            fields = line.split("|")
            if len(fields) < 6:
                continue
            job_id, job_account, name, state, elapsed, exit_code = fields[:6]
            if any(x in name.lower() for x in patterns) and (
                not account or job_account == account
            ):
                recent.append({
                    "job_id": job_id, "name": name, "state": state,
                    "elapsed": elapsed, "exit_code": exit_code,
                })
    return {
        "running": running,
        "recent": recent[-int(config.get("max_recent_scheduler_records", 600)):],
        "squeue_returncode": rc,
        "squeue_stderr": stderr[-2000:],
        "sacct_returncode": arc,
        "sacct_stderr": aerr[-2000:],
    }


def main():
    cfg = CONFIG
    values = {"project_root": cfg["project_root"]}
    manifest_path = first_match(expand(cfg["sample_manifest_candidates"], values))
    manifest = read_manifest(manifest_path)

    indexes = {}
    for stage, patterns in cfg["sample_stage_globs"].items():
        indexes[stage] = index_stage(expand(patterns, values))

    # Final-analysis manifest is authoritative. Stage directories may
    # legitimately contain an extra excluded sample such as SLO1.
    universe = {row["sample"] for row in manifest["records"]}
    if not universe:
        for index in indexes.values():
            universe.update(index["records"])

    samples = []
    for sample in sorted(universe):
        row = {"sample": sample}
        for stage, index in indexes.items():
            rec = index["records"].get(sample, {})
            row[f"{stage}_path"] = rec.get("path", "")
            row[f"{stage}_present"] = bool(rec.get("nonzero"))
        vcf = row.get("sample_vcf_path", "")
        row["sample_vcf_index_present"] = bool(
            vcf and (nonzero(vcf + ".tbi") or nonzero(vcf + ".csi"))
        )
        if row.get("sample_vcf_present") and row["sample_vcf_index_present"]:
            row["state"] = "sample_vcf_validatable"
        elif row.get("sample_vcf_present"):
            row["state"] = "sample_vcf_unindexed"
        elif row.get("pack_present"):
            row["state"] = "pack_ready_for_vg_call_a"
        elif row.get("dedup_gam_present"):
            row["state"] = "dedup_gam_ready_for_pack"
        elif row.get("input_gam_present"):
            row["state"] = "input_gam_present"
        else:
            row["state"] = "missing_gam_input"
        samples.append(row)

    products = {}
    summaries = {}
    for name, patterns in cfg["product_globs"].items():
        path = first_match(expand(patterns, values))
        products[name] = file_info(path)
        if name in {
            "merged_allsite_vcf",
            "biallelic_sv50_vcf",
            "maf_geno_unpruned_vcf",
            "ldpruned_pca_vcf",
        }:
            summaries[name] = product_summary(path)

    analyses = {
        name: table_shape(products[name]["path"])
        for name in ("pca_eigenvec", "pca_eigenval", "burden_long", "burden_wide")
    }

    observation = {
        "scanned_at_cluster_time": now_local_iso(),
        "project_root": cfg["project_root"],
        "manifest": manifest,
        "samples": samples,
        "stage_indexes": indexes,
        "products": products,
        "vcf_summaries": summaries,
        "analyses": analyses,
        "command_evidence": call_evidence(
            expand(cfg.get("command_evidence_globs", []), values)
        ),
        "jobs": scheduler(cfg),
        "io_policy": {
            "mode": "read_only",
            "gam_content_read": False,
            "pack_content_read": False,
            "vcf_text_streamed": True,
        },
    }
    print(json.dumps(observation, separators=(",", ":")))


main()
'''


def build_remote_program(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(config), separators=(",", ":"))
    return "import json\nCONFIG = json.loads(" + repr(encoded) + ")\n" + REMOTE_OBSERVER


def validate_config(config: Mapping[str, Any]) -> None:
    if not isinstance(config.get("expected_samples"), int) or config["expected_samples"] <= 0:
        raise TaskScanError("expected_samples must be a positive integer.")
    if not isinstance(config.get("project_root"), str) or not config["project_root"]:
        raise TaskScanError("project_root must be a non-empty string.")
    for key in ("sample_manifest_candidates", "command_evidence_globs"):
        value = config.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise TaskScanError(f"{key} must be a list of strings.")
    stages = config.get("sample_stage_globs")
    required_stages = {"input_gam", "dedup_gam", "pack", "sample_vcf"}
    if not isinstance(stages, Mapping) or required_stages - set(stages):
        raise TaskScanError("sample_stage_globs lacks required stages.")
    products = config.get("product_globs")
    required_products = {
        "merged_allsite_vcf", "biallelic_sv50_vcf",
        "maf_geno_unpruned_vcf", "ldpruned_pca_vcf",
        "pca_eigenvec", "pca_eigenval", "burden_long", "burden_wide",
    }
    if not isinstance(products, Mapping) or required_products - set(products):
        raise TaskScanError("product_globs lacks required products.")


class GraphSvGenotypingProfile:
    name = "graph_sv_genotyping"
    title = "Graph-based population SV genotyping"

    def collect(self, runner: RemoteRunner, config: Mapping[str, Any]) -> dict[str, Any]:
        validate_config(config)
        return runner.run_python(
            build_remote_program(config),
            timeout_seconds=int(config.get("remote_timeout_seconds", 900)),
        )

    def interpret(self, data: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        expected = int(config["expected_samples"])
        samples = list(data.get("samples", []))
        jobs = data.get("jobs", {})
        running = list(jobs.get("running", []))
        recent = list(jobs.get("recent", []))

        def sample_count(field):
            return sum(bool(row.get(field)) for row in samples)

        def product_present(name):
            info = data.get("products", {}).get(name, {})
            return bool(info.get("exists")) and (info.get("size_bytes") or 0) > 0

        input_gams = sample_count("input_gam_present")
        dedup_gams = sample_count("dedup_gam_present")
        packs = sample_count("pack_present")
        sample_vcfs = sample_count("sample_vcf_present")
        indexed_vcfs = sum(
            bool(row.get("sample_vcf_present")) and
            bool(row.get("sample_vcf_index_present"))
            for row in samples
        )
        command_call_a = bool(
            data.get("command_evidence", {}).get("vg_call_a_confirmed")
        )
        merged_path = str(
            data.get("products", {})
            .get("merged_allsite_vcf", {})
            .get("path", "")
        )
        filename_call_a = (
            bool(config.get("accept_vgcall_a_filename_provenance", False))
            and "vgcall_a" in Path(merged_path).name.lower()
        )
        call_a = command_call_a or filename_call_a
        merged = product_present("merged_allsite_vcf")
        sv50 = product_present("biallelic_sv50_vcf")
        unpruned = product_present("maf_geno_unpruned_vcf")
        ldpruned = product_present("ldpruned_pca_vcf")
        pca = product_present("pca_eigenvec") and product_present("pca_eigenval")
        burden = product_present("burden_long") and product_present("burden_wide")

        failed_states = ("FAILED", "OUT_OF_MEMORY", "TIMEOUT", "CANCELLED", "NODE_FAIL")
        failed = [
            row for row in recent
            if any(str(row.get("state", "")).upper().startswith(x) for x in failed_states)
        ]

        warnings = []
        manifest = data.get("manifest", {})
        if not manifest.get("exists"):
            warnings.append("Expected-sample manifest was not found.")
        if manifest.get("duplicate_samples"):
            warnings.append("Duplicate sample IDs occur in the manifest.")
        if len(samples) != expected:
            warnings.append(f"Observed {len(samples)} samples; expected {expected}.")
        if sample_vcfs and not call_a:
            warnings.append("Per-sample VCFs exist, but vg call -a provenance was not found.")
        elif filename_call_a and not command_call_a:
            warnings.append(
                "vg call -a is supported by the authoritative merged BCF filename; "
                "the original command was not independently recovered."
            )
        if data.get("command_evidence", {}).get("vg_call_without_a_hits"):
            warnings.append(
                "Historical vg call commands without -a were found, but they are not "
                "treated as the authoritative completed workflow."
            )

        if running:
            overall, stage, action = "running", "graph_sv_genotyping", "wait_and_rescan"
        elif pca and burden and merged and sv50 and unpruned and ldpruned:
            overall, stage, action = (
                "workflow_complete_with_dataset_qc_warning",
                "downstream_sv_analysis",
                "preserve_validated_outputs_and_review_dataset_comparability",
            )
            if failed:
                warnings.append(
                    f"{len(failed)} historical failed/cancelled scheduler records were "
                    "detected, but validated final products take precedence."
                )
        elif failed:
            overall, stage, action = (
                "attention_required", "graph_sv_genotyping",
                "inspect_failed_jobs_without_resubmitting_completed_samples",
            )
        elif ldpruned:
            overall, stage, action = (
                "ready_for_pca_or_pca_incomplete", "ld_pruning_and_pca",
                "complete_or_validate_pca_and_burden_summaries",
            )
        elif unpruned:
            overall, stage, action = (
                "filtered_unpruned_vcf_ready", "maf_geno_filtering",
                "create_ld_pruned_pca_set_and_burden_summaries",
            )
        elif sv50:
            overall, stage, action = (
                "biallelic_sv50_ready", "sv_filtering",
                "apply_maf_and_genotype_missingness_filters",
            )
        elif merged:
            overall, stage, action = (
                "merged_allsite_vcf_ready", "multisample_merge",
                "validate_samples_and_filter_biallelic_ins_del_sv50",
            )
        elif indexed_vcfs == expected and call_a:
            overall, stage, action = (
                "sample_vcfs_validated_ready_for_merge", "per_sample_vg_call_a",
                "merge_per_sample_allsite_vcfs",
            )
        elif sample_vcfs:
            overall, stage, action = (
                "sample_vcf_stage_incomplete", "per_sample_vg_call_a",
                "validate_call_a_evidence_indexes_and_missing_samples",
            )
        elif packs:
            overall, stage, action = (
                "pack_stage_incomplete", "vg_pack",
                "run_or_complete_vg_call_a_for_ready_samples",
            )
        elif dedup_gams:
            overall, stage, action = (
                "dedup_gams_ready_or_pack_incomplete", "gam_deduplication",
                "complete_vg_pack_without_reprocessing_validated_gams",
            )
        elif input_gams:
            overall, stage, action = (
                "gam_inputs_present", "gam_inputs",
                "complete_and_validate_duplicate_removal",
            )
        else:
            overall, stage, action = (
                "not_started_or_paths_not_found", "gam_inputs",
                "verify_manifest_and_configured_paths",
            )

        summaries = data.get("vcf_summaries", {})
        counts = {
            "expected_samples": expected,
            "samples_observed": len(samples),
            "input_gams_present": input_gams,
            "dedup_gams_present": dedup_gams,
            "packs_present": packs,
            "sample_vcfs_present": sample_vcfs,
            "sample_vcfs_indexed": indexed_vcfs,
            "vg_call_a_confirmed": call_a,
            "merged_allsite_samples": summaries.get("merged_allsite_vcf", {}).get("sample_count"),
            "biallelic_sv50_records": summaries.get("biallelic_sv50_vcf", {}).get("record_count"),
            "maf_geno_unpruned_records": summaries.get("maf_geno_unpruned_vcf", {}).get("record_count"),
            "ldpruned_pca_records": summaries.get("ldpruned_pca_vcf", {}).get("record_count"),
            "running_relevant_jobs": len(running),
            "recent_failed_job_records": len(failed),
        }
        for key, expected_value in config.get("expected_counts", {}).items():
            actual = counts.get(key)
            if expected_value is not None and actual is not None and actual != expected_value:
                warnings.append(
                    f"Count mismatch for {key}: expected {expected_value}, observed {actual}."
                )

        return {
            "overall_status": overall,
            "current_stage": stage,
            "next_safe_action": action,
            "counts": counts,
            "warnings": warnings,
            "workflow_validation": (
                "passed"
                if pca and burden and merged and sv50 and unpruned and ldpruned and call_a
                else "incomplete"
            ),
            "dataset_comparability": "attention_required" if pca and burden else "not_assessed",
            "automatic_execution_allowed": False,
            "samples": samples,
            "validation_boundary": (
                "File-level continuity is validated from GAM through downstream VCF, PCA, "
                "and burden outputs. Biological comparability remains a separate review."
            ),
        }

    def write_artifacts(
        self, payload: Mapping[str, Any], scan_dir: Path
    ) -> Sequence[Path]:
        sample_path = scan_dir / "sample_status.tsv"
        write_tsv(
            sample_path,
            payload["status_summary"].get("samples", []),
            [
                "sample", "state",
                "input_gam_present", "dedup_gam_present",
                "pack_present", "sample_vcf_present",
                "sample_vcf_index_present",
                "input_gam_path", "dedup_gam_path",
                "pack_path", "sample_vcf_path",
            ],
        )

        product_rows = []
        observation = payload["observation"]
        for name, info in observation.get("products", {}).items():
            summary = observation.get("vcf_summaries", {}).get(name, {})
            product_rows.append({
                "product": name,
                **dict(info),
                "sample_count": summary.get("sample_count"),
                "record_count": summary.get("record_count"),
            })
        product_path = scan_dir / "product_status.tsv"
        write_tsv(
            product_path, product_rows,
            ["product", "path", "exists", "size_bytes", "mtime",
             "sample_count", "record_count"],
        )

        command_rows = [
            {**row, "uses_all_sites_a": True}
            for row in observation.get("command_evidence", {}).get("vg_call_a_hits", [])
        ] + [
            {**row, "uses_all_sites_a": False}
            for row in observation.get("command_evidence", {}).get(
                "vg_call_without_a_hits", []
            )
        ]
        command_path = scan_dir / "vg_call_command_evidence.tsv"
        write_tsv(command_path, command_rows, ["path", "command", "uses_all_sites_a"])
        return [sample_path, product_path, command_path]

    def render_report(self, payload: Mapping[str, Any], scan_dir: Path) -> str:
        status = payload["status_summary"]
        lines = [
            "# Graph-based population SV genotyping",
            "",
            f"- Overall status: **{status['overall_status']}**",
            f"- Current stage: **{status['current_stage']}**",
            f"- Next safe action: **{status['next_safe_action']}**",
            f"- Workflow validation: **{status['workflow_validation']}**",
            f"- Dataset comparability: **{status['dataset_comparability']}**",
            "- Mode: **read-only**",
            "",
            "## Counts",
            "",
        ]
        lines.extend(
            f"- {key}: `{value}`" for key, value in status.get("counts", {}).items()
        )
        lines += ["", "## Warnings", ""]
        warnings = status.get("warnings", [])
        lines.extend(f"- {x}" for x in warnings) if warnings else lines.append("- None.")
        lines += [
            "",
            "## Validation boundary",
            "",
            status.get("validation_boundary", ""),
            "",
        ]
        return "\n".join(lines)
