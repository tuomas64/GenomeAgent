#!/usr/bin/env python3
"""Deterministic, proposal-only workflow adaptation for GenomeAgent.

Proposal Core v0.1.2 consumes canonical Task State and its bound read-only Task
Scan.  It produces immutable, reviewable workflow proposal bundles.  It has no
SSH, Slurm submission, execution, deletion, or remote-write authority.

The first supported action is ``gather_or_merge`` for scattered joint calling:
validated interval VCF shards -> chromosome VCFs -> whole-genome VCF -> final
validation.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


PROPOSAL_CORE_VERSION = "0.1.2"
PROPOSAL_SCHEMA_VERSION = "1.0"
SCATTER_GATHER_TEMPLATE_VERSION = "scattered_joint_gather_v3"
SUPPORTED_ACTIONS = {"gather_or_merge"}
COMPLETE_INTERVAL_STATES = {
    "completed",
    "complete",
    "validated_completed",
    "published_complete",
    "atomic_publish_complete",
}
FORBIDDEN_SCRIPT_PATTERNS = {
    r"\bsbatch\b": "generated proposals must not submit jobs",
    r"\bssh\b": "generated proposals must not open remote sessions",
    r"\bscp\b": "generated proposals must not transfer files",
    r"\brsync\b": "generated proposals must not transfer files",
    r"\bcurl\b": "generated proposals must not access external networks",
    r"\bwget\b": "generated proposals must not access external networks",
    r"\brm\s+-rf\b": "generated proposals must not recursively delete",
    r"\beval\b": "generated proposals must not evaluate generated code",
}


class ProposalCoreError(RuntimeError):
    """Raised when evidence is incomplete, stale, unsafe, or inconsistent."""


@dataclass(frozen=True)
class IntervalRecord:
    chromosome: str
    reference_contig: str
    start: int
    end: int
    interval: str
    vcf_path: str
    index_path: str
    batch: str
    task_index: str


@dataclass(frozen=True)
class ProposalResult:
    task: str
    action: str
    proposal_id: str
    status: str
    proposal_dir: Path
    interval_count: int
    chromosome_count: int
    artifact_paths: tuple[Path, ...]
    created: bool


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            raise ProposalCoreError(f"Immutable proposal artifact differs: {path}")
        return False
    _atomic_write(path, text)
    return True


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise ProposalCoreError(f"{label} does not exist: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalCoreError(f"Invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProposalCoreError(f"{label} must be a JSON object: {path}")
    return value, raw


def _safe_identifier(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", text):
        raise ProposalCoreError(f"Unsafe or empty {label}: {value!r}")
    return text


def _safe_contig(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.#:+-]*", text):
        raise ProposalCoreError(f"Unsafe or empty {label}: {value!r}")
    return text


def _safe_absolute(value: Any, label: str) -> str:
    text = str(value or "").strip()
    path = PurePosixPath(text)
    if (
        not text
        or not path.is_absolute()
        or "\x00" in text
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ProposalCoreError(f"{label} must be a safe absolute path: {value!r}")
    return text


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ProposalCoreError(f"{label} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProposalCoreError(f"{label} must be an integer.") from exc
    if parsed < minimum:
        raise ProposalCoreError(f"{label} must be >= {minimum}.")
    return parsed


def _nested_first(value: Any, keys: Sequence[str]) -> Any:
    """Return the first matching key found recursively in mappings."""
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                return value[key]
        for child in value.values():
            found = _nested_first(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_first(child, keys)
            if found is not None:
                return found
    return None


def _read_tsv(path: Path, label: str) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if not reader.fieldnames:
                raise ProposalCoreError(f"{label} has no header: {path}")
            rows = []
            for raw in reader:
                rows.append({str(k): str(v or "") for k, v in raw.items()})
            return rows
    except FileNotFoundError as exc:
        raise ProposalCoreError(f"{label} does not exist: {path}") from exc


def _one_row_tsv(path: Path, label: str) -> dict[str, str]:
    rows = _read_tsv(path, label)
    if len(rows) != 1:
        raise ProposalCoreError(f"{label} must contain exactly one data row: {path}")
    return rows[0]


def _column(row: Mapping[str, Any], aliases: Sequence[str]) -> str:
    normalized = {str(k).strip().lower(): str(v or "").strip() for k, v in row.items()}
    for alias in aliases:
        value = normalized.get(alias.lower())
        if value:
            return value
    return ""


def _parse_interval(text: str) -> tuple[str, int, int]:
    match = re.fullmatch(r"([^:\s]+):(\d+)-(\d+)", text.strip())
    if not match:
        raise ProposalCoreError(f"Invalid interval label: {text!r}")
    chromosome, start, end = match.groups()
    start_i, end_i = int(start), int(end)
    if start_i < 1 or end_i < start_i:
        raise ProposalCoreError(f"Invalid interval coordinates: {text!r}")
    return chromosome, start_i, end_i


def _interval_from_filename(path: str) -> tuple[str, int, int] | None:
    name = PurePosixPath(path).name
    match = re.search(r"(?:^|_)(chr[^_]+)_(\d+)_(\d+)(?:\.|_)", name)
    if not match:
        return None
    chromosome, start, end = match.groups()
    return chromosome, int(start), int(end)


def _walk_interval_candidates(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        keys = {str(key).lower() for key in value}
        if (
            keys.intersection({"interval", "region", "locus"})
            and keys.intersection({"vcf", "vcf_path", "output_vcf"})
            and keys.intersection({"index", "index_path", "tbi", "output_index"})
        ):
            yield value
        for child in value.values():
            yield from _walk_interval_candidates(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_interval_candidates(child)


def _records_from_rows(
    rows: Iterable[Mapping[str, Any]],
    chromosome_order: Sequence[str],
    contig_map: Mapping[str, str],
) -> list[IntervalRecord]:
    records: list[IntervalRecord] = []
    aliases = set(chromosome_order)
    for row in rows:
        interval = _column(row, ("interval", "region", "locus", "interval_label"))
        vcf = _column(row, ("vcf", "vcf_path", "output_vcf", "published_vcf"))
        index = _column(row, ("index", "index_path", "tbi", "output_index", "published_index"))
        if not vcf or not index:
            continue
        _safe_absolute(vcf, "interval VCF path")
        _safe_absolute(index, "interval index path")

        if interval:
            chromosome, start, end = _parse_interval(interval)
        else:
            parsed = _interval_from_filename(vcf)
            if parsed is None:
                raise ProposalCoreError(f"Cannot derive interval coordinates from {vcf}")
            chromosome, start, end = parsed
            interval = f"{chromosome}:{start}-{end}"

        if chromosome not in aliases:
            raise ProposalCoreError(
                f"Interval {interval} uses chromosome not declared by policy: {chromosome}"
            )
        status = _column(row, ("status", "state", "current_state", "classification"))
        if status and status.lower() not in COMPLETE_INTERVAL_STATES:
            # Some scanner tables use rich categories.  Explicitly reject only
            # known non-complete states; unknown empty status is tolerated when
            # the atomic summary already proves all outputs complete.
            rejected = {
                "running", "queued", "pending", "failed", "failed_needs_review",
                "submitted_unresolved", "not_submitted", "partial_output",
                "without_published_output", "incomplete", "missing",
            }
            if status.lower() in rejected:
                raise ProposalCoreError(f"Interval {interval} is not complete: {status}")

        records.append(
            IntervalRecord(
                chromosome=chromosome,
                reference_contig=str(contig_map.get(chromosome, chromosome)),
                start=start,
                end=end,
                interval=interval,
                vcf_path=vcf,
                index_path=index,
                batch=_column(row, ("batch", "batch_id", "part")),
                task_index=_column(row, ("task", "task_index", "array_task", "unit_id")),
            )
        )
    return records


def _load_interval_records(
    scan_dir: Path,
    task_scan: Mapping[str, Any],
    chromosome_order: Sequence[str],
    contig_map: Mapping[str, str],
) -> list[IntervalRecord]:
    interval_tsv = scan_dir / "interval_status.tsv"
    records: list[IntervalRecord] = []
    if interval_tsv.exists():
        records = _records_from_rows(
            _read_tsv(interval_tsv, "interval status"), chromosome_order, contig_map
        )
    if not records:
        records = _records_from_rows(
            _walk_interval_candidates(task_scan), chromosome_order, contig_map
        )
    if not records:
        raise ProposalCoreError(
            "No interval VCF/index records could be extracted from the bound Task Scan."
        )
    return records


def _validate_policy(policy: Mapping[str, Any], task: str, action: str) -> dict[str, Any]:
    if policy.get("schema_version") != "1.0":
        raise ProposalCoreError("Unsupported proposal policy schema.")
    if policy.get("proposal_core_version") != PROPOSAL_CORE_VERSION:
        raise ProposalCoreError("Proposal policy version does not match Proposal Core.")
    if policy.get("task") != task or policy.get("action") != action:
        raise ProposalCoreError("Proposal policy task/action mismatch.")
    if policy.get("template_version") != SCATTER_GATHER_TEMPLATE_VERSION:
        raise ProposalCoreError("Unsupported gather proposal template version.")

    authority = policy.get("authority")
    expected_authority = {
        "remote_reads_allowed": False,
        "remote_writes_allowed": False,
        "slurm_submission_allowed": False,
        "automatic_execution_allowed": False,
        "deletion_allowed": False,
        "knowledge_update_allowed": False,
    }
    if authority != expected_authority:
        raise ProposalCoreError("Proposal policy grants unsupported authority.")

    chromosomes = policy.get("chromosome_order")
    if not isinstance(chromosomes, list) or not chromosomes:
        raise ProposalCoreError("chromosome_order must be a non-empty list.")
    chromosomes = [_safe_identifier(item, "chromosome alias") for item in chromosomes]
    if len(chromosomes) != len(set(chromosomes)):
        raise ProposalCoreError("chromosome_order contains duplicates.")

    contig_map = policy.get("reference_contig_map")
    if not isinstance(contig_map, Mapping) or set(contig_map) != set(chromosomes):
        raise ProposalCoreError("reference_contig_map must map every chromosome alias exactly.")
    contig_map = {
        alias: _safe_contig(contig_map[alias], f"reference contig for {alias}")
        for alias in chromosomes
    }

    validated = dict(policy)
    validated["chromosome_order"] = chromosomes
    validated["reference_contig_map"] = contig_map
    validated["expected_intervals"] = _integer(
        policy.get("expected_intervals"), "expected_intervals", 1
    )
    validated["expected_samples"] = _integer(
        policy.get("expected_samples"), "expected_samples", 1
    )
    for key in (
        "reference_fasta",
        "sample_map",
        "remote_output_root",
        "cluster_environment_init",
    ):
        validated[key] = _safe_absolute(policy.get(key), key)

    software = policy.get("software")
    if not isinstance(software, Mapping):
        raise ProposalCoreError("software must be an object.")
    if not re.fullmatch(r"[A-Za-z0-9_.+/-]+", str(software.get("gatk_module") or "")):
        raise ProposalCoreError("Unsafe gatk_module.")
    if not re.fullmatch(r"[A-Za-z0-9_.+/-]+", str(software.get("bcftools_module") or "")):
        raise ProposalCoreError("Unsafe bcftools_module.")

    resources = policy.get("provisional_resources")
    if not isinstance(resources, Mapping):
        raise ProposalCoreError("provisional_resources must be an object.")
    for stage in ("gather_chromosomes", "gather_genome", "validate_final"):
        value = resources.get(stage)
        if not isinstance(value, Mapping):
            raise ProposalCoreError(f"Missing provisional resource stage: {stage}")
        _integer(value.get("cpus_per_task"), f"{stage}.cpus_per_task", 1)
        if not re.fullmatch(r"\d+[KMGTP]", str(value.get("memory") or "")):
            raise ProposalCoreError(f"Invalid memory for {stage}.")
        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", str(value.get("time_limit") or "")):
            raise ProposalCoreError(f"Invalid time_limit for {stage}.")
    return validated


def _validate_summary(summary: Mapping[str, Any], policy: Mapping[str, Any]) -> None:
    required_exact = {
        "sample_map_unique_samples": policy["expected_samples"],
        "expected_samples_per_interval": policy["expected_samples"],
        "expected_intervals": policy["expected_intervals"],
        "completed_atomic_publish_contract": policy["expected_intervals"],
        "without_published_output": 0,
        "running_intervals": 0,
        "queued_intervals": 0,
        "failed_needs_review": 0,
        "submitted_unresolved": 0,
        "not_submitted": 0,
        "scheduler_completed_output_missing": 0,
        "unmapped_intervals": 0,
        "vcf_present_index_missing": 0,
        "index_present_vcf_missing": 0,
        "ready_genomicsdb_workspaces": len(policy["chromosome_order"]),
        "expected_genomicsdb_workspaces": len(policy["chromosome_order"]),
        "active_scheduler_records": 0,
        "final_vcf_pairs_detected": 0,
    }
    for key, expected in required_exact.items():
        if key not in summary:
            raise ProposalCoreError(f"Scatter summary lacks required field: {key}")
        actual = _integer(summary[key], key, 0)
        if actual != expected:
            raise ProposalCoreError(
                f"Gather proposal blocked: {key} is {actual}, expected {expected}."
            )


def _task_state_status(state: Mapping[str, Any]) -> tuple[str, str]:
    overall = _nested_first(state, ("overall_status", "status"))
    stage = _nested_first(state, ("current_stage", "stage"))
    return str(overall or ""), str(stage or "")


def _validate_state_and_recommendations(
    task: str,
    state: Mapping[str, Any],
    recommendations: Mapping[str, Any],
    source_scan_id: str,
    task_scan_sha256: str,
) -> None:
    overall, stage = _task_state_status(state)
    if overall != "scattered_genotyping_complete" or stage != "gather_or_merge":
        raise ProposalCoreError(
            "Canonical Task State does not permit gather proposal preparation: "
            f"overall={overall!r}, stage={stage!r}."
        )
    if recommendations.get("task") != task:
        raise ProposalCoreError("Recommendation task mismatch.")
    if recommendations.get("source_scan_id") != source_scan_id:
        raise ProposalCoreError("Recommendations are not bound to the latest source scan.")
    if recommendations.get("source_task_scan_sha256") != task_scan_sha256:
        raise ProposalCoreError("Recommendations Task Scan digest mismatch.")
    if recommendations.get("automatic_execution_allowed") is not False:
        raise ProposalCoreError("Recommendations unexpectedly allow automatic execution.")
    actions = {
        str(item.get("action"))
        for item in recommendations.get("recommendations", [])
        if isinstance(item, Mapping)
    }
    if "review_gather_prerequisites" not in actions:
        raise ProposalCoreError("Gather-prerequisite recommendation is absent.")


def _validate_intervals(
    records: Sequence[IntervalRecord], policy: Mapping[str, Any]
) -> list[IntervalRecord]:
    expected = policy["expected_intervals"]
    if len(records) != expected:
        raise ProposalCoreError(
            f"Extracted {len(records)} interval records; expected {expected}."
        )
    chromosome_rank = {
        chromosome: index for index, chromosome in enumerate(policy["chromosome_order"])
    }
    sorted_records = sorted(
        records,
        key=lambda record: (
            chromosome_rank[record.chromosome], record.start, record.end, record.vcf_path
        ),
    )
    intervals = [record.interval for record in sorted_records]
    vcfs = [record.vcf_path for record in sorted_records]
    indexes = [record.index_path for record in sorted_records]
    if len(intervals) != len(set(intervals)):
        raise ProposalCoreError("Duplicate interval labels detected.")
    if len(vcfs) != len(set(vcfs)):
        raise ProposalCoreError("Duplicate interval VCF paths detected.")
    if len(indexes) != len(set(indexes)):
        raise ProposalCoreError("Duplicate interval index paths detected.")
    for record in sorted_records:
        if record.index_path != record.vcf_path + ".tbi":
            raise ProposalCoreError(
                "GatherVcfs proposal currently requires an adjacent .tbi index: "
                f"{record.interval}"
            )

    by_chromosome: dict[str, list[IntervalRecord]] = {
        chromosome: [] for chromosome in policy["chromosome_order"]
    }
    for record in sorted_records:
        by_chromosome[record.chromosome].append(record)
    for chromosome, chromosome_records in by_chromosome.items():
        if not chromosome_records:
            raise ProposalCoreError(f"No interval records found for {chromosome}.")
        previous_end = 0
        for record in chromosome_records:
            if record.start <= previous_end:
                raise ProposalCoreError(
                    f"Overlapping or unordered intervals for {chromosome}: {record.interval}"
                )
            previous_end = record.end
    return sorted_records


def _manifest_text(records: Sequence[IntervalRecord], source_scan_id: str) -> str:
    columns = [
        "order", "chromosome", "reference_contig", "start", "end", "interval",
        "batch", "task_index", "vcf_path", "index_path", "source_scan_id",
    ]
    lines = ["\t".join(columns)]
    for order, record in enumerate(records, start=1):
        values = [
            order, record.chromosome, record.reference_contig, record.start, record.end,
            record.interval, record.batch, record.task_index, record.vcf_path,
            record.index_path, source_scan_id,
        ]
        lines.append("\t".join(str(value) for value in values))
    return "\n".join(lines) + "\n"


def _inputs_text(records: Sequence[IntervalRecord]) -> str:
    return "".join(record.vcf_path + "\n" for record in records)


def _runtime_environment_block(
    policy: Mapping[str, Any], module_name: str, executable: str
) -> str:
    environment_init = policy["cluster_environment_init"]
    return f'''set -eo pipefail

# CSC software modules are not initialized in non-interactive SSH/Slurm shells.
# The CSC profile references optional shell variables, so nounset is disabled
# only while the environment is initialized.
set +u
source "{environment_init}"
module load {module_name}
set -u

command -v {executable} >/dev/null 2>&1 || {{
  echo "Required executable is unavailable after environment initialization: {executable}" >&2
  exit 10
}}
'''


def _proposal_runtime_guard(proposal_id: str) -> str:
    return f'''EXPECTED_PROPOSAL_ID="{proposal_id}"
PROPOSAL_DIR="${{GA_PROPOSAL_DIR:?GA_PROPOSAL_DIR must identify the staged proposal directory}}"

[[ "${{PROPOSAL_DIR}}" = /* ]] || {{
  echo "GA_PROPOSAL_DIR must be an absolute path: ${{PROPOSAL_DIR}}" >&2
  exit 11
}}
[[ "$(basename "${{PROPOSAL_DIR}}")" == "${{EXPECTED_PROPOSAL_ID}}" ]] || {{
  echo "Proposal ID/path mismatch: expected ${{EXPECTED_PROPOSAL_ID}}, got ${{PROPOSAL_DIR}}" >&2
  exit 12
}}
[[ -s "${{PROPOSAL_DIR}}/proposal.json" && -s "${{PROPOSAL_DIR}}/checksums.sha256" ]] || {{
  echo "Incomplete staged proposal bundle: ${{PROPOSAL_DIR}}" >&2
  exit 13
}}
(
  cd "${{PROPOSAL_DIR}}"
  sha256sum -c checksums.sha256 --quiet
) || {{
  echo "Staged proposal checksum verification failed: ${{PROPOSAL_DIR}}" >&2
  exit 14
}}
'''


def _gather_chromosomes_script(
    policy: Mapping[str, Any], proposal_id: str
) -> str:
    chromosomes = " ".join(policy["chromosome_order"])
    resources = policy["provisional_resources"]["gather_chromosomes"]
    software = policy["software"]
    prefix = policy["output_prefix"]
    output_root = policy["remote_output_root"]
    environment = _runtime_environment_block(
        policy, str(software["gatk_module"]), "gatk"
    )
    proposal_guard = _proposal_runtime_guard(proposal_id)
    return f'''#!/bin/bash
#SBATCH --account={policy["slurm_account"]}
#SBATCH --job-name=GA_gather_chr
#SBATCH --array=1-{len(policy["chromosome_order"])}%{len(policy["chromosome_order"])}
#SBATCH --cpus-per-task={resources["cpus_per_task"]}
#SBATCH --mem={resources["memory"]}
#SBATCH --time={resources["time_limit"]}
#SBATCH --output=GA_gather_chr_%A_%a.out
#SBATCH --error=GA_gather_chr_%A_%a.err

{environment}
{proposal_guard}
CHROMOSOMES=({chromosomes})
CHROM="${{CHROMOSOMES[$((SLURM_ARRAY_TASK_ID - 1))]}}"
INPUT_LIST="${{PROPOSAL_DIR}}/manifests/${{CHROM}}.inputs.list"
OUTPUT_ROOT="{output_root}"
CHROM_DIR="${{OUTPUT_ROOT}}/chromosomes"
STAGING_DIR="${{OUTPUT_ROOT}}/.staging/${{SLURM_JOB_ID}}_${{SLURM_ARRAY_TASK_ID}}"
FINAL_VCF="${{CHROM_DIR}}/{prefix}.${{CHROM}}.vcf.gz"
FINAL_TBI="${{FINAL_VCF}}.tbi"
TMP_VCF="${{STAGING_DIR}}/{prefix}.${{CHROM}}.vcf.gz"
TMP_TBI="${{TMP_VCF}}.tbi"

[[ -s "${{INPUT_LIST}}" ]] || {{ echo "Missing input list: ${{INPUT_LIST}}" >&2; exit 2; }}
[[ ! -e "${{FINAL_VCF}}" && ! -e "${{FINAL_TBI}}" ]] || {{
  echo "Refusing to overwrite published output: ${{FINAL_VCF}}" >&2
  exit 3
}}
[[ ! -e "${{STAGING_DIR}}" ]] || {{
  echo "Refusing to reuse existing staging directory: ${{STAGING_DIR}}" >&2
  exit 4
}}
mkdir -p "${{CHROM_DIR}}" "${{STAGING_DIR}}"
INPUTS=()
while IFS= read -r input || [[ -n "${{input}}" ]]; do
  [[ -n "${{input}}" ]] || continue
  INPUTS+=("${{input}}")
done < "${{INPUT_LIST}}"
(( ${{#INPUTS[@]}} > 0 )) || {{ echo "Empty input list" >&2; exit 5; }}
ARGS=()
for input in "${{INPUTS[@]}}"; do
  [[ -s "${{input}}" ]] || {{ echo "Missing VCF: ${{input}}" >&2; exit 6; }}
  [[ -s "${{input}}.tbi" ]] || {{ echo "Missing VCF index: ${{input}}.tbi" >&2; exit 7; }}
  ARGS+=(--INPUT "${{input}}")
done

gatk --java-options "-Xmx12g -Djava.io.tmpdir=${{TMPDIR:-${{STAGING_DIR}}}}" \\
  GatherVcfs \\
  "${{ARGS[@]}}" \\
  --OUTPUT "${{TMP_VCF}}" \\
  --CREATE_INDEX false

[[ -s "${{TMP_VCF}}" ]] || {{
  echo "Gather did not produce a chromosome VCF: ${{TMP_VCF}}" >&2
  exit 8
}}

gatk --java-options "-Xmx4g -Djava.io.tmpdir=${{TMPDIR:-${{STAGING_DIR}}}}" \\
  IndexFeatureFile \\
  -I "${{TMP_VCF}}"

[[ -s "${{TMP_TBI}}" ]] || {{
  echo "IndexFeatureFile did not produce: ${{TMP_TBI}}" >&2
  exit 9
}}

# Publish index first so the final VCF path appears only when its index exists.
mv "${{TMP_TBI}}" "${{FINAL_TBI}}"
mv "${{TMP_VCF}}" "${{FINAL_VCF}}"
rmdir "${{STAGING_DIR}}" 2>/dev/null || true
printf 'Published %s and %s\n' "${{FINAL_VCF}}" "${{FINAL_TBI}}"
'''


def _gather_genome_script(
    policy: Mapping[str, Any], proposal_id: str
) -> str:
    resources = policy["provisional_resources"]["gather_genome"]
    software = policy["software"]
    prefix = policy["output_prefix"]
    output_root = policy["remote_output_root"]
    chromosome_lines = "\n".join(
        f'  "${{CHROM_DIR}}/{prefix}.{chromosome}.vcf.gz"'
        for chromosome in policy["chromosome_order"]
    )
    environment = _runtime_environment_block(
        policy, str(software["gatk_module"]), "gatk"
    )
    proposal_guard = _proposal_runtime_guard(proposal_id)
    return f'''#!/bin/bash
#SBATCH --account={policy["slurm_account"]}
#SBATCH --job-name=GA_gather_genome
#SBATCH --cpus-per-task={resources["cpus_per_task"]}
#SBATCH --mem={resources["memory"]}
#SBATCH --time={resources["time_limit"]}
#SBATCH --output=GA_gather_genome_%j.out
#SBATCH --error=GA_gather_genome_%j.err

{environment}
{proposal_guard}
OUTPUT_ROOT="{output_root}"
CHROM_DIR="${{OUTPUT_ROOT}}/chromosomes"
FINAL_DIR="${{OUTPUT_ROOT}}/final"
STAGING_DIR="${{OUTPUT_ROOT}}/.staging/${{SLURM_JOB_ID}}"
FINAL_VCF="${{FINAL_DIR}}/{prefix}.whole_genome.vcf.gz"
FINAL_TBI="${{FINAL_VCF}}.tbi"
TMP_VCF="${{STAGING_DIR}}/{prefix}.whole_genome.vcf.gz"
TMP_TBI="${{TMP_VCF}}.tbi"

INPUTS=(
{chromosome_lines}
)

[[ ! -e "${{FINAL_VCF}}" && ! -e "${{FINAL_TBI}}" ]] || {{
  echo "Refusing to overwrite published output: ${{FINAL_VCF}}" >&2
  exit 2
}}
[[ ! -e "${{STAGING_DIR}}" ]] || {{
  echo "Refusing to reuse existing staging directory: ${{STAGING_DIR}}" >&2
  exit 3
}}
mkdir -p "${{FINAL_DIR}}" "${{STAGING_DIR}}"
ARGS=()
for input in "${{INPUTS[@]}}"; do
  [[ -s "${{input}}" ]] || {{ echo "Missing chromosome VCF: ${{input}}" >&2; exit 4; }}
  [[ -s "${{input}}.tbi" ]] || {{ echo "Missing chromosome index: ${{input}}.tbi" >&2; exit 5; }}
  ARGS+=(--INPUT "${{input}}")
done

gatk --java-options "-Xmx12g -Djava.io.tmpdir=${{TMPDIR:-${{STAGING_DIR}}}}" \\
  GatherVcfs \\
  "${{ARGS[@]}}" \\
  --OUTPUT "${{TMP_VCF}}" \\
  --CREATE_INDEX false

[[ -s "${{TMP_VCF}}" ]] || {{
  echo "Whole-genome gather did not produce: ${{TMP_VCF}}" >&2
  exit 6
}}

gatk --java-options "-Xmx4g -Djava.io.tmpdir=${{TMPDIR:-${{STAGING_DIR}}}}" \\
  IndexFeatureFile \\
  -I "${{TMP_VCF}}"

[[ -s "${{TMP_TBI}}" ]] || {{
  echo "IndexFeatureFile did not produce: ${{TMP_TBI}}" >&2
  exit 7
}}

mv "${{TMP_TBI}}" "${{FINAL_TBI}}"
mv "${{TMP_VCF}}" "${{FINAL_VCF}}"
rmdir "${{STAGING_DIR}}" 2>/dev/null || true
printf 'Published %s and %s\n' "${{FINAL_VCF}}" "${{FINAL_TBI}}"
'''


def _validate_final_script(
    policy: Mapping[str, Any], proposal_id: str, source_scan_id: str
) -> str:
    resources = policy["provisional_resources"]["validate_final"]
    software = policy["software"]
    prefix = policy["output_prefix"]
    output_root = policy["remote_output_root"]
    expected_contigs = " ".join(
        f'"{policy["reference_contig_map"][c]}"' for c in policy["chromosome_order"]
    )
    chromosome_aliases = " ".join(policy["chromosome_order"])
    chromosome_vcfs = "\n".join(
        f'  "${{CHROM_DIR}}/{prefix}.{chromosome}.vcf.gz"'
        for chromosome in policy["chromosome_order"]
    )
    environment = _runtime_environment_block(
        policy, str(software["bcftools_module"]), "bcftools"
    )
    proposal_guard = _proposal_runtime_guard(proposal_id)
    return f'''#!/bin/bash
#SBATCH --account={policy["slurm_account"]}
#SBATCH --job-name=GA_validate_joint
#SBATCH --cpus-per-task={resources["cpus_per_task"]}
#SBATCH --mem={resources["memory"]}
#SBATCH --time={resources["time_limit"]}
#SBATCH --output=GA_validate_joint_%j.out
#SBATCH --error=GA_validate_joint_%j.err

{environment}
{proposal_guard}
REFERENCE="{policy["reference_fasta"]}"
SAMPLE_MAP="{policy["sample_map"]}"
OUTPUT_ROOT="{output_root}"
CHROM_DIR="${{OUTPUT_ROOT}}/chromosomes"
FINAL_VCF="${{OUTPUT_ROOT}}/final/{prefix}.whole_genome.vcf.gz"
VALIDATION_DIR="${{OUTPUT_ROOT}}/validation"
EXPECTED_SAMPLES={policy["expected_samples"]}
EXPECTED_INTERVALS={policy["expected_intervals"]}
SOURCE_SCAN_ID="{source_scan_id}"
CHROMOSOME_ALIASES=({chromosome_aliases})
EXPECTED_CONTIGS=({expected_contigs})
CHROM_VCFS=(
{chromosome_vcfs}
)

mkdir -p "${{VALIDATION_DIR}}"
[[ -s "${{REFERENCE}}" && -s "${{REFERENCE}}.fai" ]] || {{ echo "Missing reference/index" >&2; exit 2; }}
[[ -s "${{SAMPLE_MAP}}" ]] || {{ echo "Missing sample map" >&2; exit 3; }}
[[ -s "${{FINAL_VCF}}" && -s "${{FINAL_VCF}}.tbi" ]] || {{ echo "Missing final VCF pair" >&2; exit 4; }}

bcftools view -h "${{FINAL_VCF}}" >/dev/null
bcftools query -l "${{FINAL_VCF}}" > "${{VALIDATION_DIR}}/final.samples.txt"
FINAL_SAMPLE_COUNT=$(wc -l < "${{VALIDATION_DIR}}/final.samples.txt" | tr -d ' ')
[[ "${{FINAL_SAMPLE_COUNT}}" -eq "${{EXPECTED_SAMPLES}}" ]] || {{
  echo "Final VCF sample count ${{FINAL_SAMPLE_COUNT}} != ${{EXPECTED_SAMPLES}}" >&2
  exit 5
}}

awk 'NF >= 2 && $1 !~ /^#/ {{print $1}}' "${{SAMPLE_MAP}}" > "${{VALIDATION_DIR}}/sample_map.samples.txt"
MAP_SAMPLE_COUNT=$(wc -l < "${{VALIDATION_DIR}}/sample_map.samples.txt" | tr -d ' ')
[[ "${{MAP_SAMPLE_COUNT}}" -eq "${{EXPECTED_SAMPLES}}" ]] || {{
  echo "Sample-map count ${{MAP_SAMPLE_COUNT}} != ${{EXPECTED_SAMPLES}}" >&2
  exit 6
}}
sort -u "${{VALIDATION_DIR}}/sample_map.samples.txt" > "${{VALIDATION_DIR}}/sample_map.samples.sorted.txt"
sort -u "${{VALIDATION_DIR}}/final.samples.txt" > "${{VALIDATION_DIR}}/final.samples.sorted.txt"
MAP_UNIQUE_COUNT=$(wc -l < "${{VALIDATION_DIR}}/sample_map.samples.sorted.txt" | tr -d ' ')
FINAL_UNIQUE_COUNT=$(wc -l < "${{VALIDATION_DIR}}/final.samples.sorted.txt" | tr -d ' ')
[[ "${{MAP_UNIQUE_COUNT}}" -eq "${{EXPECTED_SAMPLES}}" ]] || {{ echo "Duplicate sample-map IDs" >&2; exit 7; }}
[[ "${{FINAL_UNIQUE_COUNT}}" -eq "${{EXPECTED_SAMPLES}}" ]] || {{ echo "Duplicate final-VCF sample IDs" >&2; exit 8; }}
diff -u "${{VALIDATION_DIR}}/sample_map.samples.sorted.txt" "${{VALIDATION_DIR}}/final.samples.sorted.txt"

for index in "${{!CHROM_VCFS[@]}}"; do
  chromosome_vcf="${{CHROM_VCFS[$index]}}"
  chromosome_alias="${{CHROMOSOME_ALIASES[$index]}}"
  expected_contig="${{EXPECTED_CONTIGS[$index]}}"
  [[ -s "${{chromosome_vcf}}" && -s "${{chromosome_vcf}}.tbi" ]] || {{
    echo "Missing chromosome VCF pair: ${{chromosome_vcf}}" >&2
    exit 9
  }}
  bcftools query -l "${{chromosome_vcf}}" > \\
    "${{VALIDATION_DIR}}/$(basename "${{chromosome_vcf}}").samples.txt"
  diff -u "${{VALIDATION_DIR}}/final.samples.txt" \\
    "${{VALIDATION_DIR}}/$(basename "${{chromosome_vcf}}").samples.txt"
  tabix -l "${{chromosome_vcf}}" > \\
    "${{VALIDATION_DIR}}/${{chromosome_alias}}.contigs.txt"
  printf '%s\n' "${{expected_contig}}" > \\
    "${{VALIDATION_DIR}}/${{chromosome_alias}}.expected_contig.txt"
  diff -u "${{VALIDATION_DIR}}/${{chromosome_alias}}.expected_contig.txt" \\
    "${{VALIDATION_DIR}}/${{chromosome_alias}}.contigs.txt"
done

tabix -l "${{FINAL_VCF}}" > "${{VALIDATION_DIR}}/final.contigs.txt"
printf '%s\n' "${{EXPECTED_CONTIGS[@]}}" > "${{VALIDATION_DIR}}/expected.contigs.txt"
diff -u "${{VALIDATION_DIR}}/expected.contigs.txt" "${{VALIDATION_DIR}}/final.contigs.txt"

bcftools stats --threads "${{SLURM_CPUS_PER_TASK:-1}}" "${{FINAL_VCF}}" \\
  > "${{VALIDATION_DIR}}/{prefix}.whole_genome.stats.txt"
sha256sum "${{FINAL_VCF}}" "${{FINAL_VCF}}.tbi" \\
  > "${{VALIDATION_DIR}}/{prefix}.whole_genome.sha256"

PROPOSAL_CHECKSUMS_SHA256=$(sha256sum "${{PROPOSAL_DIR}}/checksums.sha256" | awk '{{print $1}}')
INTERVAL_MANIFEST_SHA256=$(sha256sum "${{PROPOSAL_DIR}}/interval_manifest.tsv" | awk '{{print $1}}')
REFERENCE_FAI_SHA256=$(sha256sum "${{REFERENCE}}.fai" | awk '{{print $1}}')

cat > "${{VALIDATION_DIR}}/validation_summary.json.tmp" <<JSON
{{
  "schema_version": "1.1",
  "status": "validated_complete",
  "proposal_id": "{proposal_id}",
  "source_scan_id": "${{SOURCE_SCAN_ID}}",
  "sample_count": ${{FINAL_SAMPLE_COUNT}},
  "expected_sample_count": ${{EXPECTED_SAMPLES}},
  "interval_count": ${{EXPECTED_INTERVALS}},
  "chromosome_vcf_count": ${{#CHROM_VCFS[@]}},
  "proposal_checksums_sha256": "${{PROPOSAL_CHECKSUMS_SHA256}}",
  "interval_manifest_sha256": "${{INTERVAL_MANIFEST_SHA256}}",
  "reference_fai_sha256": "${{REFERENCE_FAI_SHA256}}",
  "final_vcf": "${{FINAL_VCF}}",
  "final_index": "${{FINAL_VCF}}.tbi"
}}
JSON
mv "${{VALIDATION_DIR}}/validation_summary.json.tmp" \\
  "${{VALIDATION_DIR}}/validation_summary.json"
printf 'Final joint VCF validation passed: %s\n' "${{FINAL_VCF}}"
'''


def _plan_markdown(
    task: str,
    proposal_id: str,
    policy: Mapping[str, Any],
    source_scan_id: str,
    records: Sequence[IntervalRecord],
) -> str:
    counts = {
        chromosome: sum(record.chromosome == chromosome for record in records)
        for chromosome in policy["chromosome_order"]
    }
    count_lines = "\n".join(f"- `{chromosome}`: {count} intervals" for chromosome, count in counts.items())
    return f"""# GenomeAgent Proposal: gather scattered joint-calling VCFs

