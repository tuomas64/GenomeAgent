from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from genomeagent.resource_decision import (
    ResourceDecisionCore,
    ResourceDecisionError,
)


class ResourceDecisionTests(unittest.TestCase):
    def write_environments(self, root):
        environment_root = root / "environments"
        environment_root.mkdir()
        for environment_id, status in (
            ("puhti", "observed_source_environment"),
            ("roihu", "registered_without_local_resource_evidence"),
        ):
            value = {
                "schema_version": "1.0",
                "environment_id": environment_id,
                "display_name": environment_id.title(),
                "scheduler": "slurm",
                "host_aliases": [environment_id],
                "evidence_status": status,
                "scheduler_defaults": {
                    "partition": None,
                    "cpus_per_task": None,
                },
                "resource_transfer_policy": {
                    "direct_external_profile_application_allowed": False,
                    "target_pilot_required_for_external_profiles": True,
                },
            }
            (environment_root / (environment_id + ".json")).write_text(
                json.dumps(value) + "\n", encoding="utf-8"
            )
        return environment_root

    def write_resource_knowledge(
        self,
        root,
        task="scattered_joint_calling",
        profile_key="scattered_genotypegvcfs_250kb",
        source_host="puhti",
        confidence="high",
        proposal_status="proposal_available_for_researcher_review",
        successes=21,
        anomalies=None,
    ):
        resource_dir = root / "resources" / task
        resource_dir.mkdir(parents=True)
        profile = {
            "source_host": source_host,
            "profile_key": profile_key,
            "confidence": confidence,
            "sample_size_confidence": confidence,
            "confidence_limitations": [],
            "workload_coverage": {
                "diversity_required": True,
                "unique_batches": 3,
                "unique_chromosomes": 3,
                "sufficient_for_resource_proposal": proposal_status
                == "proposal_available_for_researcher_review",
                "limitations": [],
            },
            "observation_counts": {
                "attempts": successes,
                "terminal": successes,
                "successful": successes,
                "timeout": 0,
                "out_of_memory": 0,
                "failed": 0,
                "cancelled": 0,
                "states": {"COMPLETED": successes},
            },
            "successful_attempt_statistics": {
                "elapsed_seconds": {
                    "n": successes,
                    "min": 6000 if successes else None,
                    "p50": 12611 if successes else None,
                    "p90": 22000 if successes else None,
                    "p95": 23461 if successes else None,
                    "max": 24000 if successes else None,
                },
                "max_rss_bytes": {
                    "n": successes,
                    "min": 4700000000 if successes else None,
                    "p50": 4711692288 if successes else None,
                    "p90": 4720000000 if successes else None,
                    "p95": 4720824320 if successes else None,
                    "max": 4730000000 if successes else None,
                },
            },
            "proposed_resources": {
                "status": proposal_status,
                "memory_bytes": (
                    6 * 1024 ** 3
                    if proposal_status == "proposal_available_for_researcher_review"
                    else None
                ),
                "time_limit_seconds": (
                    32400
                    if proposal_status == "proposal_available_for_researcher_review"
                    else None
                ),
                "automatic_application_allowed": False,
            },
        }
        profiles = {
            "schema_version": "1.0",
            "policy_version": "fixture",
            "task": task,
            "profiles": [profile],
        }
        anomaly_document = {
            "schema_version": "1.0",
            "policy_version": "fixture",
            "task": task,
            "anomalies": list(anomalies or []),
        }
        provenance = {
            "schema_version": "1.0",
            "task": task,
            "source_snapshots": [{"host": source_host}],
        }
        (resource_dir / "resource_profiles.json").write_text(
            json.dumps(profiles) + "\n", encoding="utf-8"
        )
        (resource_dir / "resource_anomalies.json").write_text(
            json.dumps(anomaly_document) + "\n", encoding="utf-8"
        )
        (resource_dir / "provenance.json").write_text(
            json.dumps(provenance) + "\n", encoding="utf-8"
        )
        return resource_dir

    def write_state(self, root, task="scattered_joint_calling", status="running"):
        state_dir = root / "state" / task
        state_dir.mkdir(parents=True)
        value = {
            "schema_version": "1.0",
            "task": task,
            "overall_status": status,
            "source": {"scan_id": "20260715T060228Z"},
            "observation_health": {
                "healthy_for_action_planning": True,
                "blockers": [],
            },
            "execution_gate": {
                "automatic_execution_allowed": False,
                "researcher_approval_required": True,
                "fresh_pre_execution_scan_required": True,
            },
        }
        (state_dir / "current_state.json").write_text(
            json.dumps(value) + "\n", encoding="utf-8"
        )

    def core(self, root):
        return ResourceDecisionCore(
            resource_root=root / "resources",
            state_root=root / "state",
            environment_root=root / "environments",
            plan_root=root / "plans",
        )

    def test_cross_environment_profile_creates_low_confidence_pilot_only_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_environments(root)
            self.write_resource_knowledge(root)
            self.write_state(root)

            result = self.core(root).plan_resources(
                "scattered_joint_calling",
                target_environment="roihu",
            )

            self.assertEqual(result.decision, "cross_environment_pilot_proposed")
            self.assertEqual(result.confidence, "low")
            decision = json.loads((result.plan_dir / "resource_decision.json").read_text())
            self.assertEqual(
                decision["evidence_status"],
                "sufficient_for_source_environment_proposal",
            )
            self.assertTrue(decision["allocation_proposal_available"])
            allocation = decision["proposed_allocation"]
            self.assertEqual(allocation["memory_bytes"], 6 * 1024 ** 3)
            self.assertEqual(allocation["time_limit_seconds"], 32400)
            self.assertFalse(allocation["target_validated"])
            self.assertEqual(
                allocation["intended_use"], "bounded_cross_environment_pilot_only"
            )
            self.assertIn("cpus_per_task", allocation["missing_fields"])
            readiness = json.loads(
                (result.plan_dir / "execution_readiness.json").read_text()
            )
            self.assertFalse(readiness["automatic_execution_allowed"])
            self.assertIn(
                "active_workflow_scope_review_required", readiness["blockers"]
            )
            provenance = json.loads((result.plan_dir / "provenance.json").read_text())
            self.assertFalse(provenance["safety"]["remote_access"])
            self.assertFalse(provenance["safety"]["job_submission"])
            report = (result.plan_dir / "report.md").read_text()
            self.assertIn("| Elapsed seconds | 12611 | 23461 |", report)
            self.assertIn("| Peak RSS bytes | 4711692288 | 4720824320 |", report)

            before = {
                path.name: path.read_bytes() for path in result.artifact_paths
            }
            repeated = self.core(root).plan_resources(
                "scattered_joint_calling", target_environment="roihu"
            )
            after = {
                path.name: path.read_bytes() for path in repeated.artifact_paths
            }
            self.assertEqual(before, after)

    def test_target_environment_evidence_retains_profile_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_environments(root)
            self.write_resource_knowledge(root)
            self.write_state(root, status="completed")

            result = self.core(root).plan_resources(
                "scattered_joint_calling", target_environment="puhti"
            )
            self.assertEqual(
                result.decision, "target_environment_proposal_available"
            )
            self.assertEqual(result.confidence, "high")
            decision = json.loads((result.plan_dir / "resource_decision.json").read_text())
            self.assertTrue(decision["proposed_allocation"]["target_validated"])

    def test_missing_resource_knowledge_is_reported_without_inventing_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_environments(root)
            result = self.core(root).plan_resources(
                "gam_deduplication",
                target_environment="roihu",
                profile_key="gam_dedup_fast",
            )
            self.assertEqual(result.decision, "no_resource_knowledge")
            decision = json.loads((result.plan_dir / "resource_decision.json").read_text())
            self.assertEqual(decision["evidence_status"], "unavailable")
            self.assertFalse(decision["allocation_proposal_available"])
            allocation = decision["proposed_allocation"]
            self.assertEqual(allocation["status"], "withheld")
            self.assertIsNone(allocation["memory_bytes"])
            self.assertIsNone(allocation["time_limit_seconds"])
            readiness = json.loads(
                (result.plan_dir / "execution_readiness.json").read_text()
            )
            self.assertIn("current_task_state_missing", readiness["blockers"])
            report = (result.plan_dir / "report.md").read_text()
            self.assertIn("No allocation values are proposed", report)

    def test_insufficient_successes_and_blocking_anomalies_withhold_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_environments(root)
            self.write_resource_knowledge(
                root,
                task="gam_deduplication",
                profile_key="gam_dedup_fast",
                confidence="insufficient",
                proposal_status="insufficient_evidence",
                successes=0,
            )
            result = self.core(root).plan_resources(
                "gam_deduplication",
                target_environment="roihu",
                profile_key="gam_dedup_fast",
            )
            self.assertEqual(result.decision, "insufficient_successful_evidence")
            insufficient = json.loads(
                (result.plan_dir / "resource_decision.json").read_text()
            )
            self.assertEqual(
                insufficient["evidence_status"],
                "present_without_successful_attempts",
            )

            anomaly = {
                "anomaly_id": "fixture-high-review",
                "type": "runtime_instability_across_attempts",
                "severity": "high_review",
                "source_host": "puhti",
                "profile_key": "scattered_genotypegvcfs_250kb",
            }
            other = Path(tmp) / "blocking"
            other.mkdir()
            self.write_environments(other)
            self.write_resource_knowledge(other, anomalies=[anomaly])
            blocked = self.core(other).plan_resources(
                "scattered_joint_calling", target_environment="roihu"
            )
            self.assertEqual(blocked.decision, "blocked_by_resource_anomalies")

    def test_narrow_evidence_is_primary_and_anomalies_remain_secondary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_environments(root)
            anomaly = {
                "anomaly_id": "fixture-high-review",
                "type": "probable_transient_runtime_stall",
                "severity": "high_review",
                "source_host": "puhti",
                "profile_key": "scattered_genotypegvcfs_250kb",
            }
            self.write_resource_knowledge(
                root,
                confidence="low",
                proposal_status="insufficient_workload_diversity",
                successes=21,
                anomalies=[anomaly],
            )

            result = self.core(root).plan_resources(
                "scattered_joint_calling", target_environment="roihu"
            )
            self.assertEqual(result.decision, "insufficient_transferable_evidence")
            decision = json.loads((result.plan_dir / "resource_decision.json").read_text())
            self.assertEqual(decision["evidence_status"], "substantial_but_narrow")
            self.assertFalse(decision["allocation_proposal_available"])
            self.assertEqual(
                decision["blocking_anomaly_ids"], ["fixture-high-review"]
            )
            readiness = json.loads(
                (result.plan_dir / "execution_readiness.json").read_text()
            )
            self.assertIn("resource_anomaly_review_required", readiness["blockers"])

    def test_multiple_profile_keys_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_environments(root)
            resource_dir = self.write_resource_knowledge(
                root,
                task="gam_deduplication",
                profile_key="gam_dedup_fast",
            )
            profiles_path = resource_dir / "resource_profiles.json"
            document = json.loads(profiles_path.read_text())
            second = dict(document["profiles"][0])
            second["profile_key"] = "gam_dedup_residual_qc"
            document["profiles"].append(second)
            profiles_path.write_text(json.dumps(document) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ResourceDecisionError, "--profile-key"):
                self.core(root).plan_resources(
                    "gam_deduplication", target_environment="roihu"
                )

    def test_repository_environment_registry_withholds_unknown_defaults(self):
        environment_root = (
            Path(__file__).resolve().parents[1] / "config" / "environments"
        )
        for environment_id in ("puhti", "roihu"):
            config = json.loads(
                (environment_root / (environment_id + ".json")).read_text()
            )
            self.assertEqual(config["environment_id"], environment_id)
            self.assertEqual(config["scheduler"], "slurm")
            self.assertIsNone(config["scheduler_defaults"]["partition"])
            self.assertIsNone(config["scheduler_defaults"]["cpus_per_task"])
            self.assertFalse(
                config["resource_transfer_policy"]
                ["direct_external_profile_application_allowed"]
            )


if __name__ == "__main__":
    unittest.main()
