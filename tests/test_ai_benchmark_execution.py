import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from genomeagent.ai_benchmark_execution import (
    AIBenchmarkExecutionCore,
    AIBenchmarkExecutionError,
    _worker_source,
)
from genomeagent.ai_evaluation import AIRegistry


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND = "roihu_qwen3_coder"
SUITE = "genomeagent_core_v1"


class FakeRunner:
    host = "roihu-gpu"

    def __init__(self, mode="launch", responses=""):
        self.mode = mode
        self.responses = responses
        self.calls = []

    @staticmethod
    def config(program):
        match = re.search(r'base64\.b64decode\("([A-Za-z0-9+/=]+)"\)', program)
        if not match:
            raise AssertionError("embedded remote configuration not found")
        return json.loads(base64.b64decode(match.group(1)).decode("utf-8"))

    def run_python(self, program, timeout_seconds=240):
        self.calls.append((program, timeout_seconds))
        cfg = self.config(program)
        if self.mode == "launch":
            return {
                "schema_version": "1.0",
                "status": "benchmark_submitted",
                "execution_id": cfg["execution_id"],
                "job_id": "2001113",
                "control_path": cfg["control_path"],
            }
        digest = hashlib.sha256(self.responses.encode()).hexdigest()
        return {
            "status": "benchmark_completed_unscored",
            "execution_id": cfg["execution_id"],
            "job_id": cfg["job_id"],
            "responses_jsonl": self.responses,
            "responses_sha256": digest,
            "worker_status": {
                "state": "benchmark_completed_unscored",
                "model_download": False,
                "external_provider_access": False,
                "model_output_execution": False,
                "training": False,
            },
        }


