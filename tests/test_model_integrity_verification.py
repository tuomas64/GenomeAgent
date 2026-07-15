from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from genomeagent.model_integrity_verification import (
    ModelIntegrityInventoryCollector,
    ModelIntegrityVerificationCore,
    ModelIntegrityVerificationError,
    _inventory_program,
    _verification_status_program,
    _verification_submission_program,
)
import tests.test_model_acquisition_preflight as preflight_support


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ID = preflight_support.BACKEND_ID
DOWNLOAD_EXECUTION_ID = "1" * 64


class DynamicRunner:
    def __init__(self, mode="inventory", host="roihu", mutate=None):
        self.mode = mode
        self.host = host
        self.mutate = mutate
        self.calls = []

    @staticmethod
    def config(program):
        match = re.search(r"CONFIG = json\.loads\((.+)\)\n", program)
        if not match:
            raise AssertionError("remote program has no encoded CONFIG")
        return json.loads(ast.literal_eval(match.group(1)))

    def run_python(self, program, timeout_seconds=240):
        self.calls.append((program, timeout_seconds))
        config = self.config(program)
        if self.mode == "inventory":
            files = copy.deepcopy(config["expected_inventory"])
            observation = {
                "schema_version": "1.0",
                "observation_type": "staged_model_inventory_preflight",
                "backend_id": config["backend_id"],
                "bundle_id": config["bundle_id"],
                "download_execution_id": config["download_execution_id"],
                "identity": {
                    "hostname": "roihu-cpu-login2",
                    "machine": "x86_64",
                    "python_executable": "/usr/bin/python3",
                    "python_version": "3.9.25",
                },
                "paths": {
                    "project_storage": {
                        "path": config["project_storage_root"],
                        "exists": True, "is_directory": True, "is_symlink": False
                    },
                    "staging": {
                        "path": config["staging_path"],
                        "exists": True, "is_directory": True, "is_symlink": False
                    },
                    "installation": {
                        "path": config["installation_path"], "lexists": False
                    },
                    "control_root": {"lexists": False},
                    "staging_chain_symlinks": [],
                    "installation_chain_symlinks": [],
                    "control_chain_symlinks": [],
                    "worker_python": {
                        "exists": True,
                        "is_file": False,
                        "is_symlink": True,
                        "is_file_followed": True,
                        "executable_by_access_check": True,
                        "resolved_path": "/usr/bin/python3.9",
                    },
                },
                "inventory": {
                    "files": files,
                    "file_count": len(files),
                    "total_bytes": sum(item["size_bytes"] for item in files),
                    "symlinks": [],
                    "special_entries": [],
                    "truncated": False,
                    "walk_error": None,
                    "download_cache": {
                        "present": True,
                        "regular_files": 2,
                        "directories": 2,
                        "bytes": 250,
                    },
                },
                "scheduler": {
                    "sbatch_path": "/usr/bin/sbatch",
                    "squeue_path": "/usr/bin/squeue",
                    "sacct_path": "/usr/bin/sacct",
                    "sinfo_path": "/usr/bin/sinfo",
                    "expected_partition": "small",
                    "partition_query": {
                        "returncode": 0,
                        "stdout": "small|up|3-00:00:00|120\n",
                        "stderr": "",
                        "timed_out": False,
                    },
                },
                "safety": {
                    "remote_writes": False,
                    "model_file_contents_read": False,
                    "large_file_hashing": False,
                    "job_submission": False,
                    "staging_mutation": False,
                    "publication": False,
                    "gpu_allocation": False,
                    "inference": False,
                    "training": False,
                },
            }
            if self.mutate:
                self.mutate(observation)
            return observation
        if self.mode == "launch":
            return {
                "schema_version": "1.0",
                "status": "verification_submitted",
                "verification_id": config["verification_id"],
                "job_id": "987654",
                "control_directory": config["control_directory"],
                "publication": False,
                "gpu_allocation": False,
            }
        return {
            "schema_version": "1.0",
            "observation_type": "model_integrity_verification_status",
            "backend_id": config["backend_id"],
            "bundle_id": config["bundle_id"],
            "verification_id": config["verification_id"],
            "control_directory": config["control_directory"],
            "control_exists": True,
            "control_is_symlink": False,
            "submission": {"job_id": "987654"},
            "worker_status": {"status": "verified_ready_for_publication_review"},
            "result": {
                "status": "verified_ready_for_publication_review",
                "verified_file_count": 2,
                "verified_total_bytes": 61_079_826_794,
                "publication": False,
                "gpu_allocation": False,
            },
            "scheduler": {"squeue": None, "sacct": None},
            "stdout_tail": "",
            "stderr_tail": "",
            "safety": {
                "remote_writes": False,
                "model_file_contents_read": False,
                "large_file_hashing": False,
                "job_submission": False,
                "staging_mutation": False,
                "publication": False,
                "gpu_allocation": False,
                "inference": False,
                "training": False,
            },
        }


