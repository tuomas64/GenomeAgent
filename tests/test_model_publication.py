from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from genomeagent.model_publication import (
    ModelPublicationCore,
    ModelPublicationError,
    ModelPublicationPreflightCollector,
    _preflight_program,
    _publication_status_program,
    _publication_submission_program,
)
import tests.test_model_integrity_verification as integrity_support


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ID = integrity_support.BACKEND_ID
VERIFICATION_ID = "9" * 64


def compact_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class PublicationRunner:
    def __init__(self, bundle, files, mode="preflight", mutate=None, host="roihu"):
        self.bundle = bundle
        self.files = files
        self.mode = mode
        self.mutate = mutate
        self.host = host
        self.calls = []

    @staticmethod
    def config(program):
        match = re.search(r"CONFIG\s*=\s*json\.loads\((.+)\)\n", program)
        if not match:
            raise AssertionError("remote program has no encoded CONFIG")
        return json.loads(ast.literal_eval(match.group(1)))

    def manifest_candidate(self):
        return {
            "schema_version": "1.0",
            "manifest_type": "verified_model_candidate",
            "backend_id": BACKEND_ID,
            "bundle_id": self.bundle["bundle_id"],
            "verification_id": VERIFICATION_ID,
            "repository": self.bundle["repository"],
            "resolved_revision": self.bundle["resolved_revision"],
            "staging_path": self.bundle["target"]["staging_path"],
            "installation_path": self.bundle["target"]["installation_path"],
            "files": self.files,
            "file_count": len(self.files),
            "total_bytes": sum(item["size_bytes"] for item in self.files),
            "download_cache": {
                "present": True,
                "regular_files": 2,
                "directories": 2,
                "bytes": 250,
            },
            "publication_performed": False,
        }

    def run_python(self, program, timeout_seconds=240):
        self.calls.append((program, timeout_seconds))
        config = self.config(program)
        if self.mode == "launch":
            return {
                "schema_version": "1.0",
                "status": "publication_submitted",
                "publication_id": config["publication_id"],
                "job_id": "123456",
                "control_directory": config["control_directory"],
                "publication": False,
                "gpu_allocation": False,
            }
        if self.mode == "status":
            publication_id = config["publication_id"]
            installed_manifest = {
                "schema_version": "1.0",
                "manifest_type": "genomeagent_installed_model",
                "backend_id": BACKEND_ID,
                "bundle_id": self.bundle["bundle_id"],
                "verification_id": VERIFICATION_ID,
                "publication_id": publication_id,
                "repository": self.bundle["repository"],
                "resolved_revision": self.bundle["resolved_revision"],
                "installation_path": self.bundle["target"]["installation_path"],
                "installed_at_utc": "2026-07-15T13:00:00Z",
                "source_verified_manifest_sha256": "a" * 64,
                "source_verification_result_sha256": "b" * 64,
                "files": self.files,
                "file_count": len(self.files),
                "total_bytes": sum(item["size_bytes"] for item in self.files),
                "download_cache_removed": True,
                "atomic_directory_rename": True,
                "backend_activated": False,
                "inference_benchmarked": False,
            }
            manifest_raw = compact_bytes(installed_manifest)
            result = {
                "schema_version": "1.0",
                "publication_id": publication_id,
                "status": "published_ready_for_installed_model_evidence",
                "installation_path": self.bundle["target"]["installation_path"],
                "installed_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "published_file_count": len(self.files),
                "published_total_bytes": sum(item["size_bytes"] for item in self.files),
                "download_cache_removed": True,
                "atomic_directory_rename": True,
                "publication": True,
                "gpu_allocation": False,
                "inference": False,
                "training": False,
                "registry_update": False,
                "backend_activation": False,
            }
            return {
                "schema_version": "1.0",
                "observation_type": "model_publication_status",
                "backend_id": BACKEND_ID,
                "bundle_id": self.bundle["bundle_id"],
                "publication_id": publication_id,
                "control_directory": config["control_directory"],
                "submission": {"job_id": "123456"},
                "worker_status": {"status": result["status"]},
                "result": result,
                "result_record_sha256": "c" * 64,
                "paths": {
                    "staging": {
                        "path": self.bundle["target"]["staging_path"],
                        "lexists": False,
                        "exists": False,
                        "is_symlink": False,
                        "is_directory": False,
                    },
                    "installation": {
                        "path": self.bundle["target"]["installation_path"],
                        "lexists": True,
                        "exists": True,
                        "is_symlink": False,
                        "is_directory": True,
                    },
                },
                "installed": {
                    "files": [
                        {"path": item["path"], "size_bytes": item["size_bytes"]}
                        for item in self.files
                    ],
                    "file_count": len(self.files),
                    "total_bytes": sum(item["size_bytes"] for item in self.files),
                    "symlinks": [],
                    "special_entries": [],
                    "download_cache_present": False,
                    "walk_error": None,
                    "manifest": {
                        "value": installed_manifest,
                        "sha256": hashlib.sha256(manifest_raw).hexdigest(),
                        "size_bytes": len(manifest_raw),
                    },
                },
                "scheduler": {"squeue": None, "sacct": None},
                "stdout_tail": "",
                "stderr_tail": "",
                "safety": {field: False for field in (
                    "remote_writes", "model_file_contents_read", "large_file_hashing",
                    "job_submission", "staging_mutation", "cache_removal",
                    "manifest_write", "publication", "gpu_allocation", "inference",
                    "training", "registry_update", "backend_activation"
                )},
            }
        manifest = self.manifest_candidate()
        result = {
            "schema_version": "1.0",
            "verification_id": VERIFICATION_ID,
            "status": "verified_ready_for_publication_review",
            "verified_file_count": len(self.files),
            "verified_total_bytes": sum(item["size_bytes"] for item in self.files),
            "publication": False,
            "gpu_allocation": False,
        }
        observation = {
            "schema_version": "1.0",
            "observation_type": "model_publication_preflight",
            "backend_id": BACKEND_ID,
            "bundle_id": self.bundle["bundle_id"],
            "verification_id": VERIFICATION_ID,
            "identity": {"hostname": "roihu-cpu-login2", "machine": "x86_64"},
            "paths": {
                "project_storage": {
                    "path": self.bundle["target"]["project_storage_root"],
                    "lexists": True, "exists": True, "is_symlink": False,
                    "is_directory": True, "device": 42,
                },
                "staging": {
                    "path": self.bundle["target"]["staging_path"],
                    "lexists": True, "exists": True, "is_symlink": False,
                    "is_directory": True, "device": 42,
                },
                "installation": {
                    "path": self.bundle["target"]["installation_path"],
                    "lexists": False, "exists": False, "is_symlink": False,
                    "is_directory": False, "device": None,
                },
                "installation_parent": {
                    "path": str(Path(self.bundle["target"]["installation_path"]).parent),
                    "lexists": True, "exists": True, "is_symlink": False,
                    "is_directory": True, "device": 42,
                },
                "verification_control": {
                    "path": config["verification_control"], "lexists": True,
                    "exists": True, "is_symlink": False, "is_directory": True,
                    "device": 42,
                },
                "publication_control_root": {
                    "path": config["publication_control_root"], "lexists": False,
                    "exists": False, "is_symlink": False, "is_directory": False,
                    "device": None,
                },
                "worker_python": {
                    "path": "/usr/bin/python3", "lexists": True, "exists": True,
                    "is_symlink": True, "is_directory": False, "device": 42,
                    "is_file_followed": True, "executable_by_access_check": True,
                    "resolved_path": "/usr/bin/python3.9",
                },
                "staging_chain_symlinks": [],
                "installation_chain_symlinks": [],
                "verification_control_chain_symlinks": [],
                "publication_control_chain_symlinks": [],
            },
            "inventory": {
                "files": [
                    {"path": item["path"], "size_bytes": item["size_bytes"]}
                    for item in self.files
                ],
                "file_count": len(self.files),
                "total_bytes": sum(item["size_bytes"] for item in self.files),
                "symlinks": [],
                "special_entries": [],
                "walk_error": None,
                "download_cache": {
                    "present": True, "regular_files": 2, "directories": 2,
                    "bytes": 250,
                },
            },
            "verification": {
                "result": {
                    "value": result,
                    "sha256": "b" * 64,
                    "size_bytes": 500,
                },
                "manifest_candidate": {
                    "value": manifest,
                    "sha256": "a" * 64,
                    "size_bytes": 1500,
                },
            },
            "scheduler": {
                "sbatch_path": "/usr/bin/sbatch", "squeue_path": "/usr/bin/squeue",
                "sacct_path": "/usr/bin/sacct", "sinfo_path": "/usr/bin/sinfo",
                "partition_query": {"returncode": 0, "stdout": "small|up|3-00:00:00|100\n",
                    "stderr": "", "timed_out": False},
            },
            "safety": {field: False for field in (
                "remote_writes", "model_file_contents_read", "large_file_hashing",
                "job_submission", "staging_mutation", "cache_removal", "manifest_write",
                "publication", "gpu_allocation", "inference", "training",
                "registry_update", "backend_activation"
            )},
        }
        if self.mutate:
            self.mutate(observation)
        return observation


