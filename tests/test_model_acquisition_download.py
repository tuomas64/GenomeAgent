from __future__ import annotations

import ast
import copy
import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from genomeagent.model_acquisition_download import (
    ControlledModelDownloadCore,
    ModelAcquisitionDownloadError,
    _launcher_program,
)
import tests.test_model_acquisition_preflight as preflight_support


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ID = preflight_support.BACKEND_ID


class DynamicRunner:
    def __init__(self, mode="launch", host="roihu-gpu"):
        self.mode = mode
        self.host = host
        self.calls = []

    def config(self, program):
        match = re.search(r"CONFIG = json\.loads\((.+)\)\n", program)
        if not match:
            raise AssertionError("program has no encoded CONFIG")
        return json.loads(ast.literal_eval(match.group(1)))

    def run_python(self, program, timeout_seconds=240):
        self.calls.append((program, timeout_seconds))
        config = self.config(program)
        if self.mode == "launch":
            return {
                "schema_version": "1.0",
                "status": "download_launched",
                "execution_id": config["execution_id"],
                "pid": 12345,
                "control_directory": config["control_directory"],
                "staging_path": config["staging_path"],
                "installation_path": config["installation_path"],
                "model_hashing": False,
                "publication": False,
                "job_submission": False,
                "gpu_allocation": False,
            }
        return {
            "schema_version": "1.0",
            "observation_type": "model_download_status",
            "backend_id": config["backend_id"],
            "bundle_id": config["bundle_id"],
            "execution_id": config["execution_id"],
            "control_directory": config["control_directory"],
            "control_exists": True,
            "control_is_symlink": False,
            "execution": {"execution_id": config["execution_id"]},
            "launch": {"status": "download_launched"},
            "worker_status": {
                "status": "download_completed_unverified",
                "execution_id": config["execution_id"],
                "model_hashing": False,
                "publication": False,
            },
            "log_tail": "download complete",
            "safety": {
                "remote_writes": False,
                "provider_requests": False,
                "model_download_started": False,
                "job_submission": False,
                "gpu_allocation": False,
                "model_file_reads": False,
                "large_file_hashing": False,
                "publication": False,
            },
        }


