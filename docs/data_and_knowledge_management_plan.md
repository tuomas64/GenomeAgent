# Data, Resource and Knowledge Management Plan (DRKMP)

**GenomeAgent**

**Status:** Living document

**Repository:** https://github.com/tuomas64/GenomeAgent

**Current Version:** 1.15 (Draft)

**Last Updated:** July 2026

---

## Introduction

GenomeAgent is an AI-assisted software robot designed to support computational genomics research on HPC systems. Unlike a traditional Data Management Plan, this document manages scientific data, computational-resource evidence and accumulated workflow knowledge. It is maintained in the GenomeAgent GitHub repository and evolves together with the software.

## Vision

GenomeAgent automates repetitive computational work while researchers remain responsible for scientific decisions. Its guiding philosophy is:

> **Automate repetition, not curiosity.**

## Guiding Principles

- Reproducibility first.
- Scientific decisions remain with the researcher.
- Understand before acting.
- Benchmark before scaling.
- Preserve scientific data, resource evidence and computational knowledge.
- Respect shared HPC resources.
- Base future resource requests on measured evidence.
- Explain every recommendation.

## Data Categories

### Irreplaceable Scientific Data

Examples include raw sequencing data, sample metadata, primary assemblies, final validated pangenome graphs, publication datasets, analysis scripts and project documentation.

Long-term preservation uses:

- CSC Allas object storage.
- At least one independently maintained external storage device.
- HPC working copies during active analyses.
- Public repositories after publication where appropriate.

GenomeAgent never recommends deletion of irreplaceable scientific data without explicit researcher approval.

### Regenerable Computational Products

Temporary and intermediate products that can be recreated from preserved inputs and validated workflows may be removed after GenomeAgent verifies that no active workflow depends on them.

### Derived Scientific Products

Final figures, tables, summary statistics and publication-ready datasets are preserved together with the workflows used to generate them.

### Operational Resource and Workflow Evidence

Scheduler records, task states, resource measurements, validation summaries and workflow events are operational research data. They are usually regenerable or reproducible for a limited period, but compact canonical observations are preserved because they explain computational cost, support reproducibility and allow GenomeAgent to improve future resource requests.

Large complete logs are not copied into GenomeAgent merely for long-term retention. Instead, the system records bounded evidence with provenance and retains links to the original Puhti job and log records while those records remain available.

## Current Scientific Data Scope

The principal reference project used to develop and validate GenomeAgent is the European *Fragaria vesca* pangenome project. GenomeAgent records biological samples separately from assemblies, haplotypes, sequencing files and analysis-specific subsets so that file counts are never mistaken for sample counts.

### Pangenome assembly data

| Dataset component | Biological material | Assembly sequences used in the graph | Notes |
|-------------------|---------------------|--------------------------------------|-------|
| Project PacBio HiFi data | 15 accessions sampled across Europe | 26 haplotype or primary assemblies | Eleven accessions have two haplotype assemblies and four currently have one primary assembly. |
| Published UK0 accession | 1 accession from Scotland | 2 haplotype assemblies | Included as an external assembly resource. |
| H4v6 reference | *Fragaria vesca* reference genome v6 | 1 reference assembly | Provides the reference paths and coordinate system. |
| **Final pangenome graph input** | **16 biological accessions + H4v6 reference** | **29 assembly sequences** | 26 project assemblies + 2 UK0 haplotypes + H4v6. |

### Illumina population data

| Dataset component | Samples | Role |
|-------------------|---------|------|
| Own European dataset | 233 | Full project-owned Illumina mapping cohort. |
| Swedish collaborator dataset | 225 | Full collaborator Illumina mapping cohort. |
| **Combined graph-mapped cohort** | **458** | Authoritative current cohort for graph mapping, surjection, GATK HaplotypeCaller and graph-based joint genotyping. |

