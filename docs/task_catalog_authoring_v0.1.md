# GenomeAgent Task Catalog Authoring v0.1

This update adds safe task-catalog creation to dataset assignment.

## New scientific major task

`graph_sv_genotyping` is an incremental major task for Illumina structural-variant genotyping against the pangenome graph. Initially it contains only the evidenced task:

- `gam_deduplication` — GAM duplicate removal

Additional tasks are added only when corresponding datasets or successful workflow evidence are registered.

## Interactive assignment

When `--major-task` is omitted, assignment now offers:

- an existing major task;
- **Create a new major task**.

A new major task must be created together with its first evidenced task. This prevents empty speculative task definitions.

When an incremental major task is selected, task selection offers:

- an existing task;
- **Create a new task**.

New component tasks cannot be added to workflow-style major tasks. Those continue to use their defined runs and stages.

## Explicit commands

```bash
bin/genomeagent-datasets create-major-task \
  --major-task my_major_task \
  --title "My major task" \
  --description "Evidence-based scope" \
  --first-task first_task \
  --first-task-title "First evidenced task"

bin/genomeagent-datasets add-task my_major_task \
  --task second_task \
  --title "Second evidenced task"
```

## Safety

This update modifies only Git-tracked task definitions and dataset-assignment code. It does not submit, cancel, restart, move, upload, download, or delete scientific files.
