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
            dedup_dir = output_dir / "deduplicated_gams"
            stats_dir = output_dir / "dedup_stats"
            input_dir.mkdir()
            output_dir.mkdir()
            log_dir.mkdir()
            dedup_dir.mkdir()
            stats_dir.mkdir()

            (input_dir / "B.gam").write_bytes(b"original-b")
            (dedup_dir / "A.dedup.gam").write_bytes(b"deduplicated-a")
            (stats_dir / "A.dedup_summary.tsv").write_text(
                "validation_status\tEXACT_TEMPLATE_PAIR_MATCH\n",
                encoding="utf-8",
            )

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
            (log_dir / "dedup_111111_1.err").write_text(
                "ERROR missing or empty test fixture input\n",
                encoding="utf-8",
            )
            (log_dir / "dedup_111111_1.out").write_text(
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
                        "output_gam_globs": [str(dedup_dir / "*.dedup.gam")],
                        "summary_globs": [str(stats_dir / "*.dedup_summary.tsv")],
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
            self.assertEqual(counts["input_gams_nonzero"], 1)
            self.assertEqual(counts["outputs_present"], 1)
            self.assertEqual(counts["outputs_validated_exact_match"], 1)
            self.assertEqual(counts["outputs_present_unvalidated"], 0)
            self.assertEqual(counts["source_inputs_deleted_after_success"], 1)
            self.assertEqual(counts["missing_inputs"], 1)
            self.assertEqual(counts["assigned_without_output"], 1)

            states = {row["sample"]: row["state"] for row in dataset["samples"]}
            self.assertEqual(states["A"], "completed_exact_template_pair_match")
            self.assertEqual(states["B"], "assigned_pending_or_running")
            self.assertEqual(states["C"], "missing_input_no_completion_evidence")

            self.assertEqual(len(observation["logs"]["error_hits"]), 1)
            self.assertEqual(len(observation["logs"]["completion_hits"]), 1)
            self.assertFalse(observation["io_policy"]["gam_content_read"])
            self.assertFalse(observation["io_policy"]["checksums_computed"])
            self.assertFalse(observation["io_policy"]["vg_stats_run"])

            status = GamDeduplicationProfile().interpret(observation, config)
            self.assertEqual(status["overall_status"], "incomplete_inputs")
            self.assertEqual(status["counts"]["expected_samples"], 3)
            self.assertEqual(status["counts"]["missing_inputs"], 1)
            self.assertEqual(status["counts"]["historical_error_hit_groups"], 1)
            self.assertEqual(status["counts"]["current_error_hit_groups"], 0)

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
            self.assertIn("fixture\tA\tcompleted_exact_template_pair_match", (scan_dir / "sample_status.tsv").read_text())
            self.assertIn("fixture\tC", (scan_dir / "missing_inputs.tsv").read_text())
            self.assertIn("Overall status: **incomplete_inputs**", report)
            self.assertIn("Historical events", report)
            self.assertIn("does not run `vg stats`", report)

    def test_configuration_rejects_duplicate_dataset_names(self):
        invalid = {
            "datasets": [
                {
                    "name": "own",
                    "expected_samples": 1,
                    "input_dir_candidates": [],
                    "output_dir_candidates": [],
                    "output_gam_globs": [],
                    "summary_globs": [],
                    "manifest_candidates": [],
                    "worker_manifest_globs": [],
                    "log_globs": [],
                },
                {
                    "name": "own",
                    "expected_samples": 1,
                    "input_dir_candidates": [],
                    "output_dir_candidates": [],
                    "output_gam_globs": [],
                    "summary_globs": [],
                    "manifest_candidates": [],
                    "worker_manifest_globs": [],
                    "log_globs": [],
                },
            ]
        }
        with self.assertRaises(TaskScanError):
            validate_config(invalid)

    def test_residual_summary_validates_output_and_preserves_qc_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            dedup_dir = output_dir / "deduplicated_gams"
            stats_dir = output_dir / "dedup_stats"
            input_dir.mkdir()
            dedup_dir.mkdir(parents=True)
            stats_dir.mkdir()

            (dedup_dir / "AT02.dedup.gam").write_bytes(b"deduplicated-at02")
            (stats_dir / "AT02.dedup_residual_summary.tsv").write_text(
                "malformed_header_with_missing_separators\n"
                "AT02\tswedish\t40979314\t32400362\t8578952\t"
                "EXACT_TEMPLATE_PAIR_MATCH\t32400362\t1486493\t"
                "4.587890/output/path\n",
                encoding="utf-8",
            )

            config = {
                "project_root": str(root),
                "user": "nobody",
                "job_name_patterns": ["dedup"],
                "max_recent_log_files": 0,
                "max_log_tail_bytes": 4096,
                "datasets": [{
                    "name": "swedish",
                    "expected_samples": 1,
                    "input_dir_candidates": [str(input_dir)],
                    "output_dir_candidates": [str(output_dir)],
                    "output_gam_globs": [str(dedup_dir / "*.dedup.gam")],
                    "summary_globs": [
                        str(stats_dir / "*.dedup_summary.tsv"),
                        str(stats_dir / "*.dedup_residual_summary.tsv"),
                    ],
                    "manifest_candidates": [],
                    "worker_manifest_globs": [],
                    "log_globs": [],
                }],
            }

            observation = self.run_fixture_observer(config)
            dataset = observation["datasets"][0]
            sample = dataset["samples"][0]
            self.assertEqual(sample["sample"], "AT02")
            self.assertEqual(
                sample["state"], "completed_exact_template_pair_match"
            )
            self.assertEqual(sample["summary_type"], "dedup_residual_summary")
            self.assertTrue(sample["exact_template_pair_match"])
            self.assertTrue(sample["source_input_deleted_after_success"])
            self.assertEqual(
                sample["residual_qc_state"], "residual_duplicates_detected"
            )
            self.assertEqual(
                sample["residual_duplicate_primary_reads_after_dedup"], 1486493
            )
            self.assertAlmostEqual(
                sample["residual_duplicate_primary_pct_after_dedup"], 4.587890
            )
            self.assertEqual(dataset["counts"]["outputs_validated_exact_match"], 1)
            self.assertEqual(dataset["counts"]["outputs_present_unvalidated"], 0)
            self.assertEqual(dataset["counts"]["residual_validation_summaries"], 1)
            self.assertEqual(dataset["counts"]["residual_duplicates_detected"], 1)

            status = GamDeduplicationProfile().interpret(observation, config)
            self.assertEqual(status["overall_status"], "deduplication_validated")
            self.assertEqual(status["counts"]["residual_duplicates_detected"], 1)
            self.assertTrue(
                any("residual summaries report" in item for item in status["warnings"])
            )

    def test_superseded_cancellation_does_not_warn_current_attempt(self):
        observation = {
            "datasets": [
                {
                    "name": "fixture",
                    "expected_samples": 1,
                    "counts": {
                        "samples_observed": 1,
                        "input_gams_nonzero": 1,
                        "outputs_present": 0,
                        "outputs_validated_exact_match": 0,
                        "outputs_present_unvalidated": 0,
                        "source_inputs_deleted_after_success": 0,
                        "missing_inputs": 0,
                        "summary_present_output_missing": 0,
                    },
                    "manifest": {"duplicate_samples": [], "malformed_lines": []},
                    "input_duplicate_ids": [],
                    "output_duplicate_ids": [],
                    "summary_duplicate_ids": [],
                }
            ],
            "jobs": {
                "running": [
                    {
                        "job_id": "222222_1",
                        "parent_job_id": "222222",
                        "name": "fixture_dedup_fast",
                        "state": "RUNNING",
                    }
                ],
                "recent": [
                    {
                        "job_id": "111111_1",
                        "parent_job_id": "111111",
                        "name": "fixture_dedup_fast",
                        "state": "CANCELLED",
                    }
                ],
            },
            "logs": {
                "error_hits": [
                    {
                        "path": "fixture_dedup_111111_1.err",
                        "job_id": "111111_1",
                        "parent_job_id": "111111",
                        "matched_lines": ["CANCELLED"],
                    }
                ]
            },
        }

        profile = GamDeduplicationProfile()
        status = profile.interpret(observation, {})
        self.assertEqual(status["overall_status"], "running")
        self.assertEqual(status["counts"]["current_failed_job_records"], 0)
        self.assertEqual(status["counts"]["historical_failed_job_records"], 1)
        self.assertEqual(status["counts"]["current_error_hit_groups"], 0)
        self.assertEqual(status["counts"]["historical_error_hit_groups"], 1)

        observation["logs"]["error_hits"].append(
            {
                "path": "fixture_dedup_222222_1.err",
                "job_id": "222222_1",
                "parent_job_id": "222222",
                "matched_lines": ["ERROR current attempt"],
            }
        )
        current_status = profile.interpret(observation, {})
        self.assertEqual(current_status["overall_status"], "running_with_warnings")
        self.assertEqual(current_status["counts"]["current_error_hit_groups"], 1)

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
        self.assertEqual(config["remote_python"], "python3.9")
        self.assertEqual(config["profile_version"], "1.4")
        for dataset in config["datasets"]:
            self.assertTrue(
                any("dedup_residual_summary" in pattern for pattern in dataset["summary_globs"])
            )


if __name__ == "__main__":
    unittest.main()
