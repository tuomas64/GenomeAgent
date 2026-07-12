# GenomeAgent Task Scan: linear_joint_calling

Scan directory: `workspace/task_scans/linear_joint_calling/20260712T082301Z`
Cluster time: `2026-07-12T11:23:17+0300`
Overall status: **running**
Current stage: **genotypegvcfs**
Next safe action: **wait_for_genotypegvcfs_to_finish**

## Stage summary

| Stage | Status | Validated | Expected |
|---|---:|---:|---:|
| bam_markduplicates | validated_completed | 455 | 455 |
| haplotypecaller_gvcfs | validated_completed | 455 | 455 |
| genomicsdbimport | validated_completed | 7 | 7 |
| genotypegvcfs | running | 0 | 7 |
| final_merge | not_started | 0 |  |

## Counts

- samples_seen: 455
- validated_bams_with_index: 455
- validated_gvcfs_with_index: 455
- validated_genomicsdb_workspaces: 7
- expected_genomicsdb_workspaces: 7
- validated_genotype_vcfs_with_index: 0
- expected_genotype_vcfs: 7
- final_vcf_candidates: 0
- running_relevant_jobs: 7
- recent_error_hit_groups: 0

## BAM index styles

- both: 455

## Storage note

Trimmed FASTQ files may be archived to Allas and removed from scratch; missing trimmed FASTQs are not treated as failure if downstream BAM/GVCF outputs exist.

```text
Filesystem                                      Size  Used Avail Use% Mounted on
10.140.15.146@o2ib:10.140.15.145@o2ib:/scratch   10T  5.8T  4.3T  58% /scratch
```

## Output files

- `task_scan.json`
- `sample_status.tsv`
- `missing_outputs.tsv`
- `running_jobs.tsv`
- `recent_errors.txt`
