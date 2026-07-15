# Resource Evidence and Learning Core

The Resource Evidence and Learning Core turns bounded Slurm accounting observations into reusable, deterministic operational knowledge. It is separate from both the Task Scanner and the future Execution Engine:

1. The **Task Scanner** determines workflow state and output completeness.
2. The **Resource Collector** reads accounting for explicitly named Slurm jobs and writes an immutable local evidence snapshot.
3. The **Learning Core** replays all snapshots into attempt history, empirical profiles, anomaly flags and proposal-only recommendations.
4. The **Resource Decision and Transfer Core** converts profiles into explicit target-environment proposals or evidence-insufficiency decisions.
5. A future **Execution Engine** may consume approved plans, but these cores cannot submit, cancel or alter jobs.

No AI model is required to calculate the profiles or recommendations. The rules are deterministic and testable. AI-assisted interpretation may later explain evidence, but it must not silently change the recorded measurements or safety thresholds.

## Safety boundary

`collect` executes one bounded, read-only `sacct` query through the configured SSH host. Job IDs are explicit, limited to 20 per query and restricted to numeric Slurm identifiers. The collector does not run `sbatch`, `scancel`, `scontrol`, `sstat`, module commands or filesystem scans on Puhti.

`ingest` performs no remote access. It reads immutable local evidence and writes only canonical artifacts under `workspace/task_resources/<task>/`.

All recommendations contain `execution_enabled: false`. Resource proposals require at least three comparable successful attempts and researcher review. TIMEOUT observations are runtime lower bounds; OUT_OF_MEMORY observations are memory lower bounds. Neither is treated as a normal successful measurement.

## Collect evidence

Run commands from the GenomeAgent repository on the Mac. These commands read Puhti accounting and write local GenomeAgent evidence; they do not modify Puhti.

Record both task 16 attempts:

```bash
python3 scripts/task_resources.py collect scattered_joint_calling \
  --job-id 35442372_16 \
  --job-id 35452993_16 \
  --profile-key scattered_genotypegvcfs_250kb \
  --unit 35442372_16=16 \
  --unit 35452993_16=16
```

Record the AT02 residual duplicate-QC job:

```bash
python3 scripts/task_resources.py collect gam_deduplication \
  --job-id 35448117_2 \
  --profile-key gam_dedup_residual_qc \
  --unit 35448117_2=swedish:AT02
```

The `--unit` mappings are explicit provenance. For scattered joint calling, the collector can also recover known attempt-to-interval mappings from saved Task Scanner bundles.

The collector combines a Slurm array-task record with its `.batch` step. The parent record supplies lifecycle state, elapsed time, allocation and requested memory; the batch step normally supplies `MaxRSS`. This is why the Task Scanner's scheduler summaries alone are not sufficient for resource learning.

## Automatically collect newly terminal attempts

After running a Task Scanner, preview the locally discovered candidates:

```bash
python3 scripts/task_resources.py collect-new scattered_joint_calling --dry-run
```

The dry run performs no SSH access and writes nothing. To query the selected attempts and preserve their accounting evidence:

```bash
python3 scripts/task_resources.py collect-new scattered_joint_calling
python3 scripts/task_resources.py ingest scattered_joint_calling
```

The same workflow applies to GAM duplicate removal:

```bash
python3 scripts/task_resources.py collect-new gam_deduplication --dry-run
python3 scripts/task_resources.py collect-new gam_deduplication
python3 scripts/task_resources.py ingest gam_deduplication
```

Discovery replays every saved read-only Task Scanner bundle, selects terminal Slurm array elements and excludes attempts already stored as terminal resource evidence. It processes at most 20 new attempts per invocation. If a historical backlog is larger, `Remaining candidates` reports how many can be handled by later bounded runs.

Array-parent summary rows are deliberately ignored because they are not individual computational attempts and could distort empirical profiles. For scattered joint calling, batch offsets map Slurm array elements back to the 1–886 interval-task identities. GAM fast-deduplication jobs are assigned to `gam_dedup_fast`; their sample identity remains unknown unless authoritative attempt-to-sample evidence exists.

