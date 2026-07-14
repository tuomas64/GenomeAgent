# Task State and Knowledge Bridge

## Purpose

The Task State Bridge converts immutable Task Scanner observations into durable, replayable operational knowledge. It connects GenomeAgent's read-only observation layer to future reasoning and execution components without granting permission to modify Puhti.

The bridge answers four questions:

1. What is the latest trustworthy state of this workflow?
2. What changed between scans?
3. Which source observation supports each conclusion?
4. What can be recommended safely, while automatic execution remains disabled?

## Usage

Run a Task Scanner first, then ingest all available bundles for that task:

```bash
python3 scripts/task_scan.py scattered_joint_calling
python3 scripts/task_state.py ingest scattered_joint_calling
```

The GAM duplicate-removal workflow uses the same bridge:

```bash
python3 scripts/task_state.py ingest gam_deduplication
```

Alternative scan and state roots can be supplied for testing or another workspace:

```bash
python3 scripts/task_state.py ingest scattered_joint_calling \
  --scan-root workspace/task_scans \
  --state-root workspace/task_state
```

## Artifacts

The bridge writes four local artifacts under `workspace/task_state/<task>/`:

- `current_state.json`: canonical latest task, unit, group, health and safety-gate state.
- `events.jsonl`: deterministic lifecycle transitions replayed from the complete scan history.
- `recommendations.json`: read-only recommendations with explicit evidence and approval boundaries.
- `provenance.json`: ordered source scans, hashes, configuration hashes and observation health.

Re-ingesting unchanged source history produces byte-identical artifacts. The bridge rebuilds state from immutable scan bundles instead of incrementally editing opaque memory, so the result can be audited or reproduced after local state loss.

After first ingestion, the provenance ledger also makes scan history append-only: changing or removing a previously ingested `task_scan.json` causes ingestion to stop rather than silently rewriting history.

## Observation health and safety gate

The bridge verifies that every source bundle was produced in read-only mode. For action planning, the latest scan must also have successful `squeue` and `sacct` observations and task-specific integrity evidence.

For scattered joint calling, critical checks include:

- interval-manifest integrity;
- output-directory observation; and
- scheduler observation.

For GAM duplicate removal, checks include:

- dataset-directory observation;
- sample-manifest integrity; and
- scheduler observation.

If a critical check fails, `evidence_sufficient_for_action_planning` is false and the only recommendation is to repair observation. This prevents a missing scheduler command from being mistaken for an idle workflow.

## Lifecycle history

The first valid scan creates a compact baseline event. Later scans may produce:

- overall-status changes;
- observation-health changes;
- unit-state changes;
- newly discovered or missing units;
- batch or dataset status changes; and
- numeric progress-count changes.

Unit transitions retain relevant scheduler IDs, scheduler states, log paths and output paths. Every event contains the source scan ID and SHA-256 digest.

## Relationship to curated memory

Operational state and curated scientific knowledge have different trust models:

- Task Scanner bundles are immutable observations.
- Task State artifacts are deterministic, replaceable derivations.
- Validated protocols, project conventions and researcher-confirmed lessons belong in GenomeAgent's curated knowledge database.

For example, a timeout is first recorded as an observed task-state transition. After a researcher confirms that an interval is biologically or computationally exceptional and validates a recovery procedure, that conclusion can be promoted separately into curated workflow knowledge.

## Execution boundary

The bridge never connects to Puhti, submits jobs, deletes files or executes recommendations. Every recommendation records `execution_enabled: false`.

A future Execution Engine must additionally require:

- a fresh pre-execution scan;
- healthy scheduler and manifest observations;
- an allow-listed, idempotent action profile;
- researcher approval;
- a complete command and provenance record; and
- a post-execution rescan.

The state bridge provides evidence to that future engine; it does not broaden GenomeAgent's authority.
