# Workflow Graph Foundation

GenomeAgent reconstructs workflows from registered datasets and evidence-backed execution relationships.

## Graph elements

### Dataset nodes

Datasets represent stable scientific or technical identities.

### Execution edges

An execution edge describes how a task consumed one or more datasets and produced one or more new datasets.

```text
input datasets
    ↓ software + version + command + parameters
output datasets
```

### Location records

Location records describe availability, not identity:

- Puhti;
- Roihu CPU;
- Roihu GPU;
- Allas;
- complete, partial, virtual, or unknown materialization.

## Versioning

Software upgrades create new execution provenance and, when outputs differ, new dataset versions.

Historical provenance is never overwritten:

```text
workflow run v1 → vg 1.74.1 → dataset v1
workflow run v2 → newer vg → dataset v2
```

Both can coexist, be validated, and be compared.

## Evidence hierarchy

Preferred evidence includes:

1. validated output and input manifests;
2. successful scripts and scheduler records;
3. exact software and environment evidence;
4. downstream consumption;
5. researcher-confirmed historical relationships.

Inference may propose links, but unresolved provenance remains explicitly unresolved until evidence is available.
