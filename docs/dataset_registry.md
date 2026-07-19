# Dataset Registry and Assignment

Dataset Registry is the Git-backed scientific data model at the center of GenomeAgent.

## Identity, relationships, and locations

A dataset record separates three concepts:

1. **Identity** — what scientific or technical dataset this is.
2. **Scientific relationships** — which major tasks, runs, tasks, or stages produced or consumed it.
3. **Locations** — where complete or partial materializations currently exist.

One dataset may have many scientific links and many locations.

```text
own_trimmed_fastqs
├── consumed by linear_snp_indel_discovery
├── consumed by graph_snp_indel_discovery
└── stored in Allas
```

A partial restore does not change the parent identity:

```text
graph_markdup_bams_458
├── Allas: complete 458-sample archive
└── Roihu: partial 50-sample materialization
```

## Commands

```bash
bin/genomeagent-datasets list
bin/genomeagent-datasets list-unassigned
bin/genomeagent-datasets show <dataset-id-or-location>
bin/genomeagent-datasets validate
```

Register an existing path:

```bash
bin/genomeagent-datasets assign-path \
  --host puhti \
  --dataset-id example_dataset \
  /absolute/path/to/example
```

Without `--major-task`, assignment opens an interactive selector. The live catalog is also appended to:

```bash
bin/genomeagent-datasets assign-path --help
bin/genomeagent-datasets assign-transfer --help
bin/genomeagent-datasets add-task-link --help
```

Add another scientific relationship:

```bash
bin/genomeagent-datasets add-task-link example_dataset
```

Assign an existing Allas transfer retrospectively:

```bash
bin/genomeagent-datasets assign-transfer \
  <folder-task-id-or-source-path> \
  --dataset-id example_dataset
```

## Task catalog authoring

Incremental major tasks may grow during assignment when new datasets provide evidence for new workflow components.

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

Workflow-style major tasks retain their predefined run and stage structure. New component tasks are not added to them interactively.

## Provenance enrichment

Dataset relationships can begin from researcher-confirmed assignments. Task Scanners later enrich them with:

- software and version;
- environment, module, or container;
- command and parameters;
- input and output manifests;
- scheduler evidence;
- resource use;
- validation;
- errors and successful corrections.

Unresolved fields remain explicit rather than being guessed.
