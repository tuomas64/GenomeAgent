from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from genomeagent.resource_evidence import (
    ResourceCollector,
    ResourceEvidenceCore,
    ResourceEvidenceError,
    parse_slurm_duration,
    parse_slurm_memory,
)


def sacct_line(*values):
    return "|".join(str(value) for value in values) + "|\n"


class QueueRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append((list(command), timeout))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=self.outputs.pop(0),
            stderr="",
        )


class ResourceEvidenceTests(unittest.TestCase):
    def write_scan(
        self,
        scan_root,
        task,
        scan_id,
        recent_jobs,
        intervals=None,
        configuration=None,
    ):
        scan_dir = scan_root / task / scan_id
        scan_dir.mkdir(parents=True)
        payload = {
            "schema_version": "1.0",
            "task": task,
            "mode": "read_only",
            "host": "puhti",
            "created_at_utc": "{}Z".format(scan_id[:8] + "T" + scan_id[9:15]),
            "configuration": configuration or {"profile_version": "fixture"},
            "observation": {
                "jobs": {
                    "recent": recent_jobs,
                    "running": [],
                    "squeue_returncode": 0,
                    "sacct_returncode": 0,
                },
            },
            "status_summary": {
                "overall_status": "running",
                "intervals": intervals or [],
            },
        }
        (scan_dir / "task_scan.json").write_text(json.dumps(payload) + "\n")

    def task16_attempt(self, job_id, state, elapsed, total_cpu, partition, max_rss):
        root = sacct_line(
            job_id,
            "GTscatter_p1",
            partition,
            state,
            elapsed,
            "7-00:00:00" if state == "COMPLETED" else "1-12:00:00",
            1,
            total_cpu,
            elapsed,
            "24G",
            "",
            "r17c36" if state == "COMPLETED" else "r17c14",
            "0:0",
        )
        batch = sacct_line(
            job_id + ".batch",
            "batch",
            "",
            "COMPLETED" if state == "COMPLETED" else "CANCELLED",
            elapsed,
            "",
            1,
            total_cpu,
            elapsed,
            "",
            max_rss,
            "r17c36" if state == "COMPLETED" else "r17c14",
            "0:0" if state == "COMPLETED" else "0:15",
        )
        return root + batch

    def test_slurm_parsers_handle_real_accounting_formats(self):
        self.assertEqual(parse_slurm_duration("1-12:00:18"), 129618)
        self.assertAlmostEqual(parse_slurm_duration("09:12.435"), 552.435)
        self.assertEqual(parse_slurm_memory("24G"), 24 * 1024 ** 3)
        self.assertEqual(parse_slurm_memory("4599696K"), 4599696 * 1024)
        self.assertEqual(parse_slurm_memory("18862377K"), 18862377 * 1024)

    def test_task16_attempt_history_flags_transient_runtime_anomaly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = QueueRunner([
                self.task16_attempt(
                    "35442372_16", "TIMEOUT", 129618, "00:00.919", "small", "4599696K"
                ),
                self.task16_attempt(
                    "35452993_16", "COMPLETED", 6338, "09:12.435", "longrun", "4599696K"
                ),
            ])
            collector = ResourceCollector(
                evidence_root=root / "evidence",
                scan_root=root / "scans",
                runner=runner,
            )
            first = collector.collect(
                "scattered_joint_calling",
                ["35442372_16"],
                "scattered_genotypegvcfs_250kb",
                unit_overrides={"35442372_16": "16"},
            )
            second = collector.collect(
                "scattered_joint_calling",
                ["35452993_16"],
                "scattered_genotypegvcfs_250kb",
                unit_overrides={"35452993_16": "16"},
            )
            self.assertNotEqual(first.evidence_path, second.evidence_path)
            for command, timeout in runner.commands:
                joined = " ".join(command)
                self.assertIn("sacct", command)
                self.assertNotIn("sbatch", joined)
                self.assertNotIn("scancel", joined)
                self.assertNotIn("rm ", joined)
                self.assertEqual(timeout, 60)

            core = ResourceEvidenceCore(root / "evidence", root / "resources")
            result = core.ingest("scattered_joint_calling")
            self.assertEqual(result.observations, 2)
            self.assertEqual(result.current_attempts, 2)
            self.assertEqual(result.profiles, 1)

            resource_dir = root / "resources" / "scattered_joint_calling"
            profiles = json.loads((resource_dir / "resource_profiles.json").read_text())
            profile = profiles["profiles"][0]
            self.assertEqual(profile["observation_counts"]["successful"], 1)
            self.assertEqual(profile["observation_counts"]["timeout"], 1)
            self.assertEqual(profile["confidence"], "insufficient")
            self.assertEqual(
                profile["proposed_resources"]["status"], "insufficient_evidence"
            )

            anomalies = json.loads((resource_dir / "resource_anomalies.json").read_text())
            by_type = {item["type"]: item for item in anomalies["anomalies"]}
            self.assertIn("probable_transient_runtime_stall", by_type)
            transient = by_type["probable_transient_runtime_stall"]
            self.assertEqual(transient["unit_id"], "16")
            self.assertGreater(transient["evidence"]["elapsed_ratio"], 20)
            self.assertAlmostEqual(transient["evidence"]["peak_memory_ratio"], 1.0)
            self.assertIn("low_cpu_efficiency_possible_waiting", by_type)

            recommendations = json.loads(
                (resource_dir / "resource_recommendations.json").read_text()
            )
            self.assertFalse(recommendations["automatic_execution_allowed"])
            self.assertFalse(recommendations["automatic_scheduler_changes_allowed"])
            actions = {item["action"] for item in recommendations["recommendations"]}
            self.assertEqual(actions, {
                "review_resource_anomalies",
                "collect_more_successful_resource_evidence",
            })
            self.assertTrue(all(
                not item["execution_enabled"]
                for item in recommendations["recommendations"]
            ))
            report = (resource_dir / "report.md").read_text()
            self.assertIn("probable_transient_runtime_stall", report)
            profile_tsv = (resource_dir / "resource_profile_summary.tsv").read_text()
            self.assertIn("scattered_genotypegvcfs_250kb", profile_tsv)
            anomaly_tsv = (resource_dir / "resource_anomalies.tsv").read_text()
            self.assertIn(
                "runtime_variation:puhti:scattered_genotypegvcfs_250kb:16",
                anomaly_tsv,
            )

            before = {
                path.name: path.read_bytes() for path in result.artifact_paths
            }
            repeated = core.ingest("scattered_joint_calling")
            after = {
                path.name: path.read_bytes() for path in repeated.artifact_paths
            }
            self.assertEqual(before, after)

    def test_at02_peak_memory_is_retained_without_automatic_tuning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = sacct_line(
                "35448117_2", "swe_gam_dedup_residual", "small", "COMPLETED",
                5640, "1-00:00:00", 1, "58:10.000", 5640, "32G", "", "r18c20", "0:0"
            ) + sacct_line(
                "35448117_2.batch", "batch", "", "COMPLETED", 5640, "", 1,
                "58:10.000", 5640, "", "18862377K", "r18c20", "0:0"
            )
            collector = ResourceCollector(
                evidence_root=root / "evidence",
                scan_root=root / "scans",
                runner=QueueRunner([output]),
            )
            collector.collect(
                "gam_deduplication",
                ["35448117_2"],
                "gam_dedup_residual_qc",
                unit_overrides={"35448117_2": "swedish:AT02"},
            )
            ResourceEvidenceCore(root / "evidence", root / "resources").ingest(
                "gam_deduplication"
            )
            lines = (
                root / "resources" / "gam_deduplication" / "resource_observations.jsonl"
            ).read_text().splitlines()
            observation = json.loads(lines[0])
            self.assertEqual(observation["unit_id"], "swedish:AT02")
            self.assertEqual(observation["max_rss_bytes"], 18862377 * 1024)
            self.assertAlmostEqual(
                observation["memory_efficiency"],
                (18862377 * 1024) / (32 * 1024 ** 3),
            )
            profiles = json.loads((
                root / "resources" / "gam_deduplication" / "resource_profiles.json"
            ).read_text())
            self.assertEqual(
                profiles["profiles"][0]["proposed_resources"]["status"],
                "insufficient_evidence",
            )

    def test_three_successes_create_reviewable_rounded_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_dir = root / "evidence" / "gam_deduplication"
            evidence_dir.mkdir(parents=True)
            observations = []
            for number, elapsed, max_gib in (
                (1, 3600, 10),
                (2, 7200, 12),
                (3, 5400, 11),
            ):
                observations.append({
                    "task": "gam_deduplication",
                    "profile_key": "gam_dedup_fast",
                    "attempt_id": str(number),
                    "unit_id": "own:S{}".format(number),
                    "state": "COMPLETED",
                    "elapsed_seconds": elapsed,
                    "allocated_cpus": 1,
                    "total_cpu_seconds": elapsed * 0.8,
                    "requested_memory_bytes": 24 * 1024 ** 3,
                    "max_rss_bytes": max_gib * 1024 ** 3,
                })
            snapshot = {
                "schema_version": "1.0",
                "task": "gam_deduplication",
                "source_mode": "read_only_scheduler_accounting",
                "observed_at_utc": "2026-07-15T00:00:00Z",
                "observations": observations,
            }
            (evidence_dir / "three.json").write_text(json.dumps(snapshot) + "\n")
            ResourceEvidenceCore(root / "evidence", root / "resources").ingest(
                "gam_deduplication"
            )
            profiles = json.loads((
                root / "resources" / "gam_deduplication" / "resource_profiles.json"
            ).read_text())
            profile = profiles["profiles"][0]
            proposal = profile["proposed_resources"]
            self.assertEqual(profile["confidence"], "low")
            self.assertEqual(
                proposal["status"], "proposal_available_for_researcher_review"
            )
            self.assertEqual(proposal["memory_bytes"], 15 * 1024 ** 3)
            self.assertEqual(proposal["time_limit_seconds"], 10800)
            self.assertFalse(proposal["automatic_application_allowed"])

    def test_profiles_and_attempt_identities_are_isolated_by_source_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_dir = root / "evidence" / "gam_deduplication"
            evidence_dir.mkdir(parents=True)
            for host, elapsed, memory in (
                ("puhti", 100, 1024),
                ("roihu", 50, 2048),
            ):
                snapshot = {
                    "schema_version": "1.0",
                    "task": "gam_deduplication",
                    "source_mode": "read_only_scheduler_accounting",
                    "observed_at_utc": "2026-07-15T00:00:00Z",
                    "host": host,
                    "observations": [{
                        "task": "gam_deduplication",
                        "profile_key": "gam_dedup_fast",
                        "attempt_id": "12345_1",
                        "unit_id": "",
                        "state": "COMPLETED",
                        "elapsed_seconds": elapsed,
                        "allocated_cpus": 1,
                        "total_cpu_seconds": elapsed,
                        "requested_memory_bytes": 4096,
                        "max_rss_bytes": memory,
                    }],
                }
                (evidence_dir / (host + ".json")).write_text(
                    json.dumps(snapshot) + "\n"
                )

            result = ResourceEvidenceCore(
                root / "evidence", root / "resources"
            ).ingest("gam_deduplication")
            self.assertEqual(result.current_attempts, 2)
            self.assertEqual(result.profiles, 2)
            profiles = json.loads((
                root / "resources" / "gam_deduplication" / "resource_profiles.json"
            ).read_text())["profiles"]
            self.assertEqual(
                [item["source_host"] for item in profiles], ["puhti", "roihu"]
            )
            self.assertTrue(all(
                item["observation_counts"]["successful"] == 1
                for item in profiles
            ))

    def test_collect_new_discovers_only_unevidenced_terminal_scatter_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scans = root / "scans"
            evidence = root / "evidence"
            configuration = {
                "profile_version": "fixture",
                "scatter_batches": [{
                    "name": "part1",
                    "job_name": "GTscatter_p1",
                    "array_start": 1,
                    "array_end": 4,
                    "offset": 0,
                }],
            }
            intervals = [{
                "task": number,
                "batch": "part1",
                "chromosome": "1",
                "interval": "chr1:{}-{}".format(number, number),
                "scheduler_job_id": "100_{}".format(number),
            } for number in range(1, 5)]
            self.write_scan(
                scans,
                "scattered_joint_calling",
                "20260715T010000Z",
                [
                    {"job_id": "100_1", "name": "GTscatter_p1", "state": "COMPLETED", "array_task_id": 1},
                    {"job_id": "100_2", "name": "GTscatter_p1", "state": "RUNNING", "array_task_id": 2},
                    {"job_id": "100", "name": "GTscatter_p1", "state": "COMPLETED"},
                ],
                intervals=intervals,
                configuration=configuration,
            )
            self.write_scan(
                scans,
                "scattered_joint_calling",
                "20260715T020000Z",
                [
                    {"job_id": "100_1", "name": "GTscatter_p1", "state": "COMPLETED", "array_task_id": 1},
                    {"job_id": "100_2", "name": "GTscatter_p1", "state": "COMPLETED", "array_task_id": 2},
                    {"job_id": "100_3", "name": "GTscatter_p1", "state": "TIMEOUT", "array_task_id": 3},
                    {"job_id": "100", "name": "GTscatter_p1", "state": "COMPLETED"},
                ],
                intervals=intervals,
                configuration=configuration,
            )
            evidence_dir = evidence / "scattered_joint_calling"
            evidence_dir.mkdir(parents=True)
            existing = {
                "schema_version": "1.0",
                "task": "scattered_joint_calling",
                "source_mode": "read_only_scheduler_accounting",
                "observed_at_utc": "2026-07-15T01:30:00Z",
                "observations": [{
                    "task": "scattered_joint_calling",
                    "profile_key": "scattered_genotypegvcfs_250kb",
                    "attempt_id": "100_1",
                    "unit_id": "1",
                    "state": "COMPLETED",
                    "elapsed_seconds": 100,
                }],
            }
            (evidence_dir / "existing.json").write_text(json.dumps(existing) + "\n")

            output = self.task16_attempt(
                "100_2", "COMPLETED", 200, "02:30.000", "small", "1024K"
            ) + self.task16_attempt(
                "100_3", "TIMEOUT", 300, "01:00.000", "small", "1024K"
            )
            runner = QueueRunner([output])
            collector = ResourceCollector(evidence, scans, runner=runner)
            dry_plan = collector.discover_new(
                "scattered_joint_calling", maximum_attempts=20
            )
            self.assertEqual(dry_plan.source_scans, 2)
            self.assertEqual(dry_plan.terminal_attempts_seen, 3)
            self.assertEqual(dry_plan.already_evidenced, 1)
            self.assertEqual(dry_plan.ignored_non_array_records, 1)
            self.assertEqual(
                [item["attempt_id"] for item in dry_plan.selected],
                ["100_2", "100_3"],
            )
            self.assertEqual(
                [item["unit_id"] for item in dry_plan.selected], ["2", "3"]
            )
            self.assertEqual(len(runner.commands), 0)

            result = collector.collect_new("scattered_joint_calling")
            self.assertEqual(len(result.collections), 1)
            self.assertEqual(len(runner.commands), 1)
            command = " ".join(runner.commands[0][0])
            self.assertIn("100_2,100_3", command)
            self.assertNotIn("100_1", command)
            self.assertNotIn(" 100 ", command)
            collected_payload = json.loads(
                result.collections[0].evidence_path.read_text()
            )
            collected_by_attempt = {
                item["attempt_id"]: item
                for item in collected_payload["observations"]
            }
            self.assertEqual(collected_by_attempt["100_2"]["unit_id"], "2")
            self.assertEqual(
                collected_by_attempt["100_2"]["workload"]["interval"],
                "chr1:2-2",
            )

            repeated = collector.collect_new("scattered_joint_calling")
            self.assertEqual(repeated.collections, ())
            self.assertEqual(len(repeated.plan.selected), 0)
            self.assertEqual(len(runner.commands), 1)

    def test_collect_new_bounds_candidates_and_profiles_gam_fast_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scans = root / "scans"
            recent = [
                {
                    "job_id": "200_{}".format(number),
                    "name": "own_gam_dedup_fast",
                    "state": "COMPLETED",
                }
                for number in range(1, 4)
            ]
            recent.append({
                "job_id": "200_4",
                "name": "own_gam_dedup_fast",
                "state": "CANCELLED by 12345",
            })
            self.write_scan(
                scans,
                "gam_deduplication",
                "20260715T030000Z",
                recent,
            )
            collector = ResourceCollector(root / "evidence", scans, runner=QueueRunner([]))
            plan = collector.discover_new("gam_deduplication", maximum_attempts=2)
            self.assertEqual(
                [item["attempt_id"] for item in plan.selected], ["200_1", "200_2"]
            )
            self.assertEqual(plan.remaining, 2)
            self.assertTrue(all(
                item["profile_key"] == "gam_dedup_fast" for item in plan.selected
            ))
            self.assertTrue(all(item["unit_id"] == "" for item in plan.selected))

    def test_scatter_profile_aggregates_cpu_pattern_and_withholds_narrow_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_dir = root / "evidence" / "scattered_joint_calling"
            evidence_dir.mkdir(parents=True)
            observations = []
            for number in range(1, 22):
                observations.append({
                    "task": "scattered_joint_calling",
                    "profile_key": "scattered_genotypegvcfs_250kb",
                    "attempt_id": "300_{}".format(number),
                    "unit_id": str(number),
                    "state": "COMPLETED",
                    "elapsed_seconds": 10000 + number,
                    "allocated_cpus": 1,
                    "total_cpu_seconds": 500,
                    "requested_memory_bytes": 24 * 1024 ** 3,
                    "max_rss_bytes": 5 * 1024 ** 3,
                    "workload": {
                        "batch": "part1",
                        "chromosome": "1",
                        "interval": "chr1:{}-{}".format(number, number),
                    },
                })
            snapshot = {
                "schema_version": "1.0",
                "task": "scattered_joint_calling",
                "source_mode": "read_only_scheduler_accounting",
                "observed_at_utc": "2026-07-15T04:00:00Z",
                "observations": observations,
            }
            (evidence_dir / "narrow.json").write_text(json.dumps(snapshot) + "\n")
            resource_dir = root / "resources" / "scattered_joint_calling"
            ResourceEvidenceCore(root / "evidence", root / "resources").ingest(
                "scattered_joint_calling"
            )

            profiles = json.loads(
                (resource_dir / "resource_profiles.json").read_text()
            )
            profile = profiles["profiles"][0]
            self.assertEqual(profile["sample_size_confidence"], "high")
            self.assertEqual(profile["confidence"], "low")
            self.assertEqual(profile["workload_coverage"]["unique_batches"], 1)
            self.assertEqual(profile["workload_coverage"]["unique_chromosomes"], 1)
            self.assertEqual(
                profile["proposed_resources"]["status"],
                "insufficient_workload_diversity",
            )
            self.assertIsNone(profile["proposed_resources"]["memory_bytes"])

            anomalies = json.loads(
                (resource_dir / "resource_anomalies.json").read_text()
            )["anomalies"]
            self.assertEqual(len(anomalies), 1)
            self.assertEqual(
                anomalies[0]["type"], "systematic_low_cpu_efficiency_pattern"
            )
            self.assertEqual(
                anomalies[0]["evidence"]["low_efficiency_attempts"], 21
            )
            recommendations = json.loads(
                (resource_dir / "resource_recommendations.json").read_text()
            )["recommendations"]
            actions = {item["action"] for item in recommendations}
            self.assertEqual(actions, {
                "review_resource_anomalies",
                "collect_diverse_resource_evidence",
            })
            report = (resource_dir / "report.md").read_text()
            self.assertIn("1 batches / 1 chromosomes", report)
            self.assertNotIn("proposal_available_for_researcher_review", report)

    def test_changed_or_removed_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_dir = root / "evidence" / "gam_deduplication"
            evidence_dir.mkdir(parents=True)
            path = evidence_dir / "one.json"
            snapshot = {
                "schema_version": "1.0",
                "task": "gam_deduplication",
                "source_mode": "read_only_scheduler_accounting",
                "observed_at_utc": "2026-07-15T00:00:00Z",
                "observations": [{
                    "task": "gam_deduplication",
                    "profile_key": "gam_dedup",
                    "attempt_id": "1",
                    "unit_id": "own:A",
                    "state": "COMPLETED",
                    "elapsed_seconds": 10,
                    "allocated_cpus": 1,
                    "total_cpu_seconds": 8,
                    "requested_memory_bytes": 1024,
                    "max_rss_bytes": 512,
                }],
            }
            path.write_text(json.dumps(snapshot) + "\n")
            core = ResourceEvidenceCore(root / "evidence", root / "resources")
            core.ingest("gam_deduplication")
            snapshot["observations"][0]["elapsed_seconds"] = 20
            path.write_text(json.dumps(snapshot) + "\n")
            with self.assertRaisesRegex(ResourceEvidenceError, "changed content"):
                core.ingest("gam_deduplication")


if __name__ == "__main__":
    unittest.main()
