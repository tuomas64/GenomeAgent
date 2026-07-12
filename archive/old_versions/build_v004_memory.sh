#!/usr/bin/env bash
set -euo pipefail

echo "GenomeAgent Build v0.4 - Memory"
echo "Creating SQLite memory layer and CLI commands..."
echo

mkdir -p genomeagent data docs protocols/vg protocols/busco strategies/population_genomics

cat > genomeagent/__init__.py << 'PY'
__version__ = "0.4.0"
PY

cat > genomeagent/memory.py << 'PY'
#!/usr/bin/env python3

import sqlite3
from pathlib import Path
from datetime import datetime


class Memory:
    def __init__(self, db_path="data/genomeagent_memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        cur = self.conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS directories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            path TEXT UNIQUE NOT NULL,
            cluster TEXT,
            category TEXT,
            description TEXT,
            scientific_value TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS software (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            version TEXT,
            location TEXT,
            module TEXT,
            container TEXT,
            cluster TEXT,
            purpose TEXT,
            validated INTEGER DEFAULT 0,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS protocols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            domain TEXT,
            purpose TEXT,
            status TEXT,
            source TEXT,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            relation TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_name TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            evidence TEXT,
            created_at TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            lesson TEXT NOT NULL,
            evidence TEXT,
            confidence REAL DEFAULT 1.0,
            created_at TEXT
        )
        """)

        self.conn.commit()

    def now(self):
        return datetime.now().isoformat(timespec="seconds")

    def add_project(self, name, description=""):
        cur = self.conn.cursor()
        now = self.now()
        cur.execute("""
        INSERT INTO projects (name, description, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            description=excluded.description,
            updated_at=excluded.updated_at
        """, (name, description, now, now))
        self.conn.commit()

    def add_directory(self, name, path, cluster="", category="", description="", scientific_value=""):
        cur = self.conn.cursor()
        now = self.now()
        cur.execute("""
        INSERT INTO directories
        (name, path, cluster, category, description, scientific_value, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            name=excluded.name,
            cluster=excluded.cluster,
            category=excluded.category,
            description=excluded.description,
            scientific_value=excluded.scientific_value,
            updated_at=excluded.updated_at
        """, (name, path, cluster, category, description, scientific_value, now, now))
        self.conn.commit()

    def add_software(self, name, version="", location="", module="", container="", cluster="", purpose="", validated=False, notes=""):
        cur = self.conn.cursor()
        now = self.now()
        cur.execute("""
        INSERT INTO software
        (name, version, location, module, container, cluster, purpose, validated, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, version, location, module, container, cluster, purpose, int(validated), notes, now, now))
        self.conn.commit()

    def add_protocol(self, name, domain="", purpose="", status="draft", source="", notes=""):
        cur = self.conn.cursor()
        now = self.now()
        cur.execute("""
        INSERT INTO protocols
        (name, domain, purpose, status, source, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            domain=excluded.domain,
            purpose=excluded.purpose,
            status=excluded.status,
            source=excluded.source,
            notes=excluded.notes,
            updated_at=excluded.updated_at
        """, (name, domain, purpose, status, source, notes, now, now))
        self.conn.commit()

    def add_relationship(self, source_type, source_name, relation, target_type, target_name, confidence=1.0, evidence=""):
        cur = self.conn.cursor()
        cur.execute("""
        INSERT INTO relationships
        (source_type, source_name, relation, target_type, target_name, confidence, evidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (source_type, source_name, relation, target_type, target_name, confidence, evidence, self.now()))
        self.conn.commit()

    def add_lesson(self, topic, lesson, evidence="", confidence=1.0):
        cur = self.conn.cursor()
        cur.execute("""
        INSERT INTO lessons
        (topic, lesson, evidence, confidence, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (topic, lesson, evidence, confidence, self.now()))
        self.conn.commit()

    def list_table(self, table):
        allowed = {"projects", "directories", "software", "protocols", "relationships", "lessons"}
        if table not in allowed:
            raise ValueError(f"Unknown table: {table}")
        cur = self.conn.cursor()
        cur.execute(f"SELECT * FROM {table} ORDER BY id")
        return cur.fetchall()

    def summary(self):
        cur = self.conn.cursor()
        tables = ["projects", "directories", "software", "protocols", "relationships", "lessons"]
        out = {}
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
            out[t] = cur.fetchone()["n"]
        return out
PY

cat > genomeagent/seed_memory.py << 'PY'
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
PY

cat > genomeagent_cli.py << 'PY'
#!/usr/bin/env python3

import argparse
from genomeagent.memory import Memory
from genomeagent.seed_memory import seed


def print_rows(rows):
    if not rows:
        print("No records.")
        return
    for row in rows:
        print(dict(row))


def main():
    parser = argparse.ArgumentParser(description="GenomeAgent command line interface")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-memory", help="Create and seed GenomeAgent memory database")
    sub.add_parser("summary", help="Show memory summary")

    list_parser = sub.add_parser("list", help="List a memory table")
    list_parser.add_argument("table", choices=["projects", "directories", "software", "protocols", "relationships", "lessons"])

    lesson_parser = sub.add_parser("lesson", help="Add a lesson learned")
    lesson_parser.add_argument("topic")
    lesson_parser.add_argument("lesson")

    args = parser.parse_args()

    if args.command == "init-memory":
        seed()
    elif args.command == "summary":
        db = Memory()
        for key, value in db.summary().items():
            print(f"{key}: {value}")
    elif args.command == "list":
        db = Memory()
        print_rows(db.list_table(args.table))
    elif args.command == "lesson":
        db = Memory()
        db.add_lesson(args.topic, args.lesson)
        print("Lesson added.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
PY

cat > protocols/README.md << 'MD'
# Protocols

This directory stores technical protocols for tools and workflows.

A protocol describes **how to run software correctly and reproducibly**.
MD

cat > protocols/vg/giraffe_mapping.md << 'MD'
# vg giraffe short-read mapping

Status: validated draft

## Purpose

Map Illumina short reads to a pangenome graph.

## Required inputs

- R1 FASTQ
- R2 FASTQ
- GBZ graph
- minimizer index
- distance index

## Validated project knowledge

- Use the validated vg version when possible.
- Avoid many concurrent jobs reading the same graph index files.
- Use multiple graph-index copies when running large parallel mapping batches.
- Start with a single sample benchmark before scaling.
MD

cat > protocols/busco/assembly_qc.md << 'MD'
# BUSCO assembly QC

Status: validated draft

BUSCO output folders can be large. After summary statistics are collected and stored, BUSCO working folders may be removed after confirmation because they are regenerable from the original assembly.
MD

cat > strategies/population_genomics/pca.md << 'MD'
# PCA strategy

Status: draft

## Scientific decision points

- SNPs, SVs, or both
- LD pruning or unpruned variants
- Sample grouping and colors
- Number of PCs
- Figure style

## Current recommended default

- Primary analysis: LD-pruned SNP PCA
- Complementary analysis: LD-pruned SV PCA
- Plot: PC1 vs PC2
- Axes: include percentage of variance explained
- Colors: use project region colors
MD

cat > docs/data_management_plan.md << 'MD'
# Data Management Plan

This document is a living data management plan for GenomeAgent-assisted computational genomics projects.

GenomeAgent manages data according to scientific value, reproducibility and dependency relationships, not file age alone.

## Permanent data

- raw sequencing data
- genome assemblies
- final pangenome graphs
- final VCFs and structural variant datasets
- scripts
- final figures and tables

## Regenerable intermediate data

- BUSCO working directories after summaries are stored
- EDTA temporary files
- temporary mapping files
- temporary indexes
- superseded graph versions after validation

## Responsible HPC citizenship

GenomeAgent supports responsible use of shared HPC infrastructure by reducing unnecessary storage use, avoiding large untested job submissions, and encouraging small benchmark runs before scaling.
MD

cat > .gitignore << 'TXT'
.DS_Store
__pycache__/
*.pyc
.venv/
logs/
*.log
*.db
data/*.db
.vscode/
*.pem
*.key
.env
TXT

cat > README.md << 'MD'
# GenomeAgent

**GenomeAgent is an AI-assisted software robot for computational genomics.**

It automates repetitive computational genomics tasks while leaving scientific decisions to the researcher.

**Motto:** Understand first. Act second.

## Current status

Early development: **v0.4 Memory**

## Core principles

- Automate repetition, not curiosity.
- Understand before acting.
- Measure before scaling.
- Monitor early, scale later.
- Learn protocols before running workflows.
- Reason about relationships, not filenames.
- Preserve reproducibility.
- Keep the researcher in control.
MD

python genomeagent_cli.py init-memory
python genomeagent_cli.py summary

git add .
git commit -m "GenomeAgent v0.4: Add memory database and protocol foundation" || true

echo
echo "Build v0.4 complete."
echo "Try:"
echo "  python genomeagent_cli.py summary"
echo "  python genomeagent_cli.py list protocols"
echo "  python genomeagent_cli.py list lessons"
echo
echo "Then push:"
echo "  git push"
