from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from genomeagent.task_state import TaskStateBridge, TaskStateError


class TaskStateBridgeTests(unittest.TestCase):
    def write_scan(
        self,
        scan_root: Path,
        task: str,
        scan_id: str,
        observation,
        status,
        mode: str = "read_only",
    ) -> Path:
        scan_dir = scan_root / task / scan_id
        scan_dir.mkdir(parents=True)
        payload = {
            "schema_version": "1.0",
            "task": task,
            "title": task.replace("_", " "),
            "mode": mode,
            "host": "puhti",
            "created_at_utc": (
                f"2026-07-14T{scan_id[-7:-5]}:{scan_id[-5:-3]}:{scan_id[-3:-1]}+00:00"
            ),
            "configuration": {
                "profile_version": "fixture",
                "remote_python": "python3.9",
            },
            "observation": observation,
            "status_summary": status,
        }
        path = scan_dir / "task_scan.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def scattered_observation(self, scheduler_ok=True):
        code = 0 if scheduler_ok else None
        error = "" if scheduler_ok else "squeue unavailable"
        return {
            "scanned_at_cluster_time": "2026-07-14T22:00:00+0300",
            "interval_table": {
                "exists": True,
                "records": [],
                "malformed_lines": [],
                "task_line_mismatches": [],
                "duplicate_task_ids": [],
                "duplicate_output_paths": [],
                "output_scan_errors": [],
            },
            "jobs": {
                "squeue_returncode": code,
                "squeue_stderr": error,
                "sacct_returncode": code,
                "sacct_stderr": error,
                "running": [],
                "recent": [],
            },
        }

    def scattered_status(self, states, overall="running", batch_status="active"):
        intervals = []
        counts = {}
        for task, state in enumerate(states, start=1):
            counts[state] = counts.get(state, 0) + 1
            intervals.append({
                "task": task,
                "lifecycle_state": state,
                "state": "output_absent",
                "output_state": "output_absent",
                "batch": "part1" if task <= 3 else "part2",
                "chromosome": "1",
                "interval": f"chr1:{task}-{task}",
                "scheduler_job_id": f"100_{task}" if state != "not_submitted" else "",
                "scheduler_state": "TIMEOUT" if state == "failed_needs_review" else "",
                "stderr_log": f"/logs/part1_100_{task}.err",
                "stdout_log": f"/logs/part1_100_{task}.out",
                "vcf": f"/windows/{task}.vcf.gz",
                "index": f"/windows/{task}.vcf.gz.tbi",
            })
        return {
            "overall_status": overall,
            "current_stage": "scattered_genotypegvcfs",
            "next_safe_action": "wait_and_rescan",
            "counts": {
                "expected_intervals": len(states),
                "completed_atomic_publish_contract": counts.get("completed", 0),
                "running_intervals": counts.get("running", 0),
                "queued_intervals": counts.get("queued", 0),
                "failed_needs_review": counts.get("failed_needs_review", 0),
                "not_submitted": counts.get("not_submitted", 0),
            },
            "intervals": intervals,
            "batches": [
                {"batch": "part1", "status": batch_status},
                {"batch": "part2", "status": "not_submitted"},
            ],
            "warnings": [],
        }

    def test_ingest_replays_transitions_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scans = root / "scans"
            states = root / "state"
            self.write_scan(
                scans,
                "scattered_joint_calling",
                "20260714T190000Z",
                self.scattered_observation(),
                self.scattered_status(
                    ["completed", "running", "queued", "not_submitted"],
                    overall="running",
                    batch_status="active",
                ),
            )
            self.write_scan(
                scans,
                "scattered_joint_calling",
                "20260714T193000Z",
                self.scattered_observation(),
                self.scattered_status(
                    ["completed", "completed", "failed_needs_review", "not_submitted"],
                    overall="attention_required",
                    batch_status="attention_required",
                ),
            )

            bridge = TaskStateBridge(scan_root=scans, state_root=states)
            result = bridge.ingest("scattered_joint_calling")
            self.assertEqual(result.source_scans, 2)
            self.assertEqual(result.latest_scan_id, "20260714T193000Z")
            self.assertEqual(result.overall_status, "attention_required")

            state_dir = states / "scattered_joint_calling"
            current = json.loads((state_dir / "current_state.json").read_text())
            self.assertEqual(current["unit_state_counts"]["completed"], 2)
            self.assertTrue(
                current["execution_gate"]["evidence_sufficient_for_action_planning"]
            )
            self.assertFalse(current["execution_gate"]["automatic_execution_allowed"])

            events = [
                json.loads(line)
                for line in (state_dir / "events.jsonl").read_text().splitlines()
            ]
            event_types = [item["event_type"] for item in events]
            self.assertIn("baseline_created", event_types)
            self.assertIn("overall_status_changed", event_types)
            state_changes = [
                item for item in events if item["event_type"] == "unit_state_changed"
            ]
            self.assertEqual(
                {(item["details"]["unit_id"], item["details"]["to"]) for item in state_changes},
                {("2", "completed"), ("3", "failed_needs_review")},
            )
            failed_event = next(
                item for item in state_changes if item["details"]["unit_id"] == "3"
            )
            self.assertEqual(
                failed_event["details"]["evidence"]["scheduler_state"],
                "TIMEOUT",
            )

            recommendations = json.loads(
                (state_dir / "recommendations.json").read_text()
            )
            failed = next(
                item for item in recommendations["recommendations"]
                if item["action"] == "inspect_failed_intervals"
            )
            self.assertEqual(failed["scope"]["tasks"], [3])
            self.assertEqual(
                failed["evidence"]["failures"][0]["stderr_log"],
                "/logs/part1_100_3.err",
            )

            before = {
                path.name: path.read_bytes() for path in result.artifact_paths
            }
            repeated = bridge.ingest("scattered_joint_calling")
            after = {
                path.name: path.read_bytes() for path in repeated.artifact_paths
            }
            self.assertEqual(before, after)

    def test_scheduler_failure_blocks_action_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scans = root / "scans"
            states = root / "state"
            self.write_scan(
                scans,
                "scattered_joint_calling",
                "20260714T193000Z",
                self.scattered_observation(scheduler_ok=False),
                self.scattered_status(["not_submitted"], overall="awaiting_submission"),
            )
            TaskStateBridge(scans, states).ingest("scattered_joint_calling")
            state_dir = states / "scattered_joint_calling"
            current = json.loads((state_dir / "current_state.json").read_text())
            self.assertFalse(
                current["execution_gate"]["evidence_sufficient_for_action_planning"]
            )
            self.assertEqual(
                current["execution_gate"]["blockers"],
                ["scheduler_squeue", "scheduler_sacct"],
            )
            recommendations = json.loads(
                (state_dir / "recommendations.json").read_text()
            )
            self.assertEqual(
                recommendations["recommendations"][0]["action"],
                "repair_observation_before_action",
            )
            self.assertEqual(len(recommendations["recommendations"]), 1)

    def test_gam_samples_are_normalized_with_dataset_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scans = root / "scans"
            states = root / "state"
            observation = {
                "scanned_at_cluster_time": "2026-07-14T22:00:00+0300",
                "jobs": {
                    "squeue_returncode": 0,
                    "squeue_stderr": "",
                    "sacct_returncode": 0,
                    "sacct_stderr": "",
                },
                "datasets": [{
                    "name": "own",
                    "directory_errors": [],
                    "manifest": {"duplicate_samples": [], "malformed_lines": []},
                    "samples": [
                        {
                            "sample": "A",
                            "state": "completed_exact_template_pair_match",
                            "output_gam": "/output/A.dedup.gam",
                            "summary": "/output/A.dedup_residual_summary.tsv",
                            "summary_type": "dedup_residual_summary",
                            "exact_template_pair_match": True,
                            "residual_qc_state": "residual_duplicates_detected",
                            "residual_duplicate_primary_reads_after_dedup": 1486493,
                            "residual_duplicate_primary_pct_after_dedup": 4.58789,
                        },
                        {
                            "sample": "B",
                            "state": "output_present_unvalidated",
                            "output_gam": "/output/B.dedup.gam",
                        },
                    ],
                }],
            }
            status = {
                "overall_status": "incomplete_no_active_job",
                "current_stage": "gam_deduplication",
                "next_safe_action": "review_pending_samples_before_any_rerun",
                "counts": {"expected_samples": 2, "outputs_present": 2},
                "datasets": [{"dataset": "own", "status": "incomplete_no_active_job"}],
                "warnings": [],
            }
            self.write_scan(
                scans,
                "gam_deduplication",
                "20260714T193000Z",
                observation,
                status,
            )
            TaskStateBridge(scans, states).ingest("gam_deduplication")
            state_dir = states / "gam_deduplication"
            current = json.loads((state_dir / "current_state.json").read_text())
            self.assertEqual(
                [item["unit_id"] for item in current["units"]],
                ["own:A", "own:B"],
            )
            sample_a = next(
                item for item in current["units"] if item["unit_id"] == "own:A"
            )
            self.assertEqual(sample_a["summary_type"], "dedup_residual_summary")
            self.assertEqual(
                sample_a["residual_qc_state"], "residual_duplicates_detected"
            )
            recommendations = json.loads(
                (state_dir / "recommendations.json").read_text()
            )
            validation = next(
                item for item in recommendations["recommendations"]
                if item["action"] == "validate_unconfirmed_gam_outputs"
            )
            self.assertEqual(validation["scope"]["samples"], ["own:B"])
            residual = next(
                item for item in recommendations["recommendations"]
                if item["action"] == "review_observed_residual_duplication"
            )
            self.assertEqual(residual["scope"]["samples"], ["own:A"])
            self.assertAlmostEqual(
                residual["evidence"]["observations"][0][
                    "residual_duplicate_primary_pct"
                ],
                4.58789,
            )

    def test_rejects_non_read_only_scan_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scans = root / "scans"
            self.write_scan(
                scans,
                "scattered_joint_calling",
                "20260714T193000Z",
                self.scattered_observation(),
                self.scattered_status(["completed"]),
                mode="write_enabled",
            )
            with self.assertRaisesRegex(TaskStateError, "non-read-only"):
                TaskStateBridge(scans, root / "state").ingest(
                    "scattered_joint_calling"
                )

    def test_previously_ingested_scan_history_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scans = root / "scans"
            states = root / "state"
            path = self.write_scan(
                scans,
                "scattered_joint_calling",
                "20260714T193000Z",
                self.scattered_observation(),
                self.scattered_status(["completed"]),
            )
            bridge = TaskStateBridge(scans, states)
            bridge.ingest("scattered_joint_calling")

            payload = json.loads(path.read_text())
            payload["status_summary"]["overall_status"] = "edited_after_ingest"
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(TaskStateError, "changed content"):
                bridge.ingest("scattered_joint_calling")

    def test_cli_ingest_writes_standard_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scans = root / "scans"
            states = root / "state"
            self.write_scan(
                scans,
                "scattered_joint_calling",
                "20260714T193000Z",
                self.scattered_observation(),
                self.scattered_status(["completed"], overall="scattered_genotyping_complete"),
            )
            script = Path(__file__).resolve().parents[1] / "scripts" / "task_state.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "ingest",
                    "scattered_joint_calling",
                    "--scan-root",
                    str(scans),
                    "--state-root",
                    str(states),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Source scans    : 1", result.stdout)
            self.assertIn("Overall status  : scattered_genotyping_complete", result.stdout)
            self.assertTrue(
                (states / "scattered_joint_calling" / "provenance.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
