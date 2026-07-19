from __future__ import annotations

import argparse
import builtins
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.dataset_registry import (  # noqa: E402
    add_incremental_task,
    add_location,
    add_scientific_link,
    create_incremental_major_task,
    create_or_update_dataset,
    discover_datasets,
    list_unassigned,
    load_dataset,
    make_link,
    migrate_all,
    register_transfer,
    suggest_assignment,
)
from genomeagent.task_lifecycle import validate_task_definition  # noqa: E402


class DatasetRegistryTests(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        for rel in ["config/major_tasks", "config/dataset_assignment", "data_registry/datasets", "workspace/allas_tasks/_adhoc_manifests"]:
            (root / rel).mkdir(parents=True, exist_ok=True)
        for name in [
            "linear_snp_indel_discovery.json",
            "graph_snp_indel_discovery.json",
            "graph_sv_genotyping.json",
            "pangenome_construction.json",
        ]:
            shutil.copy(REPOSITORY_ROOT / "config/major_tasks" / name, root / "config/major_tasks" / name)
        shutil.copy(REPOSITORY_ROOT / "config/dataset_assignment/rules.json", root / "config/dataset_assignment/rules.json")
        return tmp, root

    def test_one_dataset_can_have_multiple_major_tasks(self) -> None:
        tmp, root = self.fixture()
        self.addCleanup(tmp.cleanup)
        create_or_update_dataset(
            root,
            dataset_id="shared_fastqs",
            title="Shared FASTQs",
            locations=[{
                "provider": "allas", "bucket": "tuomas64-genomics", "object": "reads.tar",
                "state": "archived_verified", "materialization": "complete",
            }],
        )
        for major, run, stage in [
            ("linear_snp_indel_discovery", "linear_original_455", "raw_fastq_inputs"),
            ("graph_snp_indel_discovery", "graph_initial_458", "raw_fastq_inputs"),
        ]:
            add_scientific_link(
                root,
                "shared_fastqs",
                make_link(root, major_task=major, run_id=run, stage_id=stage, relationship="consumed_by", role="input"),
            )
        _, data = load_dataset(root, "shared_fastqs")
        self.assertEqual(data["dataset_id"], "shared_fastqs")
        self.assertEqual({x["major_task"] for x in data["scientific_links"]}, {
            "linear_snp_indel_discovery", "graph_snp_indel_discovery"
        })
        self.assertEqual(len(data["locations"]), 1)

    def test_partial_materialization_keeps_dataset_identity(self) -> None:
        tmp, root = self.fixture()
        self.addCleanup(tmp.cleanup)
        create_or_update_dataset(root, dataset_id="bams458", title="BAMs")
        add_location(root, "bams458", {
            "provider": "allas", "bucket": "tuomas64-genomics", "prefix": "bams458",
            "state": "archived_verified", "materialization": "complete", "expected_samples": 458,
        })
        add_location(root, "bams458", {
            "provider": "filesystem", "host": "roihu-cpu", "path": "/scratch/project/x/subset50",
            "state": "partial_local", "materialization": "partial", "observed_samples": 50,
            "expected_samples": 458, "sample_set_id": "analysis50",
        })
        _, data = load_dataset(root, "bams458")
        self.assertEqual(data["dataset_id"], "bams458")
        self.assertEqual(len(data["locations"]), 2)
        self.assertIn("partial", {x["materialization"] for x in data["locations"]})

    def test_unassigned_transfer_can_be_assigned_retrospectively(self) -> None:
        tmp, root = self.fixture()
        self.addCleanup(tmp.cleanup)
        transfer = {
            "schema_version": "0.1",
            "dataset_id": "folder_final_assemblies_test",
            "task_mode": "ad_hoc_folder",
            "description": "test",
            "project": "project_2001113",
            "remote": {"host": "puhti", "runtime_root": "/scratch/project/runtime"},
            "local": {
                "source_root": "/scratch/project/pangenome", "paths": ["Final_assemblies"],
                "original_path": "/scratch/project/pangenome/Final_assemblies", "status": "available_local"
            },
            "archive": {"provider": "allas", "bucket": "tuomas64-genomics", "status": "not_verified"},
            "slurm": {"account": "project_2001113"},
        }
        manifest = root / "workspace/allas_tasks/_adhoc_manifests/folder_final_assemblies_test.json"
        manifest.write_text(json.dumps(transfer), encoding="utf-8")
        self.assertEqual(len(list_unassigned(root)), 1)
        links = [
            make_link(root, major_task="pangenome_construction", task=task, relationship="consumed_by", role="input")
            for task in ("minigraph_cactus", "pggb", "svim_asm")
        ]
        register_transfer(
            root,
            identifier="folder_final_assemblies_test",
            dataset_id="pangenome_final_assemblies_29",
            title="Final assemblies",
            scientific_links=links,
        )
        self.assertEqual(list_unassigned(root), [])
        _, data = load_dataset(root, "pangenome_final_assemblies_29")
        self.assertEqual(len(data["scientific_links"]), 3)
        self.assertEqual(data["transfer_links"][0]["task_id"], "folder_final_assemblies_test")

    def test_source_path_resolves_same_transfer(self) -> None:
        tmp, root = self.fixture()
        self.addCleanup(tmp.cleanup)
        transfer = {
            "schema_version": "0.1", "dataset_id": "folder_x", "task_mode": "ad_hoc_folder",
            "project": "project_2001113", "description": "x",
            "remote": {"host": "puhti", "runtime_root": "/scratch/project/runtime"},
            "local": {"source_root": "/scratch/project", "paths": ["x"], "original_path": "/scratch/project/x", "status": "available_local"},
            "archive": {"provider": "allas", "bucket": "tuomas64-genomics", "status": "not_verified"},
            "slurm": {"account": "project_2001113"},
        }
        (root / "workspace/allas_tasks/_adhoc_manifests/folder_x.json").write_text(json.dumps(transfer), encoding="utf-8")
        register_transfer(root, identifier="/scratch/project/x", dataset_id="x")
        _, data = load_dataset(root, "x")
        self.assertEqual(data["transfer_links"][0]["task_id"], "folder_x")

    def test_assignment_rule_suggests_final_assemblies(self) -> None:
        tmp, root = self.fixture()
        self.addCleanup(tmp.cleanup)
        rows = suggest_assignment(root, "/scratch/project_2001113/pangenome/minigraph_cactus_final_assemblies_nofilter_giraffe/Final_assemblies")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["dataset_id"], "pangenome_final_assemblies_29")
        self.assertEqual(len(rows[0]["scientific_links"]), 3)

    def test_old_dataset_schema_migrates_without_duplication(self) -> None:
        tmp, root = self.fixture()
        self.addCleanup(tmp.cleanup)
        # Keep this test hermetic. The repository registry is migrated during
        # installation and may legitimately contain additional Allas locations.
        # This inline fixture represents the original v1.0 record exactly.
        old = {
            "schema_version": "1.0",
            "dataset_id": "graph_markdup_bams_458",
            "title": "Deduplicated graph-mapped BAMs",
            "description": "Legacy migration fixture",
            "lifecycle_state": "available_local_and_uploading",
            "created_by": {
                "task_id": "graph_snp_indel_discovery",
                "run_id": "graph_initial_458",
                "stage_id": "markduplicates",
            },
            "used_by": [
                {
                    "task_id": "graph_snp_indel_discovery",
                    "run_id": "graph_initial_458",
                    "stage_id": "gatk_haplotypecaller",
                    "role": "input",
                }
            ],
            "locations": [
                {
                    "host": "puhti",
                    "paths": [
                        "/scratch/project_2001113/pangenome/giraffe_mapping/bam_minicactus_complete_own_data_trimmed_vg174_rg_markdup",
                        "/scratch/project_2001113/pangenome/giraffe_mapping/bam_minicactus_complete_swedish_trimmed_vg174_rg_markdup",
                    ],
                    "state": "available_local",
                }
            ],
            "safety": {"automatic_local_deletion": False},
        }
        legacy_path = root / "data_registry/datasets/graph_markdup_bams_458.json"
        legacy_path.write_text(json.dumps(old), encoding="utf-8")
        migrate_all(root)
        _, data = load_dataset(root, "graph_markdup_bams_458")
        self.assertEqual(data["schema_version"], "1.1")
        self.assertEqual(len(data["scientific_links"]), 2)
        self.assertEqual(len(data["locations"]), 2)
        self.assertEqual(
            {location["path"] for location in data["locations"]},
            set(old["locations"][0]["paths"]),
        )

    def test_incremental_pangenome_major_task_is_valid(self) -> None:
        data = json.loads((REPOSITORY_ROOT / "config/major_tasks/pangenome_construction.json").read_text())
        validated = validate_task_definition(data, "pangenome")
        self.assertEqual(validated["definition_mode"], "incremental_registry")
        self.assertEqual(validated["stages"], [])
        self.assertEqual({x["task_id"] for x in validated["tasks"]}, {"minigraph_cactus", "pggb", "svim_asm"})

    def test_cli_lists_tasks_and_shows_incremental_options(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "scripts/datasets.py"), "task-options", "pangenome_construction"],
            text=True, capture_output=True, check=True,
        )
        self.assertIn("minigraph_cactus", completed.stdout)
        self.assertIn("pggb", completed.stdout)
        self.assertIn("svim_asm", completed.stdout)
        self.assertIn("Stages: none defined yet", completed.stdout)

    def test_assignment_help_lists_live_catalog(self) -> None:
        for command in ("assign-path", "assign-transfer", "add-task-link"):
            completed = subprocess.run(
                [sys.executable, str(REPOSITORY_ROOT / "scripts/datasets.py"), command, "--help"],
                text=True, capture_output=True, check=True,
            )
            self.assertIn("Current assignment targets", completed.stdout)
            self.assertIn("graph_snp_indel_discovery", completed.stdout)
            self.assertIn("linear_original_455", completed.stdout)
            self.assertIn("raw_fastq_inputs", completed.stdout)
            self.assertIn("pangenome_construction", completed.stdout)
            self.assertIn("minigraph_cactus", completed.stdout)
            self.assertIn("graph_sv_genotyping", completed.stdout)
            self.assertIn("gam_deduplication", completed.stdout)
            self.assertIn("Create a new major task", completed.stdout)

    def test_graph_sv_incremental_catalog_contains_evidenced_tasks(self) -> None:
        data = json.loads((REPOSITORY_ROOT / "config/major_tasks/graph_sv_genotyping.json").read_text())
        validated = validate_task_definition(data, "graph_sv_genotyping")
        self.assertEqual(validated["definition_mode"], "incremental_registry")
        self.assertEqual(validated["stages"], [])
        self.assertEqual(validated["runs"], [])

        task_ids = [item["task_id"] for item in validated["tasks"]]
        self.assertGreaterEqual(len(task_ids), 2)
        self.assertEqual(task_ids[0], "gam_deduplication")
        self.assertIn("vg_pack", task_ids)
        self.assertEqual(len(task_ids), len(set(task_ids)))

    def test_create_major_task_and_add_evidenced_task(self) -> None:
        tmp, root = self.fixture()
        self.addCleanup(tmp.cleanup)
        created = create_incremental_major_task(
            root,
            task_id="new_major",
            title="New major",
            description="Evidence-based new major task",
            first_task_id="first_task",
            first_task_title="First task",
        )
        self.assertTrue(created.exists())
        added = add_incremental_task(
            root,
            major_task="new_major",
            task_id="second_task",
            title="Second task",
        )
        self.assertEqual(added, created)
        definition = json.loads(created.read_text())
        self.assertEqual(
            [item["task_id"] for item in definition["tasks"]],
            ["first_task", "second_task"],
        )
        validate_task_definition(definition, "new_major")

    def test_component_task_creation_rejects_workflow_major_task(self) -> None:
        tmp, root = self.fixture()
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(Exception, "Cannot add component tasks"):
            add_incremental_task(
                root,
                major_task="graph_snp_indel_discovery",
                task_id="not_allowed",
                title="Not allowed",
            )

    def test_guided_assignment_can_add_multiple_major_task_links(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "genomeagent_datasets_cli_test", REPOSITORY_ROOT / "scripts/datasets.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        args = argparse.Namespace(
            no_task_link=False, major_task=None, run=None, task=None, stage=None,
            relationship=None, role=None,
        )

        class InteractiveInput:
            @staticmethod
            def isatty() -> bool:
                return True

        responses = iter([
            "1", "1", "1", "1", "input", "y",
            "3", "1", "1", "1", "input", "n",
        ])
        with mock.patch.object(module.sys, "stdin", InteractiveInput()), \
             mock.patch.object(builtins, "input", side_effect=lambda _prompt="": next(responses)):
            links = module.resolve_initial_links(args)

        self.assertEqual(len(links), 2)
        self.assertEqual(
            {link["major_task"] for link in links},
            {"graph_snp_indel_discovery", "linear_snp_indel_discovery"},
        )
        self.assertEqual({link.get("stage_id") for link in links}, {"raw_fastq_inputs"})
        self.assertEqual({link.get("relationship") for link in links}, {"consumed_by"})



if __name__ == "__main__":
    unittest.main()
