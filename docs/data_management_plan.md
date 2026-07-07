# Data Management Plan

This document is a living data management plan for GenomeAgent-assisted computational genomics projects.

GenomeAgent manages data according to scientific value, reproducibility and dependency relationships, not file age alone.

## Permanent data

- raw sequencing data
- genome assemblies
- final pangenome graphs
- final VCFs and structural variant datasets
- scripts
- final figures and tables

## Regenerable intermediate data

- BUSCO working directories after summaries are stored
- EDTA temporary files
- temporary mapping files
- temporary indexes
- superseded graph versions after validation

## Responsible HPC citizenship

GenomeAgent supports responsible use of shared HPC infrastructure by reducing unnecessary storage use, avoiding large untested job submissions, and encouraging small benchmark runs before scaling.
