# Brain Knowledge Promotion and Workflow Transfer Core v2

Brain v2 connects GenomeAgent's read-only operational components to reusable,
environment-aware workflow knowledge. It does not replace Task Scanner, Task State,
Resource Evidence or Resource Decision artifacts. It records exactly which of their
facts are safe to promote and preserves the evidence needed to rebuild every claim.

## Knowledge model

Brain v2 separates four states that older project memory did not distinguish:

| State | Meaning | Automatic use |
|---|---|---|
| Observation | Immutable Task Scanner or resource evidence | Input only |
| Deterministic derived fact | State, resource profile, anomaly or decision rebuilt by policy | May be promoted |
| AI-derived candidate | Brain v1 or future model interpretation | Review queue only |
| Researcher-accepted knowledge | A future explicit promotion workflow | Authoritative after review |

Each promoted claim has a stable semantic `claim_id`, a content-sensitive
`claim_version_id`, subject, predicate, value, scope, confidence, status and one or
more source-artifact SHA-256 digests. When the same semantic claim changes, current
knowledge records the earlier claim versions it supersedes.

## Knowledge promotion

Run promotion after Task State and Resource Evidence ingestion, and after rebuilding
any relevant Resource Decision plan:

```bash
python3 scripts/brain.py ingest scattered_joint_calling
python3 scripts/brain.py ingest gam_deduplication
```

The command reads local artifacts only. It writes:

```text
workspace/brain_knowledge/<task>/
├── snapshots/<sha256>.json
├── current_knowledge.json
├── claims.jsonl
├── candidate_claims.jsonl
├── report.md
└── provenance.json
```

Snapshots are immutable and content-addressed. Replaying identical inputs does not
create another snapshot. Changed task state or resource knowledge creates a new
snapshot, while the current view records superseded claim versions.

If `workspace/project_knowledge.json` from Brain v1 exists, its AI-generated fields
are preserved in `candidate_claims.jsonl`. They are never silently inserted into
promoted knowledge.

## Versioned workflow templates

Repository templates under `config/workflows/` separate portable workflow logic
from environment-specific bindings. A template records:

- scientific and operational steps that should remain invariant;
- required software and target-environment bindings;
- workload parameters that must be supplied for a specific project;
- the output validation and atomic-publication contract;
- the empirical resource profile key used by Resource Decision;
- explicit approval and fresh-observation requirements.

The initial templates describe 250 kb scattered GenotypeGVCFs and GAM
duplicate-template removal. They are descriptive contracts, not executable scripts.

## Workflow transfer planning

After promoting current knowledge and building a target resource decision, evaluate
a workflow against a registered environment:

```bash
python3 scripts/brain.py plan-workflow scattered_joint_calling \
  --target-environment roihu
```

Outputs are written under:

```text
workspace/workflow_transfers/<workflow>/<target_environment>/
├── transfer_plan.json
├── compatibility_matrix.tsv
├── report.md
└── provenance.json
```

The compatibility matrix checks the scheduler, required software and required
environment bindings independently. The resource gate consumes the existing
Resource Decision artifact; Brain v2 does not recalculate or weaken that policy.

Possible planning outcomes include:

- `insufficient_environment_knowledge`: required target facts are unknown;
- `incompatible_target_environment`: a verified target fact conflicts with the
  workflow contract;
- `knowledge_promotion_required`: no current Brain v2 snapshot exists;
- `blocked_by_resource_decision`: environment checks pass, but resource evidence is
  missing, narrow, ambiguous or blocked;
- `workflow_transfer_proposal_available`: compatibility and resource gates pass for
  researcher review.

Even the final outcome is non-executable. It does not install software, create paths,
contact the cluster, submit work or approve a resource allocation.

## Current Puhti-to-Roihu interpretation

Puhti has verified records for the current workflow software and module setup.
Roihu is deliberately registered with empty capability records. Until Roihu software,
storage bindings, scheduler defaults and bounded pilot evidence are observed, Brain v2
will report missing target knowledge rather than infer compatibility from the cluster
name. Existing Puhti measurements remain useful as source evidence but cannot become
Roihu defaults without the Resource Decision pilot gate.

## Relationship to AI and the future Execution Engine

AI remains useful for proposing workflow interpretations, grouping scripts and
identifying questions. Such outputs enter the candidate layer. Deterministic facts can
be promoted automatically because the claim and its source digest are reconstructable.
Scientific interpretations and new defaults require researcher review.

A future Execution Engine may consume an accepted workflow-transfer proposal, but must
still require a fresh Task Scanner observation, complete target bindings, an allow-listed
action, explicit researcher approval and post-action verification.
