# GenomeAgent Dataset Registry v0.1

Dataset Registry is the Git-backed scientific data model for GenomeAgent.
It records **what a dataset is**, **which scientific work uses it**, and **where
complete or partial copies are stored**.

## Core model

One dataset record may have:

- zero, one, or many major-task relationships;
- zero, one, or many run, task, or stage relationships;
- Puhti, Roihu CPU, Roihu GPU, and Allas locations;
- complete, partial, virtual, or unknown materializations;
- one or more Allas upload or restore task links.

The registry never duplicates a dataset merely because it is used by several
major tasks or stored in several places.

## Commands

```bash
bin/genomeagent-datasets list
bin/genomeagent-datasets list-unassigned
bin/genomeagent-datasets show <dataset-id-or-location>
bin/genomeagent-datasets tasks
bin/genomeagent-datasets task-options <major-task>
bin/genomeagent-datasets suggest-assignment <path-or-transfer-id>
```

Register a Puhti or Roihu path. Without `--major-task`, the command opens an
interactive selector that lists major tasks and the selected task's existing
runs, tasks, and stages.

```bash
bin/genomeagent-datasets assign-path \
  --host puhti \
  --dataset-id example_dataset \
  /scratch/project_2001113/example
```

Assign an existing Allas folder-transfer task retrospectively:

```bash
bin/genomeagent-datasets assign-transfer \
  folder_example_0123456789 \
  --dataset-id example_dataset
```

Add another scientific relationship to the same dataset:

```bash
bin/genomeagent-datasets add-task-link example_dataset \
  --major-task graph_snp_indel_discovery \
  --run graph_initial_458 \
  --stage raw_fastq_inputs \
  --relationship consumed_by \
  --role input
```

Record a partial restore without changing the parent dataset identity:

```bash
bin/genomeagent-datasets add-location graph_markdup_bams_458 \
  --provider filesystem \
  --host roihu-cpu \
  --path /scratch/project_2001113/new_analysis/bams50 \
  --state partial_local \
  --materialization partial \
  --sample-set new_analysis_50 \
  --observed-samples 50 \
  --expected-samples 458
```

## Allas integration

Allas folder tasks may be addressed by their generated task ID or original
source path. Verified upload and restore events synchronize linked dataset
records automatically. Existing transfers can be assigned after submission or
after completion without re-uploading data.

## Incremental major tasks

Major tasks may be declared as `incremental_registry`. Such definitions contain
only tasks, stages, and runs already supported by project evidence. Dataset
assignment never requires GenomeAgent to invent a complete workflow in advance.
