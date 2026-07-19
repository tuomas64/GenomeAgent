# GenomeAgent Allas Tools v0.2 — Mac controller

## Stable command entry points

The short `bin/` commands and Python scripts are equivalent:

```bash
bin/genomeagent-allas --help
python3 scripts/allas.py --help

bin/genomeagent-task-scan --help
python3 scripts/task_scan.py --help

bin/genomeagent-task-state --help
python3 scripts/task_state.py --help
```

The `bin/` form is recommended for daily use. The Python form remains available for development and debugging.

## Discoverability

```bash
bin/genomeagent-allas --help
bin/genomeagent-allas list-datasets
bin/genomeagent-allas list-folder-uploads

bin/genomeagent-task-scan --help
bin/genomeagent-task-scan list

bin/genomeagent-task-state --help
bin/genomeagent-task-state list

bin/genomeagent-tasks
```

Task Scanner and Task State Bridge discover tasks dynamically from:

- `config/tasks/*.json`
- `workspace/task_scans/<task>/`
- `workspace/task_state/<task>/`

This means newly added task profiles appear automatically without maintaining a hard-coded list.

## Managed Allas datasets

Managed datasets have Git-tracked manifests under `data_registry/archives/`.

```bash
bin/genomeagent-allas plan-upload graph_markdup_bams_458
bin/genomeagent-allas submit-upload graph_markdup_bams_458
bin/genomeagent-allas scan graph_markdup_bams_458 --operation upload
bin/genomeagent-allas verify-upload graph_markdup_bams_458
```

## Generic folder upload

Any absolute Puhti folder can be planned and uploaded without first creating a permanent dataset manifest:

```bash
bin/genomeagent-allas plan-folder-upload \
  /scratch/project_2001113/some_folder

bin/genomeagent-allas submit-folder-upload \
  /scratch/project_2001113/some_folder
```

The tool prints a stable ad-hoc task ID derived from the host, bucket, and source path. Use it for monitoring and verification:

```bash
bin/genomeagent-allas list-folder-uploads
bin/genomeagent-allas scan-folder-upload <task-id>
bin/genomeagent-allas verify-folder-upload <task-id>
```

Optional selection globs can be repeated:

```bash
bin/genomeagent-allas plan-folder-upload \
  /scratch/project_2001113/results \
  --include '*.vcf.gz' \
  --include '*.vcf.gz.tbi'
```

The folder name and internal relative paths are preserved in the Allas bucket. v0.2 transfers regular files individually. Automatic local deletion remains disabled.

## Generic folder restore

After successful upload verification:

```bash
bin/genomeagent-allas plan-folder-restore <task-id>
bin/genomeagent-allas submit-folder-restore <task-id>
bin/genomeagent-allas verify-folder-restore <task-id>
```

Restoration refuses existing target directories and checks free space with a safety reserve.

## Promote an ad-hoc upload

A verified ad-hoc upload can be promoted into the Git-tracked managed registry:

```bash
bin/genomeagent-allas register-folder-upload <task-id> \
  --dataset-id descriptive_dataset_name \
  --description 'Durable project dataset description'
```

## Safety

- Credentials are never stored or printed.
- Submission requires researcher-activated Allas authentication.
- Existing managed manifests are preserved during upgrades.
- Existing Allas workspaces and running Puhti jobs are not modified.
- Upload verification uses `a-check` object-name verification.
- Restore verification compares paths and byte sizes against the original inventory.
- Automatic local deletion is disabled.
