#!/usr/bin/env python3

import json
import tarfile
from pathlib import Path

WORKSPACE = Path("workspace")

SNAPSHOT = WORKSPACE / "project_snapshot.json"
SNAPSHOT_BRAIN = WORKSPACE / "project_snapshot_brain.json"

TAR_IN = WORKSPACE / "scripts.tar.gz"
TAR_OUT = WORKSPACE / "scripts_brain.tar.gz"

IGNORE_DIRS = {
    ".git",
    "__pycache__",
    "miniconda",
    "conda",
    "site-packages",
    "node_modules",
    "tools",
}

KEEP_SUFFIXES = {
    ".sh",
    ".slurm",
}


def keep_file(path):
    p = Path(path)

    # Skip ignored directories
    for part in p.parts:
        if part in IGNORE_DIRS:
            return False

    # Keep *.sh and *.slurm
    if p.suffix.lower() in KEEP_SUFFIXES:
        return True

    # Keep extensionless SLURM scripts containing #SBATCH
    try:
        full_path = Path(path)
        if full_path.exists() and full_path.is_file():
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                for _ in range(10):
                    line = f.readline()
                    if not line:
                        break
                    if "#SBATCH" in line:
                        return True
    except Exception:
        pass

    return False


def main():

    if not SNAPSHOT.exists():
        raise FileNotFoundError(SNAPSHOT)

    if not TAR_IN.exists():
        raise FileNotFoundError(TAR_IN)

    data = json.loads(SNAPSHOT.read_text())

    kept = []
    keep_paths = set()
    removed = 0

    for entry in data["scripts"]:
        path = entry["path"]

        if keep_file(path):
            kept.append(entry)
            keep_paths.add(path)
        else:
            removed += 1

    with open(SNAPSHOT_BRAIN, "w") as f:
        json.dump({"scripts": kept}, f, indent=2)

    copied = 0

    with tarfile.open(TAR_IN, "r:gz") as tin, \
         tarfile.open(TAR_OUT, "w:gz") as tout:

        for member in tin.getmembers():

            member_path = member.name.lstrip("./")

            if member_path in keep_paths:
                f = tin.extractfile(member)

                if f is not None:
                    tout.addfile(member, f)
                    copied += 1

    print()
    print("=" * 60)
    print("GenomeAgent Filter")
    print("=" * 60)
    print(f"Original files : {len(data['scripts'])}")
    print(f"Remaining      : {len(kept)}")
    print(f"Removed        : {removed}")
    print(f"Copied to tar  : {copied}")
    print()
    print(f"Wrote: {SNAPSHOT_BRAIN}")
    print(f"Wrote: {TAR_OUT}")


if __name__ == "__main__":
    main()