The climatic-adaptation sampling subset is narrower than the full sequencing cohort. It contains 202 of the 233 own samples and 216 Swedish samples with strict exact identifier matches to the coordinate metadata, giving 418 eligible dataset records. The interactive project map displays 417 sample records because RUS6 lacks usable coordinates; these records occupy 412 distinct coordinate locations.

Some earlier linear-reference manifests contain 455 samples. GenomeAgent therefore stores sample counts with the exact dataset, manifest, workflow branch and validation date instead of treating every project-wide count as interchangeable. The current graph cohort is 458 samples: 233 own and 225 Swedish.

## Knowledge Management

GenomeAgent manages validated protocols, software versions, workflow relationships, benchmarking results, lessons learned, project conventions, successful solutions, rejected approaches and provenance.

For every major sequencing dataset, GenomeAgent should record at minimum the dataset name, biological sample count, assembly or file count where relevant, sequencing technology, data owner or source, authoritative manifest, storage locations, workflow scope, validation status and confidence. Subsets must retain an explicit relationship to their parent dataset and the rule used to select them.

### Operational Task State

GenomeAgent stores workflow observation, derived operational state and curated knowledge as separate layers:

- Timestamped Task Scanner bundles are immutable read-only observations with configuration and source provenance.
- The Task State Bridge deterministically rebuilds current state, lifecycle events, observation health and recommendations from those bundles.
- Researcher-validated protocols, scientific interpretations and reusable lessons are promoted separately into curated knowledge.

Derived task-state artifacts are replaceable because they can be reconstructed from retained scan bundles. Every current state and transition retains the source scan identifier and SHA-256 digest. A failed scheduler or manifest observation blocks action planning rather than being interpreted as an idle workflow.

Operational recommendations do not grant execution authority. Cluster submission, data deletion and other mutations require a fresh observation, an allow-listed action, explicit researcher approval, recorded provenance and post-action verification.

## Resource Data and Empirical Learning

GenomeAgent treats measured HPC resource use as evidence from which reusable task knowledge can be derived. Resource learning is deterministic and provenance-aware; it does not require an AI model.

### Resource observations

For each relevant Slurm job, array element or workflow unit, GenomeAgent should record where available:

- GenomeAgent task profile and profile version.
- Workflow stage, tool, reference, cohort and meaningful workload descriptors.
- Job, array-task and job-step identifiers.
- Requested partition, CPUs, memory and wall time.
- Allocated resources, elapsed time, CPU time, peak resident memory and exit state.
- Completion, timeout, out-of-memory, cancellation and application-failure outcomes.
- Scan time, source command or interface, and the originating task-scan bundle.

Array elements are observations in their own right. Parent job records and job-step records must remain distinguishable because peak memory may be reported for a `.batch` or other job step rather than for the parent record. Missing measurements are stored as unknown, never interpreted as zero.

Resource collection must remain bounded and respectful of shared scheduler services. GenomeAgent should query relevant jobs in batches, avoid continuous polling, cache terminal observations and avoid rereading complete scientific outputs solely to estimate resource use.

### Learned resource profiles

GenomeAgent derives versioned resource profiles only from comparable observations. Comparability may depend on the task profile, workflow version, software, reference, cohort size, interval or shard properties and other workload characteristics. Equal genomic interval lengths are not assumed to imply equal computational complexity.

Each learned profile should report its observation count, completion and failure outcomes, typical and high-percentile memory and elapsed time, CPU efficiency where interpretable, safety margin, confidence and provenance. Timeouts and out-of-memory failures provide lower-bound evidence; cancellations and unrelated application failures are not automatically treated as resource insufficiency.

Recommendations are conservative and deterministic. A single unusual job must not redefine a task profile, and GenomeAgent must not infer that additional CPUs will accelerate software unless the tool and workflow can use them. The exact rule and evidence behind every recommendation must be inspectable.

### Storage and promotion of resource knowledge

