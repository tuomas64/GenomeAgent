# Data, Resource and Knowledge Management Plan (DRKMP)

**GenomeAgent**

**Status:** Living document

**Repository:** https://github.com/tuomas64/GenomeAgent

**Current Version:** 1.4 (Draft)

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
| 1.4 | July 2026 | Added source-environment isolation, deterministic cross-environment resource decisions, explicit no-evidence outcomes and target-pilot safety requirements. |
| 1.3 | July 2026 | Added HPC resource observations, deterministic empirical resource profiles, retention and provenance requirements, and their controlled relationship to the future Execution Engine. |
| 1.2 | July 2026 | Added immutable task observations, deterministic operational state, provenance, observation-health gates and the future execution boundary. |
| 1.1 | July 2026 | Added the current *Fragaria vesca* pangenome and Illumina dataset scope, authoritative sample counts and subset relationships. |
| 1.0 | July 2026 | Initial Data and Knowledge Management Plan for GenomeAgent. |
