# Reusable Task Scanner Core

## Purpose

The Task Scanner Core provides a common framework for deterministic, read-only inspection of active scientific workflows. Each task profile defines what GenomeAgent should observe, how those observations are interpreted and which task-specific reports are produced.

The first reusable profile monitors GAM duplicate removal for the *Fragaria vesca* pangenome project. It covers the authoritative graph-mapping cohort of 458 samples: 233 own samples and 225 Swedish samples.

## Architecture

The implementation separates reusable infrastructure from scientific task knowledge:

- `genomeagent/task_scanner.py` provides SSH collection, standard result envelopes, timestamped output directories and safe TSV writing.
- `genomeagent/task_profiles/gam_deduplication.py` contains deterministic GAM-deduplication observation and status rules.
- `config/tasks/gam_deduplication.json` records the expected datasets, candidate Puhti paths, sample counts and bounded log-scanning limits.
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
- `missing_inputs.tsv`: manifest or worker samples without a non-empty input GAM.
- `running_jobs.tsv`: relevant jobs currently reported by `squeue`.
- `recent_jobs.tsv`: relevant jobs reported by `sacct` for the current day.
- `scanned_paths.tsv`: path candidates and the paths selected by the profile.
- `recent_errors.txt`: error-like lines from bounded tails of recent logs.

## Path selection

The configuration contains ordered path candidates because the active GAMs may be in their original mapping directories or under `restored_gams_from_allas`. The scanner selects the first existing candidate and records every checked path in `scanned_paths.tsv`.

The profile also supports master manifests that contain only remaining samples. Its sample universe is the union of:

- master-manifest sample identifiers;
- GAM filenames in the selected input directory;
- GAM filenames in the selected output directory; and
- worker-manifest sample identifiers.

This prevents an incremental rerun manifest from being mistaken for the full 233- or 225-sample cohort.

## Deterministic status model

Per-sample states are deliberately conservative:

- `output_present_unvalidated`: a non-empty output GAM exists.
- `assigned_pending_or_running`: an input exists and a worker manifest assigns the sample, but no output is present.
- `pending_unassigned`: an input exists but no output or worker assignment was found.
- `missing_input`: the sample is expected but no non-empty input GAM was found.

A non-empty output is only a metadata-level completion candidate. It is not called validated by the Task Scanner.

When all 458 expected output GAMs are present, the next safe action becomes `validate_deduplicated_gams_before_vg_pack`. Content-level validation remains a separate workflow and should confirm the established duplicate-template accounting rule, including `EXACT_TEMPLATE_PAIR_MATCH`, before `vg pack` and `vg call -a` are started.

## Safety and HPC impact

The profile is read-only. It does not:

- submit, cancel or modify SLURM jobs;
- write or remove files on Puhti;
- read complete GAM contents;
- run `vg stats`, `vg pack` or `vg call`;
- compute checksums across large files; or
- perform residual-duplication QC while the main jobs are active.

It reads directory metadata, manifests, scheduler state and at most the configured number of bytes from the tails of recent log files. This keeps monitoring lightweight while GAM processing is using the parallel filesystem.

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
