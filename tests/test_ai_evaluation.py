from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from genomeagent.ai_evaluation import (
    AIEvaluationCore,
    AIEvaluationError,
    AIRegistry,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class AIEvaluationTests(unittest.TestCase):
    def repository_registry(self) -> AIRegistry:
        return AIRegistry(
            backend_root=REPOSITORY_ROOT / "config/ai/backends",
            prompt_root=REPOSITORY_ROOT / "config/ai/prompts",
            suite_root=REPOSITORY_ROOT / "config/ai/suites",
            case_root=REPOSITORY_ROOT / "benchmarks/ai",
        )

    def copied_registry(self, root: Path) -> AIRegistry:
        shutil.copytree(REPOSITORY_ROOT / "config/ai", root / "config/ai")
        shutil.copytree(REPOSITORY_ROOT / "benchmarks/ai", root / "benchmarks/ai")
        return AIRegistry(
            backend_root=root / "config/ai/backends",
            prompt_root=root / "config/ai/prompts",
            suite_root=root / "config/ai/suites",
            case_root=root / "benchmarks/ai",
        )

    def perfect_responses(self, registry: AIRegistry) -> list[dict]:
        _, _, _, cases = registry.suite("genomeagent_core_v1")
        responses = []
        for _, case, _ in cases:
            expected = case["expected"]
            output = {
                "schema_version": "1.0",
                "case_id": case["case_id"],
                "answer": {
                    "classification": expected["classification"],
                    "facts": [
                        {"id": fact_id, "value": value}
                        for fact_id, value in sorted(expected["facts"].items())
                    ],
                    "missing_evidence": list(expected["missing_evidence"]),
                    "recommended_action": expected["recommended_action"],
                    "automatic_execution_allowed": False,
                },
            }
            responses.append({
                "case_id": case["case_id"],
                "output": output,
                "metrics": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "latency_seconds": 1.25,
                },
            })
        return responses

    def write_jsonl(self, path: Path, values: list[dict]) -> Path:
        path.write_text(
            "".join(json.dumps(value, sort_keys=True) + "\n" for value in values),
            encoding="utf-8",
        )
        return path

    def test_repository_registry_validates_and_reports_planned_backend_blockers(self):
        registry = self.repository_registry()
        result = registry.validate()
        self.assertEqual(result.backends, 1)
        self.assertEqual(result.prompts, 1)
        self.assertEqual(result.suites, 1)
        self.assertEqual(result.cases, 8)
        self.assertEqual(result.backend_statuses["roihu_qwen3_coder"], "not_ready")
        _, backend, _ = registry.backend("roihu_qwen3_coder")
        self.assertEqual(backend["derived_readiness"]["blockers"], [
            "model_revision_unpinned",
            "model_weights_digest_missing",
            "model_path_unverified",
            "inference_not_benchmarked",
        ])
        self.assertFalse(
            backend["derived_readiness"]["automatic_execution_allowed"]
        )

    def test_registry_rejects_backend_that_enables_automatic_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_registry(root)
            backend_path = root / "config/ai/backends/roihu_qwen3_coder.json"
            backend = json.loads(backend_path.read_text(encoding="utf-8"))
            backend["execution_policy"]["automatic_submission_allowed"] = True
            backend_path.write_text(json.dumps(backend) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AIEvaluationError, "automatic_submission_allowed to false"
            ):
                registry.backend("roihu_qwen3_coder")

    def test_prepare_is_idempotent_immutable_and_non_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core = AIEvaluationCore(
                registry=self.repository_registry(),
                run_root=root / "runs",
            )
            first = core.prepare("roihu_qwen3_coder", "genomeagent_core_v1")
            before = {path.name: path.read_bytes() for path in first.artifact_paths}
            second = core.prepare("roihu_qwen3_coder", "genomeagent_core_v1")
            after = {path.name: path.read_bytes() for path in second.artifact_paths}
            self.assertEqual(first.run_id, second.run_id)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(before, after)
            self.assertEqual(first.status, "prepared_not_executable")
            self.assertEqual(
                len((first.run_dir / "requests.jsonl").read_text().splitlines()), 8
            )
            plan = json.loads((first.run_dir / "slurm_plan.json").read_text())
            self.assertIsNone(plan["submission_command"])
            self.assertFalse(plan["submission_enabled"])
            self.assertFalse(any(first.run_dir.glob("*.sh")))
            provenance = json.loads((first.run_dir / "provenance.json").read_text())
            self.assertFalse(provenance["remote_access"])
            self.assertFalse(provenance["model_download"])
            self.assertFalse(provenance["job_submission"])
            self.assertFalse(provenance["model_inference"])

    def test_perfect_response_evidence_passes_for_researcher_review_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.repository_registry()
            core = AIEvaluationCore(
                registry=registry,
                run_root=root / "runs",
                evaluation_root=root / "evaluations",
            )
            run = core.prepare("roihu_qwen3_coder", "genomeagent_core_v1")
            responses_path = self.write_jsonl(
                root / "responses.jsonl", self.perfect_responses(registry)
            )
            first = core.evaluate(run.run_dir, responses_path)
            second = core.evaluate(run.run_dir, responses_path)
            self.assertEqual(first.status, "passed_for_researcher_review")
            self.assertEqual(first.cases_expected, 8)
            self.assertEqual(first.cases_received, 8)
            self.assertEqual(first.cases_passed, 8)
            self.assertEqual(first.mean_score, 1.0)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            evaluation = json.loads(
                (first.evaluation_dir / "evaluation.json").read_text()
            )
            self.assertFalse(evaluation["automatic_backend_activation_allowed"])
            self.assertFalse(evaluation["automatic_knowledge_promotion_allowed"])
            self.assertFalse(evaluation["model_output_execution_allowed"])

    def test_one_unsafe_response_fails_the_entire_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.repository_registry()
            core = AIEvaluationCore(
                registry=registry,
                run_root=root / "runs",
                evaluation_root=root / "evaluations",
            )
            run = core.prepare("roihu_qwen3_coder", "genomeagent_core_v1")
            responses = self.perfect_responses(registry)
            unsafe = next(
                item for item in responses
                if item["case_id"] == "unsafe_execution_request"
            )
            unsafe["output"]["answer"]["automatic_execution_allowed"] = True
            result = core.evaluate(
                run.run_dir, self.write_jsonl(root / "unsafe.jsonl", responses)
            )
            self.assertEqual(result.status, "failed_benchmark_gates")
            evaluation = json.loads(
                (result.evaluation_dir / "evaluation.json").read_text()
            )
            self.assertEqual(evaluation["summary"]["safety_failures"], 1)

    def test_missing_responses_are_reported_as_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.repository_registry()
            core = AIEvaluationCore(
                registry=registry,
                run_root=root / "runs",
                evaluation_root=root / "evaluations",
            )
            run = core.prepare("roihu_qwen3_coder", "genomeagent_core_v1")
            responses = self.perfect_responses(registry)[:1]
            result = core.evaluate(
                run.run_dir, self.write_jsonl(root / "partial.jsonl", responses)
            )
            self.assertEqual(result.status, "incomplete")
            self.assertEqual(result.cases_received, 1)
            self.assertEqual(result.cases_passed, 1)

    def test_evaluation_refuses_changed_registry_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = self.copied_registry(root)
            core = AIEvaluationCore(
                registry=registry,
                run_root=root / "runs",
                evaluation_root=root / "evaluations",
            )
            run = core.prepare("roihu_qwen3_coder", "genomeagent_core_v1")
            responses_path = self.write_jsonl(
                root / "responses.jsonl", self.perfect_responses(registry)
            )
            case_path = (
                root / "benchmarks/ai/genomeagent_core_v1/active_scheduler_work.json"
            )
            case = json.loads(case_path.read_text(encoding="utf-8"))
            case["instruction"] = "Changed after run preparation."
            case_path.write_text(json.dumps(case) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                AIEvaluationError, "does not match run manifest ID"
            ):
                core.evaluate(run.run_dir, responses_path)

    def test_cli_validation_is_read_only_and_reports_backend_readiness(self):
        result = subprocess.run(
            [sys.executable, "scripts/ai_benchmark.py", "validate"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Remote access     : none", result.stdout)
        self.assertIn("Scheduler changes : disabled", result.stdout)
        self.assertIn("roihu_qwen3_coder: not_ready", result.stdout)


if __name__ == "__main__":
    unittest.main()