Individual observations should be kept as append-only canonical records from which derived profiles can be rebuilt. Current state, event history, resource observations, derived profiles and recommendations remain separate artifacts. Generated project state is stored outside version control by default; validated and sanitised summaries may be promoted into repository documentation or configuration when useful.

Raw scheduler output and logs can contain usernames, project accounts and storage paths. They must be reviewed before public release. Compact non-sensitive resource profiles should be retained for at least the lifetime of the associated workflow and preferably alongside the workflow version they describe.

### Relationship to the Execution Engine

The future Execution Engine may consume a learned resource profile when preparing an execution proposal. Learned values remain recommendations until explicitly accepted or incorporated into an authoritative task configuration. Resource learning must never silently alter a running job, resubmit a failed job or change an approved scientific workflow.

### Cross-environment resource decisions

Resource observations and derived profiles retain their source computing environment. Profiles with the same scientific task key are not merged across Puhti, Roihu or another environment unless an explicit future harmonisation policy has been validated. Numeric scheduler job identifiers are not assumed to be globally unique.

When the target environment lacks local evidence, GenomeAgent may use a sufficiently supported external profile only as low-confidence pilot guidance. The decision must identify the source and target environments, preserve the original confidence and evidence counts, mark the allocation as not target-validated, disclose missing scheduler fields and require a bounded target-environment pilot. If no comparable profile exists, GenomeAgent records an explicit no-evidence decision and does not invent resource values.

The Resource Decision and Transfer Core writes resource decisions, execution-readiness gates and provenance as separate local artifacts. It cannot submit jobs. A future Execution Engine must require a fresh task observation, explicit researcher approval, a complete target scheduler allocation and post-action verification before consuming any accepted plan.

## Knowledge Promotion and Workflow Transfer

Brain v2 promotes only structured facts produced by deterministic GenomeAgent components. Each promoted claim records a stable semantic identifier, a content-sensitive claim-version identifier, scope, confidence, status and source-artifact SHA-256 digests. Content-addressed knowledge snapshots are immutable; current knowledge and supersession relationships can be rebuilt from those snapshots.

AI-derived project interpretations are candidate knowledge, not authoritative facts. Brain v1 output and future model-generated suggestions are retained in a separate review queue until a researcher explicitly accepts, edits or rejects them. Deterministic promotion does not by itself make a scientific interpretation or resource default authoritative.

Versioned workflow templates separate portable workflow logic and validation contracts from environment-specific software, storage paths, modules, scheduler defaults and resource evidence. Workflow transfer planning checks these requirements independently and records unknown or incompatible target facts rather than guessing from an environment name. A source-environment resource profile can support a bounded target pilot only through the Resource Decision policy; it is never silently converted into a new-environment default.

Brain knowledge and workflow-transfer artifacts remain non-executable. A proposal that passes all current knowledge gates still requires a fresh observation, complete target bindings, an allow-listed future Execution Engine action, explicit researcher approval and post-action verification.

## AI Backend and Evaluation Evidence

GenomeAgent records AI backends as versioned computational dependencies rather than interchangeable sources of truth. A backend record identifies its computing environment, inference runtime, model repository, pinned revision, verified local model inventory digest, installation status, intended resource request, allowed data classes and execution policy. Unknown model identity, installation or compatibility remains explicit and blocks readiness.

Prompts and benchmark suites are versioned independently from model backends. Benchmark cases contain non-sensitive fixtures, expected structured facts, missing-evidence requirements, safe recommended actions and forbidden claims. Initial evaluation data must not contain credentials, SSH keys, raw genomic data, sensitive sample metadata or unpublished records that are unnecessary for the reasoning task.

Prepared AI run packages and evaluations are content-addressed, immutable derived evidence. Their provenance records source paths and SHA-256 digests. Model responses remain evidence from an untrusted computational component: they are schema-validated and scored deterministically, but are not automatically executed or promoted into Brain knowledge. A benchmark pass means only that the backend is suitable for researcher review under that exact model, prompt, suite and environment evidence.