**Proposal ID:** `{proposal_id}`

**Task:** `{task}`

**Action:** `gather_or_merge`

**Source Task Scan:** `{source_scan_id}`

**Status:** `awaiting_researcher_review`

## Evidence gate

- {len(records)}/{policy['expected_intervals']} interval VCF/index pairs are bound to the proposal.
- {policy['expected_samples']} samples are expected in every interval and gathered output.
- Canonical Task State is `scattered_genotyping_complete` at stage `gather_or_merge`.
- No running, queued, failed, unresolved, unsubmitted, or output-missing interval remains.
- Automatic execution is disabled.

## Proposed workflow

1. Gather interval VCFs independently into seven retained chromosome VCFs.
2. Create each chromosome `.tbi` explicitly with `IndexFeatureFile` and publish only complete VCF/index pairs.
3. Gather the seven chromosome VCFs in reference order into one whole-genome VCF and index it explicitly.
4. Validate sample identity/order, per-chromosome and final contig order, readability, indexability, provenance, and summary statistics.
5. Run a fresh Task Scan and Task State ingestion after successful validation.

## Interval counts

{count_lines}

## Generated scripts

- `scripts/01_gather_chromosomes.slurm`
- `scripts/02_gather_genome.slurm`
- `scripts/03_validate_final_vcf.slurm`

