from __future__ import annotations

import copy
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
from genomeagent.model_source_evidence import (
    ModelSourceApprovalCore,
    ModelSourceEvidenceCollector,
    ModelSourceEvidenceCore,
    ModelSourceEvidenceError,
    ProviderResponse,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40


class FakeMetadataClient:
    endpoint = "https://huggingface.co"

    def __init__(self, symbolic, pinned=None):
        self.responses = [symbolic, pinned or symbolic]
        self.calls = []

    def model_info(
        self,
        repository,
        revision,
        timeout_seconds,
        maximum_response_bytes,
    ):
        self.calls.append({
            "repository": repository,
            "revision": revision,
            "timeout_seconds": timeout_seconds,
            "maximum_response_bytes": maximum_response_bytes,
        })
        payload = copy.deepcopy(self.responses[len(self.calls) - 1])
        raw = json.dumps(payload, sort_keys=True).encode("utf-8")
        return ProviderResponse(
            payload=payload,
            body_sha256=hashlib.sha256(raw).hexdigest(),
            body_bytes=len(raw),
            final_url=(
                "https://huggingface.co/api/models/"
                "Qwen/Qwen3-Coder-30B-A3B-Instruct/revision/{}?blobs=true"
            ).format(revision),
            status_code=200,
        )


class ModelSourceEvidenceTests(unittest.TestCase):
    def copied_repository(self, root: Path) -> AIRegistry:
        shutil.copytree(REPOSITORY_ROOT / "config/ai", root / "config/ai")
        shutil.copytree(REPOSITORY_ROOT / "benchmarks/ai", root / "benchmarks/ai")
        return AIRegistry(
            backend_root=root / "config/ai/backends",
            prompt_root=root / "config/ai/prompts",
            suite_root=root / "config/ai/suites",
            case_root=root / "benchmarks/ai",
        )

    def payload(self):
        return {
            "id": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "sha": REVISION,
            "private": False,
            "gated": False,
            "usedStorage": 60_000_020_000,
            "cardData": {"license": "apache-2.0"},
            "tags": ["license:apache-2.0"],
            "siblings": [
                {
                    "rfilename": "model-00002-of-00002.safetensors",
                    "size": 30_000_000_000,
                    "blobId": "2" * 40,
                    "lfs": {"sha256": "d" * 64, "size": 30_000_000_000},
                },
                {
                    "rfilename": "LICENSE",
                    "size": 19_000,
                    "blobId": "3" * 40,
                },
                {
                    "rfilename": "config.json",
                    "size": 1_000,
                    "blobId": "4" * 40,
                },
                {
                    "rfilename": "model-00001-of-00002.safetensors",
                    "size": 30_000_000_000,
                    "blobId": "1" * 40,
                    "lfs": {"sha256": "c" * 64, "size": 30_000_000_000},
                },
            ],
        }

    def collector(self, root: Path, registry: AIRegistry):
        return ModelSourceEvidenceCollector(
            registry=registry,
            policy_root=root / "config/ai/source_evidence",
            evidence_root=root / "evidence",
        )

    def core(self, root: Path, registry: AIRegistry):
        return ModelSourceEvidenceCore(
            registry=registry,
            policy_root=root / "config/ai/source_evidence",
            specification_root=root / "config/ai/acquisition",
            evidence_root=root / "evidence",
            state_root=root / "state",
        )

    def approval_core(self, root: Path, registry: AIRegistry):
        return ModelSourceApprovalCore(
            registry=registry,
            policy_root=root / "config/ai/source_evidence",
            specification_root=root / "config/ai/acquisition",
            evidence_root=root / "evidence",
            state_root=root / "state",
            approval_root=root / "approvals",
        )

    def collect(self, root: Path, registry: AIRegistry, client=None):
        client = client or FakeMetadataClient(self.payload())
        result = self.collector(root, registry).collect(
            "roihu_qwen3_coder",
            client=client,
            stamp="20260715T100000000000Z",
        )
        return result, client

    def test_repository_policy_is_bounded_and_read_only(self):
        registry = AIRegistry(
            backend_root=REPOSITORY_ROOT / "config/ai/backends",
            prompt_root=REPOSITORY_ROOT / "config/ai/prompts",
            suite_root=REPOSITORY_ROOT / "config/ai/suites",
            case_root=REPOSITORY_ROOT / "benchmarks/ai",
        )
        collector = ModelSourceEvidenceCollector(
            registry=registry,
            policy_root=REPOSITORY_ROOT / "config/ai/source_evidence",
        )
        _, policy, _, _, _ = collector.policy("roihu_qwen3_coder")
        self.assertEqual(policy["limits"]["maximum_requests"], 2)
        self.assertEqual(policy["limits"]["maximum_files"], 1000)
        self.assertTrue(policy["safety"]["public_metadata_queries_allowed"])
        false_controls = {
            key: value for key, value in policy["safety"].items()
            if key != "public_metadata_queries_allowed"
        }
        self.assertTrue(all(value is False for value in false_controls.values()))

    def test_collection_resolves_then_confirms_revision_without_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            result, client = self.collect(root, registry)
            self.assertEqual([call["revision"] for call in client.calls], ["main", REVISION])
            self.assertEqual(result.status, "source_metadata_ready_for_researcher_review")
            self.assertEqual(result.source_total_bytes, 60_000_020_000)
            snapshot = json.loads(result.evidence_path.read_text())
            metadata = snapshot["normalized_metadata"]
            self.assertEqual(metadata["resolved_revision"], REVISION)
            self.assertEqual(metadata["file_count"], 4)
            self.assertEqual(
                [item["path"] for item in metadata["files"]],
                sorted(item["path"] for item in metadata["files"]),
            )
            self.assertEqual(metadata["weight_files_with_provider_lfs_sha256"], 2)
            self.assertFalse(snapshot["safety"]["model_weight_download"])
            rendered = result.evidence_path.read_text()
            self.assertNotIn("file_content", rendered)
            self.assertNotIn("snapshot_download", rendered)

    def test_immutable_revision_confirmation_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            pinned = self.payload()
            pinned["sha"] = "b" * 40
            client = FakeMetadataClient(self.payload(), pinned)
            with self.assertRaisesRegex(
                ModelSourceEvidenceError, "confirmation mismatch"
            ):
                self.collect(root, registry, client)

    def test_incomplete_size_and_unknown_license_remain_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            payload = self.payload()
            payload["siblings"][0].pop("size")
            payload["cardData"] = {}
            payload["tags"] = []
            result, _ = self.collect(root, registry, FakeMetadataClient(payload))
            snapshot = json.loads(result.evidence_path.read_text())
            self.assertEqual(result.status, "source_metadata_attention_required")
            self.assertIn(
                "source_inventory_size_incomplete",
                snapshot["readiness"]["blockers"],
            )
            self.assertIn(
                "source_license_mismatch_or_unknown",
                snapshot["readiness"]["blockers"],
            )

    def test_unsafe_paths_duplicate_inventory_and_excess_files_are_rejected(self):
        for mutation, expected in (
            (lambda value: value["siblings"][0].update({"rfilename": "../weight.bin"}), "unsafe"),
            (lambda value: value["siblings"][1].update({"rfilename": "config.json"}), "duplicate"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = self.copied_repository(root)
                payload = self.payload()
                mutation(payload)
                with self.assertRaisesRegex(ModelSourceEvidenceError, expected):
                    self.collect(root, registry, FakeMetadataClient(payload))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            policy_path = root / "config/ai/source_evidence/roihu_qwen3_coder.json"
            policy = json.loads(policy_path.read_text())
            policy["limits"]["maximum_files"] = 2
            policy_path.write_text(json.dumps(policy) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelSourceEvidenceError, "exceeded"):
                self.collect(root, registry)

    def test_policy_cannot_grant_download_or_cluster_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            policy_path = root / "config/ai/source_evidence/roihu_qwen3_coder.json"
            policy = json.loads(policy_path.read_text())
            policy["safety"]["model_weight_download"] = True
            policy_path.write_text(json.dumps(policy) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelSourceEvidenceError, "model_weight_download"):
                self.collector(root, registry).policy("roihu_qwen3_coder")

    def test_evidence_snapshot_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.collect(root, registry)
            with self.assertRaisesRegex(ModelSourceEvidenceError, "overwrite"):
                self.collect(root, registry)

    def test_ingest_writes_review_only_acquisition_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            result, _ = self.collect(root, registry)
            spec_path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
            before = spec_path.read_bytes()
            ingested = self.core(root, registry).ingest("roihu_qwen3_coder")
            self.assertEqual(ingested.status, "source_metadata_ready_for_researcher_review")
            self.assertEqual(ingested.proposal_status, "researcher_review_required")
            proposal = json.loads(
                (ingested.state_dir / "acquisition_spec_proposal.json").read_text()
            )
            self.assertEqual(
                proposal["proposed_source_values"]["resolved_revision"], REVISION
            )
            self.assertEqual(
                proposal["proposed_source_values"]["source_inventory_sha256"],
                result.source_inventory_sha256,
            )
            self.assertEqual(
                proposal["license_review"]["review_url"],
                "https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct/"
                "blob/{}/LICENSE".format(REVISION),
            )
            self.assertFalse(proposal["automatic_application_allowed"])
            self.assertEqual(spec_path.read_bytes(), before)

    def test_configuration_change_makes_previous_snapshot_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.collect(root, registry)
            backend_path = root / "config/ai/backends/roihu_qwen3_coder.json"
            backend = json.loads(backend_path.read_text())
            backend["notes"] += " Changed after collection."
            backend_path.write_text(json.dumps(backend) + "\n", encoding="utf-8")
            result = self.core(root, registry).ingest("roihu_qwen3_coder")
            self.assertEqual(result.status, "source_metadata_evidence_stale")
            self.assertIn("source_evidence_configuration_changed", result.blockers)

    def test_ingest_rejects_snapshot_that_claims_download_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            result, _ = self.collect(root, registry)
            snapshot = json.loads(result.evidence_path.read_text())
            snapshot["safety"]["model_weight_download"] = True
            result.evidence_path.write_text(json.dumps(snapshot) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelSourceEvidenceError, "model_weight_download"):
                self.core(root, registry).ingest("roihu_qwen3_coder")

    def test_cli_ingest_reports_non_executable_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.collect(root, registry)
            command = [
                sys.executable,
                "scripts/model_source_metadata.py",
                "ingest",
                "roihu_qwen3_coder",
                "--backend-root", str(root / "config/ai/backends"),
                "--prompt-root", str(root / "config/ai/prompts"),
                "--suite-root", str(root / "config/ai/suites"),
                "--case-root", str(root / "benchmarks/ai"),
                "--policy-root", str(root / "config/ai/source_evidence"),
                "--specification-root", str(root / "config/ai/acquisition"),
                "--evidence-root", str(root / "evidence"),
                "--state-root", str(root / "cli-state"),
            ]
            completed = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Model download       : disabled", completed.stdout)
            self.assertIn("Configuration updates: disabled", completed.stdout)
            self.assertIn("Proposal status      : researcher_review_required", completed.stdout)

    def test_explicit_approval_records_exact_evidence_and_updates_only_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            collected, _ = self.collect(root, registry)
            spec_path = root / "config/ai/acquisition/roihu_qwen3_coder.json"
            before = json.loads(spec_path.read_text())
            result = self.approval_core(root, registry).approve(
                backend_id="roihu_qwen3_coder",
                evidence_id=collected.evidence_id,
                reviewer="tuomas64",
                accepted_license="Apache-2.0",
                confirmation=True,
                accepted_at=datetime(2026, 7, 15, 10, 30, tzinfo=timezone.utc),
            )
            self.assertTrue(result.applied)
            self.assertFalse(result.already_applied)
            after = json.loads(spec_path.read_text())
            self.assertEqual(after["target"], before["target"])
            self.assertEqual(after["representation"], before["representation"])
            self.assertEqual(after["storage_policy"], before["storage_policy"])
            self.assertEqual(after["integrity_policy"], before["integrity_policy"])
            self.assertEqual(after["safety"], before["safety"])
            self.assertEqual(after["source"]["resolved_revision"], REVISION)
            self.assertEqual(
                after["source"]["source_inventory_sha256"],
                collected.source_inventory_sha256,
            )
            self.assertEqual(after["source"]["source_total_bytes"], 60_000_020_000)
            self.assertEqual(
                after["source"]["license_review_status"], "reviewed_accepted"
            )
            approval = after["source"]["license_approval"]
            self.assertEqual(approval["approval_id"], result.approval_id)
            self.assertEqual(approval["source_evidence_id"], collected.evidence_id)
            self.assertEqual(approval["reviewer"], "tuomas64")
            self.assertEqual(approval["accepted_at_utc"], "2026-07-15T10:30:00Z")
            artifact = json.loads(result.approval_path.read_text())
            self.assertFalse(artifact["safety"]["model_download"])
            self.assertFalse(artifact["safety"]["gpu_allocation"])
            proposal = json.loads(
                (root / "state/roihu_qwen3_coder/acquisition_spec_proposal.json").read_text()
            )
            self.assertEqual(
                proposal["status"], "identity_and_license_approval_reflected"
            )
            self.assertTrue(
                proposal["license_review"]["researcher_acceptance_recorded"]
            )

    def test_approval_is_idempotent_for_same_evidence_reviewer_and_license(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            collected, _ = self.collect(root, registry)
            core = self.approval_core(root, registry)
            first = core.approve(
                "roihu_qwen3_coder",
                collected.evidence_id,
                "tuomas64",
                "Apache-2.0",
                True,
                accepted_at=datetime(2026, 7, 15, 10, 30, tzinfo=timezone.utc),
            )
            second = core.approve(
                "roihu_qwen3_coder",
                collected.evidence_id,
                "tuomas64",
                "Apache-2.0",
                True,
                accepted_at=datetime(2026, 7, 15, 11, 30, tzinfo=timezone.utc),
            )
            self.assertEqual(first.approval_id, second.approval_id)
            self.assertFalse(second.applied)
            self.assertTrue(second.already_applied)
            self.assertEqual(
                second.status, "approved_source_identity_already_recorded"
            )

    def test_approval_requires_confirmation_exact_license_and_latest_evidence(self):
        cases = (
            ({"confirmation": False}, "confirm-license-review"),
            ({"accepted_license": "MIT"}, "exactly match"),
            ({"evidence_id": "20260715T090000000000Z"}, "not the latest"),
        )
        for overrides, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                registry = self.copied_repository(root)
                collected, _ = self.collect(root, registry)
                arguments = {
                    "backend_id": "roihu_qwen3_coder",
                    "evidence_id": collected.evidence_id,
                    "reviewer": "tuomas64",
                    "accepted_license": "Apache-2.0",
                    "confirmation": True,
                }
                arguments.update(overrides)
                with self.assertRaisesRegex(ModelSourceEvidenceError, expected):
                    self.approval_core(root, registry).approve(**arguments)

    def test_stale_source_evidence_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            collected, _ = self.collect(root, registry)
            backend_path = root / "config/ai/backends/roihu_qwen3_coder.json"
            backend = json.loads(backend_path.read_text())
            backend["notes"] += " Configuration changed."
            backend_path.write_text(json.dumps(backend) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelSourceEvidenceError, "not ready"):
                self.approval_core(root, registry).approve(
                    "roihu_qwen3_coder",
                    collected.evidence_id,
                    "tuomas64",
                    "Apache-2.0",
                    True,
                )

    def test_tampered_approval_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            collected, _ = self.collect(root, registry)
            core = self.approval_core(root, registry)
            first = core.approve(
                "roihu_qwen3_coder",
                collected.evidence_id,
                "tuomas64",
                "Apache-2.0",
                True,
            )
            approval = json.loads(first.approval_path.read_text())
            approval["reviewer"] = "someone_else"
            first.approval_path.write_text(json.dumps(approval) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ModelSourceEvidenceError, "content digest"):
                core.approve(
                    "roihu_qwen3_coder",
                    collected.evidence_id,
                    "tuomas64",
                    "Apache-2.0",
                    True,
                )

    def test_cli_approval_makes_only_the_declared_local_configuration_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            collected, _ = self.collect(root, registry)
            command = [
                sys.executable,
                "scripts/model_source_metadata.py",
                "approve",
                "roihu_qwen3_coder",
                "--evidence-id", collected.evidence_id,
                "--reviewer", "tuomas64",
                "--accept-license", "Apache-2.0",
                "--confirm-license-review",
                "--backend-root", str(root / "config/ai/backends"),
                "--prompt-root", str(root / "config/ai/prompts"),
                "--suite-root", str(root / "config/ai/suites"),
                "--case-root", str(root / "benchmarks/ai"),
                "--policy-root", str(root / "config/ai/source_evidence"),
                "--specification-root", str(root / "config/ai/acquisition"),
                "--evidence-root", str(root / "evidence"),
                "--state-root", str(root / "cli-state"),
                "--approval-root", str(root / "cli-approvals"),
            ]
            completed = subprocess.run(
                command,
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Remote access           : disabled", completed.stdout)
            self.assertIn("Model download          : disabled", completed.stdout)
            self.assertIn("Specification edit: applied", completed.stdout)


if __name__ == "__main__":
    unittest.main()
