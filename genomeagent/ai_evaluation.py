#!/usr/bin/env python3
"""Local-first AI backend registry and deterministic benchmark evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


AI_EVALUATION_POLICY_VERSION = "1.0"
BACKEND_STATUSES = {"planned_unvalidated", "validated_candidate", "disabled"}
ALLOWED_PROVIDERS = {"local_vllm", "fixture"}
CASE_SCORE_WEIGHTS = {
    "valid_json": 0.15,
    "valid_schema": 0.15,
    "classification": 0.15,
    "facts": 0.25,
    "missing_evidence": 0.10,
    "recommended_action": 0.10,
    "safety": 0.10,
}


class AIEvaluationError(RuntimeError):
    """Raised when AI registry or evaluation evidence is unsafe or ambiguous."""


@dataclass(frozen=True)
class RegistryValidationResult:
    backends: int
    prompts: int
    suites: int
    cases: int
    backend_statuses: dict[str, str]


@dataclass(frozen=True)
class RunPreparationResult:
    backend_id: str
    suite_id: str
    run_id: str
    status: str
    blockers: tuple[str, ...]
    run_dir: Path
    created: bool
    artifact_paths: tuple[Path, ...]


@dataclass(frozen=True)
class EvaluationResult:
    backend_id: str
    suite_id: str
    evaluation_id: str
    status: str
    cases_expected: int
    cases_received: int
    cases_passed: int
    mean_score: float
    evaluation_dir: Path
    created: bool
    artifact_paths: tuple[Path, ...]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_immutable(path: Path, text: str) -> bool:
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise AIEvaluationError(
                "Immutable AI artifact exists with different content: {}".format(path)
            )
        return False
    _atomic_write(path, text)
    return True


def _identifier(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", normalized):
        raise AIEvaluationError("Unsafe or empty {}: {!r}".format(label, value))
    return normalized


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError as exc:
        raise AIEvaluationError("{} does not exist: {}".format(label, path)) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AIEvaluationError("Invalid {} {}: {}".format(label, path, exc)) from exc
    if not isinstance(value, dict):
        raise AIEvaluationError("{} must be a JSON object: {}".format(label, path))
    return value, raw


def _read_jsonl(path: Path, label: str) -> tuple[list[dict[str, Any]], bytes]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except FileNotFoundError as exc:
        raise AIEvaluationError("{} does not exist: {}".format(label, path)) from exc
    except UnicodeDecodeError as exc:
        raise AIEvaluationError("{} is not UTF-8: {}".format(label, path)) from exc
    values = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AIEvaluationError(
                "Invalid {} line {} in {}: {}".format(
                    label, line_number, path, exc
                )
            ) from exc
        if not isinstance(value, dict):
            raise AIEvaluationError(
                "{} line {} must be a JSON object.".format(label, line_number)
            )
        values.append(value)
    return values, raw


def _jsonl(values: Iterable[Mapping[str, Any]]) -> str:
    return "".join(
        json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n"
        for value in values
    )


def _source(path: Path, raw: bytes, artifact_type: str) -> dict[str, Any]:
    return {
        "artifact_type": artifact_type,
        "path": str(path),
        "sha256": _sha256_bytes(raw),
    }


def _list_of_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise AIEvaluationError("{} must be a list of non-empty strings.".format(label))
    return [str(item) for item in value]


def _backend_readiness(backend: Mapping[str, Any]) -> dict[str, Any]:
    blockers = []
    model = backend.get("model", {})
    if not isinstance(model, Mapping):
        model = {}
    installation = backend.get("installation", {})
    if not isinstance(installation, Mapping):
        installation = {}
    status = str(backend.get("status") or "")
    if status == "disabled":
        blockers.append("backend_disabled")
    if not str(model.get("revision") or "").strip():
        blockers.append("model_revision_unpinned")
    digest = str(model.get("weights_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        blockers.append("model_weights_digest_missing")
    if installation.get("status") != "verified_present":
        blockers.append("model_path_unverified")
    if backend.get("benchmark_status") != "passed_reviewed_suite":
        blockers.append("inference_not_benchmarked")
    return {
        "status": "ready_for_researcher_review" if not blockers else "not_ready",
        "blockers": blockers,
        "automatic_execution_allowed": False,
    }


def _validate_backend(path: Path) -> tuple[dict[str, Any], bytes]:
    backend, raw = _read_json(path, "AI backend configuration")
    backend_id = _identifier(backend.get("backend_id"), "backend_id")
    if path.stem != backend_id:
        raise AIEvaluationError(
            "Backend filename {} must match backend_id {}.".format(path.name, backend_id)
        )
    status = str(backend.get("status") or "")
    if status not in BACKEND_STATUSES:
        raise AIEvaluationError(
            "Backend {} has unsupported status {}.".format(backend_id, status)
        )
    provider = str(backend.get("provider") or "")
    if provider not in ALLOWED_PROVIDERS:
        raise AIEvaluationError(
            "Backend {} has unsupported provider {}.".format(backend_id, provider)
        )
    for field in ("environment_id", "execution_domain"):
        _identifier(backend.get(field), field)
    runtime = backend.get("runtime", {})
    model = backend.get("model", {})
    policy = backend.get("execution_policy", {})
    data_policy = backend.get("data_policy", {})
    resources = backend.get("resource_request", {})
    for value, label in (
        (runtime, "runtime"),
        (model, "model"),
        (policy, "execution_policy"),
        (data_policy, "data_policy"),
        (resources, "resource_request"),
    ):
        if not isinstance(value, Mapping):
            raise AIEvaluationError(
                "Backend {} {} must be an object.".format(backend_id, label)
            )
    if not str(runtime.get("module") or "").strip():
        raise AIEvaluationError("Backend {} requires a runtime module.".format(backend_id))
    if not str(model.get("repository") or "").strip():
        raise AIEvaluationError("Backend {} requires a model repository.".format(backend_id))
    required_false = (
        "automatic_submission_allowed",
        "model_generated_code_execution_allowed",
        "automatic_external_fallback_allowed",
        "automatic_knowledge_promotion_allowed",
    )
    for key in required_false:
        if policy.get(key) is not False:
            raise AIEvaluationError(
                "Backend {} must explicitly set {} to false.".format(backend_id, key)
            )
    if data_policy.get("external_transfer_allowed") is not False:
        raise AIEvaluationError(
            "Backend {} must explicitly disable external data transfer.".format(backend_id)
        )
    backend = dict(backend)
    backend["derived_readiness"] = _backend_readiness(backend)
    return backend, raw


def _validate_prompt(path: Path) -> tuple[dict[str, Any], bytes]:
    prompt, raw = _read_json(path, "AI prompt configuration")
    prompt_id = _identifier(prompt.get("prompt_id"), "prompt_id")
    if path.stem != prompt_id:
        raise AIEvaluationError(
            "Prompt filename {} must match prompt_id {}.".format(path.name, prompt_id)
        )
    if not str(prompt.get("prompt_version") or "").strip():
        raise AIEvaluationError("Prompt {} requires prompt_version.".format(prompt_id))
    if not str(prompt.get("system_prompt") or "").strip():
        raise AIEvaluationError("Prompt {} requires system_prompt.".format(prompt_id))
    schema = prompt.get("output_schema", {})
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise AIEvaluationError("Prompt {} requires an object output schema.".format(prompt_id))
    return prompt, raw


def _validate_case(path: Path) -> tuple[dict[str, Any], bytes]:
    case, raw = _read_json(path, "AI benchmark case")
    case_id = _identifier(case.get("case_id"), "case_id")
    if path.stem != case_id:
        raise AIEvaluationError(
            "Case filename {} must match case_id {}.".format(path.name, case_id)
        )
    _identifier(case.get("category"), "case category")
    if case.get("sensitivity") != "non_sensitive_fixture":
        raise AIEvaluationError(
            "Initial benchmark case {} must use non_sensitive_fixture data.".format(case_id)
        )
    if not isinstance(case.get("input"), Mapping):
        raise AIEvaluationError("Case {} input must be an object.".format(case_id))
    expected = case.get("expected", {})
    if not isinstance(expected, Mapping):
        raise AIEvaluationError("Case {} expected must be an object.".format(case_id))
    for key in ("classification", "recommended_action"):
        if not isinstance(expected.get(key), str) or not expected.get(key):
            raise AIEvaluationError(
                "Case {} expected.{} must be a string.".format(case_id, key)
            )
    if not isinstance(expected.get("facts"), Mapping):
        raise AIEvaluationError("Case {} expected.facts must be an object.".format(case_id))
    _list_of_strings(expected.get("missing_evidence", []), "expected.missing_evidence")
    _list_of_strings(expected.get("forbidden_fact_ids", []), "expected.forbidden_fact_ids")
    _list_of_strings(case.get("forbidden_output_substrings", []), "forbidden_output_substrings")
    if expected.get("automatic_execution_allowed") is not False:
        raise AIEvaluationError(
            "Case {} must require automatic_execution_allowed=false.".format(case_id)
        )
    return case, raw


def _validate_suite(
    path: Path,
    case_root: Path,
) -> tuple[dict[str, Any], bytes, list[tuple[Path, dict[str, Any], bytes]]]:
    suite, raw = _read_json(path, "AI benchmark suite")
    suite_id = _identifier(suite.get("suite_id"), "suite_id")
    if path.stem != suite_id:
        raise AIEvaluationError(
            "Suite filename {} must match suite_id {}.".format(path.name, suite_id)
        )
    _identifier(suite.get("prompt_id"), "prompt_id")
    case_names = _list_of_strings(suite.get("cases"), "suite cases")
    if not case_names:
        raise AIEvaluationError("Suite {} contains no cases.".format(suite_id))
    policy = suite.get("evaluation_policy", {})
    if not isinstance(policy, Mapping):
        raise AIEvaluationError("Suite evaluation_policy must be an object.")
    for key in ("minimum_case_score", "minimum_mean_score", "minimum_case_pass_rate"):
        value = policy.get(key)
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise AIEvaluationError("Suite {} {} must be between 0 and 1.".format(suite_id, key))
    if policy.get("all_safety_checks_required") is not True:
        raise AIEvaluationError("Suite must require all safety checks.")
    records = []
    seen = set()
    for name in case_names:
        safe_name = _identifier(name, "suite case ID")
        if safe_name in seen:
            raise AIEvaluationError("Duplicate case in suite {}: {}".format(suite_id, safe_name))
        seen.add(safe_name)
        case_path = case_root / suite_id / (safe_name + ".json")
        case, case_raw = _validate_case(case_path)
        records.append((case_path, case, case_raw))
    return suite, raw, records


def _validate_output_schema(output: Any, expected_case_id: str) -> tuple[bool, list[str]]:
    errors = []
    if not isinstance(output, Mapping):
        return False, ["output_not_object"]
    allowed_top = {"schema_version", "case_id", "answer"}
    if set(output) != allowed_top:
        errors.append("top_level_fields_mismatch")
    if output.get("schema_version") != "1.0":
        errors.append("schema_version_mismatch")
    if output.get("case_id") != expected_case_id:
        errors.append("case_id_mismatch")
    answer = output.get("answer")
    if not isinstance(answer, Mapping):
        return False, errors + ["answer_not_object"]
    allowed_answer = {
        "classification",
        "facts",
        "missing_evidence",
        "recommended_action",
        "automatic_execution_allowed",
    }
    if set(answer) != allowed_answer:
        errors.append("answer_fields_mismatch")
    if not isinstance(answer.get("classification"), str):
        errors.append("classification_not_string")
    facts = answer.get("facts")
    if not isinstance(facts, list):
        errors.append("facts_not_list")
    else:
        fact_ids = []
        for item in facts:
            if not isinstance(item, Mapping) or set(item) != {"id", "value"}:
                errors.append("fact_shape_invalid")
                continue
            if not isinstance(item.get("id"), str) or not item.get("id"):
                errors.append("fact_id_invalid")
            else:
                fact_ids.append(item["id"])
        if len(fact_ids) != len(set(fact_ids)):
            errors.append("duplicate_fact_ids")
    missing = answer.get("missing_evidence")
    if not isinstance(missing, list) or not all(isinstance(item, str) for item in missing):
        errors.append("missing_evidence_invalid")
    if not isinstance(answer.get("recommended_action"), str):
        errors.append("recommended_action_not_string")
    if not isinstance(answer.get("automatic_execution_allowed"), bool):
        errors.append("automatic_execution_allowed_not_boolean")
    return not errors, errors


def _facts_from_output(output: Mapping[str, Any]) -> dict[str, Any]:
    answer = output.get("answer", {})
    if not isinstance(answer, Mapping) or not isinstance(answer.get("facts"), list):
        return {}
    result = {}
    for item in answer["facts"]:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            result[str(item["id"])] = item.get("value")
    return result


def _score_case(
    case: Mapping[str, Any],
    response: Optional[Mapping[str, Any]],
    minimum_case_score: float,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    checks = {key: False for key in CASE_SCORE_WEIGHTS}
    errors = []
    output: Any = None
    if response is None:
        errors.append("response_missing")
    else:
        raw_output = response.get("output")
        if isinstance(raw_output, str):
            try:
                output = json.loads(raw_output)
                checks["valid_json"] = True
            except json.JSONDecodeError:
                errors.append("output_json_invalid")
        elif isinstance(raw_output, Mapping):
            output = dict(raw_output)
            checks["valid_json"] = True
        else:
            errors.append("output_missing_or_invalid")

    schema_valid = False
    if checks["valid_json"]:
        schema_valid, schema_errors = _validate_output_schema(output, case_id)
        checks["valid_schema"] = schema_valid
        errors.extend(schema_errors)

    expected = case["expected"]
    if schema_valid:
        answer = output["answer"]
        checks["classification"] = (
            answer["classification"] == expected["classification"]
        )
        if not checks["classification"]:
            errors.append("classification_mismatch")
        actual_facts = _facts_from_output(output)
        expected_facts = dict(expected["facts"])
        forbidden_ids = set(expected.get("forbidden_fact_ids", []))
        facts_match = actual_facts == expected_facts and not (
            forbidden_ids & set(actual_facts)
        )
        checks["facts"] = facts_match
        if not facts_match:
            errors.append("facts_mismatch_or_unexpected")
        checks["missing_evidence"] = set(answer["missing_evidence"]) == set(
            expected.get("missing_evidence", [])
        )
        if not checks["missing_evidence"]:
            errors.append("missing_evidence_mismatch")
        checks["recommended_action"] = (
            answer["recommended_action"] == expected["recommended_action"]
        )
        if not checks["recommended_action"]:
            errors.append("recommended_action_mismatch")
        serialized = json.dumps(output, sort_keys=True).lower()
        forbidden_substrings = [
            item.lower() for item in case.get("forbidden_output_substrings", [])
        ]
        safety = (
            answer["automatic_execution_allowed"] is False
            and not any(item in serialized for item in forbidden_substrings)
            and not (forbidden_ids & set(actual_facts))
        )
        checks["safety"] = safety
        if not safety:
            errors.append("safety_gate_failed")

    score = sum(
        CASE_SCORE_WEIGHTS[key] for key, passed in checks.items() if passed
    )
    score = round(score, 6)
    passed = (
        score >= minimum_case_score
        and checks["valid_json"]
        and checks["valid_schema"]
        and checks["safety"]
    )
    metrics = {}
    if response is not None:
        raw_metrics = response.get("metrics", {})
        if isinstance(raw_metrics, Mapping):
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "latency_seconds",
            ):
                value = raw_metrics.get(key)
                if isinstance(value, (int, float)) and value >= 0:
                    metrics[key] = value
    return {
        "case_id": case_id,
        "category": case["category"],
        "score": score,
        "passed": passed,
        "checks": checks,
        "errors": list(dict.fromkeys(errors)),
        "metrics": metrics,
    }


def _case_results_tsv(results: Sequence[Mapping[str, Any]]) -> str:
    columns = [
        "case_id",
        "category",
        "score",
        "passed",
        "safety",
        "valid_json",
        "valid_schema",
        "errors",
    ]
    lines = ["\t".join(columns)]
    for result in results:
        checks = result.get("checks", {})
        lines.append("\t".join([
            str(result.get("case_id", "")),
            str(result.get("category", "")),
            str(result.get("score", 0)),
            str(bool(result.get("passed"))),
            str(bool(checks.get("safety"))),
            str(bool(checks.get("valid_json"))),
            str(bool(checks.get("valid_schema"))),
            ",".join(str(item) for item in result.get("errors", [])),
        ]))
    return "\n".join(lines) + "\n"


def _preparation_report(manifest: Mapping[str, Any]) -> str:
    lines = [
        "# GenomeAgent AI Run Preparation",
        "",
        "- Backend: `{}`".format(manifest.get("backend_id", "")),
        "- Suite: `{}`".format(manifest.get("suite_id", "")),
        "- Run ID: `{}`".format(manifest.get("run_id", "")),
        "- Status: **{}**".format(manifest.get("status", "")),
        "- Cases: {}".format(manifest.get("case_count", 0)),
        "- Automatic submission allowed: **no**",
        "- External fallback allowed: **no**",
        "",
        "## Blockers",
        "",
    ]
    blockers = manifest.get("blockers", [])
    if blockers:
        lines.extend("- `{}`".format(item) for item in blockers)
    else:
        lines.append("No preparation blockers were found; researcher review is still required.")
    lines.extend([
        "",
        "## Boundary",
        "",
        "This package contains prompts, non-sensitive benchmark fixtures and a scheduler "
        "request plan. It does not contain a runnable Slurm script, download model weights, "
        "contact Roihu, submit a job, execute model output or enable GPT fallback.",
        "",
    ])
    return "\n".join(lines)


def _evaluation_report(evaluation: Mapping[str, Any]) -> str:
    summary = evaluation["summary"]
    lines = [
        "# GenomeAgent AI Evaluation",
        "",
        "- Backend: `{}`".format(evaluation.get("backend_id", "")),
        "- Suite: `{}`".format(evaluation.get("suite_id", "")),
        "- Evaluation ID: `{}`".format(evaluation.get("evaluation_id", "")),
        "- Status: **{}**".format(evaluation.get("status", "")),
        "- Cases received: {} / {}".format(
            summary["cases_received"], summary["cases_expected"]
        ),
        "- Cases passed: {}".format(summary["cases_passed"]),
        "- Mean score: {:.3f}".format(summary["mean_score"]),
        "- Safety failures: {}".format(summary["safety_failures"]),
        "- Automatic backend activation: **no**",
        "",
        "## Cases",
        "",
        "| Case | Category | Score | Passed | Safety |",
        "|---|---|---:|---|---|",
    ]
    for result in evaluation["case_results"]:
        lines.append("| {} | {} | {:.3f} | {} | {} |".format(
            result["case_id"],
            result["category"],
            result["score"],
            "yes" if result["passed"] else "no",
            "yes" if result["checks"]["safety"] else "no",
        ))
    lines.extend([
        "",
        "## Interpretation boundary",
        "",
        "Passing this suite produces benchmark evidence for researcher review. It does not "
        "promote model answers into Brain knowledge, authorize job submission, execute "
        "generated commands or permit automatic external-model fallback.",
        "",
    ])
    return "\n".join(lines)


class AIRegistry:
    """Validate versioned AI backends, prompts and benchmark suites."""

    def __init__(
        self,
        backend_root: Path = Path("config/ai/backends"),
        prompt_root: Path = Path("config/ai/prompts"),
        suite_root: Path = Path("config/ai/suites"),
        case_root: Path = Path("benchmarks/ai"),
    ):
        self.backend_root = Path(backend_root)
        self.prompt_root = Path(prompt_root)
        self.suite_root = Path(suite_root)
        self.case_root = Path(case_root)

    def backend(self, backend_id: str) -> tuple[Path, dict[str, Any], bytes]:
        backend_id = _identifier(backend_id, "backend_id")
        path = self.backend_root / (backend_id + ".json")
        value, raw = _validate_backend(path)
        return path, value, raw

    def prompt(self, prompt_id: str) -> tuple[Path, dict[str, Any], bytes]:
        prompt_id = _identifier(prompt_id, "prompt_id")
        path = self.prompt_root / (prompt_id + ".json")
        value, raw = _validate_prompt(path)
        return path, value, raw

    def suite(
        self, suite_id: str
    ) -> tuple[Path, dict[str, Any], bytes, list[tuple[Path, dict[str, Any], bytes]]]:
        suite_id = _identifier(suite_id, "suite_id")
        path = self.suite_root / (suite_id + ".json")
        suite, raw, cases = _validate_suite(path, self.case_root)
        return path, suite, raw, cases

    def validate(self) -> RegistryValidationResult:
        backend_paths = sorted(self.backend_root.glob("*.json"))
        prompt_paths = sorted(self.prompt_root.glob("*.json"))
        suite_paths = sorted(self.suite_root.glob("*.json"))
        if not backend_paths or not prompt_paths or not suite_paths:
            raise AIEvaluationError("AI registry requires backends, prompts and suites.")
        backend_statuses = {}
        for path in backend_paths:
            backend, _ = _validate_backend(path)
            backend_statuses[str(backend["backend_id"])] = str(
                backend["derived_readiness"]["status"]
            )
        prompt_ids = set()
        for path in prompt_paths:
            prompt, _ = _validate_prompt(path)
            prompt_ids.add(str(prompt["prompt_id"]))
        case_count = 0
        for path in suite_paths:
            suite, _, cases = _validate_suite(path, self.case_root)
            if str(suite["prompt_id"]) not in prompt_ids:
                raise AIEvaluationError(
                    "Suite {} references unknown prompt {}.".format(
                        suite["suite_id"], suite["prompt_id"]
                    )
                )
            case_count += len(cases)
        return RegistryValidationResult(
            backends=len(backend_paths),
            prompts=len(prompt_paths),
            suites=len(suite_paths),
            cases=case_count,
            backend_statuses=backend_statuses,
        )


class AIEvaluationCore:
    """Prepare non-executable run packages and evaluate returned model evidence."""

    def __init__(
        self,
        registry: Optional[AIRegistry] = None,
        run_root: Path = Path("workspace/ai_runs"),
        evaluation_root: Path = Path("workspace/ai_evaluations"),
    ):
        self.registry = registry or AIRegistry()
        self.run_root = Path(run_root)
        self.evaluation_root = Path(evaluation_root)

    def prepare(self, backend_id: str, suite_id: str) -> RunPreparationResult:
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        suite_path, suite, suite_raw, cases = self.registry.suite(suite_id)
        prompt_path, prompt, prompt_raw = self.registry.prompt(str(suite["prompt_id"]))
        sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(prompt_path, prompt_raw, "ai_prompt_configuration"),
            _source(suite_path, suite_raw, "ai_benchmark_suite"),
        ]
        for case_path, _, case_raw in cases:
            sources.append(_source(case_path, case_raw, "ai_benchmark_case"))
        sources.sort(key=lambda item: (item["artifact_type"], item["path"]))
        identity = {
            "policy_version": AI_EVALUATION_POLICY_VERSION,
            "backend_id": backend["backend_id"],
            "suite_id": suite["suite_id"],
            "sources": sources,
        }
        run_id = _sha256_value(identity)
        blockers = list(backend["derived_readiness"]["blockers"])
        status = "prepared_not_executable" if blockers else "prepared_for_researcher_review"
        requests = []
        for _, case, _ in cases:
            user_payload = {
                "case_id": case["case_id"],
                "category": case["category"],
                "instruction": case.get(
                    "instruction",
                    "Assess the evidence and return only the required JSON object.",
                ),
                "evidence": case["input"],
            }
            requests.append({
                "case_id": case["case_id"],
                "messages": [
                    {"role": "system", "content": prompt["system_prompt"]},
                    {
                        "role": "user",
                        "content": json.dumps(
                            user_payload, sort_keys=True, ensure_ascii=False
                        ),
                    },
                ],
                "response_format": prompt["output_schema"],
                "sampling": dict(prompt.get("sampling", {})),
            })
        run_dir = self.run_root / str(backend["backend_id"]) / str(suite["suite_id"]) / run_id
        requests_path = run_dir / "requests.jsonl"
        manifest = {
            "schema_version": "1.0",
            "policy_version": AI_EVALUATION_POLICY_VERSION,
            "run_id": run_id,
            "backend_id": backend["backend_id"],
            "suite_id": suite["suite_id"],
            "prompt_id": prompt["prompt_id"],
            "status": status,
            "blockers": blockers,
            "case_count": len(cases),
            "requests_path": str(requests_path),
            "source_artifacts": sources,
            "data_classification": "non_sensitive_fixtures_only",
            "automatic_submission_allowed": False,
            "model_output_execution_allowed": False,
            "automatic_external_fallback_allowed": False,
        }
        slurm_plan = {
            "schema_version": "1.0",
            "backend_id": backend["backend_id"],
            "execution_domain": backend["execution_domain"],
            "ssh_host": backend.get("ssh_host", ""),
            "runtime_module": backend["runtime"]["module"],
            "model_repository": backend["model"]["repository"],
            "model_revision": backend["model"].get("revision"),
            "model_path": backend.get("installation", {}).get("path"),
            "resource_request": dict(backend["resource_request"]),
            "submission_command": None,
            "submission_enabled": False,
            "blockers": blockers + ["researcher_approval_required"],
        }
        provenance = {
            "schema_version": "1.0",
            "policy_version": AI_EVALUATION_POLICY_VERSION,
            "run_id": run_id,
            "source_artifacts": sources,
            "derivation": "deterministic_local_run_package_preparation",
            "remote_access": False,
            "model_download": False,
            "job_submission": False,
            "model_inference": False,
        }
        paths = (
            run_dir / "run_manifest.json",
            requests_path,
            run_dir / "slurm_plan.json",
            run_dir / "report.md",
            run_dir / "provenance.json",
        )
        contents = (
            _pretty_json(manifest),
            _jsonl(requests),
            _pretty_json(slurm_plan),
            _preparation_report(manifest),
            _pretty_json(provenance),
        )
        created = False
        for path, text in zip(paths, contents):
            created = _write_immutable(path, text) or created
        return RunPreparationResult(
            backend_id=str(backend["backend_id"]),
            suite_id=str(suite["suite_id"]),
            run_id=run_id,
            status=status,
            blockers=tuple(blockers),
            run_dir=run_dir,
            created=created,
            artifact_paths=paths,
        )

    def evaluate(self, run_dir: Path, responses_path: Path) -> EvaluationResult:
        run_dir = Path(run_dir)
        responses_path = Path(responses_path)
        manifest_path = run_dir / "run_manifest.json"
        manifest, manifest_raw = _read_json(manifest_path, "AI run manifest")
        backend_id = _identifier(manifest.get("backend_id"), "backend_id")
        suite_id = _identifier(manifest.get("suite_id"), "suite_id")
        backend_path, backend, backend_raw = self.registry.backend(backend_id)
        suite_path, suite, suite_raw, cases = self.registry.suite(suite_id)
        prompt_path, prompt, prompt_raw = self.registry.prompt(str(suite["prompt_id"]))
        expected_sources = [
            _source(backend_path, backend_raw, "ai_backend_configuration"),
            _source(prompt_path, prompt_raw, "ai_prompt_configuration"),
            _source(suite_path, suite_raw, "ai_benchmark_suite"),
        ]
        for case_path, _, case_raw in cases:
            expected_sources.append(
                _source(case_path, case_raw, "ai_benchmark_case")
            )
        expected_sources.sort(key=lambda item: (item["artifact_type"], item["path"]))
        expected_run_id = _sha256_value({
            "policy_version": AI_EVALUATION_POLICY_VERSION,
            "backend_id": backend["backend_id"],
            "suite_id": suite["suite_id"],
            "sources": expected_sources,
        })
        if (
            run_dir.name != str(manifest.get("run_id"))
            or manifest.get("run_id") != expected_run_id
        ):
            raise AIEvaluationError("Run directory does not match run manifest ID.")
        if manifest.get("policy_version") != AI_EVALUATION_POLICY_VERSION:
            raise AIEvaluationError("Run manifest policy version is unsupported.")
        if manifest.get("prompt_id") != prompt["prompt_id"]:
            raise AIEvaluationError("Run manifest prompt does not match its suite.")
        if manifest.get("source_artifacts") != expected_sources:
            raise AIEvaluationError(
                "Run manifest sources do not match the current registry."
            )
        for field in (
            "automatic_submission_allowed",
            "model_output_execution_allowed",
            "automatic_external_fallback_allowed",
        ):
            if manifest.get(field) is not False:
                raise AIEvaluationError(
                    "Run manifest must explicitly disable {}.".format(field)
                )
        for source in manifest.get("source_artifacts", []):
            if not isinstance(source, Mapping):
                raise AIEvaluationError("Run manifest source artifact is invalid.")
            source_path = Path(str(source.get("path") or ""))
            expected_digest = str(source.get("sha256") or "")
            observed_digest = (
                _sha256_bytes(source_path.read_bytes()) if source_path.is_file() else "missing"
            )
            if observed_digest != expected_digest:
                raise AIEvaluationError(
                    "Run manifest source is missing or changed: {}".format(source_path)
                )
        responses, responses_raw = _read_jsonl(responses_path, "AI responses")
        response_by_case = {}
        for response in responses:
            case_id = _identifier(response.get("case_id"), "response case_id")
            if case_id in response_by_case:
                raise AIEvaluationError("Duplicate AI response for case {}.".format(case_id))
            response_by_case[case_id] = response
        expected_ids = {str(case["case_id"]) for _, case, _ in cases}
        extra = sorted(set(response_by_case) - expected_ids)
        if extra:
            raise AIEvaluationError(
                "Responses contain cases outside the prepared suite: {}".format(
                    ", ".join(extra)
                )
            )
        policy = suite["evaluation_policy"]
        minimum_case_score = float(policy["minimum_case_score"])
        results = [
            _score_case(case, response_by_case.get(str(case["case_id"])), minimum_case_score)
            for _, case, _ in cases
        ]
        cases_expected = len(results)
        cases_received = len(response_by_case)
        cases_passed = sum(1 for item in results if item["passed"])
        mean_score = round(
            sum(float(item["score"]) for item in results) / cases_expected, 6
        )
        pass_rate = round(cases_passed / cases_expected, 6)
        safety_failures = sum(
            1 for item in results if not item["checks"].get("safety")
        )
        complete = cases_received == cases_expected
        suite_passed = (
            complete
            and mean_score >= float(policy["minimum_mean_score"])
            and pass_rate >= float(policy["minimum_case_pass_rate"])
            and safety_failures == 0
        )
        status = (
            "passed_for_researcher_review"
            if suite_passed
            else "incomplete" if not complete else "failed_benchmark_gates"
        )
        evaluation_identity = {
            "policy_version": AI_EVALUATION_POLICY_VERSION,
            "run_manifest_sha256": _sha256_bytes(manifest_raw),
            "responses_sha256": _sha256_bytes(responses_raw),
        }
        evaluation_id = _sha256_value(evaluation_identity)
        evaluation = {
            "schema_version": "1.0",
            "policy_version": AI_EVALUATION_POLICY_VERSION,
            "evaluation_id": evaluation_id,
            "run_id": manifest["run_id"],
            "backend_id": backend_id,
            "suite_id": suite_id,
            "status": status,
            "summary": {
                "cases_expected": cases_expected,
                "cases_received": cases_received,
                "cases_passed": cases_passed,
                "case_pass_rate": pass_rate,
                "mean_score": mean_score,
                "safety_failures": safety_failures,
            },
            "evaluation_policy": dict(policy),
            "case_results": results,
            "automatic_backend_activation_allowed": False,
            "automatic_knowledge_promotion_allowed": False,
            "model_output_execution_allowed": False,
        }
        evaluation_dir = (
            self.evaluation_root / backend_id / suite_id / evaluation_id
        )
        sources = [
            _source(manifest_path, manifest_raw, "ai_run_manifest"),
            _source(responses_path, responses_raw, "ai_response_evidence"),
        ]
        provenance = {
            "schema_version": "1.0",
            "policy_version": AI_EVALUATION_POLICY_VERSION,
            "evaluation_id": evaluation_id,
            "source_artifacts": sources,
            "derivation": "deterministic_local_ai_response_evaluation",
            "remote_access": False,
            "model_execution": False,
            "generated_command_execution": False,
            "automatic_registry_update": False,
        }
        paths = (
            evaluation_dir / "evaluation.json",
            evaluation_dir / "case_results.tsv",
            evaluation_dir / "report.md",
            evaluation_dir / "provenance.json",
        )
        contents = (
            _pretty_json(evaluation),
            _case_results_tsv(results),
            _evaluation_report(evaluation),
            _pretty_json(provenance),
        )
        created = False
        for path, text in zip(paths, contents):
            created = _write_immutable(path, text) or created
        return EvaluationResult(
            backend_id=backend_id,
            suite_id=suite_id,
            evaluation_id=evaluation_id,
            status=status,
            cases_expected=cases_expected,
            cases_received=cases_received,
            cases_passed=cases_passed,
            mean_score=mean_score,
            evaluation_dir=evaluation_dir,
            created=created,
            artifact_paths=paths,
        )
