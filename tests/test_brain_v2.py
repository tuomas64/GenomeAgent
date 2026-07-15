from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from genomeagent.brain_v2 import BrainKnowledgeCore, WorkflowTransferCore


class BrainV2Tests(unittest.TestCase):
    def write_json(self, path: Path, value) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def workflow(self):
        return {
            "schema_version": "1.0",
            "workflow_id": "scattered_joint_calling",
            "workflow_version": "fixture",
            "task": "scattered_joint_calling",
            "scheduler": "slurm",
            "resource_profile_key": "scatter_250kb",
            "requirements": {
                "software": [{
                    "name": "gatk", "version": "4.5.0.0", "comparison": "exact"
                }],
                "environment_bindings": ["project_storage_root"],
            },
            "workflow_parameters": ["reference_fasta"],
            "source_environment_parameters": {
                "puhti": {"reference_fasta": "/reference.fa"}
            },
            "validation_contract": {"atomic_publication_required": True},
            "execution_policy": {
                "automatic_execution_allowed": False,
                "researcher_approval_required": True,
                "fresh_pre_execution_scan_required": True,
            },
        }

    def state(self, scan_id="20260715T010000Z", overall="running"):
        return {
            "schema_version": "1.0",
            "task": "scattered_joint_calling",
            "overall_status": overall,
            "current_stage": "scattered_genotypegvcfs",
            "next_safe_action": "wait_and_rescan",
            "unit_state_counts": {"completed": 10, "running": 2},
            "source": {"scan_id": scan_id},
            "observation_health": {"healthy_for_action_planning": True},
        }

    def environment(self, environment_id="puhti", complete=True):
        capabilities = {"software": {}, "bindings": {}}
        if complete:
            capabilities = {
                "software": {
                    "gatk": {
                        "status": "verified",
                        "versions": ["4.5.0.0"],
                        "evidence": "fixture",
                    }
                },
                "bindings": {
                    "project_storage_root": {
                        "status": "verified",
                        "value": "/scratch/project",
                        "evidence": "fixture",
                    }
                },
            }
        return {
            "schema_version": "1.0",
            "environment_id": environment_id,
            "scheduler": "slurm",
            "host_aliases": [environment_id],
            "capabilities": capabilities,
        }

    def build_core(self, root: Path) -> BrainKnowledgeCore:
        self.write_json(
            root / "workflows" / "scattered_joint_calling.json", self.workflow()
        )
        self.write_json(root / "environments" / "puhti.json", self.environment())
        self.write_json(
            root / "environments" / "roihu.json", self.environment("roihu", complete=False)
        )
        self.write_json(
            root / "state" / "scattered_joint_calling" / "current_state.json",
            self.state(),
        )
        return BrainKnowledgeCore(
            state_root=root / "state",
            resource_root=root / "resources",
            plan_root=root / "plans",
            workflow_root=root / "workflows",
            environment_root=root / "environments",
            knowledge_root=root / "knowledge",
            legacy_knowledge_path=root / "project_knowledge.json",
        )

    def test_deterministic_promotion_is_idempotent_and_snapshot_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self.build_core(root)
            first = core.ingest("scattered_joint_calling")
            before = {
                path.name: path.read_bytes() for path in first.artifact_paths
            }
            second = core.ingest("scattered_joint_calling")
            after = {
                path.name: path.read_bytes() for path in second.artifact_paths
            }
            self.assertEqual(first.snapshot_id, second.snapshot_id)
            self.assertTrue(first.snapshot_created)
            self.assertFalse(second.snapshot_created)
            self.assertEqual(before, after)
            snapshots = list((first.knowledge_dir / "snapshots").glob("*.json"))
            self.assertEqual(len(snapshots), 1)
            current = json.loads((first.knowledge_dir / "current_knowledge.json").read_text())
            classes = {item["knowledge_class"] for item in current["claims"]}
            self.assertEqual(classes, {
                "operational_state", "workflow_template", "environment_record"
            })
            self.assertFalse(current["automatic_execution_allowed"])

    def test_ai_legacy_knowledge_remains_review_required_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self.build_core(root)
            self.write_json(root / "project_knowledge.json", {
                "project_name": "Fragaria project",
                "recommended_defaults": ["use 100 GB memory"],
            })
            result = core.ingest("scattered_joint_calling")
            current = json.loads((result.knowledge_dir / "current_knowledge.json").read_text())
            promoted_classes = {item["knowledge_class"] for item in current["claims"]}
            self.assertNotIn("legacy_ai_candidate", promoted_classes)
            self.assertEqual(len(current["candidate_claims"]), 2)
            self.assertTrue(all(
                item["status"] == "candidate_requires_researcher_review"
                for item in current["candidate_claims"]
            ))
            self.assertTrue(all(
                item["confidence"] == "unverified_ai_derived"
                for item in current["candidate_claims"]
            ))

    def test_changed_source_creates_new_snapshot_and_supersedes_old_claim_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self.build_core(root)
            first = core.ingest("scattered_joint_calling")
            self.write_json(
                root / "state" / "scattered_joint_calling" / "current_state.json",
                self.state("20260715T020000Z", overall="completed"),
            )
            second = core.ingest("scattered_joint_calling")
            self.assertNotEqual(first.snapshot_id, second.snapshot_id)
            current = json.loads((second.knowledge_dir / "current_knowledge.json").read_text())
            self.assertEqual(current["snapshot_count"], 2)
            state_claim = next(
                item for item in current["claims"]
                if item["knowledge_class"] == "operational_state"
            )
            self.assertEqual(state_claim["value"]["overall_status"], "completed")
            self.assertEqual(len(state_claim["supersedes_claim_versions"]), 1)

    def test_transfer_reports_unknown_target_environment_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self.build_core(root)
            core.ingest("scattered_joint_calling")
            transfer = WorkflowTransferCore(
                workflow_root=root / "workflows",
                environment_root=root / "environments",
                knowledge_root=root / "knowledge",
                plan_root=root / "plans",
                transfer_root=root / "transfers",
            ).plan("scattered_joint_calling", "roihu")
            self.assertEqual(transfer.status, "insufficient_environment_knowledge")
            plan = json.loads((transfer.transfer_dir / "transfer_plan.json").read_text())
            self.assertIn("target_environment_requirements_unknown", plan["blockers"])
            self.assertIn("target_resource_decision_missing", plan["blockers"])
            self.assertFalse(plan["execution_enabled"])
            statuses = {row["status"] for row in plan["compatibility"]}
            self.assertIn("unknown", statuses)

    def test_compatible_target_and_actionable_resources_create_review_only_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self.build_core(root)
            decision_path = (
                root / "plans" / "scattered_joint_calling" / "puhti" /
                "scatter_250kb" / "resource_decision.json"
            )
            self.write_json(decision_path, {
                "schema_version": "1.0",
                "task": "scattered_joint_calling",
                "profile_key": "scatter_250kb",
                "target_environment": "puhti",
                "source_environment": "puhti",
                "decision": "target_environment_proposal_available",
                "evidence_status": "sufficient_for_source_environment_proposal",
                "allocation_proposal_available": True,
                "automatic_execution_allowed": False,
            })
            environment_path = root / "environments" / "puhti.json"
            environment_digest = hashlib.sha256(environment_path.read_bytes()).hexdigest()
            self.write_json(decision_path.with_name("provenance.json"), {
                "schema_version": "1.0",
                "task": "scattered_joint_calling",
                "inputs": {
                    "environment_configurations": [{
                        "path": str(environment_path),
                        "sha256": environment_digest,
                    }]
                },
            })
            core.ingest("scattered_joint_calling")
            transfer = WorkflowTransferCore(
                workflow_root=root / "workflows",
                environment_root=root / "environments",
                knowledge_root=root / "knowledge",
                plan_root=root / "plans",
                transfer_root=root / "transfers",
            ).plan("scattered_joint_calling", "puhti")
            self.assertEqual(transfer.status, "workflow_transfer_proposal_available")
            plan = json.loads((transfer.transfer_dir / "transfer_plan.json").read_text())
            self.assertEqual(plan["blockers"], [])
            self.assertEqual(plan["resource_gate"]["status"], "passed_for_review")
            self.assertTrue(plan["requires_researcher_approval"])
            self.assertFalse(plan["automatic_execution_allowed"])

    def test_stale_resource_decision_is_not_used_as_transfer_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = self.build_core(root)
            decision_path = (
                root / "plans" / "scattered_joint_calling" / "puhti" /
                "scatter_250kb" / "resource_decision.json"
            )
            self.write_json(decision_path, {
                "schema_version": "1.0",
                "task": "scattered_joint_calling",
                "profile_key": "scatter_250kb",
                "target_environment": "puhti",
                "source_environment": "puhti",
                "decision": "target_environment_proposal_available",
                "evidence_status": "sufficient_for_source_environment_proposal",
                "allocation_proposal_available": True,
            })
            environment_path = root / "environments" / "puhti.json"
            self.write_json(decision_path.with_name("provenance.json"), {
                "schema_version": "1.0",
                "task": "scattered_joint_calling",
                "inputs": {
                    "environment_configurations": [{
                        "path": str(environment_path),
                        "sha256": "0" * 64,
                    }]
                },
            })
            core.ingest("scattered_joint_calling")
            transfer = WorkflowTransferCore(
                workflow_root=root / "workflows",
                environment_root=root / "environments",
                knowledge_root=root / "knowledge",
                plan_root=root / "plans",
                transfer_root=root / "transfers",
            ).plan("scattered_joint_calling", "puhti")
            self.assertEqual(transfer.status, "blocked_by_resource_decision")
            plan = json.loads((transfer.transfer_dir / "transfer_plan.json").read_text())
            self.assertIn("resource_decision_stale", plan["blockers"])
            self.assertEqual(plan["resource_gate"]["freshness"]["status"], "stale")
            self.assertFalse(plan["execution_enabled"])

    def test_repository_templates_are_valid_and_cli_exposes_both_commands(self):
        repository = Path(__file__).resolve().parents[1]
        for task in ("scattered_joint_calling", "gam_deduplication"):
            value = json.loads((repository / "config" / "workflows" / (task + ".json")).read_text())
            self.assertEqual(value["workflow_id"], task)
            self.assertEqual(value["task"], task)
            self.assertFalse(value["execution_policy"]["automatic_execution_allowed"])
        completed = subprocess.run(
            [sys.executable, str(repository / "scripts" / "brain.py"), "--help"],
            cwd=repository,
            check=True,
            text=True,
            capture_output=True,
        )
        self.assertIn("plan-workflow", completed.stdout)
        self.assertIn("ingest", completed.stdout)


if __name__ == "__main__":
    unittest.main()
