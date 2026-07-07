#!/usr/bin/env python3

from genomeagent.memory import Memory


def seed():
    db = Memory()

    db.add_project(
        "Fragaria vesca pangenome",
        "European Fragaria vesca pangenome, population genomics, climate adaptation and precision breeding project."
    )

    db.add_directory("CSC project scratch", "/scratch/project_2001113", cluster="puhti", category="project_root", description="Main CSC project scratch directory.", scientific_value="active_computation")
    db.add_directory("Pangenome", "/scratch/project_2001113/pangenome", cluster="puhti", category="pangenome", description="Pangenome graphs, graph indexes and structural variant workflows.", scientific_value="high")
    db.add_directory("PacBio assemblies", "/scratch/project_2001113/pacbio_set1", cluster="puhti", category="assemblies", description="Long-read assemblies and related assembly workflows.", scientific_value="high")

    db.add_software("vg", version="1.74.1", location="/scratch/project_2001113/tuomas64/tools/vg-1.74.1", cluster="puhti", purpose="Graph mapping, graph indexes and variant calling.", validated=True, notes="Validated version for current pangenome workflows.")
    db.add_software("OrthoFinder", version="2.5.5", container="/scratch/project_2001113/${USER}/containers/orthofinder_2.5.5.sif", cluster="puhti", purpose="Orthology and gene family analysis.", validated=True, notes="Preferred usage: apptainer exec --bind /scratch:/scratch ${SIF} orthofinder ...")

    db.add_protocol("vg giraffe short-read mapping", domain="pangenomics", purpose="Map Illumina reads to a pangenome graph using GBZ, minimizer and distance indexes.", status="validated", source="project experience", notes="Use validated vg version and avoid I/O bottlenecks by using multiple graph index copies when running many jobs.")
    db.add_protocol("BUSCO assembly QC", domain="assembly_qc", purpose="Evaluate genome assembly completeness.", status="validated", source="project experience", notes="After BUSCO summary metrics are collected and stored, large BUSCO working directories can often be removed after confirmation.")

    db.add_lesson("storage management", "Storage cleanup should be based on scientific value, reproducibility and dependencies, not file age.", evidence="BUSCO and EDTA working directories can become very large but are often regenerable after summaries are stored.", confidence=0.95)
    db.add_lesson("HPC resource use", "New workflows should be tested on one sample before scaling to many samples.", evidence="Small pilot runs allow memory, CPU, runtime and temporary disk usage to be estimated.", confidence=0.99)

    db.add_relationship("software", "vg", "implements", "protocol", "vg giraffe short-read mapping", confidence=1.0, evidence="Project mapping workflows use vg giraffe.")
    db.add_relationship("directory", "Pangenome", "contains", "protocol", "vg giraffe short-read mapping", confidence=0.8, evidence="Pangenome directory contains graph indexes used for mapping.")

    print("GenomeAgent memory seeded successfully.")
    print(db.summary())


if __name__ == "__main__":
    seed()
