# Proposal Core v0.1.1

Proposal Core converts evidence-grounded Task State into immutable, reviewable workflow proposals. It is deliberately separate from execution.

## Authority boundary

Proposal Core:

- reads local Task State and Task Scan artifacts;
- verifies that a proposal is allowed by current evidence;
- creates manifests, plans, proposed scripts, validation rules and checksums;
- does not use SSH;
- does not submit Slurm jobs;
- does not stage or execute generated scripts;
- does not delete data;
- does not update project knowledge.

## First supported workflow

The first action is `gather_or_merge` for `scattered_joint_calling`:

```text
validated interval VCF/index pairs
        ↓
seven retained chromosome VCFs
        ↓
one whole-genome VCF
        ↓
final sample, contig, index and statistics validation
```

## Commands

```bash
python3 scripts/task_proposal.py prepare \
  scattered_joint_calling \
  --action gather_or_merge

python3 scripts/task_proposal.py validate \
  workspace/proposals/scattered_joint_calling/<proposal_id>

python3 scripts/task_proposal.py show \
  workspace/proposals/scattered_joint_calling/<proposal_id>
```

## Entry gate

Preparation is blocked unless Task State and the bound scan prove:

- state `scattered_genotyping_complete`;
- stage `gather_or_merge`;
- 886/886 outputs satisfy the atomic publication contract;
- no missing VCF or index;
- no running, queued, failed, unresolved or unsubmitted interval;
- seven GenomicsDB workspaces are ready;
- no final VCF already exists;
- recommendations include `review_gather_prerequisites`;
- automatic execution remains disabled.

## Runtime portability added in v0.1.1

The first Puhti production trial exposed three deployment assumptions. v0.1.1 corrects all three:

1. Every generated job initializes CSC software modules explicitly through `/appl/profile/zz-csc-env.sh` while temporarily disabling Bash `nounset`.
2. Jobs require the absolute staged proposal path in `GA_PROPOSAL_DIR`; they do not infer it from `BASH_SOURCE`, because Slurm executes a spool copy of submitted scripts.
3. `GatherVcfs` and `IndexFeatureFile` are separate explicit operations. A VCF is published only after its `.tbi` has been created and verified.

Every job also checks the staged proposal bundle with `sha256sum -c checksums.sha256` before reading manifests or writing outputs.

## Mac-controlled submission

Proposal Core still has no execution authority. After researcher approval, a generated script can be submitted from a Mac because the job initializes its own Puhti environment.

```bash
PROPOSAL_ID=<proposal_id>
REMOTE_DIR="/scratch/project_2001113/GenomeAgent/proposals/scattered_joint_calling/${PROPOSAL_ID}"
RUN_DIR="/scratch/project_2001113/GATK/jointcalling/genotyped_scatter_250kb/gathered/runs/${PROPOSAL_ID}/gather_genome"

ssh -T puhti "mkdir -p '${RUN_DIR}' && \
  sbatch --parsable \
    --export=ALL,GA_PROPOSAL_DIR='${REMOTE_DIR}' \
    --chdir='${RUN_DIR}' \
    --output='${RUN_DIR}/GA_gather_genome_%j.out' \
    --error='${RUN_DIR}/GA_gather_genome_%j.err' \
    '${REMOTE_DIR}/scripts/02_gather_genome.slurm'"
```

The same pattern applies to chromosome gathering and final validation.

## Bundle

```text
workspace/proposals/scattered_joint_calling/<proposal_id>/
├── proposal.json
├── plan.md
├── evidence_snapshot.json
├── interval_manifest.tsv
├── manifests/
├── scripts/
│   ├── 01_gather_chromosomes.slurm
│   ├── 02_gather_genome.slurm
│   └── 03_validate_final_vcf.slurm
├── validation_rules.json
├── resource_proposal.json
└── checksums.sha256
```

The proposal ID is content-addressed from the policy, canonical state, recommendations, provenance, Task Scan and ordered interval evidence. Re-preparing from identical evidence is idempotent.

## Resource boundary

The proposal contains conservative provisional Slurm values. It does not claim that Resource Evidence and Learning Core has established an optimal gather profile. Resources remain subject to researcher review and cannot be changed automatically.
