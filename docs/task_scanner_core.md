# Reusable Task Scanner Core

## Purpose

The Task Scanner Core provides a common framework for deterministic, read-only inspection of active scientific workflows. Each task profile defines what GenomeAgent should observe, how those observations are interpreted and which task-specific reports are produced.

The first reusable profile monitors GAM duplicate removal for the *Fragaria vesca* pangenome project. It covers the authoritative graph-mapping cohort of 458 samples: 233 own samples and 225 Swedish samples. A second profile monitors the 455-sample linear-reference joint-calling workflow scattered into manifest-defined 250 kb intervals.

## Architecture

The implementation separates reusable infrastructure from scientific task knowledge:

- `genomeagent/task_scanner.py` provides SSH collection, standard result envelopes, timestamped output directories and safe TSV writing.
- `genomeagent/task_profiles/gam_deduplication.py` contains deterministic GAM-deduplication observation and status rules.
- `genomeagent/task_profiles/scattered_joint_calling.py` models interval-table integrity, GenomicsDB prerequisites, atomic interval-output publication and scheduler state.
- `config/tasks/gam_deduplication.json` records the expected datasets, candidate Puhti paths, sample counts and bounded log-scanning limits.
- `config/tasks/scattered_joint_calling.json` records the current joint-calling paths, eight submission batches and the worker's atomic publication contract.
- `scripts/task_scan.py` is the generic command-line entry point and profile registry.

The remote observation is performed in one SSH session. The profile sends a Python program through standard input and receives one structured JSON observation. No GenomeAgent installation is required on Puhti.

## Running the GAM deduplication scan

Run this from the GenomeAgent repository on the Mac:

```bash
python3 scripts/task_scan.py gam_deduplication
```

The defaults use the SSH alias `puhti` and restrict scheduler observations to the CSC account `project_2001113`. Another alias or configuration can be selected explicitly:

```bash
python3 scripts/task_scan.py gam_deduplication \
  --host puhti \
  --config config/tasks/gam_deduplication.json
```

Results are written locally under:

```text
workspace/task_scans/gam_deduplication/<UTC_TIMESTAMP>/
```

The scan produces:

- `report.md`: concise status and next safe action.
- `task_scan.json`: complete structured observation, configuration and interpretation.
- `dataset_summary.tsv`: progress for own and Swedish datasets.
- `sample_status.tsv`: one row per observed sample.
- `missing_inputs.tsv`: samples without a non-empty input GAM or successful retained completion evidence.
- `running_jobs.tsv`: relevant jobs currently reported by `squeue`.
- `recent_jobs.tsv`: relevant jobs reported by `sacct` for the current day.
- `scanned_paths.tsv`: path candidates and the paths selected by the profile.
- `recent_errors.txt`: error-like lines from bounded tails of recent logs.

## Path selection

The configuration contains ordered path candidates because the active GAMs may be in their original mapping directories or under `restored_gams_from_allas`. The scanner selects the first existing candidate and records every checked path in `scanned_paths.tsv`.

The profile also supports master manifests that contain only remaining samples. Its sample universe is the union of:

- master-manifest sample identifiers;
- GAM filenames in the selected input directory;
- deduplicated GAMs matched under `deduplicated_gams/`;
- validation summaries matched under `dedup_stats/`; and
- worker-manifest sample identifiers.

This prevents an incremental rerun manifest from being mistaken for the full 233- or 225-sample cohort.

## Deterministic status model

Per-sample states are deliberately conservative:

- `completed_exact_template_pair_match`: the deduplicated GAM and its summary exist, and the summary records `EXACT_TEMPLATE_PAIR_MATCH`.
- `output_present_unvalidated`: a non-empty output GAM exists.
- `summary_present_output_missing`: validation evidence exists but the expected output GAM is missing.
- `assigned_pending_or_running`: an input exists and a worker manifest assigns the sample, but no output is present.
- `pending_unassigned`: an input exists but no output or worker assignment was found.
- `missing_input_no_completion_evidence`: neither a non-empty input nor successful completion evidence was found.

The production worker deletes a restored source GAM only after writing the deduplicated GAM and an `EXACT_TEMPLATE_PAIR_MATCH` summary. The scanner therefore treats a missing source GAM as a successful lifecycle transition when both retained outputs exist. It does not report that sample as a missing input.

