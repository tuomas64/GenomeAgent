import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).parents[1] / "tools" / "allas" / "genomeagent_allas.py"
spec = importlib.util.spec_from_file_location("ga_allas", MODULE)
ga = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(ga)


class AllasMacTests(unittest.TestCase):
    def manifest(self):
        return {
            "schema_version": "0.1", "dataset_id": "x", "project": "project_1",
            "remote": {"host": "puhti", "runtime_root": "/scratch/project_1/runtime"},
            "local": {"source_root": "/scratch/project_1/data", "paths": ["a", "b"]},
            "archive": {"bucket": "bucket"}, "slurm": {},
            "validation": {"primary_suffix": ".bam", "expected_primary_files": 2, "require_index": True},
        }

    def test_manifest_valid(self):
        ga.validate_manifest(self.manifest())

    def test_manifest_rejects_parent_path(self):
        m = self.manifest()
        m["local"]["paths"] = ["../bad"]
        with self.assertRaises(SystemExit):
            ga.validate_manifest(m)

    def test_bam_inventory_validation(self):
        m = self.manifest()
        inv = {"missing_roots": [], "file_count": 4, "total_bytes": 10, "files": [
            {"path": "a/A.bam", "bytes": 3}, {"path": "a/A.bam.bai", "bytes": 1},
            {"path": "b/B.bam", "bytes": 5}, {"path": "b/B.bai", "bytes": 1},
        ]}
        self.assertTrue(ga.validate_inventory(m, inv)["passed"])
        inv["files"].pop()
        self.assertFalse(ga.validate_inventory(m, inv)["passed"])

    def test_inventory_comparison(self):
        a = {"file_count": 1, "total_bytes": 3, "files": [{"path": "x", "bytes": 3}]}
        b = {"file_count": 1, "total_bytes": 3, "files": [{"path": "x", "bytes": 3}]}
        self.assertTrue(ga.compare_inventories(a, b)["passed"])
        b["files"][0]["bytes"] = 4
        self.assertFalse(ga.compare_inventories(a, b)["passed"])

    def test_generated_upload_contains_no_secret(self):
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("OS_PASSWORD=", source)
        self.assertIn("OS_PASSWORD:-", source)


if __name__ == "__main__":
    unittest.main()
