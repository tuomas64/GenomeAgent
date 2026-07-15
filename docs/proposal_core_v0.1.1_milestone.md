# Proposal Core v0.1.1 production milestone

On 15 July 2026, Proposal Core generated its first production proposal for a completed scattered GATK joint-calling workflow.

The deterministic proposal bound 886 validated interval VCF/index pairs across seven chromosomes and 455 samples. The generated interval ordering, chromosome manifests and gather workflow were correct. GATK successfully produced all seven chromosome VCFs on the first production workflow trial.

The trial exposed three execution-environment issues rather than workflow-reasoning errors:

- non-interactive Puhti jobs required explicit CSC environment initialization;
- Slurm spool copies made `BASH_SOURCE` unsuitable for locating the staged proposal;
- `GatherVcfs` did not create the expected `.tbi`, requiring an explicit `IndexFeatureFile` step.

Proposal Core v0.1.1 incorporates these observations as deterministic runtime guards and regression tests. Failed attempts remain useful execution evidence and were not classified as GATK resource failures.

This milestone demonstrates the intended GenomeAgent cycle:

```text
Task Scan → Task State → Proposal Core → researcher review → execution evidence → improvement
```

## Production completion

The proposal subsequently produced a 38 GB whole-genome VCF for 455 samples and passed final sample, contig, index, statistics and checksum validation. Proposal Core v0.1.2 incorporates the final linear-contig and TBI-validation lessons from this completed run.
