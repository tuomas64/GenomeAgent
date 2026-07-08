# Data and Knowledge Management Plan (DKMP)

**GenomeAgent**

**Status:** Living document

**Repository:** https://github.com/tuomas64/GenomeAgent

**Current Version:** 1.0 (Draft)

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

## Knowledge Management

GenomeAgent manages validated protocols, software versions, workflow relationships, benchmarking results, lessons learned, project conventions, successful solutions, rejected approaches and provenance.

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
| 1.0 | July 2026 | Initial Data and Knowledge Management Plan for GenomeAgent. |