Repeated collection is safe: once a terminal attempt is present in immutable evidence, later `collect-new` runs do not query it again. A no-op run performs no SSH query and writes no new snapshot.

## Build resource knowledge

After collecting one or more snapshots:

```bash
python3 scripts/task_resources.py ingest scattered_joint_calling
python3 scripts/task_resources.py ingest gam_deduplication
```

The reducer writes:

| Artifact | Purpose |
|---|---|
| `resource_observations.jsonl` | Every normalized observation, including repeated observations of the same attempt. |
| `resource_profiles.json` | Successful-attempt distributions, confidence, censored failure counts and cautious proposals. |
| `resource_anomalies.json` | Deterministic attempt-level and cross-attempt flags with interpretation boundaries. |
| `resource_recommendations.json` | Non-executable review or collection recommendations. |
| `resource_profile_summary.tsv` | Compact profile counts, percentiles and proposal status for terminal inspection. |
| `resource_anomalies.tsv` | Compact anomaly list with units and attempt IDs. |
| `report.md` | Human-readable profile, anomaly and recommendation summary. |
| `provenance.json` | Source paths, SHA-256 digests, observation counts and safety declarations. |

Previously ingested evidence may be extended with new snapshots, but it may not be silently changed or removed. Re-ingesting unchanged evidence produces byte-identical artifacts.

## Learning rules

Profiles group comparable attempts by the explicit `profile_key` supplied during collection. Successful terminal jobs define ordinary elapsed-time, peak-memory and efficiency distributions. The initial confidence levels are:

Resource policy v1.2 additionally groups observations by `source_host`. Identical profile keys and Slurm attempt IDs from different environments remain separate, preventing future Roihu evidence from being merged with Puhti evidence.

| Successful attempts | Confidence | Resource proposal |
|---:|---|---|
| 0–2 | insufficient | none |
| 3–4 | low | only if task-specific diversity is sufficient |
| 5–19 | medium | only if task-specific diversity is sufficient |
| 20 or more | high by sample size | only if task-specific diversity is sufficient |

Sample count is only one part of confidence. Scattered joint-calling profiles additionally require successful evidence from at least three configured scatter batches and three chromosomes before a resource proposal is exposed. Until that diversity boundary is met, sample-size confidence and final confidence are reported separately, and the proposed memory and walltime remain empty.

When evidence is sufficiently diverse, the initial proposal is the nearest-rank p95 of successful attempts, with a 25% peak-memory margin and 30% runtime margin. Memory is rounded up to GiB and time to hours. These constants and diversity requirements are versioned policy, not learned truth, and can be revised through tests and researcher review.

Low CPU efficiency is recorded as a possible waiting or accounting anomaly, not as proof that fewer CPUs should be requested. When at least three low-efficiency successes comprise at least half of a profile's measured successes, they are aggregated into one `systematic_low_cpu_efficiency_pattern` instead of generating one alert per attempt. A timeout followed by a completion of the same unit in less than one-third of the time is flagged as runtime instability. Similar peak memory across those attempts strengthens the more specific `probable_transient_runtime_stall` flag. Task 16 is the fixture for this rule.

The GAM Task Scanner requests expanded Slurm array accounting records with `sacct --array`. It preserves both formatted array identities and raw physical Slurm IDs and searches a bounded multi-day accounting window. Allocation-only scanning remains lightweight, while the individual terminal array IDs become available for later bounded resource collection. Peak memory and CPU measurements still come from the separate Resource Collector's explicit job and `.batch` queries.

## What the core does not infer

- A single successful job cannot define a resource request.
- A timeout is not automatically interpreted as insufficient walltime.
- Low CPU efficiency does not automatically imply wasted CPUs.
- Similar memory use does not prove a node or filesystem fault.
- Resource evidence does not validate scientific output content.
- No learned profile grants execution authority.
