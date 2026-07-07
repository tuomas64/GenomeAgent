# vg giraffe short-read mapping

Status: validated draft

## Purpose

Map Illumina short reads to a pangenome graph.

## Required inputs

- R1 FASTQ
- R2 FASTQ
- GBZ graph
- minimizer index
- distance index

## Validated project knowledge

- Use the validated vg version when possible.
- Avoid many concurrent jobs reading the same graph index files.
- Use multiple graph-index copies when running large parallel mapping batches.
- Start with a single sample benchmark before scaling.
