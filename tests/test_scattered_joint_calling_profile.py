from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from genomeagent.task_profiles.scattered_joint_calling import (
    ScatteredJointCallingProfile,
    build_remote_program,
    validate_config,
)
from genomeagent.task_scanner import TaskScanError


class ScatteredJointCallingProfileTests(unittest.TestCase):
    def run_fixture_observer(self, config):
        result = subprocess.run(
            [sys.executable, "-c", build_remote_program(config)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def make_fixture(self, root: Path):
        joint = root / "jointcalling"
        outbase = joint / "genotyped_scatter_250kb"
        reference_dir = joint / "reference"
        log_dir = outbase / "logs"
        for directory in (joint, outbase, reference_dir, log_dir):
            directory.mkdir(parents=True, exist_ok=True)

        reference = reference_dir / "H4v6_jointcalling.fa"
        reference.write_text(">chr1\nACGT\n", encoding="utf-8")
        Path(str(reference) + ".fai").write_text("chr1\t4\t6\t4\t5\n", encoding="utf-8")

        sample_map = joint / "sample_map.txt"
        sample_map.write_text(
            "A\t/a.g.vcf.gz\nB\t/b.g.vcf.gz\nC\t/c.g.vcf.gz\n",
            encoding="utf-8",
        )

        for chromosome in ("1", "2"):
            workspace = joint / f"GenomicsDB_chr{chromosome}"
            workspace.mkdir()
            (workspace / "callset.json").write_text("{}\n", encoding="utf-8")

        outputs = [outbase / f"window_{task}.vcf.gz" for task in range(1, 5)]
        interval_table = outbase / "intervals_250kb.tsv"
        interval_table.write_text(
            "\n".join(
                [
                    f"1\t1\tchr1\t1\t250000\t{outputs[0]}",
                    f"2\t1\tchr1\t250001\t500000\t{outputs[1]}",
                    f"3\t2\tchr2\t1\t250000\t{outputs[2]}",
                    f"4\t2\tchr2\t250001\t500000\t{outputs[3]}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        outputs[0].write_bytes(b"complete-vcf")
        Path(str(outputs[0]) + ".tbi").write_bytes(b"complete-index")
        outputs[1].write_bytes(b"vcf-without-index")
        Path(str(outputs[2]) + ".tbi").write_bytes(b"orphan-index")

        config = {
            "joint_dir_candidates": [str(joint)],
            "outbase_candidates": ["{joint_dir}/genotyped_scatter_250kb"],
            "sample_map_candidates": ["{joint_dir}/sample_map.txt"],
            "interval_table_candidates": ["{outbase}/intervals_250kb.tsv"],
            "reference_candidates": ["{joint_dir}/reference/H4v6_jointcalling.fa"],
            "workspace_template": "{joint_dir}/GenomicsDB_chr{chromosome}",
            "index_suffixes": [".tbi", ".csi"],
            "job_name_patterns": ["scatter_fixture"],
            "log_globs": ["{outbase}/logs/*.err"],
            "final_vcf_globs": ["{outbase}/final*.vcf.gz"],
            "expected_samples_fallback": 455,
            "publication_contract": {
                "atomic_final_vcf_and_index_after_validation": True,
            },
            "user": "fixture-user",
            "max_recent_log_files": 10,
            "max_log_tail_bytes": 4096,
        }
        return config, outputs, interval_table

    def test_observer_uses_manifest_outputs_and_atomic_publication_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, outputs, _ = self.make_fixture(root)
            observation = self.run_fixture_observer(config)

            states = {
                row["task"]: row["state"]
                for row in observation["interval_table"]["records"]
            }
            self.assertEqual(states[1], "completed_atomic_publish_contract")
            self.assertEqual(states[2], "vcf_present_index_missing")
            self.assertEqual(states[3], "index_present_vcf_missing")
            self.assertEqual(states[4], "pending")
            self.assertEqual(observation["sample_map"]["unique_samples"], 3)
            self.assertEqual(len(observation["workspaces"]), 2)
            self.assertTrue(all(item["nonempty"] for item in observation["workspaces"]))
            self.assertFalse(observation["io_policy"]["vcf_content_read"])
            self.assertFalse(observation["io_policy"]["bcftools_run"])

            status = ScatteredJointCallingProfile().interpret(observation, config)
            self.assertEqual(status["overall_status"], "attention_required")
            self.assertEqual(status["counts"]["expected_samples_per_interval"], 3)
            self.assertEqual(status["counts"]["expected_intervals"], 4)
            self.assertEqual(status["counts"]["completed_atomic_publish_contract"], 1)
            self.assertEqual(status["counts"]["vcf_present_index_missing"], 1)
            self.assertEqual(status["counts"]["index_present_vcf_missing"], 1)
            self.assertIn("sample map count is authoritative", status["warnings"][0].lower())

            scan_dir = root / "scan"
            scan_dir.mkdir()
            payload = {"observation": observation, "status_summary": status}
            profile = ScatteredJointCallingProfile()
            artifacts = profile.write_artifacts(payload, scan_dir)
            report = profile.render_report(payload, scan_dir)
            self.assertTrue(all(path.exists() for path in artifacts))
            self.assertIn("completed_atomic_publish_contract", (scan_dir / "interval_status.tsv").read_text())
            self.assertIn("Overall status: **attention_required**", report)
            self.assertIn("does not read complete VCFs", report)

            for output in outputs:
                output.write_bytes(b"published-vcf")
                Path(str(output) + ".tbi").write_bytes(b"published-index")
            completed_observation = self.run_fixture_observer(config)
            completed_status = profile.interpret(completed_observation, config)
            self.assertEqual(completed_status["overall_status"], "scattered_genotyping_complete")
            self.assertEqual(
                completed_status["next_safe_action"],
                "review_interval_completeness_then_prepare_gather",
            )

    def test_physical_line_mismatch_blocks_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, _, interval_table = self.make_fixture(root)
            lines = interval_table.read_text(encoding="utf-8").splitlines()
            lines[1] = lines[1].replace("2\t", "9\t", 1)
            interval_table.write_text("\n".join(lines) + "\n", encoding="utf-8")

            observation = self.run_fixture_observer(config)
            status = ScatteredJointCallingProfile().interpret(observation, config)
            self.assertEqual(len(observation["interval_table"]["task_line_mismatches"]), 1)
            self.assertEqual(status["overall_status"], "invalid_interval_table")
            self.assertEqual(
                status["next_safe_action"],
                "repair_interval_table_before_any_submission",
            )

    def test_configuration_requires_explicit_atomic_publication_contract(self):
        with self.assertRaises(TaskScanError):
            validate_config({
                "joint_dir_candidates": ["/joint"],
                "outbase_candidates": ["/out"],
                "interval_table_candidates": ["/intervals"],
                "sample_map_candidates": ["/samples"],
                "reference_candidates": ["/reference"],
                "job_name_patterns": ["scatter"],
                "log_globs": ["/logs/*"],
                "publication_contract": {},
            })

    def test_repository_configuration_describes_current_scatter_workflow(self):
        config_path = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "tasks"
            / "scattered_joint_calling.json"
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["expected_samples_fallback"], 455)
        self.assertEqual(config["expected_batches"], 8)
        self.assertIn("intervals_250kb.tsv", config["interval_table_candidates"][0])
        self.assertTrue(
            config["publication_contract"]["atomic_final_vcf_and_index_after_validation"]
        )


if __name__ == "__main__":
    unittest.main()
