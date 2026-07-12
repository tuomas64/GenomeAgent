#!/usr/bin/env python3
"""
GenomeAgent Scanner v2.2
- Fast script discovery using find
- Skips unreadable files instead of failing
- Reports skipped files
"""

import json
import shlex
import subprocess
from pathlib import Path
from datetime import datetime, UTC

SSH_TARGET = "puhti"
PROJECT_ROOT = "/scratch/project_2001113"
WORKSPACE = Path("workspace")

PATTERNS = [
    "*.slurm","*.sbatch","*.sh","*.bash","*.zsh",
    "*.py","*.R","*.r","*.smk","Snakefile","*.nf",
    "nextflow.config","*.yaml","*.yml","*.json",
    "*.toml","*.ini","*.cfg","*.conf","README*","*.md"
]

def build_find():
    expr = " -o ".join(f"-name {shlex.quote(p)}" for p in PATTERNS)
    return (
        f"find {shlex.quote(PROJECT_ROOT)} "
        f"\\( {expr} \\) -type f -readable"
    )

def main():
    WORKSPACE.mkdir(exist_ok=True)
    scripts = WORKSPACE / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for d in ("ai","knowledge","history"):
        (WORKSPACE / d).mkdir(exist_ok=True)

    remote = (
        "set -e; "
        "cd /; "
        f"{build_find()} > /tmp/genomeagent_readable.txt; "
        "tar --ignore-failed-read -czf - -T /tmp/genomeagent_readable.txt; "
        "find " + PROJECT_ROOT +
        " \\( " +
        " -o ".join(f"-name {shlex.quote(p)}" for p in PATTERNS) +
        " \\) -type f ! -readable > /tmp/genomeagent_skipped.txt || true; "
        "cat /tmp/genomeagent_skipped.txt >&2 || true"
    )

    tarball = WORKSPACE / "scripts.tar.gz"

    print("Connecting to Puhti...")
    print("Finding readable scripts...")

    with open(tarball, "wb") as out:
        res = subprocess.run(
            ["ssh", SSH_TARGET, remote],
            stdout=out,
            stderr=subprocess.PIPE,
            text=True
        )

    if res.returncode != 0:
        raise SystemExit("Remote scan failed.")

    subprocess.run(
        ["tar", "-xzf", str(tarball), "-C", str(scripts)],
        check=True
    )

    collected = []
    for f in scripts.rglob("*"):
        if f.is_file():
            collected.append({
                "path": str(f.relative_to(scripts)),
                "size": f.stat().st_size
            })

    skipped = [x for x in res.stderr.splitlines() if x.strip()]

    meta = {
        "scanner": "GenomeAgent Scanner v2.2",
        "cluster": SSH_TARGET,
        "project_root": PROJECT_ROOT,
        "scan_time": datetime.now(UTC).isoformat(),
        "scripts_found": len(collected),
        "scripts_skipped": len(skipped)
    }

    (WORKSPACE/"metadata.json").write_text(json.dumps(meta,indent=2))
    (WORKSPACE/"project_snapshot.json").write_text(json.dumps({"scripts":collected},indent=2))

    print("\n✓ Scan completed")
    print(f"Scripts collected : {len(collected)}")
    print(f"Scripts skipped   : {len(skipped)}")

    if skipped:
        print("\nSkipped (permission denied):")
        for s in skipped:
            print("  ", s)

if __name__ == "__main__":
    main()