class AIBenchmarkExecutionTests(unittest.TestCase):
    def fixture(self, root: Path):
        for relative in ("config/ai/backends", "config/ai/prompts", "config/ai/suites",
                         "config/ai/benchmark_execution", "benchmarks/ai"):
            shutil.copytree(REPOSITORY_ROOT / relative, root / relative)
        backend_path = root / "config/ai/backends" / (BACKEND + ".json")
        backend = json.loads(backend_path.read_text())
        backend["status"] = "validated_candidate"
        backend["benchmark_status"] = "not_run"
        backend["model"].update({
            "revision": "a" * 40,
            "weights_sha256": "c" * 64,
            "weights_digest_type": "verified_model_candidate_manifest_sha256",
        })
        backend["installation"].update({
            "status": "verified_present",
            "manifest_sha256": "d" * 64,
            "verified_inventory_sha256": "c" * 64,
        })
        backend_path.write_text(json.dumps(backend, indent=2, sort_keys=True) + "\n")
        state_dir = root / "workspace/ai_backend_state" / BACKEND
        state_dir.mkdir(parents=True)
        state = {
            "schema_version": "1.0",
            "backend_id": BACKEND,
            "readiness": {
                "status": "ready_for_bounded_benchmark_review",
                "blockers": ["inference_not_benchmarked"],
            },
            "observation": {
                "model": {
                    "inventory_manifest": {
                        "metadata": {"status": "parsed", "sha256": "d" * 64}
                    }
                }
            },
        }
        (state_dir / "current_evidence.json").write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n"
        )
        registry = AIRegistry(
            root / "config/ai/backends", root / "config/ai/prompts",
            root / "config/ai/suites", root / "benchmarks/ai",
        )
        core = AIBenchmarkExecutionCore(
            registry=registry,
            policy_root=root / "config/ai/benchmark_execution",
            backend_state_root=root / "workspace/ai_backend_state",
            run_root=root / "workspace/ai_runs",
            plan_root=root / "workspace/ai_benchmark_plans",
            authorization_root=root / "workspace/ai_benchmark_authorizations",
            execution_root=root / "workspace/ai_benchmark_executions",
            evaluation_root=root / "workspace/ai_evaluations",
        )
        return core, backend_path

    def perfect_responses(self, root: Path) -> str:
        values = []
        suite = json.loads((root / "config/ai/suites" / (SUITE + ".json")).read_text())
        for case_id in suite["cases"]:
            case = json.loads((root / "benchmarks/ai" / SUITE / (case_id + ".json")).read_text())
            expected = case["expected"]
            values.append({
                "case_id": case_id,
                "output": {
                    "schema_version": "1.0",
                    "case_id": case_id,
                    "answer": {
                        "classification": expected["classification"],
                        "facts": [
                            {"id": key, "value": value}
                            for key, value in expected["facts"].items()
                        ],
                        "missing_evidence": expected["missing_evidence"],
                        "recommended_action": expected["recommended_action"],
                        "automatic_execution_allowed": False,
                    },
                },
                "metrics": {"prompt_tokens": 100, "completion_tokens": 50, "latency_seconds": 1.0},
            })
        return "".join(json.dumps(value, sort_keys=True) + "\n" for value in values)

    def test_repository_policy_is_strictly_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            core, _ = self.fixture(Path(tmp))
            _, policy, _ = core.policy(BACKEND)
            self.assertEqual(policy["slurm"]["gpus"], 1)
            self.assertEqual(policy["slurm"]["gpu_type"], "gh200")
            self.assertEqual(policy["slurm"]["time_limit"], "00:15:00")
            self.assertTrue(all(value is False for value in policy["safety"].values()))

    def test_prepare_binds_registered_manifest_fixtures_and_fresh_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); core, _ = self.fixture(root)
            result = core.prepare(BACKEND, SUITE)
            self.assertEqual(result.status, "ready_for_bounded_benchmark_authorization_review")
            plan = json.loads((result.plan_dir / "benchmark_plan.json").read_text())
            self.assertEqual(plan["case_count"], 8)
            self.assertEqual(plan["model_manifest_sha256"], "d" * 64)
            self.assertFalse(plan["automatic_submission_allowed"])
            self.assertFalse(plan["model_output_execution_allowed"])

    def test_prepare_rejects_changed_installed_manifest_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); core, _ = self.fixture(root)
            state_path = root / "workspace/ai_backend_state" / BACKEND / "current_evidence.json"
            state = json.loads(state_path.read_text())
            state["observation"]["model"]["inventory_manifest"]["metadata"]["sha256"] = "0" * 64
            state_path.write_text(json.dumps(state))
            with self.assertRaisesRegex(AIBenchmarkExecutionError, "registered manifest"):
                core.prepare(BACKEND, SUITE)

    def test_authorization_is_explicit_and_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); core, _ = self.fixture(root)
            plan = core.prepare(BACKEND, SUITE)
            with self.assertRaisesRegex(AIBenchmarkExecutionError, "confirm-bounded"):
                core.authorize(BACKEND, SUITE, plan.plan_id, "tuomas64", False)
            now = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
            auth = core.authorize(BACKEND, SUITE, plan.plan_id, "tuomas64", True, now=now)
            with self.assertRaisesRegex(AIBenchmarkExecutionError, "expired"):
                core.launch(
                    BACKEND, auth.authorization_id, FakeRunner(), True,
                    now=now + timedelta(minutes=16),
                )

    def test_launch_is_one_offline_bounded_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); core, _ = self.fixture(root)
            plan = core.prepare(BACKEND, SUITE)
            now = datetime.now(timezone.utc)
            auth = core.authorize(BACKEND, SUITE, plan.plan_id, "tuomas64", True, now=now)
            runner = FakeRunner()
            launch = core.launch(BACKEND, auth.authorization_id, runner, True, now=now)
            self.assertEqual(launch.job_id, "2001113")
            program = runner.calls[0][0]
            self.assertIn("--gres=gpu:", program)
            remote_config = runner.config(program)
            self.assertIn("HF_HUB_OFFLINE", remote_config["shell_source"])
            self.assertIn("HF_HUB_OFFLINE", remote_config["worker_source"])
            self.assertNotIn("curl", program)
            self.assertNotIn("wget", program)
            payload = json.loads(launch.launch_path.read_text())
            self.assertFalse(payload["model_output_execution"])
            self.assertFalse(payload["training"])

    def test_status_imports_bounded_responses_and_evaluation_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); core, _ = self.fixture(root)
            plan = core.prepare(BACKEND, SUITE)
            now = datetime.now(timezone.utc)
            auth = core.authorize(BACKEND, SUITE, plan.plan_id, "tuomas64", True, now=now)
            core.launch(BACKEND, auth.authorization_id, FakeRunner(), True, now=now)
            runner = FakeRunner("status", self.perfect_responses(root))
            status = core.status(
                BACKEND, auth.authorization_id, runner,
                stamp="20260715T133000000000Z",
            )
            self.assertEqual(status.status, "benchmark_completed_unscored")
            self.assertTrue(status.responses_path.is_file())
            evaluation = core.evaluate(BACKEND, auth.authorization_id)
            self.assertEqual(evaluation.status, "passed_for_researcher_review")
            self.assertEqual(evaluation.cases_passed, 8)
            self.assertEqual(evaluation.mean_score, 1.0)

    def test_worker_never_downloads_trains_or_executes_output(self):
        worker = _worker_source()
        self.assertIn('HF_HUB_OFFLINE"]="1"', worker)
        self.assertIn('TRANSFORMERS_OFFLINE"]="1"', worker)
        self.assertNotIn("subprocess", worker)
        self.assertNotIn("exec(", worker)
        self.assertNotIn("eval(", worker)
        self.assertNotIn("train(", worker)

    def test_cli_without_confirmation_performs_no_remote_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); core, _ = self.fixture(root)
            plan = core.prepare(BACKEND, SUITE)
            command = [
                sys.executable, str(REPOSITORY_ROOT / "scripts/ai_benchmark_execution.py"),
                "authorize", BACKEND, "--suite", SUITE, "--plan-id", plan.plan_id,
                "--reviewer", "tuomas64", "--backend-root", str(root / "config/ai/backends"),
                "--prompt-root", str(root / "config/ai/prompts"), "--suite-root", str(root / "config/ai/suites"),
                "--case-root", str(root / "benchmarks/ai"), "--policy-root", str(root / "config/ai/benchmark_execution"),
                "--backend-state-root", str(root / "workspace/ai_backend_state"), "--run-root", str(root / "workspace/ai_runs"),
                "--plan-root", str(root / "workspace/ai_benchmark_plans"), "--authorization-root", str(root / "workspace/authorizations"),
                "--execution-root", str(root / "workspace/executions"), "--evaluation-root", str(root / "workspace/evaluations"),
            ]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("confirm-bounded", completed.stderr)
            self.assertFalse((root / "workspace/authorizations").exists())


if __name__ == "__main__":
    unittest.main()
