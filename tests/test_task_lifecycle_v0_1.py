import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
MODULE = ROOT / "genomeagent" / "task_lifecycle.py"
spec = importlib.util.spec_from_file_location("task_lifecycle", MODULE)
tl = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(tl)


class TaskLifecycleTests(unittest.TestCase):
    def test_major_task_definitions_validate(self):
        rows = tl.validate_all(ROOT)
        self.assertEqual({x[0] for x in rows}, {
            "linear_snp_indel_discovery",
            "graph_snp_indel_discovery",
            "graph_sv_genotyping",
            "pangenome_construction",
        })

    def test_linear_original_and_rerun_are_separate(self):
        task = tl.task_by_id(ROOT, "linear_snp_indel_discovery")
        original = tl.run_by_id(task, "linear_original_455")
        rerun = tl.run_by_id(task, "linear_outlier_removed")
        self.assertEqual(original["status"], "completed")
        self.assertEqual(rerun["derived_from"], "linear_original_455")
        states = tl.resolved_stage_states(task, rerun)
        self.assertEqual(states["gatk_haplotypecaller"], "reused_from_parent_run")
        self.assertEqual(states["genotypegvcfs"], "active")

    def test_graph_markdup_paths_are_external_to_gatk_graph(self):
        task = tl.task_by_id(ROOT, "graph_snp_indel_discovery")
        run = tl.run_by_id(task, "graph_initial_458")
        locations = run["external_input_locations"]
        self.assertIn("/pangenome/giraffe_mapping/", locations["own_markdup_bams"])
        self.assertIn("/pangenome/giraffe_mapping/", locations["swedish_markdup_bams"])
        self.assertEqual(locations["gvcfs"], "/scratch/project_2001113/GATK_graph/gvcf")

    def test_methods_snapshot_is_not_falsely_ready(self):
        task = tl.task_by_id(ROOT, "linear_snp_indel_discovery")
        run = tl.run_by_id(task, "linear_original_455")
        snap = tl.methods_evidence_snapshot(task, run)
        self.assertFalse(snap["publication_methods_ready"])
        self.assertGreater(snap["evidence_fields_required"], 0)
        self.assertEqual(snap["evidence_fields_resolved"], 0)

    def test_export_methods_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config" / "major_tasks").mkdir(parents=True)
            source = ROOT / "config" / "major_tasks" / "linear_snp_indel_discovery.json"
            (root / "config" / "major_tasks" / source.name).write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
            out = tl.export_methods_snapshot(root, "linear_snp_indel_discovery", "linear_original_455")
            self.assertTrue((out / "methods_evidence.json").is_file())
            self.assertTrue((out / "methods_evidence.md").is_file())
            payload = json.loads((out / "methods_evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "linear_original_455")


if __name__ == "__main__":
    unittest.main()
