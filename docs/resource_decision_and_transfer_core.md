# Resource Decision and Transfer Core

The Resource Decision and Transfer Core converts learned resource profiles into explicit, non-executable decisions for a declared target computing environment. It is the planning boundary between the Resource Evidence and Learning Core and a future supervised Execution Engine.

The planner is deterministic. It does not use an AI model, connect through SSH, query Slurm, submit jobs or modify scheduler settings.

## Inputs

The planner reads only local artifacts:

- `workspace/task_resources/<task>/resource_profiles.json`
- `workspace/task_resources/<task>/resource_anomalies.json`
- `workspace/task_resources/<task>/provenance.json`
- `workspace/task_state/<task>/current_state.json`, when available
- registered environment descriptions under `config/environments/`

Resource policy v1.2 separates profiles by both `source_host` and `profile_key`. Evidence from Puhti and Roihu therefore cannot be merged merely because the task profile or Slurm job number is the same.

## Environment registry

The repository contains conservative environment declarations for `puhti` and `roihu`. They identify scheduler and host aliases but intentionally leave unknown partitions, CPUs and hardware capabilities unset. GenomeAgent must not infer resource capabilities from a cluster name.

An external profile can seed a target-environment pilot, but it cannot be directly applied. Target-environment evidence remains the preferred source whenever it exists.

## Build a plan

After rebuilding resource knowledge under policy v1.2:

```bash
python3 scripts/task_resources.py ingest scattered_joint_calling
```

Plan resources for Roihu:

```bash
python3 scripts/task_plan.py resources \
  scattered_joint_calling \
  --target-environment roihu
```

Tasks with more than one resource profile require explicit selection:

```bash
python3 scripts/task_plan.py resources \
  gam_deduplication \
  --profile-key gam_dedup_fast \
  --target-environment roihu
```

When several source environments contain the same profile and no target-environment profile exists, choose the source explicitly:

```bash
python3 scripts/task_plan.py resources \
  scattered_joint_calling \
  --profile-key scattered_genotypegvcfs_250kb \
  --source-environment puhti \
  --target-environment roihu
```

## Decisions

| Decision | Meaning |
|---|---|
| `target_environment_proposal_available` | A reviewable proposal is supported by evidence from the target environment. |
| `cross_environment_pilot_proposed` | Source-environment values may seed only a bounded target pilot; confidence is reduced to low. |
| `insufficient_successful_evidence` | Fewer than three comparable successful attempts exist. |
| `insufficient_transferable_evidence` | Successes exist, but task-specific workload diversity is inadequate. |
| `blocked_by_resource_anomalies` | A high-review anomaly blocks allocation planning. |
| `no_resource_knowledge` | No derived resource artifacts exist for the task. |
| `no_matching_task_profile` | Resource knowledge exists, but not for the requested profile. |
| `source_environment_unresolved` | A profile cannot be assigned to a registered source environment. |
| `ambiguous_source_resource_profiles` | More than one eligible source exists and GenomeAgent refuses to choose silently. |

Missing evidence is a first-class result. The planner writes an explicit withheld allocation with null values instead of inventing memory, time, CPU or partition settings.

Resource-decision policy v1.1 separates `evidence_status` from `allocation_proposal_available`. A profile can therefore report substantial measured knowledge while still withholding transferable allocation values. When successful evidence lacks workload diversity, `insufficient_transferable_evidence` is the primary decision; anomalies remain visible as secondary review requirements. High-review anomalies become the primary blocker only when an otherwise reviewable source proposal exists.

## Outputs

Canonical outputs are written under:

```text
workspace/task_plans/<task>/<target_environment>/<profile_key>/
```

| Artifact | Purpose |
|---|---|
| `resource_decision.json` | Decision, evidence status, allocation availability, confidence, observed statistics, anomalies and proposed or withheld allocation. |
| `execution_readiness.json` | Deterministic blockers, current task-state health and mandatory approval gates. |
| `report.md` | Human-readable decision and allocation summary. |
| `provenance.json` | SHA-256 digests of every available input, policy identity and safety declarations. |

Re-running the planner with unchanged inputs produces byte-identical canonical artifacts.

## Cross-environment boundary

A cross-environment proposal preserves only source-derived memory and walltime values that already passed the Resource Learning Core policy. Confidence is reduced to `low`, `target_validated` is false, and intended use is `bounded_cross_environment_pilot_only`.

CPU count and partition remain unset unless they have been explicitly registered for the target environment. An incomplete proposal cannot become an executable scheduler request.

After a Roihu pilot, its terminal Slurm attempt should be collected and ingested under the Roihu host. GenomeAgent then builds a separate Roihu profile and will prefer it for later Roihu decisions.

## Safety boundary

Every plan retains:

- `automatic_execution_allowed: false`
- `researcher_approval_required: true`
- `fresh_pre_execution_scan_required: true`
- explicit execution-readiness blockers
- provenance for resource, task-state and environment inputs

The current component cannot invoke `ssh`, `sacct`, `sbatch`, `scancel` or any future Execution Engine action.
