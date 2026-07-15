from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from genomeagent.proposal_core import ProposalCore, ProposalCoreError  # noqa: E402


class ProposalCoreTests(unittest.TestCase):
    def fixture(self, root: Path) -> ProposalCore:
        policy_dir = root / "config/proposals"
        state_dir = root / "workspace/task_state/test_task"
        scan_dir = root / "workspace/task_scans/test_task/20260715T160720Z"
        policy_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)
        scan_dir.mkdir(parents=True)

        remote_root = (root / "remote").resolve()
        windows_root = remote_root / "windows"
        output_root = remote_root / "gathered"
        fake_bin = root / "fake_bin"
        fake_bin.mkdir(parents=True)
        environment_init = (root / "zz-csc-env.sh").resolve()
        environment_init.write_text(
            "module() { return 0; }\n"
            f'export PATH="{fake_bin}:$PATH"\n'
        )
        fake_gatk = fake_bin / "gatk"
        fake_gatk.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "command_name=''\n"
            "for arg in \"$@\"; do\n"
            "  [[ \"$arg\" == 'GatherVcfs' || \"$arg\" == 'IndexFeatureFile' ]] && command_name=\"$arg\"\n"
            "done\n"
            "if [[ \"$command_name\" == 'GatherVcfs' ]]; then\n"
            "  output=''\n"
            "  previous=''\n"
            "  for arg in \"$@\"; do\n"
            "    [[ \"$previous\" == '--OUTPUT' ]] && output=\"$arg\"\n"
            "    previous=\"$arg\"\n"
            "  done\n"
            "  [[ -n \"$output\" ]]\n"
            "  printf 'fake gathered vcf\\n' > \"$output\"\n"
            "elif [[ \"$command_name\" == 'IndexFeatureFile' ]]; then\n"
            "  input=''\n"
            "  previous=''\n"
            "  for arg in \"$@\"; do\n"
            "    [[ \"$previous\" == '-I' ]] && input=\"$arg\"\n"
            "    previous=\"$arg\"\n"
            "  done\n"
            "  [[ -s \"$input\" ]]\n"
            "  printf 'fake index\\n' > \"${input}.tbi\"\n"
            "else\n"
            "  exit 20\n"
            "fi\n"
        )
        fake_gatk.chmod(0o755)

        reference = remote_root / "ref.fa"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(">ref_chr1\nA\n>ref_chr2\nA\n")
        Path(str(reference) + ".fai").write_text("ref_chr1\t1\t0\t1\t2\nref_chr2\t1\t0\t1\t2\n")
        sample_map = remote_root / "sample_map.txt"
        sample_map.write_text("s1\ta\ns2\tb\ns3\tc\n")

        policy = {
            "schema_version": "1.0",
            "proposal_core_version": "0.1.1",
            "task": "test_task",
            "action": "gather_or_merge",
            "template_version": "scattered_joint_gather_v2",
            "authority": {
                "remote_reads_allowed": False,
                "remote_writes_allowed": False,
                "slurm_submission_allowed": False,
                "automatic_execution_allowed": False,
                "deletion_allowed": False,
                "knowledge_update_allowed": False,
            },
            "expected_intervals": 4,
            "expected_samples": 3,
            "chromosome_order": ["chr1", "chr2"],
            "reference_contig_map": {"chr1": "ref_chr1", "chr2": "ref_chr2"},
            "reference_fasta": str(reference),
            "sample_map": str(sample_map),
            "remote_output_root": str(output_root),
            "cluster_environment_init": str(environment_init),
            "output_prefix": "test_joint",
            "slurm_account": "project_1",
            "software": {"gatk_module": "gatk/4.5.0.0", "bcftools_module": "biokit"},
            "provisional_resources": {
                "gather_chromosomes": {"cpus_per_task": 2, "memory": "16G", "time_limit": "04:00:00"},
                "gather_genome": {"cpus_per_task": 2, "memory": "16G", "time_limit": "04:00:00"},
                "validate_final": {"cpus_per_task": 4, "memory": "16G", "time_limit": "04:00:00"},
            },
        }
        (policy_dir / "test_task_gather_or_merge.json").write_text(json.dumps(policy))
        state = {"overall_status": "scattered_genotyping_complete", "current_stage": "gather_or_merge"}
        (state_dir / "current_state.json").write_text(json.dumps(state))
        (state_dir / "provenance.json").write_text(json.dumps({"source_scans": 1}))

        interval_rows = [
            ("chr1:250001-500000", str(windows_root / "chr1/x_chr1_00250001_00500000.vcf.gz"), "part1", "2"),
            ("chr2:1-250000", str(windows_root / "chr2/x_chr2_00000001_00250000.vcf.gz"), "part2", "3"),
            ("chr1:1-250000", str(windows_root / "chr1/x_chr1_00000001_00250000.vcf.gz"), "part1", "1"),
            ("chr2:250001-500000", str(windows_root / "chr2/x_chr2_00250001_00500000.vcf.gz"), "part2", "4"),
        ]
        for _, vcf, _, _ in interval_rows:
            vcf_path = Path(vcf)
            vcf_path.parent.mkdir(parents=True, exist_ok=True)
            vcf_path.write_text("fake interval vcf\n")
            Path(vcf + ".tbi").write_text("fake interval index\n")

        with (scan_dir / "interval_status.tsv").open("w", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["interval", "status", "batch", "task", "vcf", "index"])
            for interval, vcf, batch, task in interval_rows:
                writer.writerow([interval, "completed", batch, task, vcf, vcf + ".tbi"])
        summary = {
            "sample_map_unique_samples": 3,
            "expected_samples_per_interval": 3,
            "expected_intervals": 4,
            "completed_atomic_publish_contract": 4,
            "without_published_output": 0,
            "running_intervals": 0,
            "queued_intervals": 0,
            "failed_needs_review": 0,
            "submitted_unresolved": 0,
            "not_submitted": 0,
            "scheduler_completed_output_missing": 0,
            "unmapped_intervals": 0,
            "vcf_present_index_missing": 0,
            "index_present_vcf_missing": 0,
            "ready_genomicsdb_workspaces": 2,
            "expected_genomicsdb_workspaces": 2,
            "active_scheduler_records": 0,
            "scheduler_failed_records_observed": 2,
            "final_vcf_pairs_detected": 0,
        }
        with (scan_dir / "scatter_summary.tsv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(summary), delimiter="\t")
            writer.writeheader(); writer.writerow(summary)
        task_scan = {
            "task": "test_task",
            "mode": "read_only",
            "overall_status": "scattered_genotyping_complete",
            "current_stage": "gather_or_merge",
        }
        task_scan_path = scan_dir / "task_scan.json"
        task_scan_path.write_text(json.dumps(task_scan))
        import hashlib
        digest = hashlib.sha256(task_scan_path.read_bytes()).hexdigest()
        recommendations = {
            "schema_version": "1.0",
            "task": "test_task",
            "source_scan_id": "20260715T160720Z",
            "source_task_scan_sha256": digest,
            "automatic_execution_allowed": False,
            "recommendations": [{"action": "review_gather_prerequisites"}],
        }
        (state_dir / "recommendations.json").write_text(json.dumps(recommendations))
        return ProposalCore(
            policy_root=policy_dir,
            task_state_root=root / "workspace/task_state",
            task_scan_root=root / "workspace/task_scans",
            proposal_root=root / "workspace/proposals",
        )

    def test_prepare_is_content_addressed_immutable_and_non_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = self.fixture(Path(tmp))
            first = core.prepare("test_task", "gather_or_merge")
            second = core.prepare("test_task", "gather_or_merge")
            self.assertEqual(first.proposal_id, second.proposal_id)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            proposal = json.loads((first.proposal_dir / "proposal.json").read_text())
            self.assertFalse(proposal["automatic_execution_allowed"])
            self.assertFalse(proposal["slurm_submission_allowed"])
            self.assertEqual(proposal["validated_interval_inputs"], 4)

    def test_manifest_uses_numeric_reference_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = self.fixture(Path(tmp))
            result = core.prepare("test_task", "gather_or_merge")
            with (result.proposal_dir / "interval_manifest.tsv").open() as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual([row["interval"] for row in rows], [
                "chr1:1-250000", "chr1:250001-500000",
                "chr2:1-250000", "chr2:250001-500000",
            ])

    def test_incomplete_summary_blocks_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); core = self.fixture(root)
            path = root / "workspace/task_scans/test_task/20260715T160720Z/scatter_summary.tsv"
            with path.open() as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            rows[0]["completed_atomic_publish_contract"] = "3"
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
                writer.writeheader(); writer.writerows(rows)
            with self.assertRaisesRegex(ProposalCoreError, "completed_atomic_publish_contract"):
                core.prepare("test_task", "gather_or_merge")

    def test_validate_detects_tampering_and_checks_shell_syntax(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = self.fixture(Path(tmp))
            result = core.prepare("test_task", "gather_or_merge")
            validation = core.validate(result.proposal_dir)
            self.assertEqual(validation["status"], "valid_for_researcher_review")
            script = result.proposal_dir / "scripts/01_gather_chromosomes.slurm"
            script.write_text(script.read_text() + "\n# changed\n")
            with self.assertRaisesRegex(ProposalCoreError, "checksum mismatch"):
                core.validate(result.proposal_dir)

    def test_validate_rejects_changed_canonical_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); core = self.fixture(root)
            result = core.prepare("test_task", "gather_or_merge")
            state_path = root / "workspace/task_state/test_task/current_state.json"
            state = json.loads(state_path.read_text())
            state["current_stage"] = "validated_complete"
            state_path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ProposalCoreError, "proposal is stale"):
                core.validate(result.proposal_dir)

    def test_generated_scripts_are_puhti_and_spool_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = self.fixture(Path(tmp))
            result = core.prepare("test_task", "gather_or_merge")
            script = (result.proposal_dir / "scripts/01_gather_chromosomes.slurm").read_text()
            self.assertIn("source \"", script)
            self.assertIn("zz-csc-env.sh", script)
            self.assertIn("GA_PROPOSAL_DIR", script)
            self.assertIn("sha256sum -c checksums.sha256 --quiet", script)
            self.assertIn("IndexFeatureFile", script)
            self.assertIn("--CREATE_INDEX false", script)
            self.assertNotIn("BASH_SOURCE", script)
            self.assertNotIn("mapfile", script)

    def test_spool_copy_uses_explicit_proposal_dir_and_publishes_indexed_vcf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self.fixture(root)
            result = core.prepare("test_task", "gather_or_merge")
            original = result.proposal_dir / "scripts/01_gather_chromosomes.slurm"
            spool_dir = root / "var/spool/slurmd/job123"
            spool_dir.mkdir(parents=True)
            spool_copy = spool_dir / "slurm_script"
            spool_copy.write_text(original.read_text())
            env = dict(os.environ)
            env.update({
                "GA_PROPOSAL_DIR": str(result.proposal_dir),
                "SLURM_ARRAY_TASK_ID": "1",
                "SLURM_JOB_ID": "123",
            })
            completed = subprocess.run(
                ["bash", str(spool_copy)], text=True, capture_output=True, env=env
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output_root = root / "remote/gathered/chromosomes"
            final_vcf = output_root / "test_joint.chr1.vcf.gz"
            self.assertTrue(final_vcf.is_file())
            self.assertTrue(Path(str(final_vcf) + ".tbi").is_file())

    def test_cli_prepare_and_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root)
            script = REPOSITORY_ROOT / "scripts/task_proposal.py"
            common = [
                "--policy-root", str(root / "config/proposals"),
                "--task-state-root", str(root / "workspace/task_state"),
                "--task-scan-root", str(root / "workspace/task_scans"),
                "--proposal-root", str(root / "workspace/proposals"),
            ]
            completed = subprocess.run(
                [sys.executable, str(script), "prepare", "test_task", "--action", "gather_or_merge", *common],
                text=True, capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Automatic execute : disabled", completed.stdout)


if __name__ == "__main__":
    unittest.main()
