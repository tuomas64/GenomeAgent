# Pinned Model Acquisition Planning Core v1

The Pinned Model Acquisition Planning Core turns the registered AI backend, a
versioned acquisition specification and saved environment evidence into an immutable,
review-only acquisition plan. It deliberately does not resolve model metadata or
perform acquisition itself.

## Current boundary

Planning is entirely local. It does not:

- contact Hugging Face or Roihu;
- download, import or publish model files;
- recursively scan or hash an installed model;
- submit a Slurm job or allocate a GPU;
- edit the backend registry;
- activate an AI backend.

Every generated readiness artifact explicitly disables those actions. Researcher
approval and fresh environment evidence remain required even when all planning gates
pass.

## Versioned acquisition specification

Specifications live under:

```text
config/ai/acquisition/<backend_id>.json
```

The initial `roihu_qwen3_coder` specification records the intended repository,
installation path, BF16 representation, storage policy, integrity contract and safety
boundary. Its source revision, inventory digest and inventory size are intentionally
null, and its license is unreviewed. GenomeAgent therefore reports
`model_identity_resolution_required` instead of inventing those values.

An acquisition source is sufficiently pinned only when it records:

- an immutable 40-hex repository commit;
- a canonical digest of the complete source inventory;
- the inventory's total byte size;
- a reviewed and accepted license identifier matching the backend registry.

An accepted license also requires structured provenance tying the reviewer, timestamp,
review URL and immutable revision to one exact source-evidence SHA-256. Setting only
`license_review_status` is rejected.

The planner's `source_metadata_request.json` describes the evidence the separate
[Read-only Model Source Metadata Collector](model_source_metadata_collector.md)
must obtain. It is a data request, not an executable provider command. Public provider
metadata supplies the immutable revision, complete path/size inventory, Git blob IDs
and available LFS SHA-256 values. A later approved acquisition step must still compute
and verify SHA-256 for every downloaded regular file before publication.

## Storage reasoning

Before a provider inventory exists, the planner derives only a theoretical lower bound:

```text
registered total parameters × bytes per parameter
```

For the registered 30.5-billion-parameter BF16 candidate, this is 61,000,000,000
bytes. It excludes configuration, tokenizers, cache, filesystem metadata, temporary
publication and other repository files, so it cannot authorize acquisition.

Once `source_total_bytes` is verified, required working free space is calculated from
the versioned policy:

```text
source bytes × (complete acquisition copies + headroom fraction)
```

The current conservative policy represents a staging copy plus a published copy and
10% headroom. The result is compared only with parsed `csc-workspaces` evidence for the
configured project quota. Generic filesystem free space is retained as informational
evidence because it may not reflect the project's allocation. A future executor must
repeat the quota check immediately before acquisition.

## Integrity and publication contract

The generated integrity plan requires:

- downloaded paths and sizes to equal the approved source inventory;
- every available provider Git LFS SHA-256 to match downloaded content;
- a locally computed SHA-256 for every regular file, including large weights;
- recorded paths, sizes, digests, source revision, runtime and environment identity;
- no symlink resolving outside approved model storage;
- staging on the destination filesystem;
- atomic publication by final-directory rename;
- fresh read-only evidence after publication;
- a separate reviewed registry update and bounded GPU benchmark.

Large-file hashing is required before activation but is not performed by this planner.
Git blob IDs are retained as provenance but are not misrepresented as raw-file
SHA-256 values. Plan policy v1.1 makes this provider-digest boundary explicit.

## Build a plan

First collect and ingest current backend evidence, then run:

```bash
python3 scripts/model_acquisition.py plan roihu_qwen3_coder
```

When the plan requests source identity resolution, collect and ingest bounded public
metadata separately:

```bash
python3 scripts/model_source_metadata.py collect roihu_qwen3_coder
python3 scripts/model_source_metadata.py ingest roihu_qwen3_coder
```

This produces a review-only proposal. It never edits the acquisition specification.

Artifacts are stored under a content-addressed directory:

```text
workspace/model_acquisition_plans/<backend_id>/<plan_id>/
├── model_acquisition_plan.json
├── source_metadata_request.json
├── integrity_plan.json
├── execution_readiness.json
├── report.md
└── provenance.json
```

The plan ID includes the digests of the backend configuration, acquisition
specification and saved environment state. Changing any input creates a new plan; an
existing plan is never silently overwritten.

## Readiness states

| Status | Meaning | Next safe action |
|---|---|---|
| `environment_evidence_required` | Current compatible backend-environment evidence is missing, stale or blocked | Collect and ingest fresh read-only evidence |
| `model_identity_resolution_required` | Revision, source inventory, size or license review is incomplete | Resolve and review pinned source metadata |
| `storage_preflight_required` | Source identity is complete but capacity is absent or insufficient | Review capacity and acquisition layout |
| `ready_for_researcher_acquisition_review` | Deterministic planning gates pass | Researcher reviews a still non-executable acquisition plan |

Readiness never grants download, scheduler, publication, registry-update or activation
authority.
