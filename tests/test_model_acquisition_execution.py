from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from genomeagent.ai_evaluation import AIRegistry
from genomeagent.model_acquisition import ModelAcquisitionPlanner
from genomeagent.model_acquisition_execution import (
    ControlledModelAcquisitionCore,
    ModelAcquisitionExecutionError,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ID = "roihu_qwen3_coder"
EVIDENCE_ID = "20260715T100000000000Z"
REVISION = "a" * 40


def canonical_sha256(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


class ModelAcquisitionExecutionTests(unittest.TestCase):
    def copied_repository(self, root: Path) -> AIRegistry:
        shutil.copytree(REPOSITORY_ROOT / "config/ai", root / "config/ai")
        shutil.copytree(REPOSITORY_ROOT / "benchmarks/ai", root / "benchmarks/ai")
        path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
        specification = json.loads(path.read_text())
        specification["source"].update({
            "resolved_revision": None,
            "source_inventory_sha256": None,
            "source_total_bytes": None,
            "license_identifier": "Apache-2.0",
            "license_review_status": "unreviewed",
        })
        specification["source"].pop("license_approval", None)
        path.write_text(json.dumps(specification) + "\n", encoding="utf-8")
        return AIRegistry(
            backend_root=root / "config/ai/backends",
            prompt_root=root / "config/ai/prompts",
            suite_root=root / "config/ai/suites",
            case_root=root / "benchmarks/ai",
        )

    def write_environment_state(self, root: Path) -> None:
        backend_path = root / "config/ai/backends/roihu_qwen3_coder.json"
        state_dir = root / "backend-state/roihu_qwen3_coder"
        state_dir.mkdir(parents=True)
        state = {
            "schema_version": "1.0",
            "backend_id": BACKEND_ID,
            "automatic_execution_allowed": False,
            "automatic_registry_update_allowed": False,
            "readiness": {
                "status": "model_identity_and_acquisition_required",
                "environment_blockers": [],
            },
            "observation": {
                "storage": {
                    "workspace_command": {
                        "returncode": 0,
                        "stdout": "/scratch/project_2001113 4.0K/250G 1/500K 180d\n",
                    }
                }
            },
        }
        provenance = {
            "schema_version": "1.0",
            "backend_id": BACKEND_ID,
            "source_artifacts": [{
                "artifact_type": "ai_backend_configuration",
                "path": str(backend_path),
                "sha256": hashlib.sha256(backend_path.read_bytes()).hexdigest(),
            }],
        }
        (state_dir / "current_evidence.json").write_text(
            json.dumps(state) + "\n", encoding="utf-8"
        )
        (state_dir / "provenance.json").write_text(
            json.dumps(provenance) + "\n", encoding="utf-8"
        )

    def write_approved_source(self, root: Path) -> Path:
        files = [
            {
                "path": "LICENSE",
                "size_bytes": 19_000,
                "blob_id": "1" * 40,
                "lfs_sha256": None,
                "lfs_size_bytes": None,
                "xet_hash": None,
                "is_weight_file": False,
            },
            {
                "path": "config.json",
                "size_bytes": 1_000,
                "blob_id": "2" * 40,
                "lfs_sha256": None,
                "lfs_size_bytes": None,
                "xet_hash": None,
                "is_weight_file": False,
            },
            {
                "path": "model.safetensors",
                "size_bytes": 60_000_000_000,
                "blob_id": "3" * 40,
                "lfs_sha256": "c" * 64,
                "lfs_size_bytes": 60_000_000_000,
                "xet_hash": None,
                "is_weight_file": True,
            },
        ]
        total = sum(item["size_bytes"] for item in files)
        metadata = {
            "repository": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "resolved_revision": REVISION,
            "private": False,
            "gated": False,
            "files": files,
            "file_count": len(files),
            "files_missing_size": [],
            "source_total_bytes": total,
            "source_inventory_sha256": canonical_sha256(files),
            "provider_reported_used_storage_bytes": total,
            "weight_file_count": 1,
            "weight_files_with_provider_lfs_sha256": 1,
            "license_identifier": "Apache-2.0",
            "license_files": ["LICENSE"],
            "limitations": [],
        }
        snapshot = {
            "schema_version": "1.0",
            "policy_version": "1.0",
            "source_mode": "bounded_public_metadata_queries",
            "backend_id": BACKEND_ID,
            "evidence_id": EVIDENCE_ID,
            "normalized_metadata": metadata,
            "readiness": {
                "status": "source_metadata_ready_for_researcher_review",
                "blockers": [],
            },
            "safety": {
                "public_metadata_queries": True,
                "repository_file_download": False,
                "model_weight_download": False,
                "remote_cluster_access": False,
                "remote_cluster_writes": False,
                "job_submission": False,
                "gpu_allocation": False,
                "large_file_hashing": False,
                "automatic_specification_update": False,
                "automatic_registry_update": False,
                "automatic_backend_activation": False,
            },
        }
        evidence_dir = root / "source-evidence/roihu_qwen3_coder"
        evidence_dir.mkdir(parents=True)
        evidence_path = evidence_dir / (EVIDENCE_ID + ".json")
        evidence_path.write_text(json.dumps(snapshot) + "\n", encoding="utf-8")
        evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

        spec_path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
        spec = json.loads(spec_path.read_text())
        approved_values = {
            "resolved_revision": REVISION,
            "source_inventory_sha256": metadata["source_inventory_sha256"],
            "source_total_bytes": total,
            "license_identifier": "Apache-2.0",
            "license_review_status": "reviewed_accepted",
        }
        review_url = (
            "https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct/"
            "blob/{}/LICENSE".format(REVISION)
        )
        approval_identity = {
            "policy_version": "1.0",
            "backend_id": BACKEND_ID,
            "source_evidence_id": EVIDENCE_ID,
            "source_evidence_sha256": evidence_sha256,
            "reviewer": "test_researcher",
            "accepted_license_identifier": "Apache-2.0",
            "review_url": review_url,
            "approved_source_values": approved_values,
        }
        approval_id = canonical_sha256(approval_identity)
        spec["source"].update(approved_values)
        spec["source"]["license_approval"] = {
            "schema_version": "1.0",
            "approval_id": approval_id,
            "source_evidence_id": EVIDENCE_ID,
            "source_evidence_sha256": evidence_sha256,
            "reviewer": "test_researcher",
            "accepted_at_utc": "2026-07-15T10:00:00Z",
            "license_identifier": "Apache-2.0",
            "review_url": review_url,
            "resolved_revision": REVISION,
        }
        spec_path.write_text(json.dumps(spec) + "\n", encoding="utf-8")
        return evidence_path

    def setup_ready(self, root: Path):
        registry = self.copied_repository(root)
        evidence_path = self.write_approved_source(root)
        self.write_environment_state(root)
        planner = ModelAcquisitionPlanner(
            registry=registry,
            specification_root=root / "config/ai/acquisition",
            backend_state_root=root / "backend-state",
            plan_root=root / "plans",
        )
        plan = planner.plan(BACKEND_ID)
        self.assertEqual(plan.status, "ready_for_researcher_acquisition_review")
        core = ControlledModelAcquisitionCore(
            registry=registry,
            specification_root=root / "config/ai/acquisition",
            backend_state_root=root / "backend-state",
            plan_root=root / "plans",
            source_evidence_root=root / "source-evidence",
            approval_root=root / "approvals",
            bundle_root=root / "bundles",
        )
        return registry, plan, core, evidence_path

    def test_plan_v1_1_expresses_truthful_provider_digest_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, plan, _, _ = self.setup_ready(root)
            integrity = json.loads(
                (plan.plan_dir / "integrity_plan.json").read_text()
            )
            self.assertEqual(integrity["policy_version"], "1.1")
            self.assertIn(
                "available_provider_sha256_digests_match_downloaded_files",
                integrity["pre_publication_requirements"],
            )
            self.assertNotIn(
                "every_regular_file_sha256_matches_approved_inventory",
                integrity["pre_publication_requirements"],
            )
            self.assertEqual(
                integrity["provider_digest_boundary"]["git_blob_id"],
                "provenance_only_not_raw_file_sha256",
            )

    def test_approval_requires_confirmation_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, plan, core, _ = self.setup_ready(root)
            with self.assertRaisesRegex(
                ModelAcquisitionExecutionError, "confirm-execution-preparation"
            ):
                core.approve(BACKEND_ID, plan.plan_id, "tuomas64", False)
            approved_at = datetime(2026, 7, 15, 10, 30, tzinfo=timezone.utc)
            first = core.approve(
                BACKEND_ID, plan.plan_id, "tuomas64", True, approved_at=approved_at
            )
            second = core.approve(
                BACKEND_ID, plan.plan_id, "tuomas64", True
            )
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.approval_id, second.approval_id)
            approval = json.loads(first.approval_path.read_text())
            self.assertEqual(
                approval["authorization_scope"],
                "prepare_controlled_acquisition_bundle_only",
            )
            self.assertTrue(all(value is False for value in approval["safety"].values()))
            approval["approved_at_utc"] = "2026-07-15T11:00:00Z"
            first.approval_path.write_text(json.dumps(approval) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ModelAcquisitionExecutionError, "record digest mismatch"
            ):
                core.prepare(BACKEND_ID, plan.plan_id, first.approval_id)

    def test_old_or_tampered_plan_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, plan, core, _ = self.setup_ready(root)
            with self.assertRaisesRegex(
                ModelAcquisitionExecutionError, "not the current plan"
            ):
                core.approve(BACKEND_ID, "0" * 64, "tuomas64", True)
            readiness_path = plan.plan_dir / "execution_readiness.json"
            readiness = json.loads(readiness_path.read_text())
            readiness["automatic_execution_allowed"] = True
            readiness_path.write_text(json.dumps(readiness) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ModelAcquisitionExecutionError, "different content|disable"
            ):
                core.approve(BACKEND_ID, plan.plan_id, "tuomas64", True)

    def test_prepared_bundle_is_data_only_and_preserves_integrity_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, plan, core, _ = self.setup_ready(root)
            approval = core.approve(BACKEND_ID, plan.plan_id, "tuomas64", True)
            result = core.prepare(BACKEND_ID, plan.plan_id, approval.approval_id)
            self.assertEqual(result.status, "environment_execution_preflight_required")
            self.assertIn("acquisition_runtime_unverified", result.blockers)
            bundle = json.loads((result.bundle_dir / "acquisition_bundle.json").read_text())
            integrity = bundle["integrity_contract"]
            self.assertEqual(integrity["expected_file_count"], 3)
            self.assertEqual(integrity["provider_sha256_file_count"], 1)
            self.assertEqual(integrity["provider_sha256_missing_file_count"], 2)
            self.assertEqual(integrity["local_sha256_required_file_count"], 3)
            self.assertFalse(bundle["execution_contract"]["remote_worker_generated"])
            rendered = json.dumps(bundle)
            self.assertNotIn("snapshot_download", rendered)
            self.assertNotIn("subprocess", rendered)
            self.assertNotIn("sbatch", rendered)
            self.assertTrue(all(
                value is False for key, value in bundle.items()
                if key.startswith("automatic_")
            ))

    def test_changed_source_evidence_blocks_bundle_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, plan, core, evidence_path = self.setup_ready(root)
            approval = core.approve(BACKEND_ID, plan.plan_id, "tuomas64", True)
            evidence = json.loads(evidence_path.read_text())
            evidence["normalized_metadata"]["files"][0]["size_bytes"] += 1
            evidence_path.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ModelAcquisitionExecutionError, "evidence digest mismatch"
            ):
                core.prepare(BACKEND_ID, plan.plan_id, approval.approval_id)

    def test_cli_approval_and_prepare_keep_execution_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, plan, _, _ = self.setup_ready(root)
            shared = [
                BACKEND_ID,
                "--plan-id", plan.plan_id,
                "--backend-root", str(root / "config/ai/backends"),
                "--prompt-root", str(root / "config/ai/prompts"),
                "--suite-root", str(root / "config/ai/suites"),
                "--case-root", str(root / "benchmarks/ai"),
                "--specification-root", str(root / "config/ai/acquisition"),
                "--backend-state-root", str(root / "backend-state"),
                "--plan-root", str(root / "plans"),
                "--source-evidence-root", str(root / "source-evidence"),
                "--approval-root", str(root / "approvals"),
                "--bundle-root", str(root / "bundles"),
            ]
            approve = subprocess.run(
                [
                    sys.executable,
                    "scripts/model_acquisition_control.py",
                    "approve",
                    *shared,
                    "--reviewer", "tuomas64",
                    "--confirm-execution-preparation",
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(approve.returncode, 0, approve.stderr)
            self.assertIn("Model download       : disabled", approve.stdout)
            approval_id = next(
                line.split(":", 1)[1].strip()
                for line in approve.stdout.splitlines()
                if line.startswith("Approval ID")
            )
            prepare = subprocess.run(
                [
                    sys.executable,
                    "scripts/model_acquisition_control.py",
                    "prepare",
                    *shared,
                    "--approval-id", approval_id,
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(prepare.returncode, 0, prepare.stderr)
            self.assertIn("Remote access/writes : disabled", prepare.stdout)
            self.assertIn("environment_execution_preflight_required", prepare.stdout)


if __name__ == "__main__":
    unittest.main()
