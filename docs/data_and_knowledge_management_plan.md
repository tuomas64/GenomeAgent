# Data and Knowledge Management Plan (DKMP)

**GenomeAgent**

**Status:** Living document

**Repository:** https://github.com/tuomas64/GenomeAgent

**Current Version:** 1.2 (Draft)

**Last Updated:** July 2026

---

## Introduction

GenomeAgent is an AI-assisted software robot designed to support computational genomics research on HPC systems. Unlike a traditional Data Management Plan, this document manages both scientific data and accumulated computational knowledge. It is maintained in the GenomeAgent GitHub repository and evolves together with the software.

## Vision

GenomeAgent automates repetitive computational work while researchers remain responsible for scientific decisions. Its guiding philosophy is:

> **Automate repetition, not curiosity.**

## Guiding Principles

- Reproducibility first.
- Scientific decisions remain with the researcher.
- Understand before acting.
- Benchmark before scaling.
- Preserve both data and knowledge.
- Respect shared HPC resources.
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

## Responsible HPC Usage

GenomeAgent benchmarks new workflows before scaling, monitors early analyses, estimates resource requirements, removes validated temporary files, removes unnecessary empty directories after verification, and minimises unnecessary storage consumption.

## Relationship-aware Data Management

Maintenance decisions are based on scientific value, reproducibility, workflow dependencies, provenance and project status rather than file age alone.

## Learning

GenomeAgent continuously learns from validated workflows, official documentation, benchmarking and researcher feedback while keeping validated knowledge separate from experimental observations.

## AI Governance

GenomeAgent may automate validated computational tasks, but it never changes scientific hypotheses, replaces researcher judgement or deletes irreplaceable scientific data automatically.

## Future Development

This Data and Knowledge Management Plan is a living document maintained in the GenomeAgent GitHub repository and updated as GenomeAgent evolves.

## Revision History

| Version | Date | Description |
|----------|------|-------------|
| 1.2 | July 2026 | Added immutable task observations, deterministic operational state, provenance, observation-health gates and the future execution boundary. |
| 1.1 | July 2026 | Added the current *Fragaria vesca* pangenome and Illumina dataset scope, authoritative sample counts and subset relationships. |
| 1.0 | July 2026 | Initial Data and Knowledge Management Plan for GenomeAgent. |
