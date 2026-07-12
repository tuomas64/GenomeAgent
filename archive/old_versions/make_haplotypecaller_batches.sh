#!/bin/bash
set -euo pipefail

OUTDIR=/scratch/project_2001113/GATK/gvcf
LOGDIR=/scratch/project_2001113/GATK/logs/haplotypecaller
mkdir -p "${OUTDIR}" "${LOGDIR}"

for part in 1 2 3 4; do
  case "$part" in
    1) START=1;   END=114 ;;
    2) START=115; END=228 ;;
    3) START=229; END=342 ;;
    4) START=343; END=455 ;;
  esac

  cat > "03_haplotypecaller_part${part}.sh" <<EOT
#!/bin/bash
#SBATCH --job-name=hc_part${part}
#SBATCH --account=project_2001113
#SBATCH --partition=small
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --array=${START}-${END}%12
#SBATCH --output=/scratch/project_2001113/GATK/logs/haplotypecaller/hc_part${part}_%A_%a.out
#SBATCH --error=/scratch/project_2001113/GATK/logs/haplotypecaller/hc_part${part}_%A_%a.err

set -euo pipefail

module load gatk/4.5.0.0
module load biokit

REF=/scratch/project_2001113/pangenome/Fragaria_vesca_v6_genome.fasta
BAMDIR=/scratch/project_2001113/GATK/bam
OUTDIR=/scratch/project_2001113/GATK/gvcf
LIST=/scratch/project_2001113/GATK/master_fastq_list.tsv

mkdir -p "\${OUTDIR}"

SAMPLE=\$(awk -v line="\${SLURM_ARRAY_TASK_ID}" 'NR==line {print \$1}' "\${LIST}")

if [[ -z "\${SAMPLE}" ]]; then
  echo "No sample found for array task \${SLURM_ARRAY_TASK_ID}"
  exit 1
fi

BAM="\${BAMDIR}/\${SAMPLE}.bam"
BAI="\${BAM}.bai"
GVCF="\${OUTDIR}/\${SAMPLE}.g.vcf.gz"

echo "Sample: \${SAMPLE}"
echo "BAM: \${BAM}"
echo "Output: \${GVCF}"

if [[ ! -s "\${BAM}" ]]; then
  echo "ERROR: missing BAM: \${BAM}"
  exit 1
fi

if [[ ! -s "\${BAI}" ]]; then
  echo "ERROR: missing BAM index: \${BAI}"
  exit 1
fi

if [[ -s "\${GVCF}" && -s "\${GVCF}.tbi" ]]; then
  echo "GVCF already exists, skipping: \${GVCF}"
  exit 0
fi

gatk --java-options "-Xmx28g -Djava.io.tmpdir=\${OUTDIR}" HaplotypeCaller \\
  -R "\${REF}" \\
  -I "\${BAM}" \\
  -O "\${GVCF}" \\
  -ERC GVCF \\
  --native-pair-hmm-threads "\${SLURM_CPUS_PER_TASK}"

echo "Done: \${SAMPLE}"
EOT

  chmod +x "03_haplotypecaller_part${part}.sh"
done

echo "Created:"
ls -lh 03_haplotypecaller_part*.sh