class ModelIntegrityVerificationTests(unittest.TestCase):
    def setup_repository(self, root: Path, observed=None, mutate=None):
        helper = preflight_support.ModelAcquisitionPreflightTests()
        registry, backend, bundle_id, bundle = helper.setup_repository(root)
        policy_source = (
            REPOSITORY_ROOT
            / "config/ai/integrity_verification/roihu_qwen3_coder.json"
        )
        policy_target = root / "config/ai/integrity_verification/roihu_qwen3_coder.json"
        policy_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(policy_source, policy_target)
        download_dir = (
            root / "downloads" / BACKEND_ID / bundle_id / DOWNLOAD_EXECUTION_ID
        )
        status_dir = download_dir / "status"
        status_dir.mkdir(parents=True)
        launch = {
            "schema_version": "1.0",
            "source_mode": "explicit_authorized_remote_public_model_download_launch",
            "backend_id": BACKEND_ID,
            "bundle_id": bundle_id,
            "execution_id": DOWNLOAD_EXECUTION_ID,
            "model_hashing": False,
            "publication": False,
            "remote_result": {
                "staging_path": bundle["target"]["staging_path"],
                "status": "download_launched",
            },
        }
        (download_dir / "launch.json").write_text(json.dumps(launch) + "\n")
        completion = {
            "schema_version": "1.0",
            "source_mode": "bounded_read_only_download_status_observation",
            "backend_id": BACKEND_ID,
            "bundle_id": bundle_id,
            "execution_id": DOWNLOAD_EXECUTION_ID,
            "status": "download_completed_unverified",
            "publication": False,
            "large_file_hashing": False,
        }
        (status_dir / "20260715T120000Z.json").write_text(
            json.dumps(completion) + "\n"
        )
        collector = ModelIntegrityInventoryCollector(
            registry=registry,
            policy_root=root / "config/ai/integrity_verification",
            bundle_root=root / "bundles",
            plan_root=root / "plans",
            download_execution_root=root / "downloads",
            evidence_root=root / "evidence",
        )
        core = ModelIntegrityVerificationCore(
            registry=registry,
            policy_root=root / "config/ai/integrity_verification",
            bundle_root=root / "bundles",
            plan_root=root / "plans",
            download_execution_root=root / "downloads",
            evidence_root=root / "evidence",
            state_root=root / "state",
            authorization_root=root / "authorizations",
            verification_root=root / "verifications",
        )
        observed = observed or datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        result = collector.collect(
            BACKEND_ID,
            bundle_id,
            DOWNLOAD_EXECUTION_ID,
            DynamicRunner(mutate=mutate),
            stamp="20260715T120000Z",
            observed_at=observed,
        )
        core.ingest(BACKEND_ID, bundle_id, now=observed)
        return registry, backend, bundle_id, bundle, collector, core, result, observed

    def authorize_ready(self, root: Path):
        values = self.setup_repository(root)
        _, _, bundle_id, _, _, core, result, observed = values
        authorization = core.authorize(
            BACKEND_ID,
            bundle_id,
            DOWNLOAD_EXECUTION_ID,
            result.evidence_id,
            "tuomas64",
            True,
            now=observed,
        )
        return (*values, authorization)

    def test_repository_policy_is_serial_cpu_only_and_forbids_publication(self):
        policy = json.loads((
            REPOSITORY_ROOT
            / "config/ai/integrity_verification/roihu_qwen3_coder.json"
        ).read_text())
        self.assertEqual(policy["ssh_host"], "roihu")
        self.assertEqual(policy["slurm"]["partition"], "small")
        self.assertEqual(policy["slurm"]["cpus_per_task"], 1)
        self.assertNotIn("gpu", policy["slurm"])
        self.assertTrue(all(value is False for value in policy["safety"].values()))

    def test_inventory_program_is_bounded_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, bundle_id, bundle, _, core, _, _ = self.setup_repository(root)
            _, policy, _ = core.policy(BACKEND_ID)
            inventory = bundle["approved_source_inventory"]
            program = _inventory_program(
                BACKEND_ID, bundle_id, DOWNLOAD_EXECUTION_ID, bundle, policy, inventory
            )
            compile(program, "<inventory>", "exec")
            self.assertIn("os.walk", program)
            self.assertNotIn("hashlib", program)
            self.assertNotIn("bounded_run([sbatch", program)
            self.assertNotIn("open(\"rb\"", program)

    def test_exact_inventory_collects_and_ingests_ready_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.setup_repository(Path(tmp))
            _, _, _, _, _, core, result, observed = values
            self.assertEqual(
                result.status, "ready_for_hash_verification_authorization_review"
            )
            state = core.ingest(BACKEND_ID, result.bundle_id, now=observed)
            self.assertEqual(
                state.blockers, ("fresh_hash_verification_authorization_missing",)
            )
            evidence = json.loads(result.evidence_path.read_text())
            self.assertEqual(
                evidence["inventory_summary"]["download_cache"]["regular_files"], 2
            )

    def test_inventory_mismatch_blocks_hash_authorization(self):
        def mutate(observation):
            observation["inventory"]["files"][0]["size_bytes"] += 1

        with tempfile.TemporaryDirectory() as tmp:
            values = self.setup_repository(Path(tmp), mutate=mutate)
            _, _, bundle_id, _, _, core, result, observed = values
            self.assertEqual(result.status, "staging_inventory_attention_required")
            with self.assertRaisesRegex(
                ModelIntegrityVerificationError, "not hash-authorization-ready"
            ):
                core.authorize(
                    BACKEND_ID,
                    bundle_id,
                    DOWNLOAD_EXECUTION_ID,
                    result.evidence_id,
                    "tuomas64",
                    True,
                    now=observed,
                )

    def test_authorization_requires_confirmation_and_binds_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.setup_repository(Path(tmp))
            _, _, bundle_id, _, _, core, result, observed = values
            with self.assertRaisesRegex(
                ModelIntegrityVerificationError,
                "confirm-staging-hash-verification",
            ):
                core.authorize(
                    BACKEND_ID,
                    bundle_id,
                    DOWNLOAD_EXECUTION_ID,
                    result.evidence_id,
                    "tuomas64",
                    False,
                    now=observed,
                )
            authorization = core.authorize(
                BACKEND_ID,
                bundle_id,
                DOWNLOAD_EXECUTION_ID,
                result.evidence_id,
                "tuomas64",
                True,
                now=observed,
            )
            record = json.loads(authorization.authorization_path.read_text())
            self.assertTrue(record["source_artifact_sha256"])
            self.assertEqual(record["slurm"]["partition"], "small")
            self.assertTrue(all(value is False for value in record["safety"].values()))

    def test_derived_state_cannot_extend_direct_inventory_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            values = self.setup_repository(root)
            _, _, bundle_id, _, _, core, result, observed = values
            state_path = root / "state" / BACKEND_ID / bundle_id / "current_evidence.json"
            state = json.loads(state_path.read_text())
            state["latest_expires_at_utc"] = (
                observed + timedelta(hours=3)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            state_path.write_text(json.dumps(state) + "\n")
            with self.assertRaisesRegex(
                ModelIntegrityVerificationError, "expired or not authorization-ready"
            ):
                core.authorize(
                    BACKEND_ID,
                    bundle_id,
                    DOWNLOAD_EXECUTION_ID,
                    result.evidence_id,
                    "tuomas64",
                    True,
                    now=observed + timedelta(minutes=31),
                )

    def test_submission_worker_hashes_exact_files_without_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.authorize_ready(Path(tmp))
            _, _, bundle_id, bundle, _, core, _, _, authorization = values
            _, policy, _ = core.policy(BACKEND_ID)
            record = json.loads(authorization.authorization_path.read_text())
            program = _verification_submission_program(
                BACKEND_ID,
                bundle_id,
                "9" * 64,
                bundle,
                bundle["approved_source_inventory"],
                policy,
                record,
            )
            compile(program, "<verification-submission>", "exec")
            self.assertIn("hashlib.sha256", program)
            self.assertIn("O_NOFOLLOW", program)
            self.assertIn('"sbatch"', program)
            self.assertIn("manifest_candidate.json", program)
            self.assertNotIn("os.rename", program)
            self.assertNotIn("shutil.rmtree", program)
            self.assertNotIn("--gres", program)

    def test_generated_worker_verifies_real_fixture_without_touching_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "models/.staging"
            staging.mkdir(parents=True)
            files = {"config.json": b"configuration\n", "model.bin": b"weights\x00\x01"}
            for name, payload in files.items():
                (staging / name).write_bytes(payload)
            cache = staging / ".cache/huggingface"
            cache.mkdir(parents=True)
            (cache / "metadata").write_text("transfer metadata\n")
            before = {
                name: (staging / name).read_bytes()
                for name in files
            }
            inventory = [
                {
                    "path": name,
                    "size_bytes": len(payload),
                    "provider_lfs_sha256": (
                        hashlib.sha256(payload).hexdigest()
                        if name == "model.bin"
                        else None
                    ),
                    "provider_git_blob_id": None,
                    "provider_xet_hash": None,
                }
                for name, payload in sorted(files.items())
            ]
            policy = json.loads((
                REPOSITORY_ROOT
                / "config/ai/integrity_verification/roihu_qwen3_coder.json"
            ).read_text())
            policy["control_root"] = str(root / ".genomeagent/verifications")
            bundle = {
                "repository": "example/model",
                "resolved_revision": "b" * 40,
                "target": {
                    "project_storage_root": str(root),
                    "staging_path": str(staging),
                    "installation_path": str(root / "models/final"),
                },
            }
            verification_id = "9" * 64
            program = _verification_submission_program(
                BACKEND_ID,
                "8" * 64,
                verification_id,
                bundle,
                inventory,
                policy,
                {"authorization_id": "7" * 64},
            )
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fake_sbatch = binary_dir / "sbatch"
            fake_sbatch.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess, sys\n"
                "subprocess.run([sys.argv[-1]], check=False)\n"
                "print('987654')\n"
            )
            fake_sbatch.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = str(binary_dir) + os.pathsep + environment["PATH"]
            completed = subprocess.run(
                [sys.executable, "-c", program],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            control = Path(policy["control_root"]) / verification_id
            result = json.loads((control / "result.json").read_text())
            manifest = json.loads((control / "manifest_candidate.json").read_text())
            self.assertEqual(result["status"], "verified_ready_for_publication_review")
            self.assertEqual(manifest["file_count"], 2)
            self.assertFalse(manifest["publication_performed"])
            self.assertEqual(
                {name: (staging / name).read_bytes() for name in files}, before
            )
            self.assertFalse((root / "models/final").exists())

    def test_launch_is_explicit_idempotent_and_blocks_changed_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            values = self.authorize_ready(root)
            _, _, bundle_id, _, _, core, _, observed, authorization = values
            with self.assertRaisesRegex(
                ModelIntegrityVerificationError, "confirm-submit-hash-verification"
            ):
                core.launch(
                    BACKEND_ID,
                    bundle_id,
                    authorization.authorization_id,
                    DynamicRunner(mode="launch"),
                    False,
                    now=observed,
                )
            runner = DynamicRunner(mode="launch")
            launched = core.launch(
                BACKEND_ID,
                bundle_id,
                authorization.authorization_id,
                runner,
                True,
                now=observed,
            )
            self.assertEqual(launched.job_id, "987654")
            repeated = core.launch(
                BACKEND_ID,
                bundle_id,
                authorization.authorization_id,
                runner,
                True,
                now=observed,
            )
            self.assertFalse(repeated.created)
            self.assertEqual(len(runner.calls), 1)
            policy_path = root / "config/ai/integrity_verification/roihu_qwen3_coder.json"
            policy = json.loads(policy_path.read_text())
            policy["limits"]["authorization_validity_seconds"] = 601
            policy_path.write_text(json.dumps(policy) + "\n")
            local_launch = launched.launch_path
            local_launch.unlink()
            with self.assertRaisesRegex(ModelIntegrityVerificationError, "changed"):
                core.launch(
                    BACKEND_ID,
                    bundle_id,
                    authorization.authorization_id,
                    DynamicRunner(mode="launch"),
                    True,
                    now=observed,
                )

    def test_status_is_read_only_and_preserves_publication_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.authorize_ready(Path(tmp))
            _, _, bundle_id, _, _, core, _, observed, authorization = values
            core.launch(
                BACKEND_ID,
                bundle_id,
                authorization.authorization_id,
                DynamicRunner(mode="launch"),
                True,
                now=observed,
            )
            runner = DynamicRunner(mode="status")
            result = core.status(
                BACKEND_ID,
                bundle_id,
                authorization.authorization_id,
                runner,
                stamp="20260715T121500Z",
            )
            self.assertEqual(result.status, "verified_ready_for_publication_review")
            evidence = json.loads(result.observation_path.read_text())
            self.assertFalse(evidence["publication"])
            status_program = runner.calls[0][0]
            compile(status_program, "<status>", "exec")
            self.assertNotIn("hashlib", status_program)
            self.assertNotIn("sbatch", status_program)

    def test_cli_collect_describes_read_only_boundary(self):
        completed = subprocess.run(
            [sys.executable, "scripts/model_integrity_verification.py", "--help"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Verify a downloaded model without publishing", completed.stdout)


if __name__ == "__main__":
    unittest.main()