class ModelAcquisitionDownloadTests(unittest.TestCase):
    def setup_ready(self, root: Path, observed=None):
        helper = preflight_support.ModelAcquisitionPreflightTests()
        registry, backend, bundle_id, bundle = helper.setup_repository(root)
        collector, preflight = helper.components(root, registry)
        observed = observed or datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        evidence_id = "20260715T120000Z"
        collector.collect(
            BACKEND_ID,
            bundle_id,
            type("Runner", (), {
                "host": "roihu-gpu",
                "run_python": lambda self, program, timeout_seconds=240: copy.deepcopy(
                    helper.observation(bundle_id, bundle)
                ),
            })(),
            stamp=evidence_id,
            observed_at=observed,
        )
        preflight.ingest(BACKEND_ID, bundle_id, now=observed)
        core = ControlledModelDownloadCore(
            registry=registry,
            policy_root=root / "config/ai/acquisition_execution",
            bundle_root=root / "bundles",
            plan_root=root / "plans",
            preflight_evidence_root=root / "evidence",
            preflight_state_root=root / "state",
            authorization_root=root / "authorizations",
            execution_root=root / "downloads",
        )
        return helper, registry, backend, bundle_id, bundle, evidence_id, core, observed

    def test_policy_allows_only_staging_download_and_no_credentials_or_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _, _, _, _, core, _ = self.setup_ready(root)
            _, policy, _ = core.policy(BACKEND_ID)
            self.assertTrue(policy["download"]["staging_only"])
            self.assertFalse(policy["download"]["publish_after_download"])
            self.assertEqual(policy["download"]["max_workers"], 2)
            self.assertTrue(all(value is False for value in policy["safety"].values()))
            self.assertIn(
                "create_confined_model_parent_directories",
                policy["allowed_remote_mutations"],
            )

    def test_authorization_requires_confirmation_and_binds_every_current_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _, bundle_id, _, evidence_id, core, observed = self.setup_ready(root)
            with self.assertRaisesRegex(ModelAcquisitionDownloadError, "confirm-public-model-download"):
                core.authorize(BACKEND_ID, bundle_id, evidence_id, "tuomas64", False, now=observed)
            result = core.authorize(
                BACKEND_ID, bundle_id, evidence_id, "tuomas64", True, now=observed
            )
            approval = json.loads(result.authorization_path.read_text())
            self.assertEqual(
                approval["authorization_scope"],
                "launch_exact_public_revision_into_bundle_staging_only",
            )
            self.assertTrue(approval["source_artifact_sha256"])
            self.assertTrue(all(value is False for value in approval["safety"].values()))
            again = core.authorize(
                BACKEND_ID, bundle_id, evidence_id, "tuomas64", True, now=observed
            )
            self.assertFalse(again.created)
            self.assertEqual(result.authorization_id, again.authorization_id)

    def test_authorization_or_preflight_expiry_blocks_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _, bundle_id, _, evidence_id, core, observed = self.setup_ready(root)
            approval = core.authorize(
                BACKEND_ID, bundle_id, evidence_id, "tuomas64", True, now=observed
            )
            with self.assertRaisesRegex(ModelAcquisitionDownloadError, "expired"):
                core.launch(
                    BACKEND_ID,
                    bundle_id,
                    approval.authorization_id,
                    DynamicRunner(),
                    True,
                    now=observed + timedelta(minutes=11),
                )

    def test_derived_state_cannot_extend_immutable_preflight_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _, bundle_id, _, evidence_id, core, observed = self.setup_ready(root)
            state_dir = root / "state" / BACKEND_ID / bundle_id
            state_path = state_dir / "current_evidence.json"
            state = json.loads(state_path.read_text())
            state["latest_expires_at_utc"] = (
                observed + timedelta(hours=2)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            state_path.write_text(json.dumps(state) + "\n")
            with self.assertRaisesRegex(
                ModelAcquisitionDownloadError,
                "expired or not authorization-ready",
            ):
                core.authorize(
                    BACKEND_ID,
                    bundle_id,
                    evidence_id,
                    "tuomas64",
                    True,
                    now=observed + timedelta(minutes=31),
                )

    def test_launcher_is_confined_resumable_staging_only_and_contains_no_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, backend, bundle_id, bundle, evidence_id, core, observed = self.setup_ready(root)
            approval = core.authorize(
                BACKEND_ID, bundle_id, evidence_id, "tuomas64", True, now=observed
            )
            _, policy, _ = core.policy(BACKEND_ID)
            authorization = json.loads(approval.authorization_path.read_text())
            program = _launcher_program(backend, policy, {
                **bundle,
                "bundle_id": bundle_id,
            }, authorization, "d" * 64)
            compile(program, "<remote-download-launcher>", "exec")
            self.assertIn("token=False", program)
            self.assertIn('max_workers=int(spec["max_workers"])', program)
            self.assertIn("start_new_session=True", program)
            self.assertIn("ensure_directory", program)
            self.assertNotIn("sha256", program.lower())
            self.assertNotIn("sbatch", program)
            self.assertNotIn("srun", program)
            self.assertNotIn("os.rename", program)
            self.assertNotIn("shutil.rmtree", program)

    def test_explicit_launch_writes_local_evidence_but_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _, bundle_id, _, evidence_id, core, observed = self.setup_ready(root)
            approval = core.authorize(
                BACKEND_ID, bundle_id, evidence_id, "tuomas64", True, now=observed
            )
            with self.assertRaisesRegex(ModelAcquisitionDownloadError, "confirm-execute-approved-download"):
                core.launch(
                    BACKEND_ID, bundle_id, approval.authorization_id,
                    DynamicRunner(), False, now=observed,
                )
            runner = DynamicRunner()
            result = core.launch(
                BACKEND_ID, bundle_id, approval.authorization_id,
                runner, True, now=observed,
            )
            self.assertEqual(result.remote_status, "download_launched")
            evidence = json.loads(result.launch_path.read_text())
            self.assertFalse(evidence["publication"])
            self.assertFalse(evidence["job_submission"])
            self.assertFalse(evidence["gpu_allocation"])
            self.assertEqual(len(runner.calls), 1)
            repeat = core.launch(
                BACKEND_ID, bundle_id, approval.authorization_id,
                runner, True, now=observed,
            )
            self.assertFalse(repeat.created)
            self.assertEqual(len(runner.calls), 1)

    def test_changed_policy_after_authorization_blocks_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _, bundle_id, _, evidence_id, core, observed = self.setup_ready(root)
            approval = core.authorize(
                BACKEND_ID, bundle_id, evidence_id, "tuomas64", True, now=observed
            )
            path = root / "config/ai/acquisition_execution/roihu_qwen3_coder.json"
            policy = json.loads(path.read_text())
            policy["authorization_validity_seconds"] = 601
            path.write_text(json.dumps(policy) + "\n")
            with self.assertRaisesRegex(ModelAcquisitionDownloadError, "inputs changed"):
                core.launch(
                    BACKEND_ID, bundle_id, approval.authorization_id,
                    DynamicRunner(), True, now=observed,
                )

    def test_status_is_read_only_and_records_unverified_download_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, _, bundle_id, _, evidence_id, core, observed = self.setup_ready(root)
            approval = core.authorize(
                BACKEND_ID, bundle_id, evidence_id, "tuomas64", True, now=observed
            )
            runner = DynamicRunner(mode="status")
            result = core.status(
                BACKEND_ID, bundle_id, approval.authorization_id, runner,
                stamp="20260715T121500Z",
            )
            self.assertEqual(result.status, "download_completed_unverified")
            evidence = json.loads(result.observation_path.read_text())
            self.assertFalse(evidence["remote_writes"])
            self.assertFalse(evidence["large_file_hashing"])
            self.assertFalse(evidence["publication"])
            self.assertNotIn("open(\"wb\"", runner.calls[0][0])

    def test_cli_authorization_still_reports_hashing_and_gpu_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, registry, _, bundle_id, _, evidence_id, _, observed = self.setup_ready(
                root, observed=datetime.now(timezone.utc)
            )
            command = [
                sys.executable, "scripts/model_acquisition_download.py", "authorize",
                BACKEND_ID, "--bundle-id", bundle_id,
                "--preflight-evidence-id", evidence_id,
                "--reviewer", "tuomas64", "--confirm-public-model-download",
                "--backend-root", str(root / "config/ai/backends"),
                "--prompt-root", str(root / "config/ai/prompts"),
                "--suite-root", str(root / "config/ai/suites"),
                "--case-root", str(root / "benchmarks/ai"),
                "--policy-root", str(root / "config/ai/acquisition_execution"),
                "--bundle-root", str(root / "bundles"),
                "--plan-root", str(root / "plans"),
                "--preflight-evidence-root", str(root / "evidence"),
                "--preflight-state-root", str(root / "state"),
                "--authorization-root", str(root / "cli-authorizations"),
                "--execution-root", str(root / "downloads"),
            ]
            completed = subprocess.run(
                command, cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Hashing/publication     : forbidden", completed.stdout)
            self.assertIn("Slurm/GPU/inference     : forbidden", completed.stdout)


if __name__ == "__main__":
    unittest.main()
