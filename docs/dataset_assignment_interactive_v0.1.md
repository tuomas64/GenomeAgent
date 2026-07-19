# GenomeAgent Dataset Assignment Interactive v0.1

This additive update improves `genomeagent-datasets` assignment usability.

## Changes

- `assign-path --help`, `assign-transfer --help`, and `add-task-link --help` dynamically list all current major tasks, runs, incremental tasks, and workflow stages from `config/major_tasks/`.
- Omitting `--major-task` starts guided selection with identifiers, titles, and statuses.
- Workflow tasks guide selection through run and stage.
- Incremental major tasks guide selection through their currently evidenced tasks.
- Relationship choices include short explanations.
- `assign-path` and `assign-transfer` can add several scientific links in one interactive session, supporting shared datasets such as FASTQs used by both linear and graph workflows.
- Explicit non-interactive assignments remain supported.

No dataset records, task definitions, Allas manifests, transfer state, or scheduler jobs are modified by installation.
