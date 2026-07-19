# GenomeAgent Allas Tools v0.1 — Mac controller

## Architecture

The permanent Allas controller, manifests, and task history live in the GenomeAgent Git repository on the Mac. Commands execute on Puhti over SSH. Only generated scripts, input lists, markers, verification listings, and logs are staged under:

`/scratch/project_2001113/GenomeAgent_runtime/allas_tasks`

No complete GenomeAgent installation is created on Puhti.

## First dataset

`graph_markdup_bams_458` represents the 458 deduplicated graph-mapped BAMs and indexes in:

- `/scratch/project_2001113/pangenome/giraffe_mapping/bam_minicactus_complete_own_data_trimmed_vg174_rg_markdup`
- `/scratch/project_2001113/pangenome/giraffe_mapping/bam_minicactus_complete_swedish_trimmed_vg174_rg_markdup`

Only `*.bam`, `*.bam.bai`, and `*.bai` files are selected. Upload preparation requires exactly 458 BAMs and an index for every BAM.

## Commands from the Mac

```bash
cd /Users/tuomastoivainen/GenomeAgent

bin/genomeagent-allas doctor graph_markdup_bams_458
bin/genomeagent-allas plan-upload graph_markdup_bams_458
bin/genomeagent-allas prepare-upload graph_markdup_bams_458
bin/genomeagent-allas submit-upload graph_markdup_bams_458
bin/genomeagent-allas scan graph_markdup_bams_458 --operation upload
bin/genomeagent-allas verify-upload graph_markdup_bams_458

bin/genomeagent-allas plan-restore graph_markdup_bams_458
bin/genomeagent-allas prepare-restore graph_markdup_bams_458
bin/genomeagent-allas submit-restore graph_markdup_bams_458
bin/genomeagent-allas scan graph_markdup_bams_458 --operation restore
bin/genomeagent-allas verify-restore graph_markdup_bams_458
```

`submit-upload` and `submit-restore` open an interactive Puhti authentication step. The Allas password remains in the remote submission shell long enough for Slurm to inherit `OS_PASSWORD`; GenomeAgent does not save it.

## Safety

- No credentials are stored or printed by GenomeAgent.
- Existing restore targets block restoration.
- Restore checks free space with a 10% reserve.
- Active transfer status can be scanned from the Mac.
- Upload is verified with `a-check` at object-name level.
- Restore is verified against the original Mac-held inventory by path and byte size.
- Automatic local deletion is disabled.
