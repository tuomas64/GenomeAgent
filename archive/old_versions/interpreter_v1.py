#!/usr/bin/env python3
"""
GenomeAgent Interpreter v1.0

Reads workspace/project_snapshot.json and enriches each file with
deterministic classifications. Produces:

    workspace/project_knowledge.json

No AI is used in this stage.
"""

import json
from pathlib import Path
from collections import Counter

WORKSPACE = Path("workspace")
SNAPSHOT = WORKSPACE / "project_snapshot.json"
OUTPUT = WORKSPACE / "project_knowledge.json"

THIRD_PARTY_DIRS = {
    "tools", "miniconda", "conda", ".git", "__pycache__",
    "node_modules", ".venv", "env", "site-packages"
}

REPORT_PATTERNS = (
    "fastp.json",
    "multiqc",
    "busco",
)

def classify(entry):
    path = entry["path"]
    p = Path(path)
    name = p.name.lower()

    parts = {x.lower() for x in p.parts}

    language = "unknown"
    category = "unknown"
    subsystem = p.parts[0] if p.parts else "root"

    if parts & THIRD_PARTY_DIRS:
        category = "third_party"

    elif name.endswith((".slurm", ".sbatch")):
        language = "slurm"
        category = "workflow"

    elif name.endswith((".sh", ".bash", ".zsh")):
        language = "shell"
        category = "workflow"

    elif name.endswith(".py"):
        language = "python"
        category = "workflow"

    elif name.endswith(".r"):
        language = "R"
        category = "workflow"

    elif name.endswith((".yaml",".yml",".json",".toml",".ini",".cfg",".conf")):
        language = "configuration"
        category = "configuration"

    elif name.startswith("readme") or name.endswith(".md"):
        language = "documentation"
        category = "documentation"

    for pat in REPORT_PATTERNS:
        if pat in name:
            category = "report"

    entry["language"] = language
    entry["category"] = category
    entry["subsystem"] = subsystem
    entry["ignored"] = category == "third_party"
    return entry

def main():
    if not SNAPSHOT.exists():
        raise SystemExit(f"Missing {SNAPSHOT}")

    data = json.loads(SNAPSHOT.read_text())

    files = data["scripts"]
    out = []
    counts = Counter()

    for f in files:
        e = classify(dict(f))
        counts[e["category"]] += 1
        out.append(e)

    knowledge = {
        "version": "GenomeAgent Interpreter v1.0",
        "files": out,
        "summary": counts
    }

    OUTPUT.write_text(json.dumps(knowledge, indent=2))

    print("="*60)
    print("GenomeAgent Interpreter v1.0")
    print("="*60)
    for k,v in sorted(counts.items()):
        print(f"{k:18s} {v:6d}")
    print("="*60)
    print(f"Knowledge file written to:\n{OUTPUT}")

if __name__=="__main__":
    main()
