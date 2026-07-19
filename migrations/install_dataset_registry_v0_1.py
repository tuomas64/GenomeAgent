#!/usr/bin/env python3
"""Idempotent repository migration for Dataset Registry v0.1."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

ROOT = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from genomeagent.dataset_registry import (  # noqa: E402
    DatasetRegistryError,
    create_or_update_dataset,
    make_link,
    migrate_all,
    register_managed_archive,
    register_transfer,
    transfer_by_identifier,
)


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def migrate_linear_run_id() -> None:
    path = ROOT / "config" / "major_tasks" / "linear_snp_indel_discovery.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(data)
    if "linear_baseline_455" not in text:
        return
    text = text.replace("linear_baseline_455", "linear_original_455")
    data = json.loads(text)
    for run in data.get("runs", []):
        if run.get("run_id") == "linear_original_455":
            if run.get("kind") == "baseline":
                run["kind"] = "original"
            if run.get("accepted_as") == "baseline":
                run["accepted_as"] = "original"
            purpose = str(run.get("purpose", ""))
            run["purpose"] = purpose.replace("baseline comparison", "original comparison dataset")
    write_json(path, data)
    old_methods = ROOT / "workspace" / "methods" / "linear_snp_indel_discovery" / "linear_baseline_455"
    new_methods = ROOT / "workspace" / "methods" / "linear_snp_indel_discovery" / "linear_original_455"
    if old_methods.exists() and not new_methods.exists():
        old_methods.rename(new_methods)
    print("MIGRATED run ID: linear_baseline_455 -> linear_original_455")


def pangenome_links() -> list[dict]:
    return [
        make_link(
            ROOT,
            major_task="pangenome_construction",
            task=task,
            relationship="consumed_by",
            role="input",
            evidence=["researcher-confirmed use of final assemblies"],
        )
        for task in ("minigraph_cactus", "pggb", "svim_asm")
    ]


def register_final_assemblies() -> None:
    dataset_id = "pangenome_final_assemblies_29"
    title = "Final pangenome assemblies (29 assemblies)"
    description = "Final assembly collection consumed by Minigraph-Cactus, PGGB, and SVIM-asm."
    source = "/scratch/project_2001113/pangenome/minigraph_cactus_final_assemblies_nofilter_giraffe/Final_assemblies"
    links = pangenome_links()
    try:
        transfer_by_identifier(ROOT, "folder_final_assemblies_ca9a84679e")
        path = register_transfer(
            ROOT,
            identifier="folder_final_assemblies_ca9a84679e",
            dataset_id=dataset_id,
            title=title,
            description=description,
            scientific_links=links,
        )
    except DatasetRegistryError:
        path = create_or_update_dataset(
            ROOT,
            dataset_id=dataset_id,
            title=title,
            description=description,
            locations=[
                {
                    "provider": "filesystem",
                    "host": "puhti",
                    "path": source,
                    "state": "available_local",
                    "materialization": "complete",
                }
            ],
            scientific_links=links,
            lifecycle_state="available_local",
            expected={"assemblies": 29},
        )
    print(f"REGISTERED {dataset_id}: {path}")


def main() -> int:
    migrate_linear_run_id()
    migrated = migrate_all(ROOT)
    for path in migrated:
        print(f"MIGRATED dataset schema: {path}")
    for archive in sorted((ROOT / "data_registry" / "archives").glob("*.json")) if (ROOT / "data_registry" / "archives").exists() else []:
        linked = register_managed_archive(ROOT, archive.stem)
        if linked:
            print(f"LINKED managed archive: {linked}")
    register_final_assemblies()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
