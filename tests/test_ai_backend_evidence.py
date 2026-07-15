from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from genomeagent.ai_backend_evidence import (
    AIBackendEvidenceCollector,
    AIBackendEvidenceCore,
    AIBackendEvidenceError,
    _probe_program,
)
from genomeagent.ai_evaluation import AIRegistry


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    def __init__(self, observation, host="roihu-gpu"):
        self.host = host
        self.observation = observation
        self.calls = []

    def run_python(self, program, timeout_seconds=240):
        self.calls.append((program, timeout_seconds))
        return copy.deepcopy(self.observation)


class AIBackendEvidenceTests(unittest.TestCase):
    def registry(self, root: Path) -> AIRegistry:
        return AIRegistry(
            backend_root=root / "config/ai/backends",
            prompt_root=root / "config/ai/prompts",
            suite_root=root / "config/ai/suites",
            case_root=root / "benchmarks/ai",
        )

    def copied_repository(self, root: Path) -> AIRegistry:
        shutil.copytree(REPOSITORY_ROOT / "config/ai", root / "config/ai")
        shutil.copytree(REPOSITORY_ROOT / "benchmarks/ai", root / "benchmarks/ai")
        return self.registry(root)

    def observation(self, model_exists=False):
        return {
            "schema_version": "1.0",
            "observation_type": "ai_backend_environment",
            "backend_id": "roihu_qwen3_coder",
            "identity": {
                "hostname": "roihu-gpu-login2",
                "machine": "aarch64",
                "platform": "Linux fixture",
                "remote_python_executable": "/usr/bin/python3",
                "remote_python_version": "3.12.0",
            },
            "module": {
                "expected_module": "python-vllm/0.19.1",
                "expected_version": "0.19.1",
                "selected_initialization": "/usr/share/lmod/lmod/init/bash",
                "module_use_paths": [
                    "/appl/modulefiles/manual/aida/aarch64"
                ],
                "availability": {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                    "timed_out": False,
                },
                "package_metadata": {
                    "returncode": 0,
                    "stdout": json.dumps({
                        "package_version": "0.19.1",
                        "python_version": "3.12.0",
                        "machine": "aarch64",
                        "python_executable": "/appl/vllm/bin/python3",
                    }) + "\n",
                    "stderr": "",
                    "timed_out": False,
                },
            },
            "scheduler": {
                "sbatch_path": "/usr/bin/sbatch",
                "sinfo_path": "/usr/bin/sinfo",
                "expected_partition": "gputest",
                "expected_gpu_type": "gh200",
                "partition_query": {
                    "returncode": 0,
                    "stdout": "gputest|up|00:15:00|8|gpu:gh200:4|72|480000\n",
                    "stderr": "",
                    "timed_out": False,
                },
            },
            "storage": {
                "path": "/scratch/project_2001113",
                "exists": True,
                "is_directory": True,
                "is_symlink": False,
                "filesystem_capacity_bytes": 250 * 1024 ** 3,
                "filesystem_available_bytes": 240 * 1024 ** 3,
            },
            "model": {
                "path": (
                    "/scratch/project_2001113/models/"
                    "Qwen3-Coder-30B-A3B-Instruct"
                ),
                "exists": model_exists,
                "is_directory": model_exists,
                "is_symlink": False,
                "scan_depth": 1,
                "large_files_hashed": False,
                "entries": [],
                "entries_observed": 0,
                "entries_truncated": False,
                "observed_git_revision": None,
                "inventory_manifest": {
                    "path": "fixture-manifest",
                    "exists": False,
                    "verified_against_model_files": False,
                },
            },
            "safety": {
                "remote_writes": False,
                "job_submission": False,
                "gpu_allocation": False,
                "model_download": False,
                "model_import": False,
                "recursive_model_scan": False,
                "large_model_file_hashing": False,
            },
        }

    def collector(self, root: Path, registry: AIRegistry) -> AIBackendEvidenceCollector:
        return AIBackendEvidenceCollector(
            registry=registry,
            policy_root=root / "config/ai/evidence",
            evidence_root=root / "evidence",
        )

    def core(self, root: Path, registry: AIRegistry) -> AIBackendEvidenceCore:
        return AIBackendEvidenceCore(
            registry=registry,
            policy_root=root / "config/ai/evidence",
            evidence_root=root / "evidence",
            state_root=root / "state",
        )

    def test_repository_policy_is_bounded_and_explicitly_read_only(self):
        registry = AIRegistry(
            backend_root=REPOSITORY_ROOT / "config/ai/backends",
            prompt_root=REPOSITORY_ROOT / "config/ai/prompts",
            suite_root=REPOSITORY_ROOT / "config/ai/suites",
            case_root=REPOSITORY_ROOT / "benchmarks/ai",
        )
        collector = AIBackendEvidenceCollector(
            registry=registry,
            policy_root=REPOSITORY_ROOT / "config/ai/evidence",
        )
        _, policy, _ = collector.policy("roihu_qwen3_coder")
        self.assertEqual(policy["limits"]["maximum_model_entries"], 200)
        self.assertEqual(policy["limits"]["timeout_seconds"], 90)
        self.assertEqual(
            policy["module_initialization_candidates"],
            ["/usr/share/lmod/lmod/init/bash"],
        )
        self.assertEqual(
            policy["module_use_paths"],
            ["/appl/modulefiles/manual/aida/aarch64"],
        )
        self.assertTrue(all(value is False for value in policy["safety"].values()))

    def test_collection_writes_immutable_snapshot_without_execution_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            collector = self.collector(root, registry)
            runner = FakeRunner(self.observation())
            result = collector.collect(
                "roihu_qwen3_coder", runner, stamp="20260715T120000Z"
            )
            self.assertEqual(
                result.environment_status, "model_identity_and_acquisition_required"
            )
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][1], 90)
            payload = json.loads(result.evidence_path.read_text())
            self.assertEqual(
                payload["source_mode"], "bounded_read_only_backend_observation"
            )
            self.assertEqual(payload["query"]["model_inventory_depth"], 1)
            self.assertTrue(all(value is False for value in payload["safety"].values()))
            self.assertEqual(payload["readiness"]["environment_blockers"], [])
            self.assertIn("model_path_absent", payload["readiness"]["model_blockers"])
            report = result.evidence_path.with_suffix(".md").read_text()
            self.assertIn("Large model files hashed: **no**", report)
            with self.assertRaisesRegex(
                AIBackendEvidenceError, "overwrite immutable backend evidence"
            ):
                collector.collect(
                    "roihu_qwen3_coder", runner, stamp="20260715T120000Z"
                )

    def test_ingest_rebuilds_current_state_without_editing_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            backend_path = root / "config/ai/backends/roihu_qwen3_coder.json"
            backend_before = backend_path.read_bytes()
            self.collector(root, registry).collect(
                "roihu_qwen3_coder",
                FakeRunner(self.observation()),
                stamp="20260715T120000Z",
            )
            result = self.core(root, registry).ingest("roihu_qwen3_coder")
            self.assertEqual(result.snapshots, 1)
            self.assertEqual(result.status, "model_identity_and_acquisition_required")
            self.assertEqual(
                result.next_safe_action, "review_pinned_model_acquisition_plan"
            )
            self.assertEqual(backend_path.read_bytes(), backend_before)
            readiness = json.loads((result.state_dir / "readiness.json").read_text())
            self.assertFalse(readiness["automatic_execution_allowed"])
            self.assertFalse(readiness["automatic_model_download_allowed"])
            self.assertFalse(readiness["automatic_job_submission_allowed"])
            provenance = json.loads((result.state_dir / "provenance.json").read_text())
            self.assertFalse(provenance["remote_access_during_ingest"])
            self.assertFalse(provenance["automatic_registry_update"])

    def test_changed_policy_marks_saved_observation_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.collector(root, registry).collect(
                "roihu_qwen3_coder",
                FakeRunner(self.observation()),
                stamp="20260715T120000Z",
            )
            policy_path = root / "config/ai/evidence/roihu_qwen3_coder.json"
            policy = json.loads(policy_path.read_text())
            policy["limits"]["maximum_model_entries"] = 201
            policy_path.write_text(json.dumps(policy) + "\n", encoding="utf-8")
            result = self.core(root, registry).ingest("roihu_qwen3_coder")
            self.assertEqual(result.status, "environment_evidence_stale")
            self.assertIn("configuration_changed_since_observation", result.blockers)

    def test_unsafe_policy_and_unsafe_remote_claim_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            policy_path = root / "config/ai/evidence/roihu_qwen3_coder.json"
            policy = json.loads(policy_path.read_text())
            policy["safety"]["job_submission_allowed"] = True
            policy_path.write_text(json.dumps(policy) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AIBackendEvidenceError, "job_submission_allowed to false"
            ):
                self.collector(root, registry).policy("roihu_qwen3_coder")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            unsafe = self.observation()
            unsafe["safety"]["job_submission"] = True
            with self.assertRaisesRegex(
                AIBackendEvidenceError, "disable job_submission"
            ):
                self.collector(root, registry).collect(
                    "roihu_qwen3_coder",
                    FakeRunner(unsafe),
                    stamp="20260715T120000Z",
                )

    def test_ingest_rejects_snapshot_with_mutating_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            result = self.collector(root, registry).collect(
                "roihu_qwen3_coder",
                FakeRunner(self.observation()),
                stamp="20260715T120000Z",
            )
            payload = json.loads(result.evidence_path.read_text())
            payload["safety"]["automatic_registry_update"] = True
            result.evidence_path.write_text(
                json.dumps(payload) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                AIBackendEvidenceError, "disable automatic_registry_update"
            ):
                self.core(root, registry).ingest("roihu_qwen3_coder")

    def test_probe_program_has_bounded_inventory_and_no_mutating_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            _, backend, _ = registry.backend("roihu_qwen3_coder")
            _, policy, _ = self.collector(root, registry).policy(
                "roihu_qwen3_coder"
            )
            program = _probe_program(backend, policy)
            self.assertIn("os.scandir(path_text)", program)
            self.assertIn('source "$MODULE_INIT"', program)
            self.assertIn('module use "$module_path"', program)
            self.assertIn('"bash", "-c", module_prefix', program)
            self.assertNotIn('["bash", "-lic"]', program)
            self.assertNotIn("os.walk", program)
            self.assertNotIn("nvidia-smi", program)
            self.assertNotIn("huggingface_hub", program)
            self.assertNotIn("snapshot_download", program)
            self.assertNotIn("subprocess.run([\"sbatch\"", program)
            self.assertNotIn("rm -", program)
            self.assertLess(len(program), 20000)

    def test_unsafe_module_use_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            policy_path = root / "config/ai/evidence/roihu_qwen3_coder.json"
            policy = json.loads(policy_path.read_text())
            policy["module_use_paths"] = ["/appl/modules; touch /tmp/unsafe"]
            policy_path.write_text(json.dumps(policy) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AIBackendEvidenceError, "unsupported shell characters"
            ):
                self.collector(root, registry).policy("roihu_qwen3_coder")

    def test_cli_ingest_reports_model_acquisition_as_next_review_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_repository(root)
            self.collector(root, registry).collect(
                "roihu_qwen3_coder",
                FakeRunner(self.observation()),
                stamp="20260715T120000Z",
            )
            command = [
                sys.executable,
                "scripts/ai_backend_evidence.py",
                "ingest",
                "roihu_qwen3_coder",
                "--backend-root", str(root / "config/ai/backends"),
                "--prompt-root", str(root / "config/ai/prompts"),
                "--suite-root", str(root / "config/ai/suites"),
                "--case-root", str(root / "benchmarks/ai"),
                "--policy-root", str(root / "config/ai/evidence"),
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
            self.assertIn("Remote access     : none during ingest", completed.stdout)
            self.assertIn(
                "Status            : model_identity_and_acquisition_required",
                completed.stdout,
            )
            self.assertIn(
                "Next safe action  : review_pinned_model_acquisition_plan",
                completed.stdout,
            )


if __name__ == "__main__":
    unittest.main()