Local and external AI backends require separate registry entries, data policies and evaluations. GenomeAgent must never send project data to an external provider through silent fallback. Any future fallback requires explicit researcher choice, an allowed data classification and recorded provenance. Local inference likewise requires a pinned model revision, verified model artifacts, a bounded resource request and explicit approval before job submission.

### AI backend environment observations

Backend configuration and observed backend state remain separate. The read-only AI Backend Evidence Collector records bounded login-host identity, architecture, exact runtime-module metadata, Slurm partition metadata, storage availability and shallow model-path inventory. It does not allocate a GPU, test inference, download or import a model, recursively enumerate model storage or hash large weight files.

Each immutable observation retains the backend and collection-policy digests used at collection time. Current readiness is a deterministic, replaceable view rebuilt from those snapshots. Changed configuration makes older evidence stale; it is never silently reinterpreted as current. Observed environment compatibility, verified model identity and integrity, benchmark performance and execution authority are independent gates.

Backend evidence may contain cluster hostnames, project paths, module names, quota summaries and model filenames. It is retained locally under the ignored `workspace/` tree and must be reviewed before public release. Credentials, private keys, SSH certificates and access tokens are never collection targets and must not be stored in backend configuration or evidence.

Commands used as authoritative capacity evidence are versioned environment bindings.
When a command is absent from a non-login observation `PATH`, GenomeAgent may execute
only an explicit validated absolute path from the evidence policy. It must not perform
an unbounded filesystem search or silently substitute generic filesystem availability
for project quota. Missing, non-approved or unsuccessful quota commands remain
blocking unknowns.

### Pinned model acquisition planning

Model acquisition is planned separately from model download, installation, registry update and activation. A versioned acquisition specification identifies the intended provider repository, target environment and installation path, representation, storage policy, integrity contract and approval boundary. Unresolved source revision, provider inventory, inventory size or license review remains explicit and blocks acquisition readiness.

Acquisition plans are content-addressed immutable artifacts derived from the backend registry, acquisition specification and current read-only environment evidence. A provisional parameter-byte estimate is labelled as a theoretical lower bound and cannot replace a complete provider file inventory. Working-space requirements are calculated only after source inventory size is known, using the recorded number of complete acquisition copies and headroom policy.

The integrity contract requires an immutable source revision, complete recursive file
inventory, comparison of every available provider content SHA-256, locally computed
SHA-256 evidence for every regular file, rejection of symlinks, same-filesystem staging
and atomic final-directory publication. Git blob IDs are provenance identifiers and
are not represented as raw-file SHA-256 values. Large-file hashing, download and
publication are separately approved execution steps; the planner performs none of
them. Backend registry identity, verified local model inventory and benchmark status
are updated only through separate evidence and researcher-review gates.

### Controlled model acquisition preparation

Acquisition-preparation approval is bound to one exact current plan, all plan-artifact
digests, the approved source-evidence digest and the researcher identifier. Its scope
is limited to creating a deterministic data contract. The resulting bundle records
the approved inventory, integrity requirements, same-filesystem staging layout and
remaining execution blockers, but contains no worker program, credentials, provider
token, remote command or scheduler submission.

Remote runtime compatibility, transfer execution context, current target state and a
fresh execution authorization remain independent gates. Approval to prepare a bundle
must never be interpreted as authority to download, hash, publish, activate, infer or
train. Acquisition observations, approvals, bundles, later execution evidence and
installed-model evidence are retained as separate provenance layers.

### Acquisition runtime preflight evidence

Immediately before model-acquisition authorization, GenomeAgent records a bounded
read-only observation tied to one exact acquisition bundle. It verifies the registered
runtime module, vLLM version, availability of the Hugging Face snapshot API, future
GPU partition and accelerator identity, project quota, target and staging absence,
path-component symlink safety and same-filesystem publication feasibility. Importing
the transfer API is not a provider request and does not download data.

