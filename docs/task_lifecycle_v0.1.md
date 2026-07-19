# GenomeAgent Task Lifecycle Foundation v0.1

GenomeAgent now distinguishes stable scientific tasks from individual executions.

## Major tasks

- `linear_snp_indel_discovery`
- `graph_snp_indel_discovery`

The linear task contains a completed baseline run and a separate active rerun with outlier samples removed. The graph task contains the initial 458-sample run. Pangenome construction remains a future separate task.

## Commands

```bash
bin/genomeagent-task-lifecycle --help
bin/genomeagent-task-lifecycle list
bin/genomeagent-task-lifecycle runs linear_snp_indel_discovery
bin/genomeagent-task-lifecycle show-run \
  linear_snp_indel_discovery linear_outlier_removed
bin/genomeagent-task-lifecycle methods-status \
  linear_snp_indel_discovery linear_original_455
bin/genomeagent-task-lifecycle export-methods \
  graph_snp_indel_discovery graph_initial_458
bin/genomeagent-task-lifecycle validate
```

The `bin/` entry point and Python form are equivalent:

```bash
python3 scripts/task_lifecycle.py list
```

## Materials and Methods evidence

Each stage declares the evidence required for publication-quality methods: software and versions, parameters, inputs, outputs, validation, resource evidence and provenance. The initial definitions intentionally do not claim that these fields are already resolved. Task Scanner, Task State Bridge, successful scripts, logs and researcher-approved decisions will populate them.

`export-methods` writes an evidence inventory under:

```text
workspace/methods/<task>/<run>/<timestamp>/
```

It does not generate unsupported publication prose and does not execute remote work.

## Integration boundary

Major-task definitions are listed by the Task Scanner and Task State Bridge discovery shims. A major-task definition does not by itself implement a scanner. Dedicated scanner profiles for the two discovery workflows are the next step.
