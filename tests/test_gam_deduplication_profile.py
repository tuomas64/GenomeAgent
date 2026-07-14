from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from genomeagent.task_profiles.gam_deduplication import (
    GamDeduplicationProfile,
    build_remote_program,
    validate_config,
)
from genomeagent.task_scanner import TaskScanError


class GamDeduplicationProfileTests(unittest.TestCase):
    def run_fixture_observer(self, config):
        result = subprocess.run(
            [sys.executable, "-c", build_remote_program(config)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_observer_classifies_outputs_assignments_and_missing_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            log_dir = output_dir / "logs"
            input_dir.mkdir()
            output_dir.mkdir()
            log_dir.mkdir()

            (input_dir / "A.gam").write_bytes(b"original-a")
            (input_dir / "B.gam").write_bytes(b"original-b")
            (output_dir / "A.dedup.gam").write_bytes(b"deduplicated-a")

            manifest = root / "remaining_manifest.tsv"
            manifest.write_text(
                "sample\tinput_gam\n"
                f"A\t{input_dir / 'A.gam'}\n"
                f"B\t{input_dir / 'B.gam'}\n"
                f"C\t{input_dir / 'C.gam'}\n",
                encoding="utf-8",
            )
            worker_manifest = output_dir / "worker_1_remaining_manifest.tsv"
            worker_manifest.write_text(
                "sample\tinput_gam\n"
                f"B\t{input_dir / 'B.gam'}\n",
                encoding="utf-8",
            )
            (log_dir / "dedup_1.err").write_text(
                "ERROR missing or empty test fixture input\n",
                encoding="utf-8",
            )
            (log_dir / "dedup_1.out").write_text(
                "A EXACT_TEMPLATE_PAIR_MATCH\n",
                encoding="utf-8",
            )

            config = {
                "project_root": str(root),
                "user": "nobody",
                "job_name_patterns": ["dedup"],
                "max_recent_log_files": 10,
                "max_log_tail_bytes": 4096,
                "datasets": [
                    {
                        "name": "fixture",
                        "expected_samples": 3,
                        "input_dir_candidates": [str(input_dir)],
                        "output_dir_candidates": [str(output_dir)],
                        "manifest_candidates": [str(manifest)],
                        "worker_manifest_globs": [str(output_dir / "worker_*manifest.tsv")],
                        "log_globs": [str(log_dir / "*.out"), str(log_dir / "*.err")],
                    }
                ],
            }

            observation = self.run_fixture_observer(config)
            dataset = observation["datasets"][0]
            counts = dataset["counts"]
            self.assertEqual(counts["expected_samples"], 3)
            self.assertEqual(counts["samples_observed"], 3)
            self.assertEqual(counts["input_gams_nonzero"], 2)
            self.assertEqual(counts["outputs_present_unvalidated"], 1)
            self.assertEqual(counts["missing_inputs"], 1)
            self.assertEqual(counts["assigned_without_output"], 1)

            states = {row["sample"]: row["state"] for row in dataset["samples"]}
            self.assertEqual(states["A"], "output_present_unvalidated")
            self.assertEqual(states["B"], "assigned_pending_or_running")
            self.assertEqual(states["C"], "missing_input")

            self.assertEqual(len(observation["logs"]["error_hits"]), 1)
            self.assertEqual(len(observation["logs"]["completion_hits"]), 1)
            self.assertFalse(observation["io_policy"]["gam_content_read"])
            self.assertFalse(observation["io_policy"]["checksums_computed"])
            self.assertFalse(observation["io_policy"]["vg_stats_run"])

            status = GamDeduplicationProfile().interpret(observation, config)
            self.assertEqual(status["overall_status"], "attention_required")
            self.assertEqual(status["counts"]["expected_samples"], 3)
            self.assertEqual(status["counts"]["missing_inputs"], 1)

            scan_dir = root / "local_report"
            scan_dir.mkdir()
            payload = {
                "observation": observation,
                "status_summary": status,
            }
            profile = GamDeduplicationProfile()
            artifact_paths = profile.write_artifacts(payload, scan_dir)
            report = profile.render_report(payload, scan_dir)
            (scan_dir / "report.md").write_text(report, encoding="utf-8")

            self.assertTrue(all(path.exists() for path in artifact_paths))
            self.assertIn("fixture\tA\toutput_present_unvalidated", (scan_dir / "sample_status.tsv").read_text())
            self.assertIn("fixture\tC", (scan_dir / "missing_inputs.tsv").read_text())
            self.assertIn("Overall status: **attention_required**", report)
            self.assertIn("does not run `vg stats`", report)

    def test_configuration_rejects_duplicate_dataset_names(self):
        invalid = {
            "datasets": [
                {
                    "name": "own",
                    "expected_samples": 1,
                    "input_dir_candidates": [],
                    "output_dir_candidates": [],
                    "manifest_candidates": [],
                    "worker_manifest_globs": [],
                    "log_globs": [],
                },
                {
                    "name": "own",
                    "expected_samples": 1,
                    "input_dir_candidates": [],
                    "output_dir_candidates": [],
                    "manifest_candidates": [],
                    "worker_manifest_globs": [],
                    "log_globs": [],
                },
            ]
        }
        with self.assertRaises(TaskScanError):
            validate_config(invalid)

    def test_repository_configuration_preserves_authoritative_cohort_counts(self):
        config_path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "tasks"
            / "gam_deduplication.json"
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        counts = {item["name"]: item["expected_samples"] for item in config["datasets"]}
        self.assertEqual(counts, {"own": 233, "swedish": 225})
        self.assertEqual(sum(counts.values()), 458)


if __name__ == "__main__":
    unittest.main()
