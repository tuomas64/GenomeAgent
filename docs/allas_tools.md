# Allas Tools

GenomeAgent Allas Tools manages archival and restoration while preserving scientific dataset identity and provenance.

## Principles

- The local GenomeAgent Git repository is the permanent control plane.
- Puhti or Roihu receives only generated runtime scripts and scheduler logs.
- Credentials and authentication tokens are never written to Git or manifests.
- Upload verification is required before an archive is considered complete.
- Automatic local deletion is disabled.
- Restoring a dataset adds a location; it does not create a new scientific identity.

## Managed datasets

```bash
bin/genomeagent-allas list
bin/genomeagent-allas plan-upload <dataset-id>
bin/genomeagent-allas submit-upload <dataset-id>
bin/genomeagent-allas scan-upload <dataset-id>
bin/genomeagent-allas verify-upload <dataset-id>
```

## Folder uploads

```bash
bin/genomeagent-allas plan-folder-upload /absolute/puhti/path
bin/genomeagent-allas submit-folder-upload /absolute/puhti/path
```

Submission creates an ad-hoc transfer task ID such as:

```text
folder_final_assemblies_ca9a84679e
```

Monitoring commands accept either that task ID or the original source path:

```bash
bin/genomeagent-allas show-folder-upload <task-id-or-source-path>
bin/genomeagent-allas scan-folder-upload <task-id-or-source-path>
bin/genomeagent-allas verify-folder-upload <task-id-or-source-path>
```

## Registry integration

An upload can be assigned before submission or retrospectively:

```bash
bin/genomeagent-datasets assign-transfer \
  <task-id-or-source-path> \
  --dataset-id <dataset-id>
```

After verification, the same dataset record contains both locations:

```text
filesystem / Puhti / available_local
Allas / archived_verified
```

## Partial restoration

A subset restore should record:

- parent dataset identity;
- selected sample-set identity;
- observed and expected sample counts;
- destination host and path;
- partial materialization state.

The complete archive remains unchanged.