class ModelPublicationTests(unittest.TestCase):
    def setup_repository(self, root: Path, mutate=None):
        helper = integrity_support.ModelIntegrityVerificationTests()
        registry, backend, bundle_id, bundle, _, _, _, observed = helper.setup_repository(root)
        source = REPOSITORY_ROOT / "config/ai/model_publication/roihu_qwen3_coder.json"
        target = root / "config/ai/model_publication/roihu_qwen3_coder.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        files = []
        for number, item in enumerate(bundle["approved_source_inventory"], 1):
            provider = item.get("provider_lfs_sha256")
            files.append({
                "path": item["path"],
                "size_bytes": item["size_bytes"],
                "local_sha256": str(number) * 64,
                "provider_lfs_sha256": provider,
                "provider_lfs_sha256_match": None if provider is None else True,
                "provider_git_blob_id": item.get("provider_git_blob_id"),
                "provider_xet_hash": item.get("provider_xet_hash"),
            })
        verification_dir = root / "verifications" / BACKEND_ID / bundle_id / VERIFICATION_ID
        status_dir = verification_dir / "status"
        status_dir.mkdir(parents=True)
        control = "/scratch/project_2001113/.genomeagent/model-verifications/" + VERIFICATION_ID
        launch = {
            "schema_version": "1.0",
            "source_mode": "explicit_authorized_cpu_model_hash_verification_submission",
            "backend_id": BACKEND_ID,
            "bundle_id": bundle_id,
            "authorization_id": "8" * 64,
            "verification_id": VERIFICATION_ID,
            "job_id": "987654",
            "control_directory": control,
            "expected_file_count": len(files),
            "expected_total_bytes": sum(item["size_bytes"] for item in files),
            "publication": False,
            "gpu_allocation": False,
        }
        (verification_dir / "launch.json").write_text(json.dumps(launch) + "\n")
        status = {
            "schema_version": "1.0",
            "source_mode": "bounded_read_only_model_integrity_status_observation",
            "backend_id": BACKEND_ID,
            "bundle_id": bundle_id,
            "authorization_id": "8" * 64,
            "verification_id": VERIFICATION_ID,
            "status": "verified_ready_for_publication_review",
            "publication": False,
            "staging_mutation": False,
        }
        (status_dir / "20260715T120500Z.json").write_text(json.dumps(status) + "\n")
        common = dict(
            registry=registry,
            policy_root=root / "config/ai/model_publication",
            bundle_root=root / "bundles",
            plan_root=root / "plans",
            verification_root=root / "verifications",
            evidence_root=root / "publication_evidence",
        )
        collector = ModelPublicationPreflightCollector(**common)
        core = ModelPublicationCore(
            **common,
            state_root=root / "state",
            authorization_root=root / "authorizations",
            publication_root=root / "publications",
        )
        runner = PublicationRunner(bundle, files, mutate=mutate)
        result = collector.collect(
            BACKEND_ID,
            bundle_id,
            VERIFICATION_ID,
            runner,
            stamp="20260715T123000Z",
            observed_at=observed,
        )
        core.ingest(BACKEND_ID, bundle_id, now=observed)
        return registry, backend, bundle_id, bundle, files, collector, core, result, observed

    def authorize_ready(self, root: Path):
        values = self.setup_repository(root)
        _, _, bundle_id, _, _, _, core, result, observed = values
        authorization = core.authorize(
            BACKEND_ID,
            bundle_id,
            VERIFICATION_ID,
            result.evidence_id,
            "tuomas64",
            True,
            True,
            now=observed,
        )
        return (*values, authorization)

    def test_repository_policy_is_non_overwriting_cpu_only(self):
        policy = json.loads((
            REPOSITORY_ROOT / "config/ai/model_publication/roihu_qwen3_coder.json"
        ).read_text())
        self.assertEqual(policy["ssh_host"], "roihu")
        self.assertEqual(policy["slurm"]["partition"], "small")
        self.assertEqual(policy["slurm"]["cpus_per_task"], 1)
        self.assertFalse(policy["safety"]["overwrite_existing_installation_allowed"])
        self.assertFalse(policy["safety"]["cross_filesystem_publication_allowed"])
        self.assertFalse(policy["safety"]["gpu_allocation_allowed"])

    def test_preflight_is_metadata_only_and_compiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, bundle_id, bundle, files, _, core, _, _ = self.setup_repository(Path(tmp))
            _, policy, _ = core.policy(BACKEND_ID)
            program = _preflight_program(
                BACKEND_ID, bundle_id, VERIFICATION_ID, bundle, policy,
                "/scratch/project_2001113/.genomeagent/model-verifications/" + VERIFICATION_ID,
                files,
            )
            compile(program, "<preflight>", "exec")
            self.assertIn("os.walk", program)
            self.assertNotIn('subprocess.run(["sbatch"', program)
            self.assertNotIn("hash_chunk_bytes", program)

    def test_ready_preflight_ingests_without_execution_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.setup_repository(Path(tmp))
            _, _, bundle_id, _, _, _, core, result, observed = values
            self.assertEqual(result.status, "ready_for_publication_authorization_review")
            state = core.ingest(BACKEND_ID, bundle_id, now=observed)
            self.assertEqual(state.blockers, ("fresh_publication_authorization_missing",))
            readiness = json.loads((state.state_dir / "readiness.json").read_text())
            self.assertFalse(readiness["automatic_publication_allowed"])

    def test_existing_installation_blocks_authorization(self):
        def mutate(observation):
            observation["paths"]["installation"].update({
                "lexists": True, "exists": True, "is_directory": True
            })

        with tempfile.TemporaryDirectory() as tmp:
            values = self.setup_repository(Path(tmp), mutate=mutate)
            _, _, bundle_id, _, _, _, core, result, observed = values
            self.assertIn("installation_target_not_absent", result.blockers)
            with self.assertRaisesRegex(ModelPublicationError, "not authorization-ready"):
                core.authorize(
                    BACKEND_ID, bundle_id, VERIFICATION_ID, result.evidence_id,
                    "tuomas64", True, True, now=observed
                )

    def test_authorization_requires_both_mutation_confirmations(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.setup_repository(Path(tmp))
            _, _, bundle_id, _, _, _, core, result, observed = values
            with self.assertRaisesRegex(ModelPublicationError, "atomic-model-publication"):
                core.authorize(
                    BACKEND_ID, bundle_id, VERIFICATION_ID, result.evidence_id,
                    "tuomas64", False, True, now=observed
                )
            with self.assertRaisesRegex(ModelPublicationError, "remove-download-cache"):
                core.authorize(
                    BACKEND_ID, bundle_id, VERIFICATION_ID, result.evidence_id,
                    "tuomas64", True, False, now=observed
                )

    def test_submission_program_rehashes_then_renames_without_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.authorize_ready(Path(tmp))
            _, _, bundle_id, bundle, files, _, core, _, _, authorization = values
            _, policy, _ = core.policy(BACKEND_ID)
            record = json.loads(authorization.authorization_path.read_text())
            program = _publication_submission_program(
                BACKEND_ID, bundle_id, "7" * 64, bundle, files, policy, record,
                "/scratch/project_2001113/.genomeagent/model-verifications/" + VERIFICATION_ID,
            )
            compile(program, "<publication>", "exec")
            self.assertIn("hashlib.sha256", program)
            self.assertIn("shutil.rmtree(cache_path)", program)
            self.assertIn("os.rename(staging,target)", program)
            self.assertNotIn("os.replace(staging,target)", program)
            self.assertNotIn("--gres", program)

    def test_generated_worker_rehashes_and_atomically_publishes_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root / "models"
            staging = models / ".staging"
            target = models / "installed"
            verification = root / "verification"
            staging.mkdir(parents=True)
            verification.mkdir()
            payloads = {"config.json": b"configuration\n", "model.bin": b"weights\x00\x01"}
            files = []
            for name, payload in sorted(payloads.items()):
                (staging / name).write_bytes(payload)
                files.append({
                    "path": name, "size_bytes": len(payload),
                    "local_sha256": hashlib.sha256(payload).hexdigest(),
                    "provider_lfs_sha256": None,
                    "provider_lfs_sha256_match": None,
                    "provider_git_blob_id": None, "provider_xet_hash": None,
                })
            cache = staging / ".cache/huggingface"
            cache.mkdir(parents=True)
            (cache / "metadata").write_text("transfer metadata\n")
            candidate = {
                "schema_version": "1.0", "manifest_type": "verified_model_candidate",
                "backend_id": BACKEND_ID, "bundle_id": "6" * 64,
                "verification_id": VERIFICATION_ID, "repository": "Qwen/test",
                "resolved_revision": "b" * 40, "staging_path": str(staging),
                "installation_path": str(target), "files": files,
                "file_count": len(files), "total_bytes": sum(i["size_bytes"] for i in files),
                "publication_performed": False,
            }
            verification_result = {
                "schema_version": "1.0", "verification_id": VERIFICATION_ID,
                "status": "verified_ready_for_publication_review", "publication": False,
            }
            candidate_raw = compact_bytes(candidate)
            result_raw = compact_bytes(verification_result)
            (verification / "manifest_candidate.json").write_bytes(candidate_raw)
            (verification / "result.json").write_bytes(result_raw)
            policy = json.loads((
                REPOSITORY_ROOT / "config/ai/model_publication/roihu_qwen3_coder.json"
            ).read_text())
            policy["control_root"] = str(root / "control")
            bundle = {
                "repository": "Qwen/test", "resolved_revision": "b" * 40,
                "target": {"project_storage_root": str(root),
                    "staging_path": str(staging), "installation_path": str(target)},
            }
            authorization = {
                "verification_id": VERIFICATION_ID, "authorization_id": "2" * 64,
                "verification_result_sha256": hashlib.sha256(result_raw).hexdigest(),
                "verified_manifest_sha256": hashlib.sha256(candidate_raw).hexdigest(),
            }
            publication_id = "7" * 64
            program = _publication_submission_program(
                BACKEND_ID, "6" * 64, publication_id, bundle, files, policy,
                authorization, str(verification)
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sbatch = fake_bin / "sbatch"
            sbatch.write_text("#!/bin/sh\nprintf '123456\\n'\n")
            sbatch.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
            submitted = subprocess.run(
                [sys.executable, "-c", program], text=True, capture_output=True,
                env=environment, check=False
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr + submitted.stdout)
            control = Path(policy["control_root"]) / publication_id
            worker = subprocess.run(
                ["/usr/bin/python3", str(control / "publish_worker.py"),
                 str(control / "publication_spec.json")],
                text=True, capture_output=True, check=False
            )
            self.assertEqual(worker.returncode, 0, worker.stderr + worker.stdout)
            self.assertFalse(staging.exists())
            self.assertTrue(target.is_dir())
            self.assertFalse((target / ".cache").exists())
            self.assertEqual((target / "model.bin").read_bytes(), payloads["model.bin"])
            installed = json.loads((target / ".genomeagent-model-manifest.json").read_text())
            self.assertEqual(installed["publication_id"], publication_id)
            self.assertFalse(installed["backend_activated"])
            result = json.loads((control / "result.json").read_text())
            self.assertEqual(result["status"], "published_ready_for_installed_model_evidence")

    def test_generated_worker_refuses_changed_model_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "models/.staging"
            target = root / "models/installed"
            verification = root / "verification"
            staging.mkdir(parents=True)
            verification.mkdir()
            original = b"before"
            (staging / "model.bin").write_bytes(original)
            cache = staging / ".cache/huggingface"
            cache.mkdir(parents=True)
            (cache / "metadata").write_text("transfer metadata\n")
            files = [{
                "path": "model.bin", "size_bytes": len(original),
                "local_sha256": hashlib.sha256(original).hexdigest(),
                "provider_lfs_sha256": None, "provider_lfs_sha256_match": None,
                "provider_git_blob_id": None, "provider_xet_hash": None,
            }]
            candidate = {
                "schema_version": "1.0", "manifest_type": "verified_model_candidate",
                "backend_id": BACKEND_ID, "bundle_id": "6" * 64,
                "verification_id": VERIFICATION_ID, "repository": "Qwen/test",
                "resolved_revision": "b" * 40, "staging_path": str(staging),
                "installation_path": str(target), "files": files,
                "file_count": 1, "total_bytes": len(original),
                "publication_performed": False,
            }
            verification_result = {
                "schema_version": "1.0", "verification_id": VERIFICATION_ID,
                "status": "verified_ready_for_publication_review", "publication": False,
            }
            candidate_raw = compact_bytes(candidate)
            result_raw = compact_bytes(verification_result)
            (verification / "manifest_candidate.json").write_bytes(candidate_raw)
            (verification / "result.json").write_bytes(result_raw)
            policy = json.loads((
                REPOSITORY_ROOT / "config/ai/model_publication/roihu_qwen3_coder.json"
            ).read_text())
            policy["control_root"] = str(root / "control")
            bundle = {
                "repository": "Qwen/test", "resolved_revision": "b" * 40,
                "target": {"project_storage_root": str(root),
                    "staging_path": str(staging), "installation_path": str(target)},
            }
            authorization = {
                "verification_id": VERIFICATION_ID, "authorization_id": "2" * 64,
                "verification_result_sha256": hashlib.sha256(result_raw).hexdigest(),
                "verified_manifest_sha256": hashlib.sha256(candidate_raw).hexdigest(),
            }
            publication_id = "7" * 64
            program = _publication_submission_program(
                BACKEND_ID, "6" * 64, publication_id, bundle, files, policy,
                authorization, str(verification)
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sbatch = fake_bin / "sbatch"
            sbatch.write_text("#!/bin/sh\nprintf '123456\\n'\n")
            sbatch.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
            submitted = subprocess.run(
                [sys.executable, "-c", program], text=True, capture_output=True,
                env=environment, check=False
            )
            self.assertEqual(submitted.returncode, 0, submitted.stderr + submitted.stdout)
            (staging / "model.bin").write_bytes(b"tamper")
            control = Path(policy["control_root"]) / publication_id
            worker = subprocess.run(
                ["/usr/bin/python3", str(control / "publish_worker.py"),
                 str(control / "publication_spec.json")],
                text=True, capture_output=True, check=False
            )
            self.assertNotEqual(worker.returncode, 0)
            self.assertTrue(staging.is_dir())
            self.assertTrue((staging / ".cache").is_dir())
            self.assertFalse((staging / ".genomeagent-model-manifest.json").exists())
            self.assertFalse(target.exists())
            result = json.loads((control / "result.json").read_text())
            self.assertEqual(result["status"], "publication_failed_integrity")
            self.assertFalse(result["staging_mutation_started"])

    def test_launch_and_success_status_preserve_activation_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            values = self.authorize_ready(Path(tmp))
            _, _, bundle_id, bundle, files, _, core, _, _, authorization = values
            launch = core.launch(
                BACKEND_ID, bundle_id, authorization.authorization_id,
                PublicationRunner(bundle, files, mode="launch"), True,
                now=values[-2]
            )
            self.assertEqual(launch.remote_status, "publication_submitted")
            status_runner = PublicationRunner(bundle, files, mode="status")
            # The fake installed manifest must use the exact source digests bound in
            # the real launch artifact.
            launch_data = json.loads(launch.launch_path.read_text())
            self.assertEqual(launch_data["verified_manifest_sha256"], "a" * 64)
            result = core.status(
                BACKEND_ID, bundle_id, authorization.authorization_id,
                status_runner, stamp="20260715T130500Z"
            )
            self.assertEqual(result.status, "published_ready_for_installed_model_evidence")
            evidence = json.loads(result.observation_path.read_text())
            self.assertFalse(evidence["backend_activation"])
            self.assertFalse(evidence["inference"])

    def test_status_program_is_read_only(self):
        program = _publication_status_program(
            BACKEND_ID, "6" * 64, "7" * 64,
            "/scratch/project_2001113/.genomeagent/model-publications/" + "7" * 64,
            "/scratch/project_2001113/models/.staging",
            "/scratch/project_2001113/models/model",
            ".genomeagent-model-manifest.json", ".cache", 2000, 2000000, 8000,
        )
        compile(program, "<status>", "exec")
        self.assertNotIn("hashlib.sha256(block", program)
        self.assertNotIn("os.rename", program)
        self.assertNotIn("shutil.rmtree", program)
        self.assertNotIn("sbatch", program)


if __name__ == "__main__":
    unittest.main()