The scripts are proposals only. Proposal Core cannot stage, submit, execute, delete, or update knowledge.
At execution time they require `GA_PROPOSAL_DIR` to point to the checksum-verified staged proposal and initialize the CSC software environment through `{policy['cluster_environment_init']}`.
Resources are conservative provisional values and require researcher review because they are not claimed as learned optimal allocations.
"""


class ProposalCore:
    """Prepare, validate, and display immutable proposal bundles."""

    def __init__(
        self,
        *,
        policy_root: Path = Path("config/proposals"),
        task_state_root: Path = Path("workspace/task_state"),
        task_scan_root: Path = Path("workspace/task_scans"),
        proposal_root: Path = Path("workspace/proposals"),
    ) -> None:
        self.policy_root = Path(policy_root)
        self.task_state_root = Path(task_state_root)
        self.task_scan_root = Path(task_scan_root)
        self.proposal_root = Path(proposal_root)

    def policy_path(self, task: str, action: str) -> Path:
        return self.policy_root / f"{task}_{action}.json"

    def _latest_scan_id(self, task: str, recommendations: Mapping[str, Any]) -> str:
        source = str(recommendations.get("source_scan_id") or "")
        if not re.fullmatch(r"\d{8}T\d{6}Z", source):
            raise ProposalCoreError("Recommendations do not identify a safe source scan ID.")
        return source

    def prepare(self, task: str, action: str) -> ProposalResult:
        task = _safe_identifier(task, "task")
        action = _safe_identifier(action, "action")
        if action not in SUPPORTED_ACTIONS:
            raise ProposalCoreError(f"Unsupported proposal action: {action}")

        policy_path = self.policy_path(task, action)
        policy_raw, policy_bytes = _read_json(policy_path, "proposal policy")
        policy = _validate_policy(policy_raw, task, action)

        state_dir = self.task_state_root / task
        state_path = state_dir / "current_state.json"
        recommendations_path = state_dir / "recommendations.json"
        provenance_path = state_dir / "provenance.json"
        state, state_bytes = _read_json(state_path, "canonical task state")
        recommendations, recommendations_bytes = _read_json(
            recommendations_path, "task recommendations"
        )
        provenance, provenance_bytes = _read_json(provenance_path, "task-state provenance")

        source_scan_id = self._latest_scan_id(task, recommendations)
        scan_dir = self.task_scan_root / task / source_scan_id
        task_scan_path = scan_dir / "task_scan.json"
        task_scan, task_scan_bytes = _read_json(task_scan_path, "bound Task Scan")
        task_scan_sha256 = _sha256_bytes(task_scan_bytes)
        _validate_state_and_recommendations(
            task, state, recommendations, source_scan_id, task_scan_sha256
        )

        summary_path = scan_dir / "scatter_summary.tsv"
        summary = _one_row_tsv(summary_path, "scatter summary")
        _validate_summary(summary, policy)

        records = _load_interval_records(
            scan_dir, task_scan, policy["chromosome_order"], policy["reference_contig_map"]
        )
        records = _validate_intervals(records, policy)

        source_artifacts = [
            {"artifact_type": "proposal_policy", "path": str(policy_path), "sha256": _sha256_bytes(policy_bytes)},
            {"artifact_type": "current_task_state", "path": str(state_path), "sha256": _sha256_bytes(state_bytes)},
            {"artifact_type": "task_recommendations", "path": str(recommendations_path), "sha256": _sha256_bytes(recommendations_bytes)},
            {"artifact_type": "task_state_provenance", "path": str(provenance_path), "sha256": _sha256_bytes(provenance_bytes)},
            {"artifact_type": "task_scan", "path": str(task_scan_path), "sha256": task_scan_sha256},
            {"artifact_type": "scatter_summary", "path": str(summary_path), "sha256": _sha256_file(summary_path)},
            {"artifact_type": "interval_status", "path": str(scan_dir / "interval_status.tsv"), "sha256": _sha256_file(scan_dir / "interval_status.tsv")},
        ]
        identity = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "proposal_core_version": PROPOSAL_CORE_VERSION,
            "task": task,
            "action": action,
            "source_scan_id": source_scan_id,
            "source_artifacts": source_artifacts,
            "interval_manifest": [record.__dict__ for record in records],
            "policy": policy,
        }
        proposal_id = _sha256_value(identity)
        proposal_dir = self.proposal_root / task / proposal_id

        by_chromosome = {
            chromosome: [record for record in records if record.chromosome == chromosome]
            for chromosome in policy["chromosome_order"]
        }
        manifest = _manifest_text(records, source_scan_id)
        evidence_snapshot = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "task": task,
            "action": action,
            "proposal_id": proposal_id,
            "source_scan_id": source_scan_id,
            "source_artifacts": source_artifacts,
            "scatter_summary": {key: int(value) if value.isdigit() else value for key, value in summary.items()},
            "task_state": state,
            "recommendations": recommendations,
            "provenance": provenance,
        }
        proposal = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "proposal_core_version": PROPOSAL_CORE_VERSION,
            "template_version": policy["template_version"],
            "proposal_id": proposal_id,
            "proposal_type": "workflow_stage",
            "task": task,
            "action": action,
            "status": "awaiting_researcher_review",
            "source_scan_id": source_scan_id,
            "source_task_scan_sha256": task_scan_sha256,
            "expected_interval_inputs": policy["expected_intervals"],
            "validated_interval_inputs": len(records),
            "expected_samples": policy["expected_samples"],
            "expected_chromosome_outputs": len(policy["chromosome_order"]),
            "expected_final_outputs": 1,
            "stages": [
                "gather_chromosomes",
                "gather_genome",
                "validate_final_vcf",
            ],
            "automatic_execution_allowed": False,
            "slurm_submission_allowed": False,
            "remote_writes_allowed_by_proposal_core": False,
            "researcher_approval_required_before_execution": True,
            "fresh_pre_execution_scan_required": True,
            "resources_are_empirically_learned": False,
            "mac_controlled_submission_supported": True,
            "required_submission_environment": ["GA_PROPOSAL_DIR"],
            "resource_review_required": True,
            "remote_output_root": policy["remote_output_root"],
            "final_vcf": f"{policy['remote_output_root']}/final/{policy['output_prefix']}.whole_genome.vcf.gz",
        }
        validation_rules = {
            "schema_version": "1.0",
            "task": task,
            "action": action,
            "proposal_id": proposal_id,
            "rules": [
                {"id": "source_state_complete", "expected": True},
                {"id": "interval_count", "expected": policy["expected_intervals"]},
                {"id": "unique_interval_vcf_paths", "expected": policy["expected_intervals"]},
                {"id": "chromosome_outputs", "expected": len(policy["chromosome_order"])},
                {"id": "sample_count", "expected": policy["expected_samples"]},
                {"id": "sample_set_matches_sample_map", "expected": True},
                {"id": "sample_order_consistent_across_gathered_vcfs", "expected": True},
                {"id": "contig_order_matches_reference", "expected": policy["reference_contig_map"]},
                {"id": "final_vcf_index_pair", "expected": True},
                {"id": "explicit_index_creation", "expected": True},
                {"id": "staged_proposal_runtime_checksum", "expected": True},
                {"id": "mac_controlled_submission_supported", "expected": True},
                {"id": "automatic_execution_allowed", "expected": False},
            ],
        }
        resource_proposal = {
            "schema_version": "1.0",
            "task": task,
            "action": action,
            "proposal_id": proposal_id,
            "proposal_status": "provisional_researcher_review_required",
            "allocation_proposal_available_from_learning_core": False,
            "reason": "No comparable validated gather resource profile is bound to this proposal.",
            "provisional_resources": policy["provisional_resources"],
            "automatic_resource_change_allowed": False,
        }

        artifacts: dict[Path, str] = {
            proposal_dir / "proposal.json": _pretty_json(proposal),
            proposal_dir / "plan.md": _plan_markdown(task, proposal_id, policy, source_scan_id, records),
            proposal_dir / "evidence_snapshot.json": _pretty_json(evidence_snapshot),
            proposal_dir / "interval_manifest.tsv": manifest,
            proposal_dir / "validation_rules.json": _pretty_json(validation_rules),
            proposal_dir / "resource_proposal.json": _pretty_json(resource_proposal),
            proposal_dir / "scripts" / "01_gather_chromosomes.slurm": _gather_chromosomes_script(policy, proposal_id),
            proposal_dir / "scripts" / "02_gather_genome.slurm": _gather_genome_script(policy, proposal_id),
            proposal_dir / "scripts" / "03_validate_final_vcf.slurm": _validate_final_script(policy, proposal_id, source_scan_id),
        }
        for chromosome, chromosome_records in by_chromosome.items():
            artifacts[proposal_dir / "manifests" / f"{chromosome}.inputs.list"] = _inputs_text(chromosome_records)

        created = False
        for path, text in artifacts.items():
            created = _write_immutable(path, text) or created

        checksum_lines = []
        for path in sorted(artifacts, key=lambda item: str(item.relative_to(proposal_dir))):
            relative = path.relative_to(proposal_dir)
            checksum_lines.append(f"{_sha256_file(path)}  {relative}")
        checksum_text = "\n".join(checksum_lines) + "\n"
        created = _write_immutable(proposal_dir / "checksums.sha256", checksum_text) or created
        artifact_paths = tuple(sorted(proposal_dir.rglob("*")))
        return ProposalResult(
            task=task,
            action=action,
            proposal_id=proposal_id,
            status="awaiting_researcher_review",
            proposal_dir=proposal_dir,
            interval_count=len(records),
            chromosome_count=len(by_chromosome),
            artifact_paths=artifact_paths,
            created=created,
        )

    def validate(self, proposal_dir: Path) -> dict[str, Any]:
        proposal_dir = Path(proposal_dir)
        proposal, _ = _read_json(proposal_dir / "proposal.json", "proposal")
        if proposal.get("schema_version") != PROPOSAL_SCHEMA_VERSION:
            raise ProposalCoreError("Unsupported proposal schema.")
        if proposal.get("automatic_execution_allowed") is not False:
            raise ProposalCoreError("Proposal unexpectedly allows automatic execution.")
        if proposal.get("slurm_submission_allowed") is not False:
            raise ProposalCoreError("Proposal unexpectedly allows Slurm submission.")

        evidence_snapshot, _ = _read_json(
            proposal_dir / "evidence_snapshot.json", "proposal evidence snapshot"
        )
        for source in evidence_snapshot.get("source_artifacts", []):
            if not isinstance(source, Mapping):
                raise ProposalCoreError("Malformed source-artifact binding.")
            source_path = Path(str(source.get("path") or ""))
            source_digest = str(source.get("sha256") or "")
            if not source_path.is_file():
                raise ProposalCoreError(f"Bound source artifact is missing: {source_path}")
            if _sha256_file(source_path) != source_digest:
                raise ProposalCoreError(
                    f"Bound source artifact changed; proposal is stale: {source_path}"
                )

        checksums_path = proposal_dir / "checksums.sha256"
        try:
            checksum_lines = checksums_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError as exc:
            raise ProposalCoreError("Proposal checksums are missing.") from exc
        verified = 0
        for line in checksum_lines:
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if not match:
                raise ProposalCoreError("Malformed proposal checksum line.")
            expected, relative = match.groups()
            path = proposal_dir / relative
            if not path.is_file() or _sha256_file(path) != expected:
                raise ProposalCoreError(f"Proposal artifact checksum mismatch: {relative}")
            verified += 1

        manifest_rows = _read_tsv(proposal_dir / "interval_manifest.tsv", "proposal manifest")
        expected_intervals = _integer(proposal.get("expected_interval_inputs"), "expected_interval_inputs", 1)
        if len(manifest_rows) != expected_intervals:
            raise ProposalCoreError("Proposal manifest interval count mismatch.")
        vcf_paths = [row.get("vcf_path", "") for row in manifest_rows]
        if len(vcf_paths) != len(set(vcf_paths)):
            raise ProposalCoreError("Proposal manifest contains duplicate VCF paths.")

        chromosome_counts: dict[str, int] = {}
        for row in manifest_rows:
            chromosome_counts[row["chromosome"]] = chromosome_counts.get(row["chromosome"], 0) + 1
        manifest_dir = proposal_dir / "manifests"
        for chromosome, expected_count in chromosome_counts.items():
            lines = [line for line in (manifest_dir / f"{chromosome}.inputs.list").read_text().splitlines() if line]
            if len(lines) != expected_count:
                raise ProposalCoreError(f"Input-list count mismatch for {chromosome}.")

        script_results = []
        for script in sorted((proposal_dir / "scripts").glob("*.slurm")):
            text = script.read_text(encoding="utf-8")
            for pattern, reason in FORBIDDEN_SCRIPT_PATTERNS.items():
                if re.search(pattern, text):
                    raise ProposalCoreError(f"Unsafe script {script.name}: {reason}.")
            completed = subprocess.run(
                ["bash", "-n", str(script)], text=True, capture_output=True
            )
            if completed.returncode != 0:
                raise ProposalCoreError(
                    f"Shell syntax validation failed for {script.name}: {completed.stderr}"
                )
            script_results.append({"script": script.name, "bash_syntax": "passed"})

        return {
            "schema_version": "1.0",
            "proposal_id": proposal.get("proposal_id"),
            "status": "valid_for_researcher_review",
            "artifacts_verified": verified,
            "intervals_verified": len(manifest_rows),
            "chromosomes_verified": len(chromosome_counts),
            "scripts": script_results,
            "automatic_execution_allowed": False,
        }

    def show(self, proposal_dir: Path) -> str:
        proposal, _ = _read_json(Path(proposal_dir) / "proposal.json", "proposal")
        lines = [
            "=" * 80,
            f"GenomeAgent Proposal Core v{proposal.get('proposal_core_version', PROPOSAL_CORE_VERSION)}",
            "=" * 80,
            f"Proposal ID      : {proposal.get('proposal_id')}",
            f"Task             : {proposal.get('task')}",
            f"Action           : {proposal.get('action')}",
            f"Status           : {proposal.get('status')}",
            f"Source scan      : {proposal.get('source_scan_id')}",
            f"Interval inputs  : {proposal.get('validated_interval_inputs')}/{proposal.get('expected_interval_inputs')}",
            f"Chromosome VCFs  : {proposal.get('expected_chromosome_outputs')}",
            f"Final VCF        : {proposal.get('final_vcf')}",
            "Automatic execute: no",
            "Researcher review: required",
        ]
        return "\n".join(lines) + "\n"
