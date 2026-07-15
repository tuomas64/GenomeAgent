from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from genomeagent.ai_evaluation import AIRegistry
from genomeagent.model_acquisition_preflight import (
    ModelAcquisitionPreflightCollector,
    ModelAcquisitionPreflightCore,
    ModelAcquisitionPreflightError,
    _probe_program,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ID = "roihu_qwen3_coder"
PLAN_ID = "7" * 64
APPROVAL_ID = "5" * 64
REVISION = "b" * 40


def canonical_sha256(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class FakeRunner:
    def __init__(self, observation, host="roihu-gpu"):
        self.observation = observation
        self.host = host
        self.calls = []

    def run_python(self, program, timeout_seconds=240):
        self.calls.append((program, timeout_seconds))
        return copy.deepcopy(self.observation)


class ModelAcquisitionPreflightTests(unittest.TestCase):
    def setup_repository(self, root: Path):
        shutil.copytree(REPOSITORY_ROOT / "config/ai", root / "config/ai")
        shutil.copytree(REPOSITORY_ROOT / "benchmarks/ai", root / "benchmarks/ai")
        registry = AIRegistry(
            backend_root=root / "config/ai/backends",
            prompt_root=root / "config/ai/prompts",
            suite_root=root / "config/ai/suites",
            case_root=root / "benchmarks/ai",
        )
        _, backend, _ = registry.backend(BACKEND_ID)
        installation = backend["installation"]["path"]
        approval_sha = "a" * 64
        source_sha = "c" * 64
        identity = {
            "policy_version": "1.0",
            "backend_id": BACKEND_ID,
            "plan_id": PLAN_ID,
            "approval_id": APPROVAL_ID,
            "approval_sha256": approval_sha,
            "source_evidence_id": "20260715T100000000000Z",
            "source_evidence_sha256": source_sha,
        }
        bundle_id = canonical_sha256(identity)
        inventory = [
            {
                "path": "config.json",
                "size_bytes": 1000,
                "provider_lfs_sha256": None,
                "provider_git_blob_id": "1" * 40,
                "provider_xet_hash": None,
            },
            {
                "path": "model.safetensors",
                "size_bytes": 61_079_825_794,
                "provider_lfs_sha256": "2" * 64,
                "provider_git_blob_id": "3" * 40,
                "provider_xet_hash": None,
            },
        ]
        staging = str(
            Path(installation).parent
            / (".genomeagent-staging-Qwen3-Coder-30B-A3B-Instruct-" + bundle_id[:16])
        )
        bundle = {
            "schema_version": "1.0",
            "policy_version": "1.0",
            "source_mode": "deterministic_local_execution_bundle_preparation",
            "bundle_id": bundle_id,
            **identity,
            "repository": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "resolved_revision": REVISION,
            "target": {
                "project_storage_root": "/scratch/project_2001113",
                "staging_path": staging,
                "installation_path": installation,
                "manifest_path": installation + "/.genomeagent-model-manifest.json",
                "publication_strategy": "atomic_directory_rename_same_filesystem",
            },
            "approved_source_inventory": inventory,
            "integrity_contract": {
                "expected_file_count": len(inventory),
                "expected_total_bytes": sum(item["size_bytes"] for item in inventory),
            },
            "execution_contract": {
                "remote_worker_generated": False,
                "remote_command_generated": False,
                "credentials_included": False,
                "fresh_environment_preflight_required": True,
                "separate_explicit_execution_authorization_required": True,
            },
            "automatic_execution_allowed": False,
            "automatic_remote_access_allowed": False,
            "automatic_model_download_allowed": False,
            "automatic_job_submission_allowed": False,
            "automatic_publication_allowed": False,
            "automatic_registry_update_allowed": False,
            "automatic_backend_activation_allowed": False,
        }
        blockers = [
            "acquisition_runtime_unverified",
            "transfer_execution_context_unregistered",
            "remote_target_state_unverified",
            "fresh_execution_authorization_missing",
        ]
        readiness = {
            "schema_version": "1.0",
            "policy_version": "1.0",
            "backend_id": BACKEND_ID,
            "plan_id": PLAN_ID,
            "approval_id": APPROVAL_ID,
            "bundle_id": bundle_id,
            "status": "environment_execution_preflight_required",
            "blockers": blockers,
            "automatic_execution_allowed": False,
        }
        bundle_dir = root / "bundles" / BACKEND_ID / bundle_id
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "acquisition_bundle.json").write_text(json.dumps(bundle) + "\n")
        (bundle_dir / "execution_readiness.json").write_text(json.dumps(readiness) + "\n")
        (bundle_dir / "report.md").write_text("fixture bundle report\n")
        (bundle_dir / "provenance.json").write_text(json.dumps({
            "schema_version": "1.0", "backend_id": BACKEND_ID, "bundle_id": bundle_id
        }) + "\n")
        plan_dir = root / "plans" / BACKEND_ID / PLAN_ID
        plan_dir.mkdir(parents=True)
        plan = {
            "schema_version": "1.0", "backend_id": BACKEND_ID, "plan_id": PLAN_ID,
            "status": "ready_for_researcher_acquisition_review", "blockers": [],
            "storage": {"required_working_free_bytes": 128_267_636_268},
            "automatic_execution_allowed": False,
        }
        (plan_dir / "model_acquisition_plan.json").write_text(json.dumps(plan) + "\n")
        return registry, backend, bundle_id, bundle

    def observation(self, bundle_id, bundle, available="4.0K/250G"):
        device = 42
        root_state = {
            "path": "/scratch/project_2001113", "exists": True, "lexists": True,
            "is_symlink": False, "is_directory": True, "is_file": False,
            "device": device, "mode": 0o770,
        }
        parent = str(Path(bundle["target"]["installation_path"]).parent)
        ancestor = {
            "path": "/scratch/project_2001113", "exists": True, "lexists": True,
            "is_symlink": False, "is_directory": True, "is_file": False,
            "device": device, "mode": 0o770, "missing_components": 1,
        }
        chain = {"within_root": True, "entries": [root_state],
                 "symlink_detected": False, "truncated": False}
        absent = lambda path: {
            "path": path, "exists": False, "lexists": False,
            "is_symlink": False, "is_directory": False, "is_file": False,
        }
        return {
            "schema_version": "1.0",
            "observation_type": "model_acquisition_runtime_preflight",
            "backend_id": BACKEND_ID,
            "bundle_id": bundle_id,
            "identity": {"hostname": "roihu-gpu-login2", "machine": "aarch64",
                         "python_executable": "/usr/bin/python3", "python_version": "3.12.12"},
            "runtime": {
                "selected_initialization": "/usr/share/lmod/lmod/init/bash",
                "expected_module": "python-vllm/0.19.1",
                "availability": {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False},
                "loaded_python_executable": "/appl/vllm/bin/python3",
                "package_metadata": {"returncode": 0, "stdout": json.dumps({
                    "vllm_version": "0.19.1", "huggingface_hub_version": "0.36.0",
                    "snapshot_download_callable": True, "machine": "aarch64",
                }) + "\n", "stderr": "", "timed_out": False},
            },
            "scheduler": {
                "sbatch_path": "/usr/bin/sbatch", "sinfo_path": "/usr/bin/sinfo",
                "expected_partition": "gputest", "expected_gpu_type": "gh200",
                "partition_query": {"returncode": 0,
                    "stdout": "gputest|up|00:15:00|8|gpu:gh200:4|72|480000\n",
                    "stderr": "", "timed_out": False},
            },
            "storage": {
                **root_state, "writable_by_access_check": True,
                "filesystem_available_bytes": 240 * 1024 ** 3,
                "workspace_command_path": "/appl/soft/manual/general/aarch64/csc-tools/bin/csc-workspaces",
                "workspace_command": {"returncode": 0,
                    "stdout": "/scratch/project_2001113 {} 1/500K 180d\n".format(available),
                    "stderr": "", "timed_out": False},
            },
            "target": {
                "staging": absent(bundle["target"]["staging_path"]),
                "installation": absent(bundle["target"]["installation_path"]),
                "staging_nearest_existing": ancestor,
                "installation_nearest_existing": ancestor,
                "staging_chain": chain,
                "installation_chain": chain,
            },
            "transfer_context": {
                "method": "huggingface_hub.snapshot_download",
                "provider": "huggingface_hub",
                "host_role": "login_node_data_transfer",
                "network_direction": "inbound_public_model_only",
                "credentials_mode": "public_repository_no_token",
                "project_data_egress_allowed": False,
            },
            "safety": {
                "remote_writes": False, "provider_requests": False,
                "model_download": False, "job_submission": False,
                "gpu_allocation": False, "recursive_model_scan": False,
                "model_file_reading": False, "large_file_hashing": False,
                "publication": False,
            },
        }

    def components(self, root, registry):
        common = dict(
            registry=registry,
            policy_root=root / "config/ai/acquisition_runtime",
            bundle_root=root / "bundles",
            plan_root=root / "plans",
            evidence_root=root / "evidence",
        )
        return ModelAcquisitionPreflightCollector(**common), ModelAcquisitionPreflightCore(
            **common, state_root=root / "state"
        )

    def test_policy_registers_only_public_inbound_transfer_and_disables_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, _, _, _ = self.setup_repository(root)
            collector, _ = self.components(root, registry)
            _, policy, _ = collector.policy(BACKEND_ID)
            self.assertEqual(policy["transfer_context"]["network_direction"],
                             "inbound_public_model_only")
            self.assertFalse(policy["transfer_context"]["project_data_egress_allowed"])
            self.assertTrue(all(value is False for value in policy["safety"].values()))

    def test_probe_imports_but_never_calls_snapshot_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, backend, bundle_id, bundle = self.setup_repository(root)
            collector, _ = self.components(root, registry)
            _, policy, _ = collector.policy(BACKEND_ID)
            bundle["bundle_id"] = bundle_id
            bundle["_preflight_required_working_free_bytes"] = 128_267_636_268
            program = _probe_program(backend, policy, bundle)
            self.assertIn("from huggingface_hub import snapshot_download", program)
            self.assertNotIn("snapshot_download(", program)
            self.assertNotIn("run([sbatch", program)
            self.assertNotIn('subprocess.run(["sbatch"', program)
            self.assertNotIn("requests.get", program)

    def test_ready_collection_leaves_only_fresh_authorization_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, _, bundle_id, bundle = self.setup_repository(root)
            collector, _ = self.components(root, registry)
            runner = FakeRunner(self.observation(bundle_id, bundle))
            result = collector.collect(
                BACKEND_ID, bundle_id, runner, stamp="20260715T120000Z",
                observed_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(result.status, "ready_for_execution_authorization_review")
            self.assertEqual(result.blockers, ("fresh_execution_authorization_missing",))
            snapshot = json.loads(result.evidence_path.read_text())
            self.assertEqual(snapshot["query"]["provider_requests"], 0)
            self.assertTrue(snapshot["safety"]["bounded_remote_read"])
            self.assertTrue(all(value is False for key, value in snapshot["safety"].items()
                                if key != "bounded_remote_read"))
            self.assertEqual(len(runner.calls), 1)

    def test_ingest_expires_evidence_and_never_grants_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, _, bundle_id, bundle = self.setup_repository(root)
            collector, core = self.components(root, registry)
            observed = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
            collector.collect(BACKEND_ID, bundle_id,
                FakeRunner(self.observation(bundle_id, bundle)),
                stamp="20260715T120000Z", observed_at=observed)
            fresh = core.ingest(BACKEND_ID, bundle_id, now=observed + timedelta(minutes=20))
            self.assertEqual(fresh.status, "ready_for_execution_authorization_review")
            readiness = json.loads((fresh.state_dir / "readiness.json").read_text())
            self.assertFalse(readiness["automatic_execution_allowed"])
            stale = core.ingest(BACKEND_ID, bundle_id, now=observed + timedelta(minutes=31))
            self.assertEqual(stale.status, "preflight_evidence_stale")
            self.assertIn("preflight_evidence_expired", stale.blockers)

    def test_target_conflict_or_insufficient_quota_blocks_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, _, bundle_id, bundle = self.setup_repository(root)
            collector, _ = self.components(root, registry)
            observation = self.observation(bundle_id, bundle, available="150G/250G")
            observation["target"]["installation"].update({
                "exists": True, "lexists": True, "is_directory": True,
            })
            result = collector.collect(BACKEND_ID, bundle_id, FakeRunner(observation),
                stamp="20260715T120000Z",
                observed_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc))
            self.assertEqual(result.status, "remote_target_review_required")
            self.assertIn("remote_target_state_unverified", result.blockers)

    def test_changed_bundle_report_makes_saved_evidence_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, _, bundle_id, bundle = self.setup_repository(root)
            collector, core = self.components(root, registry)
            observed = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
            collector.collect(BACKEND_ID, bundle_id,
                FakeRunner(self.observation(bundle_id, bundle)),
                stamp="20260715T120000Z", observed_at=observed)
            report = root / "bundles" / BACKEND_ID / bundle_id / "report.md"
            report.write_text("changed after observation\n")
            result = core.ingest(BACKEND_ID, bundle_id, now=observed)
            self.assertEqual(result.status, "preflight_evidence_stale")
            self.assertIn("preflight_inputs_changed", result.blockers)

    def test_unsafe_policy_or_remote_safety_claim_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, _, bundle_id, bundle = self.setup_repository(root)
            collector, _ = self.components(root, registry)
            policy_path = root / "config/ai/acquisition_runtime/roihu_qwen3_coder.json"
            policy = json.loads(policy_path.read_text())
            policy["transfer_context"]["project_data_egress_allowed"] = True
            policy_path.write_text(json.dumps(policy) + "\n")
            with self.assertRaisesRegex(ModelAcquisitionPreflightError, "inbound-only"):
                collector.policy(BACKEND_ID)
            policy["transfer_context"]["project_data_egress_allowed"] = False
            policy_path.write_text(json.dumps(policy) + "\n")
            observation = self.observation(bundle_id, bundle)
            observation["safety"]["model_download"] = True
            with self.assertRaisesRegex(ModelAcquisitionPreflightError, "disable model_download"):
                collector.collect(BACKEND_ID, bundle_id, FakeRunner(observation),
                    stamp="20260715T120000Z")

    def test_cli_ingest_reports_execution_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, _, bundle_id, bundle = self.setup_repository(root)
            collector, _ = self.components(root, registry)
            now = datetime.now(timezone.utc)
            collector.collect(BACKEND_ID, bundle_id,
                FakeRunner(self.observation(bundle_id, bundle)),
                stamp="20260715T120000Z", observed_at=now)
            command = [
                sys.executable, "scripts/model_acquisition_preflight.py", "ingest",
                BACKEND_ID, "--bundle-id", bundle_id,
                "--backend-root", str(root / "config/ai/backends"),
                "--prompt-root", str(root / "config/ai/prompts"),
                "--suite-root", str(root / "config/ai/suites"),
                "--case-root", str(root / "benchmarks/ai"),
                "--policy-root", str(root / "config/ai/acquisition_runtime"),
                "--bundle-root", str(root / "bundles"),
                "--plan-root", str(root / "plans"),
                "--evidence-root", str(root / "evidence"),
                "--state-root", str(root / "cli-state"),
            ]
            completed = subprocess.run(command, cwd=REPOSITORY_ROOT,
                capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Execution         : disabled", completed.stdout)
            self.assertIn("ready_for_execution_authorization_review", completed.stdout)


if __name__ == "__main__":
    unittest.main()
