# Graph-based population SV genotyping

The `graph_sv_genotyping` profile represents the validated workflow:

```text
input GAM
→ deduplicated GAM
→ vg pack
→ vg call -a
→ indexed per-sample all-site VCF
→ cohort merge
→ biallelic INS/DEL SVs ≥50 bp
→ MAF and genotype-missingness filtering
→ LD pruning for PCA
→ PCA
→ unpruned SV burden and HET/HOM summaries
```

It explicitly distinguishes computational completion from biological
comparability:

```text
workflow_validation: passed
dataset_comparability: attention_required
automatic_execution_allowed: false
```

The default validated counts are:

```text
samples:                         457
biallelic INS/DEL SVs ≥50 bp: 118206
MAF ≥0.01 and GENO ≤0.10:       16601
LD-pruned PCA SVs:              10664
```

## Install

From the GenomeAgent repository root:

```bash
python3 /path/to/GenomeAgent_graph_sv_genotyping_update/install_update.py .
python3 -m pytest -q
```

Review `config/tasks/graph_sv_genotyping.json`, especially the manifest and
directory globs, before the first scan.

## Run

```bash
python3 scripts/task_scan.py graph_sv_genotyping --host puhti
python3 scripts/task_state.py ingest graph_sv_genotyping
```

The profile is read-only and never submits jobs or modifies cluster outputs.
