from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from genomeagent.ai_backend_evidence import _interpret as interpret_backend_evidence
from genomeagent.ai_evaluation import AIRegistry
from genomeagent.model_registration import (
    InstalledModelEvidenceCollector,
    InstalledModelEvidenceCore,
    ModelRegistrationApprovalCore,
    ModelRegistrationError,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ID = "roihu_qwen3_coder"
BUNDLE_ID = "b" * 64
VERIFICATION_ID = "9" * 64
PUBLICATION_ID = "7" * 64
REVISION = "a" * 40


def compact(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class LocalRunner:
    host = "roihu"

    def __init__(self, mutate=None):
        self.mutate = mutate
        self.calls = []

    def run_python(self, program, timeout_seconds=240):
        self.calls.append((program, timeout_seconds))
        completed = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr)
        value = json.loads(completed.stdout)
        if self.mutate:
            self.mutate(value)
        return value


class ModelRegistrationTests(unittest.TestCase):
    def setup_fixture(self, root: Path):
        config = root / "config/ai"
        for relative in (
            "backends/roihu_qwen3_coder.json",
            "acquisition/roihu_qwen3_coder.json",
            "model_registration/roihu_qwen3_coder.json",
            "evidence/roihu_qwen3_coder.json",
        ):
            target = config / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((REPOSITORY_ROOT / "config/ai" / relative).read_bytes())
        installation = root / "scratch/models/Qwen"
        installation.mkdir(parents=True)
        files = []
        for name, payload in (
            ("config.json", b'{"model_type":"qwen3_moe"}\n'),
            ("model-00001-of-00002.safetensors", b"weight-one"),
            ("model-00002-of-00002.safetensors", b"weight-two"),
        ):
            path = installation / name
            path.write_bytes(payload)
            files.append({
                "path": name,
                "size_bytes": len(payload),
                "local_sha256": hashlib.sha256(payload).hexdigest(),
                "provider_lfs_sha256": (
                    hashlib.sha256(payload).hexdigest() if name.endswith("safetensors") else None
                ),
                "provider_lfs_sha256_match": (
                    True if name.endswith("safetensors") else None
                ),
                "provider_git_blob_id": None,
                "provider_xet_hash": None,
            })
        files.sort(key=lambda item: item["path"])
        verified_manifest_sha = "c" * 64
        verification_result_sha = "d" * 64
        installed_manifest = {
            "schema_version": "1.0",
            "manifest_type": "genomeagent_installed_model",
            "backend_id": BACKEND_ID,
            "bundle_id": BUNDLE_ID,
            "verification_id": VERIFICATION_ID,
            "publication_id": PUBLICATION_ID,
            "repository": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "resolved_revision": REVISION,
            "installation_path": str(installation),
            "installed_at_utc": "2026-07-15T12:40:00Z",
            "source_verified_manifest_sha256": verified_manifest_sha,
            "source_verification_result_sha256": verification_result_sha,
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(item["size_bytes"] for item in files),
            "download_cache_removed": True,
            "atomic_directory_rename": True,
            "backend_activated": False,
            "inference_benchmarked": False,
        }
        manifest_raw = compact(installed_manifest)
        manifest_path = installation / ".genomeagent-model-manifest.json"
        manifest_path.write_bytes(manifest_raw)
        installed_manifest_sha = hashlib.sha256(manifest_raw).hexdigest()

        backend_path = config / "backends/roihu_qwen3_coder.json"
        backend = json.loads(backend_path.read_text())
        # Registration tests always start from an explicit pre-registration
        # fixture, even when the repository itself records a verified install.
        backend["status"] = "planned_unvalidated"
        backend["benchmark_status"] = "not_run"
        backend["model"]["revision"] = None
        backend["model"]["weights_sha256"] = None
        backend["model"].pop("weights_digest_type", None)
        backend["installation"]["status"] = "planned_not_present"
        backend["installation"].pop("manifest_sha256", None)
        backend["installation"].pop("verified_inventory_sha256", None)
        backend["installation"].pop("registration", None)
        backend["installation"]["path"] = str(installation)
        backend_path.write_text(json.dumps(backend, indent=2) + "\n")
        specification_path = config / "acquisition/roihu_qwen3_coder.json"
        specification = json.loads(specification_path.read_text())
        specification["source"].update({
            "resolved_revision": REVISION,
            "source_inventory_sha256": "e" * 64,
            "source_total_bytes": sum(item["size_bytes"] for item in files),
            "license_identifier": "Apache-2.0",
            "license_review_status": "reviewed_accepted",
        })
        specification["source"]["license_approval"] = {
            "schema_version": "1.0", "approval_id": "f" * 64,
        }
        specification["target"]["project_storage_root"] = str(root / "scratch")
        specification["target"]["installation_path"] = str(installation)
        specification_path.write_text(json.dumps(specification, indent=2) + "\n")

        bundle = {
            "schema_version": "1.0",
            "policy_version": "1.0",
            "source_mode": "deterministic_local_execution_bundle_preparation",
            "backend_id": BACKEND_ID,
            "bundle_id": BUNDLE_ID,
            "repository": installed_manifest["repository"],
            "resolved_revision": REVISION,
            "target": {
                "project_storage_root": str(root / "scratch"),
                "staging_path": str(root / "scratch/.staging"),
                "installation_path": str(installation),
                "manifest_path": str(manifest_path),
                "publication_strategy": "atomic_directory_rename_same_filesystem",
            },
        }
        bundle_path = root / "bundles" / BACKEND_ID / BUNDLE_ID / "acquisition_bundle.json"
        bundle_path.parent.mkdir(parents=True)
        bundle_path.write_text(json.dumps(bundle) + "\n")

        publication_dir = root / "publications" / BACKEND_ID / BUNDLE_ID / PUBLICATION_ID
        publication_dir.mkdir(parents=True)
        launch = {
            "schema_version": "1.0",
            "source_mode": "explicit_authorized_atomic_model_publication_submission",
            "backend_id": BACKEND_ID,
            "bundle_id": BUNDLE_ID,
            "verification_id": VERIFICATION_ID,
            "publication_id": PUBLICATION_ID,
            "installation_path": str(installation),
            "verified_files": files,
            "verified_manifest_sha256": verified_manifest_sha,
            "verification_result_sha256": verification_result_sha,
            "publication_started": False,
            "gpu_allocation": False,
            "inference": False,
            "registry_update": False,
        }
        (publication_dir / "launch.json").write_text(json.dumps(launch) + "\n")
        publication_status = {
            "schema_version": "1.0",
            "source_mode": "bounded_read_only_model_publication_status_observation",
            "backend_id": BACKEND_ID,
            "bundle_id": BUNDLE_ID,
            "publication_id": PUBLICATION_ID,
            "status": "published_ready_for_installed_model_evidence",
            "remote_observation": {
                "result": {"installed_manifest_sha256": installed_manifest_sha},
                "installed": {
                    "manifest": {
                        "value": installed_manifest,
                        "sha256": installed_manifest_sha,
                    }
                },
            },
            "publication": False,
            "model_file_contents_read": False,
            "large_file_hashing": False,
            "registry_update": False,
            "backend_activation": False,
        }
        status_dir = publication_dir / "status"
        status_dir.mkdir()
        (status_dir / "20260715T124000Z.json").write_text(
            json.dumps(publication_status) + "\n"
        )
        verification_dir = root / "verifications" / BACKEND_ID / BUNDLE_ID / VERIFICATION_ID
        (verification_dir / "status").mkdir(parents=True)
        verification_status = {
            "schema_version": "1.0",
            "source_mode": "bounded_read_only_model_integrity_status_observation",
            "backend_id": BACKEND_ID,
            "bundle_id": BUNDLE_ID,
            "verification_id": VERIFICATION_ID,
            "status": "verified_ready_for_publication_review",
            "publication": False,
            "staging_mutation": False,
        }
        (verification_dir / "status/20260715T120000Z.json").write_text(
            json.dumps(verification_status) + "\n"
        )
        registry = AIRegistry(
            backend_root=config / "backends",
            prompt_root=config / "prompts",
            suite_root=config / "suites",
            case_root=root / "benchmarks/ai",
        )
        roots = {
            "policy": config / "model_registration",
            "specification": config / "acquisition",
            "bundles": root / "bundles",
            "publications": root / "publications",
            "verifications": root / "verifications",
            "evidence": root / "installed-evidence",
            "state": root / "installed-state",
            "approvals": root / "registration-approvals",
        }
        return registry, roots, installed_manifest_sha

    def components(self, registry, roots):
        collector = InstalledModelEvidenceCollector(
            registry, roots["policy"], roots["specification"], roots["bundles"],
            roots["publications"], roots["verifications"], roots["evidence"],
        )
        core = InstalledModelEvidenceCore(
            registry, roots["policy"], roots["specification"], roots["bundles"],
            roots["publications"], roots["verifications"], roots["evidence"], roots["state"],
        )
        approval = ModelRegistrationApprovalCore(
            registry, roots["policy"], roots["specification"], roots["bundles"],
            roots["publications"], roots["verifications"], roots["evidence"],
            roots["state"], roots["approvals"],
        )
        return collector, core, approval

    def test_collect_and_ingest_bind_publication_without_reading_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, roots, _ = self.setup_fixture(Path(tmp))
            collector, core, _ = self.components(registry, roots)
            runner = LocalRunner()
            result = collector.collect(
                BACKEND_ID, BUNDLE_ID, PUBLICATION_ID, runner,
                stamp="20260715T130000Z",
            )
            self.assertEqual(result.status, "ready_for_backend_registration_review")
            snapshot = json.loads(result.evidence_path.read_text())
            self.assertFalse(snapshot["safety"]["model_weight_contents_read"])
            self.assertFalse(snapshot["safety"]["large_file_hashing"])
            self.assertNotIn("local_sha256", runner.calls[0][0])
            ingested = core.ingest(BACKEND_ID)
            self.assertEqual(ingested.proposal_status, "researcher_review_required")
            proposal = json.loads((ingested.state_dir / "backend_registration_proposal.json").read_text())
            self.assertEqual(
                proposal["proposed_values"]["model_weights_digest_type"],
                "verified_model_candidate_manifest_sha256",
            )
            self.assertFalse(proposal["automatic_application_allowed"])

    def test_changed_installed_manifest_is_rejected_before_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, roots, _ = self.setup_fixture(Path(tmp))
            collector, _, _ = self.components(registry, roots)
            runner = LocalRunner(
                lambda value: value["manifest"].update({"sha256": "0" * 64})
            )
            result = collector.collect(
                BACKEND_ID, BUNDLE_ID, PUBLICATION_ID, runner,
                stamp="20260715T130001Z",
            )
            self.assertEqual(result.status, "installed_model_evidence_attention_required")
            self.assertIn("installed_manifest_unverified", result.blockers)

    def test_explicit_approval_updates_only_identity_and_leaves_benchmark_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, roots, installed_sha = self.setup_fixture(Path(tmp))
            collector, core, approval = self.components(registry, roots)
            collected = collector.collect(
                BACKEND_ID, BUNDLE_ID, PUBLICATION_ID, LocalRunner(),
                stamp="20260715T130002Z",
            )
            core.ingest(BACKEND_ID)
            with self.assertRaisesRegex(ModelRegistrationError, "confirm-register"):
                approval.approve(
                    BACKEND_ID, collected.evidence_id, "tuomas64", False
                )
            result = approval.approve(
                BACKEND_ID,
                collected.evidence_id,
                "tuomas64",
                True,
                registered_at=datetime(2026, 7, 15, 13, 5, tzinfo=timezone.utc),
            )
            self.assertTrue(result.applied)
            _, backend, _ = registry.backend(BACKEND_ID)
            self.assertEqual(backend["status"], "validated_candidate")
            self.assertEqual(backend["model"]["revision"], REVISION)
            self.assertEqual(backend["model"]["weights_sha256"], "c" * 64)
            self.assertEqual(backend["installation"]["manifest_sha256"], installed_sha)
            self.assertEqual(backend["installation"]["status"], "verified_present")
            self.assertEqual(
                backend["derived_readiness"]["blockers"], ["inference_not_benchmarked"]
            )
            approval_value = json.loads(result.approval_path.read_text())
            self.assertFalse(approval_value["safety"]["gpu_allocation"])
            self.assertFalse(approval_value["safety"]["backend_activation"])

    def test_registration_approval_is_idempotent_and_tamper_evident(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, roots, _ = self.setup_fixture(Path(tmp))
            collector, core, approval = self.components(registry, roots)
            collected = collector.collect(
                BACKEND_ID, BUNDLE_ID, PUBLICATION_ID, LocalRunner(),
                stamp="20260715T130004Z",
            )
            core.ingest(BACKEND_ID)
            first = approval.approve(BACKEND_ID, collected.evidence_id, "tuomas64", True)
            second = approval.approve(BACKEND_ID, collected.evidence_id, "tuomas64", True)
            self.assertTrue(first.applied)
            self.assertTrue(second.already_applied)
            value = json.loads(first.approval_path.read_text())
            value["installation_identity"]["file_count"] += 1
            first.approval_path.write_text(json.dumps(value) + "\n")
            with self.assertRaisesRegex(ModelRegistrationError, "digest mismatch"):
                approval.approve(BACKEND_ID, collected.evidence_id, "tuomas64", True)

    def test_backend_change_after_observation_blocks_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, roots, _ = self.setup_fixture(Path(tmp))
            collector, core, approval = self.components(registry, roots)
            collected = collector.collect(
                BACKEND_ID, BUNDLE_ID, PUBLICATION_ID, LocalRunner(),
                stamp="20260715T130005Z",
            )
            core.ingest(BACKEND_ID)
            backend_path = registry.backend_root / (BACKEND_ID + ".json")
            backend = json.loads(backend_path.read_text())
            backend["notes"] = "changed after observation"
            backend_path.write_text(json.dumps(backend) + "\n")
            with self.assertRaisesRegex(
                ModelRegistrationError, "not registration-ready|changed"
            ):
                approval.approve(BACKEND_ID, collected.evidence_id, "tuomas64", True)

    def test_cli_ingest_keeps_registry_update_and_gpu_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, roots, _ = self.setup_fixture(root)
            collector, _, _ = self.components(registry, roots)
            collector.collect(
                BACKEND_ID, BUNDLE_ID, PUBLICATION_ID, LocalRunner(),
                stamp="20260715T130006Z",
            )
            completed = subprocess.run([
                sys.executable,
                str(REPOSITORY_ROOT / "scripts/model_registration.py"),
                "ingest", BACKEND_ID,
                "--backend-root", str(registry.backend_root),
                "--prompt-root", str(registry.prompt_root),
                "--suite-root", str(registry.suite_root),
                "--case-root", str(registry.case_root),
                "--policy-root", str(roots["policy"]),
                "--specification-root", str(roots["specification"]),
                "--bundle-root", str(roots["bundles"]),
                "--publication-root", str(roots["publications"]),
                "--verification-root", str(roots["verifications"]),
                "--evidence-root", str(roots["evidence"]),
                "--state-root", str(roots["state"]),
            ], capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Registry updates  : disabled", completed.stdout)
            self.assertIn("GPU/inference     : disabled", completed.stdout)
            backend = json.loads((registry.backend_root / (BACKEND_ID + ".json")).read_text())
            self.assertIsNone(backend["model"]["revision"])

    def test_normal_backend_evidence_accepts_only_matching_registered_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, roots, installed_sha = self.setup_fixture(Path(tmp))
            collector, core, approval = self.components(registry, roots)
            collected = collector.collect(
                BACKEND_ID, BUNDLE_ID, PUBLICATION_ID, LocalRunner(),
                stamp="20260715T130003Z",
            )
            core.ingest(BACKEND_ID)
            approval.approve(BACKEND_ID, collected.evidence_id, "tuomas64", True)
            _, backend, _ = registry.backend(BACKEND_ID)
            policy = {
                "project_storage_root": str(Path(tmp) / "scratch"),
                "workspace_quota_command_candidates": [],
            }
            observation = {
                "identity": {}, "module": {}, "scheduler": {}, "storage": {},
                "model": {
                    "path": backend["installation"]["path"],
                    "exists": True,
                    "is_directory": True,
                    "inventory_manifest": {
                        "exists": True,
                        "metadata": {"status": "parsed", "sha256": installed_sha},
                    },
                },
            }
            readiness = interpret_backend_evidence(backend, policy, observation, True)
            self.assertNotIn("verified_model_inventory_missing", readiness["model_blockers"])
            observation["model"]["inventory_manifest"]["metadata"]["sha256"] = "0" * 64
            changed = interpret_backend_evidence(backend, policy, observation, True)
            self.assertIn("verified_model_inventory_missing", changed["model_blockers"])

    def test_repository_registration_policy_has_no_execution_authority(self):
        policy = json.loads((
            REPOSITORY_ROOT / "config/ai/model_registration/roihu_qwen3_coder.json"
        ).read_text())
        self.assertTrue(all(value is False for value in policy["safety"].values()))
        self.assertEqual(policy["ssh_host"], "roihu")
        self.assertLessEqual(policy["limits"]["maximum_manifest_bytes"], 2_000_000)


if __name__ == "__main__":
    unittest.main()
