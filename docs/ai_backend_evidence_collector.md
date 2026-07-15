# Read-only AI Backend Evidence Collector v1

The AI Backend Evidence Collector observes whether a registered inference backend's
computing environment actually matches its configuration. It is separate from both
benchmark preparation and model execution. Collection uses one bounded SSH session;
ingestion then replays saved evidence locally without contacting the cluster.

## What it observes

The initial Roihu-GPU policy permits these checks:

- login-host identity, architecture and system Python version;
- existence of an approved module-initialization script;
- exact vLLM module availability and installed package metadata;
- presence of the Slurm client and metadata for the configured `gputest` partition;
- GPU type advertised by Slurm partition metadata;
- existence and filesystem metadata of the configured project storage root;
- bounded `csc-workspaces` output from an explicit approved executable path;
- existence and at most 200 top-level entries of the configured model directory;
- small `config.json`, Git revision and GenomeAgent model-manifest metadata when
  present.

The collector does not prove that a GPU program can run. That requires a later
researcher-approved pilot. Slurm partition metadata is recorded as scheduler evidence,
not runtime validation.

## Safety policy

The versioned policy is stored under:

```text
config/ai/evidence/<backend_id>.json
```

It must explicitly disable remote writes, job submission, GPU allocation, model
download, model import, recursive model scans and hashing of large model files. Policy
validation fails closed if any of those fields is missing or enabled.

The remote observation uses the system Python named by the policy. Module
initialization is also explicit policy. On Roihu, the collector sources the verified
Lmod initialization script `/usr/share/lmod/lmod/init/bash` and adds only the
allow-listed module tree `/appl/modulefiles/manual/aida/aarch64`. It uses an ordinary
non-interactive shell and does not source the user's login or interactive shell startup
files. The collector loads the exact module only into a temporary subprocess
environment and reads package metadata without importing vLLM.

Roihu's non-login Python environment does not expose `csc-workspaces` through `PATH`.
Evidence policy v1.1 therefore records the observed absolute CSC software path
`/appl/soft/manual/general/aarch64/csc-tools/bin/csc-workspaces`. The probe checks
ordinary `PATH` only for an exact match to an approved candidate, otherwise validates
and executes that configured path directly. It performs no recursive command search
and does not start a login or interactive shell. A missing, non-executable, timed-out
or non-approved quota command is an environment blocker rather than permission to use
generic filesystem free space as project quota.

Evidence policy v1.2 recognizes a separately approved installed-model registration.
The collector accepts inventory identity only when the observed small-manifest
SHA-256 exactly matches the registered installed-manifest digest, the installation is
`verified_present`, and the model digest has explicit
`verified_model_candidate_manifest_sha256` semantics matching the registered verified
inventory. Manifest presence alone is never treated as integrity evidence, and weight
files are not rehashed by this collector.

## SSH prerequisite

The registered backend uses the `roihu-gpu` SSH alias. Configure that alias in the
user's SSH configuration so it targets `roihu-gpu.csc.fi` with the user's Roihu key and
short-lived certificate. Keys and certificates remain outside the GenomeAgent
repository. A different already configured alias can be supplied with `--host`.

Verify connectivity independently before collection:

```bash
ssh roihu-gpu hostname
```

This connection test is not performed by ingestion.

## Collect one immutable snapshot

From the repository root:

```bash
python3 scripts/ai_backend_evidence.py collect roihu_qwen3_coder
```

If another alias is required:

```bash
python3 scripts/ai_backend_evidence.py collect roihu_qwen3_coder \
  --host roihu-gpu.csc.fi
```

The command writes one JSON snapshot and one Markdown report:

```text
workspace/ai_backend_evidence/<backend_id>/
├── <evidence_id>.json
└── <evidence_id>.md
```

Snapshots include the SHA-256 digests of the backend record and evidence policy used
for collection. An existing evidence ID is never overwritten.

## Rebuild current evidence

```bash
python3 scripts/ai_backend_evidence.py ingest roihu_qwen3_coder
```

Ingestion has no remote access. It validates and replays every immutable snapshot into:

```text
workspace/ai_backend_state/<backend_id>/
├── current_evidence.json
├── evidence_history.jsonl
├── readiness.json
├── report.md
└── provenance.json
```

If the backend or collection policy has changed since the latest observation, the
state is marked stale rather than reinterpreted as current evidence.

## Readiness states

| Status | Meaning | Next safe action |
|---|---|---|
| `environment_evidence_incomplete` | One or more architecture, module, Slurm, GPU or storage checks are unknown or mismatched | Review failed checks |
| `environment_evidence_stale` | Registry or policy changed after observation | Collect fresh evidence |
| `model_identity_and_acquisition_required` | Environment checks pass, but the pinned model identity or installation is missing | Review a pinned acquisition plan |
| `model_integrity_evidence_required` | Model is present but verified inventory or digest evidence is incomplete | Verify model inventory |
| `ready_for_bounded_benchmark_review` | Environment and model evidence pass; inference remains unbenchmarked | Review a bounded GPU pilot |
| `benchmark_evidence_available_for_review` | Registered benchmark evidence is present | Researcher reviews backend candidate |

No state grants execution authority. The collector and reducer never edit the backend
registry, download a model, submit work, activate a backend or promote information into
Brain knowledge.