The registered transfer context permits only a future inbound copy of the approved
public model at its immutable revision. It does not permit project-data egress,
credential collection or arbitrary repository access. Preflight evidence expires
after 30 minutes and configuration or bundle changes make it stale. Even a fully
verified preflight retains `fresh_execution_authorization_missing`; observation never
becomes execution authority.

### Controlled public model staging download

A model download requires a new content-addressed researcher authorization bound to
the exact acquisition bundle, every current input-artifact digest and one unexpired
preflight observation. The authorization expires after at most ten minutes and permits
only the approved public repository revision to enter the bundle's hidden staging
directory. Reusing a different host, revision, bundle, policy or evidence digest is
rejected.

The launcher creates only confined control metadata, logs, missing model-parent
directories and the exact staging directory. It removes model-provider token variables,
passes `token=False`, limits transfer concurrency to two workers and records the
background process. It cannot read project data for transfer, delete remote data, hash
model files, publish the final directory, submit jobs, allocate GPUs, infer, train,
update the registry or activate a backend.

A successful transfer is classified as `download_completed_unverified`. Download
completion is not installation: exact path and size validation, full local SHA-256,
provider LFS digest comparison, manifest construction and atomic publication remain a
separate compute-stage review and authorization boundary.

### Staged model integrity verification

Before publication, GenomeAgent records a bounded metadata-only inventory from the
Roihu-CPU environment and compares exact relative paths, sizes, file counts and byte
totals with the approved source inventory. Hugging Face `.cache` transfer metadata is
classified separately, must be symlink-free and is excluded from the trusted model
manifest. Any unexpected model file, missing file, size mismatch, symlink, special
entry, target-path conflict or unverified scheduler context blocks hashing.

Hashing requires a new content-addressed researcher authorization bound to the direct
unexpired inventory snapshot, derived state, acquisition bundle, completed download
evidence, policy and requested Slurm resources. The authorized job uses Roihu's serial
`small` CPU partition with one CPU, 4 GiB memory and a two-hour limit. It reads only
approved staged regular files, computes local SHA-256 for all of them, compares every
available provider LFS SHA-256 and detects files that change during reading.

The job writes status, results and a manifest candidate only below its confined
verification control directory. It cannot alter staging, remove transfer metadata,
publish or rename the model, allocate a GPU, perform inference or activate the backend.
Successful hashing ends at `verified_ready_for_publication_review`; publication
requires a separate fresh observation and explicit authorization.

### Public model source metadata

Public model-source metadata is recorded as immutable evidence separately from the
backend registry, acquisition specification and downloaded model files. A collection
records the requested symbolic revision, immutable resolved commit, complete provider
path and size inventory, available Git blob or LFS identifiers, license metadata,
provider response digests, collection limits and configuration provenance.

The canonical source-inventory digest identifies the normalized metadata inventory; it
is not presented as a digest of the downloaded model contents. Provider-supplied LFS
SHA-256 values are retained when available, while missing provider checksums remain an
explicit limitation. A later approved acquisition process must inventory and compute
SHA-256 for every downloaded regular file before atomic publication and activation.

Source observation cannot accept a model license or silently edit an acquisition
specification. It produces a proposal containing observed values and a direct license
review target. A researcher must review the immutable revision, inventory, capacity
implications and license before those values become authoritative configuration.

Explicit model-source approval is a narrow, auditable configuration mutation. The
approval record binds the researcher identifier, acceptance timestamp, license URL and
identifier to an exact source-evidence ID, evidence SHA-256, immutable revision,
inventory digest and source size. The versioned acquisition specification retains this
provenance; a bare `reviewed_accepted` status without the structured record is invalid.