When all 458 expected outputs have `EXACT_TEMPLATE_PAIR_MATCH` evidence, the next safe action becomes `review_residual_duplication_qc_before_vg_pack`. Residual-duplication QC remains separate from the worker's exact duplicate-template accounting.

Scheduler failures and log errors are associated with SLURM parent job IDs. Errors from a superseded attempt remain visible as historical provenance but do not change the status of a newer active attempt. Errors belonging to the current parent job still produce `running_with_warnings` or `attention_required`.

## Running the scattered joint-calling scan

Run the profile from the GenomeAgent repository on the Mac:

```bash
python3 scripts/task_scan.py scattered_joint_calling
```

Results are written under:

```text
workspace/task_scans/scattered_joint_calling/<UTC_TIMESTAMP>/
```

The authoritative task universe comes from `genotyped_scatter_250kb/intervals_250kb.tsv`. Each physical row is checked because the worker calculates `LINE=SLURM_ARRAY_TASK_ID+OFFSET`, reads that physical line with `sed`, and requires the table's `TASK` value to equal `LINE`. Blank or malformed rows, duplicate task IDs, duplicate output paths and TASK/line mismatches block a recommendation to submit more work.

For every interval, the scanner checks the manifest-defined VCF and its index. The production worker writes into a task-specific temporary directory, verifies that the VCF and TBI are non-empty, checks VCF header readability and verifies 455 samples before moving the pair to its final paths. The profile therefore records a final non-empty VCF/index pair as `completed_atomic_publish_contract`. This is workflow-contract evidence, not an independent content scan: GenomeAgent deliberately does not launch `bcftools` once per interval while the scattered workflow is active.

Interval output metadata is collected with one directory scan per parent directory rather than separate existence and stat calls for every expected path. This is important on Puhti's parallel filesystem, especially while most interval outputs are still absent. The report and `scan_timings.tsv` retain phase timings so unusually slow filesystem, scheduler or log observations can be identified without guessing.

Log discovery is failure-directed and bounded before file access. The production configuration recognizes the `GTscatter_*` scheduler names and selects log files only when their embedded SLURM parent IDs occur in scheduler-confirmed failed records. Eligible filenames are ranked by SLURM parent job ID, with error logs ahead of standard output for the same job, and only the configured maximum is opened and tailed. Metadata is obtained from the already-open file descriptor. Routine progress scans therefore do not open active or historical success logs.

The joint-calling scan produces:

- `scatter_summary.tsv`: combined sample, interval, workspace and scheduler counts.
- `scan_timings.tsv`: remote observation time spent in each bounded scan phase.
- `interval_status.tsv`: one row for every interval-table task.
- `incomplete_intervals.tsv`: pending intervals and inconsistent VCF/index pairs.
- `workspace_status.tsv`: the GenomicsDB workspace required by each chromosome label.
- `running_jobs.tsv` and `recent_jobs.tsv`: relevant scheduler observations.
- `scanned_paths.tsv`: configured path candidates and selected paths.
- `recent_errors.txt`: error-like lines from bounded tails of recent logs.

The sample count is derived from the selected sample map. The configured value 455 is only a fallback and a consistency expectation for the current linear-reference workflow. Interval progress, rather than the number of submitted batches, is authoritative because batches can be interrupted or retried.

## Safety and HPC impact

The profile is read-only. It does not:

- submit, cancel or modify SLURM jobs;
- write or remove files on Puhti;
- read complete GAM contents;
- run `vg stats`, `vg pack` or `vg call`;
- run GATK or `bcftools`;
- compute checksums across large files; or
- perform residual-duplication QC while the main jobs are active.

It reads directory metadata, small deduplication summary files, scheduler state and at most the configured number of bytes from the tails of recent log files. This keeps monitoring lightweight while GAM processing is using the parallel filesystem.

## Adding another task profile

New profiles implement four operations:

1. `collect`: gather a structured read-only observation.
2. `interpret`: derive deterministic status and the next safe action.
3. `write_artifacts`: create task-specific TSV or text outputs.
4. `render_report`: create the human-readable report.

The profile is then added to `PROFILE_REGISTRY` in `scripts/task_scan.py`. Reusable SSH and output handling remain in the core instead of being copied into each workflow scanner.

## Tests

Run all standard-library tests locally:

```bash
python3 -m unittest discover -s tests -v
```

The GAM profile tests use temporary synthetic GAM metadata, manifests and logs. They do not connect to Puhti.
