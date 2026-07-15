# Read-only Model Source Metadata Collector v1

The Model Source Metadata Collector resolves the public source identity needed by the
Pinned Model Acquisition Planning Core. It observes repository metadata without
downloading any repository file or model weight and converts immutable observations
into a separate review-only acquisition-specification proposal.

## Observation contract

The initial policy is registered under:

```text
config/ai/source_evidence/roihu_qwen3_coder.json
```

One collection performs exactly two unauthenticated HTTPS GET requests to the
allow-listed Hugging Face model-info endpoint:

1. Resolve the configured symbolic revision (`main`) to a 40-hex commit.
2. Query that immutable commit and confirm that the returned revision is unchanged.

Each request has a timeout and response-size bound. The returned repository inventory
is also file-count bounded. Redirects outside `https://huggingface.co` are rejected.
The collector uses the Python standard library and does not require a Hugging Face
token or client package.

## Collected evidence

The normalized evidence contains:

- repository and immutable resolved revision;
- public/private and gating state;
- sorted file paths and byte sizes;
- provider Git blob IDs, LFS SHA-256 values and Xet hashes when exposed;
- weight-file counts and provider-checksum coverage;
- license identifier and listed license files;
- a SHA-256 digest of the canonical normalized inventory;
- response-body digests, source configuration digests and safety controls.

The inventory digest identifies this metadata record. It is not a substitute for
hashing the acquired files. Missing file sizes block readiness. Missing provider LFS
SHA-256 values are preserved as a limitation because full content integrity is checked
only after a future approved download.

## Collect and ingest

From the repository root:

```bash
python3 scripts/model_source_metadata.py collect roihu_qwen3_coder
python3 scripts/model_source_metadata.py ingest roihu_qwen3_coder
```

Immutable observations are written under:

```text
workspace/model_source_evidence/<backend_id>/<timestamp>.json
workspace/model_source_evidence/<backend_id>/<timestamp>.md
```

Ingestion performs no network access. It replays all valid snapshots into:

```text
workspace/model_source_state/<backend_id>/
├── current_source_metadata.json
├── evidence_history.jsonl
├── acquisition_spec_proposal.json
├── report.md
└── provenance.json
```

Changing the registered backend or source-evidence policy makes prior evidence stale.
The acquisition specification is a proposal target rather than an observation source,
so researcher-approved edits do not falsely invalidate the original provider evidence.

## Review boundary

When the public repository is ungated, all file sizes are known, the observed license
matches the backend registry and a license file is listed, status becomes
`source_metadata_ready_for_researcher_review`. The proposal remains
`researcher_review_required` until a researcher reviews it.

The separate [Explicit Model Source and License Approval Core](model_source_approval.md)
can record that review for one exact evidence snapshot and make only the verified
source values authoritative. The collector itself remains read-only.

The component cannot:

- download repository files or model weights;
- accept or reject the license;
- edit the acquisition specification or backend registry;
- access Roihu, submit Slurm jobs or allocate a GPU;
- recursively scan an installed model or hash large files;
- activate inference, fine-tuning or training.

After review, a separate approved acquisition mechanism must download to same-filesystem
staging, verify the complete file set and SHA-256 of every regular file, publish
atomically, refresh backend evidence and pass a bounded GPU inference benchmark.

## Inference and training boundary

GenomeAgent can first use this model for inference after pinned acquisition, full local
integrity verification and the existing non-sensitive benchmark suite pass under a
reviewed Roihu GPU allocation. Fine-tuning is a later, separate workflow. It requires a
versioned training dataset, rights and sensitivity review, train/validation/test split,
contamination controls, resource evidence, checkpoint policy and comparison against the
unchanged base model. Model-source readiness alone never authorizes training.
