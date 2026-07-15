from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from genomeagent.ai_evaluation import AIRegistry
from genomeagent.model_acquisition import (
    ModelAcquisitionError,
    ModelAcquisitionPlanner,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ModelAcquisitionPlannerTests(unittest.TestCase):
    def copied_repository(self, root: Path) -> AIRegistry:
        shutil.copytree(REPOSITORY_ROOT / "config/ai", root / "config/ai")
        shutil.copytree(REPOSITORY_ROOT / "benchmarks/ai", root / "benchmarks/ai")
        return AIRegistry(
            backend_root=root / "config/ai/backends",
            prompt_root=root / "config/ai/prompts",
            suite_root=root / "config/ai/suites",
            case_root=root / "benchmarks/ai",
        )

    def planner(self, root: Path, registry: AIRegistry) -> ModelAcquisitionPlanner:
        return ModelAcquisitionPlanner(
            registry=registry,
            specification_root=root / "config/ai/acquisition",
            backend_state_root=root / "backend-state",
            plan_root=root / "plans",
        )

    def write_environment_state(
        self,
        root: Path,
        available_bytes: int = 200_000_000_000,
        backend_digest: Optional[str] = None,
        environment_blockers=None,
    ) -> None:
        backend_path = root / "config/ai/backends/roihu_qwen3_coder.json"
        digest = backend_digest or hashlib.sha256(backend_path.read_bytes()).hexdigest()
        state_dir = root / "backend-state/roihu_qwen3_coder"
        state_dir.mkdir(parents=True)
        state = {
            "schema_version": "1.0",
            "backend_id": "roihu_qwen3_coder",
            "automatic_execution_allowed": False,
            "automatic_registry_update_allowed": False,
            "readiness": {
                "status": "model_identity_and_acquisition_required",
                "environment_blockers": list(environment_blockers or []),
            },
            "observation": {
                "storage": {
                    "filesystem_available_bytes": available_bytes,
                    "workspace_command": {
                        "returncode": 0,
                        "stdout": (
                            "/scratch/project_2001113 "
                            "{}G/250G 1/500K 180d\n".format(
                                (250_000_000_000 - available_bytes) / 1_000_000_000
                            )
                        ),
                    },
                }
            },
        }
        provenance = {
            "schema_version": "1.0",
            "backend_id": "roihu_qwen3_coder",
            "source_artifacts": [
                {
                    "artifact_type": "ai_backend_configuration",
                    "path": str(backend_path),
                    "sha256": digest,
                }
            ],
        }
        (state_dir / "current_evidence.json").write_text(
            json.dumps(state) + "\n", encoding="utf-8"
        )
        (state_dir / "provenance.json").write_text(
            json.dumps(provenance) + "\n", encoding="utf-8"
        )

    def pin_source(self, root: Path, total_bytes: int = 60_000_000_000) -> None:
        path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
        value = json.loads(path.read_text())
        approved_values = {
            "resolved_revision": "a" * 40,
            "source_inventory_sha256": "b" * 64,
            "source_total_bytes": total_bytes,
            "license_identifier": "Apache-2.0",
            "license_review_status": "reviewed_accepted",
        }
        approval_identity = {
            "policy_version": "1.0",
            "backend_id": "roihu_qwen3_coder",
            "source_evidence_id": "20260715T100000000000Z",
            "source_evidence_sha256": "d" * 64,
            "reviewer": "test_researcher",
            "accepted_license_identifier": "Apache-2.0",
            "review_url": (
                "https://huggingface.co/Qwen/"
                "Qwen3-Coder-30B-A3B-Instruct/blob/{}/LICENSE"
            ).format("a" * 40),
            "approved_source_values": approved_values,
        }
        approval_id = hashlib.sha256(
            json.dumps(
                approval_identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        value["source"].update(approved_values)
        value["source"]["license_approval"] = {
            "schema_version": "1.0",
            "approval_id": approval_id,
            "source_evidence_id": "20260715T100000000000Z",
            "source_evidence_sha256": "d" * 64,
            "reviewer": "test_researcher",
            "accepted_at_utc": "2026-07-15T10:00:00Z",
            "license_identifier": "Apache-2.0",
            "review_url": (
                "https://huggingface.co/Qwen/"
                "Qwen3-Coder-30B-A3B-Instruct/blob/{}/LICENSE"
            ).format("a" * 40),
            "resolved_revision": "a" * 40,
        }
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def test_repository_specification_is_planning_only_and_matches_backend(self):
        root = REPOSITORY_ROOT
        registry = AIRegistry(
            backend_root=root / "config/ai/backends",
            prompt_root=root / "config/ai/prompts",
            suite_root=root / "config/ai/suites",
            case_root=root / "benchmarks/ai",
        )
        _, backend, _ = registry.backend("roihu_qwen3_coder")
        spec = json.loads(
            (root / "config/ai/acquisition/roihu_qwen3_coder.json").read_text()
        )
        self.assertEqual(spec["source"]["repository"], backend["model"]["repository"])
        self.assertEqual(
            spec["target"]["installation_path"], backend["installation"]["path"]
        )
        self.assertIsNone(spec["source"]["resolved_revision"])
        self.assertTrue(all(value is False for value in spec["safety"].values()))

    def test_verified_environment_produces_identity_resolution_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root)
            result = self.planner(root, registry).plan("roihu_qwen3_coder")
            self.assertEqual(result.status, "model_identity_resolution_required")
            self.assertEqual(
                result.next_safe_action, "resolve_and_review_pinned_source_metadata"
            )
            self.assertIn("model_revision_unpinned", result.blockers)
            plan = json.loads((result.plan_dir / "model_acquisition_plan.json").read_text())
            self.assertEqual(
                plan["storage"]["theoretical_parameter_bytes_lower_bound"],
                61_000_000_000,
            )
            self.assertIsNone(plan["storage"]["required_working_free_bytes"])
            readiness = json.loads((result.plan_dir / "execution_readiness.json").read_text())
            self.assertEqual(readiness["environment_gate"]["status"], "verified")
            self.assertFalse(readiness["automatic_execution_allowed"])
            self.assertFalse(readiness["automatic_model_download_allowed"])

    def test_missing_or_stale_environment_evidence_blocks_planning_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            missing = self.planner(root, registry).plan("roihu_qwen3_coder")
            self.assertEqual(missing.status, "environment_evidence_required")
            self.assertIn("backend_environment_evidence_missing", missing.blockers)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root, backend_digest="0" * 64)
            stale = self.planner(root, registry).plan("roihu_qwen3_coder")
            self.assertEqual(stale.status, "environment_evidence_required")
            self.assertIn("backend_environment_evidence_stale", stale.blockers)

    def test_pinned_source_and_capacity_create_review_only_ready_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root, available_bytes=200_000_000_000)
            self.pin_source(root)
            result = self.planner(root, registry).plan("roihu_qwen3_coder")
            self.assertEqual(result.status, "ready_for_researcher_acquisition_review")
            self.assertEqual(result.blockers, ())
            plan = json.loads((result.plan_dir / "model_acquisition_plan.json").read_text())
            self.assertEqual(plan["storage"]["required_working_free_bytes"], 126_000_000_000)
            self.assertEqual(
                plan["storage"]["status"],
                "sufficient_observed_capacity_for_review",
            )
            self.assertEqual(
                plan["storage"]["project_quota_evidence"]["source"],
                "csc_workspaces_project_quota",
            )
            self.assertFalse(plan["automatic_model_download_allowed"])
            self.assertFalse(plan["automatic_backend_activation_allowed"])

    def test_insufficient_observed_storage_is_an_explicit_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root, available_bytes=100_000_000_000)
            self.pin_source(root)
            result = self.planner(root, registry).plan("roihu_qwen3_coder")
            self.assertEqual(result.status, "storage_preflight_required")
            self.assertIn("insufficient_observed_storage", result.blockers)

    def test_generic_filesystem_free_space_does_not_satisfy_project_quota_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root, available_bytes=10_000_000_000_000)
            state_path = (
                root / "backend-state/roihu_qwen3_coder/current_evidence.json"
            )
            state = json.loads(state_path.read_text())
            del state["observation"]["storage"]["workspace_command"]
            state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
            self.pin_source(root)
            result = self.planner(root, registry).plan("roihu_qwen3_coder")
            self.assertEqual(result.status, "storage_preflight_required")
            self.assertIn("target_storage_capacity_unverified", result.blockers)
            plan = json.loads((result.plan_dir / "model_acquisition_plan.json").read_text())
            self.assertIsNone(plan["storage"]["observed_available_bytes"])
            self.assertEqual(
                plan["storage"]["project_quota_evidence"][
                    "filesystem_available_bytes_informational"
                ],
                10_000_000_000_000,
            )

    def test_metadata_request_contains_evidence_requirements_not_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root)
            result = self.planner(root, registry).plan("roihu_qwen3_coder")
            request = json.loads((result.plan_dir / "source_metadata_request.json").read_text())
            self.assertFalse(request["remote_access_allowed"])
            self.assertFalse(request["model_download_allowed"])
            rendered = json.dumps(request)
            self.assertNotIn("snapshot_download", rendered)
            self.assertNotIn("subprocess", rendered)
            self.assertNotIn("sbatch", rendered)

    def test_unsafe_acquisition_authority_and_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
            value = json.loads(path.read_text())
            value["safety"]["model_download_allowed"] = True
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelAcquisitionError, "model_download_allowed"):
                self.planner(root, registry).plan("roihu_qwen3_coder")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
            value = json.loads(path.read_text())
            value["target"]["installation_path"] = "/scratch/project/../unsafe"
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelAcquisitionError, "unsafe path segment"):
                self.planner(root, registry).plan("roihu_qwen3_coder")

    def test_accepted_license_without_exact_approval_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root)
            path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
            value = json.loads(path.read_text())
            value["source"].update({
                "resolved_revision": "a" * 40,
                "source_inventory_sha256": "b" * 64,
                "source_total_bytes": 60_000_000_000,
                "license_review_status": "reviewed_accepted",
            })
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelAcquisitionError, "license_approval"):
                self.planner(root, registry).plan("roihu_qwen3_coder")

    def test_tampered_license_approval_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root)
            self.pin_source(root)
            path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
            value = json.loads(path.read_text())
            value["source"]["license_approval"]["reviewer"] = "someone_else"
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelAcquisitionError, "content digest"):
                self.planner(root, registry).plan("roihu_qwen3_coder")

    def test_plan_is_content_addressed_idempotent_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root)
            planner = self.planner(root, registry)
            first = planner.plan("roihu_qwen3_coder")
            second = planner.plan("roihu_qwen3_coder")
            self.assertEqual(first.plan_id, second.plan_id)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            report = first.plan_dir / "report.md"
            report.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelAcquisitionError, "different content"):
                planner.plan("roihu_qwen3_coder")

    def test_cli_reports_non_executable_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.write_environment_state(root)
            command = [
                sys.executable,
                "scripts/model_acquisition.py",
                "plan",
                "roihu_qwen3_coder",
                "--backend-root", str(root / "config/ai/backends"),
                "--prompt-root", str(root / "config/ai/prompts"),
                "--suite-root", str(root / "config/ai/suites"),
                "--case-root", str(root / "benchmarks/ai"),
                "--specification-root", str(root / "config/ai/acquisition"),
                "--backend-state-root", str(root / "backend-state"),
                "--plan-root", str(root / "cli-plans"),
            ]
            completed = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Remote access     : none", completed.stdout)
            self.assertIn("Model download    : disabled", completed.stdout)
            self.assertIn("Status            : model_identity_resolution_required", completed.stdout)


if __name__ == "__main__":
    unittest.main()