Approval authorizes only the recorded acquisition source identity and license state.
It does not grant model download, provider authentication, remote writes, Slurm
submission, GPU allocation, registry update, backend activation, inference or training
authority. Repeating the same approval is idempotent, while changed configuration,
newer evidence or modified approval artifacts block application.

## Responsible HPC Usage

GenomeAgent benchmarks new workflows before scaling, monitors representative and exceptional analyses, compares requested resources with measured use, estimates future requirements from comparable jobs, removes validated temporary files, removes unnecessary empty directories after verification, and minimises unnecessary storage and scheduler load.

## Relationship-aware Data Management

Maintenance decisions are based on scientific value, reproducibility, workflow dependencies, provenance and project status rather than file age alone.

## Learning

GenomeAgent continuously learns from validated workflows, official documentation, benchmarking, measured resource use and researcher feedback. Raw observations, deterministic derived profiles, accepted recommendations and authoritative configuration remain separate so that every change can be reconstructed and reviewed.

## AI Governance

GenomeAgent may automate validated computational tasks, but it never changes scientific hypotheses, replaces researcher judgement, deletes irreplaceable scientific data or applies learned resource settings without the required approval.

## Future Development

This Data, Resource and Knowledge Management Plan is a living document maintained in the GenomeAgent GitHub repository and updated as GenomeAgent evolves.

## Revision History

| Version | Date | Description |
|----------|------|-------------|
| 1.15 | July 2026 | Added exact staged inventory evidence and explicitly authorized serial SHA-256 verification with provider-LFS comparison and a separate publication gate. |
| 1.14 | July 2026 | Added expiring exact-input download authorization, confined public inbound staging transfer and read-only download status evidence. |
| 1.13 | July 2026 | Added time-bounded, bundle-bound acquisition runtime, transfer-context, quota and remote target preflight evidence. |
| 1.12 | July 2026 | Added exact-plan acquisition-preparation approvals, data-only execution bundles, truthful provider-versus-local digest semantics and independent runtime, target-state and fresh-execution gates. |
| 1.11 | July 2026 | Added explicit absolute environment bindings for authoritative project-quota commands, bounded direct execution and rejection of generic filesystem availability as a quota substitute. |
| 1.10 | July 2026 | Added exact-evidence model-source and license approval, structured reviewer provenance, narrow configuration mutation, idempotency and rejection of unproven accepted-license states. |
| 1.9 | July 2026 | Added bounded public model-source metadata observations, immutable revision confirmation, canonical inventory evidence, provider-checksum limitations and review-only acquisition-specification proposals. |
| 1.8 | July 2026 | Added content-addressed pinned-model acquisition planning, source identity and license gates, transparent storage lower bounds, full-file integrity contracts and atomic publication requirements. |
| 1.7 | July 2026 | Added bounded read-only AI backend environment observations, shallow model inventory policy, stale-evidence handling, local retention controls and independent environment/model/benchmark gates. |
| 1.6 | July 2026 | Added versioned AI backend, prompt and benchmark records; immutable evaluation evidence; model identity requirements; data-classification controls; and explicit local-versus-external fallback governance. |
| 1.5 | July 2026 | Added deterministic Brain v2 knowledge promotion, immutable claim snapshots, AI-candidate isolation and environment-aware workflow transfer gates. |
| 1.4 | July 2026 | Added source-environment isolation, deterministic cross-environment resource decisions, explicit no-evidence outcomes and target-pilot safety requirements. |
| 1.3 | July 2026 | Added HPC resource observations, deterministic empirical resource profiles, retention and provenance requirements, and their controlled relationship to the future Execution Engine. |
| 1.2 | July 2026 | Added immutable task observations, deterministic operational state, provenance, observation-health gates and the future execution boundary. |
| 1.1 | July 2026 | Added the current *Fragaria vesca* pangenome and Illumina dataset scope, authoritative sample counts and subset relationships. |
| 1.0 | July 2026 | Initial Data and Knowledge Management Plan for GenomeAgent. |
