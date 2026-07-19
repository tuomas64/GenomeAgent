# Task Catalog

Task Catalog supplies the scientific assignment targets used by Dataset Registry.

## Two major-task modes

### Workflow mode

Used when an established run and stage structure is already known.

```text
linear_snp_indel_discovery
├── linear_original_455
└── linear_outlier_removed
```

### Incremental registry mode

Used when workflows are being reconstructed gradually from real datasets and successful evidence.

```text
graph_sv_genotyping
├── gam_deduplication
└── vg_pack
```

Only evidenced tasks are added. Future tasks such as `vg_call_all_sites` are created when the corresponding pack or VCF datasets are registered.

## Commands

```bash
bin/genomeagent-tasks list
bin/genomeagent-tasks show <major-task>
bin/genomeagent-task-lifecycle validate
```

Create an incremental major task and its first evidenced task:

```bash
bin/genomeagent-datasets create-major-task \
  --major-task <id> \
  --title "<title>" \
  --description "<scope>" \
  --first-task <task-id> \
  --first-task-title "<task title>"
```

Add an evidenced task later:

```bash
bin/genomeagent-datasets add-task <major-task> \
  --task <task-id> \
  --title "<task title>"
```

Task creation modifies only Git-tracked catalog definitions. It does not submit, cancel, move, upload, restore, or delete scientific files.
